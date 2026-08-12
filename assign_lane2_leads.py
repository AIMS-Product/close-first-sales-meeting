#!/usr/bin/env python3
"""
Lane 2 Scraper lead assignment — top-up round robin.

Decides WHO owns a lead. It does not touch Recapture State — that's
`lane2_state.py`'s job. The two are deliberately independent:

    assigner    -> sets Lead Owner (sticky; survives state changes)
    reconciler  -> sets Recapture State (fluid; moves under the owner)
    smart views -> the intersection, filtered to CURRENT_USER

So a lead assigned to Vince while it's in Blitz stays Vince's as it ages into
Active-Nurture. It just moves between *his* views. Nothing is ever reassigned
on a state change.

TOP-UP, not straight round robin. Each rep holds a working queue of MAX_QUEUE
leads; the job tops up whoever is furthest below it. That self-balances to
actual work rate, keeps unworked leads in the pool instead of stranded under a
departed rep, and hands out the hottest buckets first.

    python3 assign_lane2_leads.py            # dry run
    python3 assign_lane2_leads.py --apply
"""

import argparse
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

# Reuse the query builders and the month-partitioned search that works around
# Close's 10,000-row pagination cap.
from lane2_state import (
    BASE, F_OWNER, F_STATE, F_OVERRIDE, SUPPRESS_STATUSES,
    CloseError, _req, _wrap, cf, search, status_in, WRITE_WORKERS,
)

# ============================================================================
# CONFIG
# ============================================================================

# The dialing rotation. Setters are deliberately excluded — William Nowak and
# Spencer Reynolds work inbound, not the dormant book.
SCRAPERS = {
    "user_dQi0iL0igjCKtEXPSsv8ALDZMAz9orJxL60O7Q921jy": "Vince Bartolini",
    "user_IeWR2TlhpjqoXy3K6jX7u9C8c83iBnHXSIvFZpotF3z": "Jacob Hepner",
    "user_p2y1gLbIgUb9xognGTvuXoRpzp4Ro8QkO20ltgF1CvJ": "Jacob Herbig",
    "user_yZWJTiMjUBfJt8pUPQG6hS7QfKUxwt322aYEABSUrQb": "Charlie Ingram",
    "user_0SuNg0OWd2reYMeyuDVqiVvjiGcRiFheKKOXXZpyaPZ": "Pearl Sathekge",
    "user_WquWudQN7dghZsAPiNY80eJUmg1EadQg2UCQdvgbif7": "Kelly Schrader",  # moved to Lane 2 2026-08-05
    "user_wH5PGq1Wm84UW6KrKCt6YCioWocmlffYkbadH6rN43H": "August Young",    # added 2026-08-06
    "user_rGhjlxgkAA0mXchgw6zggbWXqHSYpfpzCvO6UgkqVnm": "Amy Mulch",      # added 2026-08-06
    "user_Hoijs8g8hxab7NN7tMVvC4dpzwHcxSgkIuHeBRphyUL": "Cassie Caraballo",  # added 2026-08-07
    "user_WmBJj4uIsE9WRLKMn5Y1i8MinIDJG5GjOHPeX2sUJCp": "Jessica Zatkin",    # added 2026-08-07
    "user_O9qFgDidrldSA1zU3pKPpz5zUbCcNpoEBTCrtAolDUi": "Abigail Garza",     # added 2026-08-12

    # Ariella Irvine — Hybrid Setter, added to the dialing rotation 2026-08-12.
    # She is in BOTH queues on purpose: assign_setter_leads.py deals her fresh
    # hot inbound (she books and runs her own calls), and this script gives her
    # a dormant book to work between them.
    #
    # She stays in HYBRID_SETTERS in lane2_state.py — do NOT copy her into
    # SCRAPERS there. That dict drives Owner Team, and lane2_state's roster
    # guard fails hard on anyone listed twice. Her Owner Team stays "Setter".
    "user_BaN2TstWtyF34eaQSSLG11j6DhKKm67Y6JltbIYCafO": "Ariella Irvine",

    # Setters (NOT in this rotation): William Nowak, Spencer Reynolds.
    # Sydney Boyd is not joining — removed 2026-08-06.
    #
    # This list is NO LONGER a mirror of SCRAPERS in lane2_state.py — Ariella is
    # here and not there, deliberately (see above). That one drives Owner Team,
    # this one drives who gets dealt leads. Everyone else appears in both.

    # Not started yet — add when their Close users exist:
    # "user_...": "Connor George",

    # Jason Aaron manages Lane 2 and holds lost deals — not a dialing seat.
    # Uncomment only if he should receive round-robin volume.
    # "user_MrBLkl5wCqTm7QxHxPo2ydNV5KxMllg6YZDVc12Aqzj": "Jason Aaron",
}

# Target queue size per rep. None = uncapped (deal the whole pool).
#
# 1,000 ≈ a few weeks of runway at ~100 calls/day. The assigner is a TOP-UP that
# runs every weekday morning, so a capped queue refills as reps work it down —
# nobody runs dry, and the pool stays intact for new hires (Sydney, Connor) and
# for reclaim headroom. Uncapped dealt 28,392 in one go and emptied the pool.
#
# This is a DEFAULT, not a ceiling on what's already held: the cap stops giving,
# it never claws back. A rep over target simply receives nothing.
#
# Deliberately not None: a bodyless cron dispatch sends no max_queue input, and
# the safe behaviour has to be what happens when nobody passes an argument.
# Same reasoning as the dry_run inversion on the reconciler workflow.
MAX_QUEUE = 1000

# Handed out in this order — hottest first. A rep's queue fills with Blitz before
# it ever reaches Active-Nurture.
PRIORITY_STATES = ["Blitz", "Active-Nurture", "Deep-Nurture"]

# Hot-Inbound is deliberately absent: that's the Setter lane.

# --- reclaim ----------------------------------------------------------------
# Leads owned by someone who has left are invisible: they're not in the unclaimed
# pool (they HAVE an owner) so they're never dealt out, and nobody works them.
# ~8k leads were sitting like this as of 2026-08-04.
#
# Active Close users are fetched live at runtime, so a new hire is never mistaken
# for a leaver. RECLAIM_EXTRA covers the other case — someone who has left but
# whose Close seat hasn't been revoked yet.
#
# Kelly Schrader was listed here as a leaver. Removed 2026-08-05 — she moved to
# Lane 2 as a Scraper and now holds Juan's old book. Leaving her here would have
# made --reclaim strip those leads straight back into the unclaimed pool on the
# next run, undoing the reassignment.
#
# A name must never be in both SCRAPERS and RECLAIM_EXTRA — see the guard below.
RECLAIM_EXTRA = {
    # Left the company. Listed here rather than waiting on the Close seat being
    # revoked — this way the reclaim frees her book on the next run regardless.
    "user_QgFeDsKkV4fsOtkTYeOJMURXPqqhZA8d4kHbE8rzat7": "Jennifer Padilla",  # 2026-08-07
}

# Ryan Jones (user_3nrtuEmgPYd5VA15NvrxgQxDVNWbhrNSzitEKGwi8s6) is deliberately
# in NO Lane 2 roster — he works upsells only. He is active in Close, so reclaim
# already skips him; his ~293 leads stay with him and show Owner Team = None.

# Never reclaim from these even if they look inactive (service accounts, admins).
RECLAIM_NEVER = {
    "user_5cZRqXu8kb4O1IeBVA98UMcMEhYZUhx1fnCHfSL0YMV",   # Stephen Olivas
}

# A rep in both lists would be dealt leads and then have them stripped away again
# on the next --reclaim, silently churning their queue. Fail loudly instead.
_conflict = set(SCRAPERS) & set(RECLAIM_EXTRA)
if _conflict:
    sys.exit("CONFIG ERROR — in SCRAPERS and RECLAIM_EXTRA at once: "
             + ", ".join(SCRAPERS[u] for u in _conflict))

# ============================================================================

def owner_is(user_ids, negate=False):
    return {"type": "field_condition", "negate": negate,
            "field": {"type": "custom_field", "custom_field_id": F_OWNER},
            "condition": {"type": "reference", "reference_type": "user_or_group",
                          "object_ids": list(user_ids)}}

def owner_empty():
    return {"type": "field_condition", "negate": True,
            "field": {"type": "custom_field", "custom_field_id": F_OWNER},
            "condition": {"type": "exists"}}

def state_is(values, negate=False):
    return {"type": "field_condition", "negate": negate,
            "field": {"type": "custom_field", "custom_field_id": F_STATE},
            "condition": {"type": "term", "values": values}}

def override_set():
    return {"type": "field_condition", "negate": False,
            "field": {"type": "custom_field", "custom_field_id": F_OVERRIDE},
            "condition": {"type": "term", "values": ["Yes"]}}


def active_user_ids():
    """
    Users with a CURRENT membership of this organisation.

    NOT `GET /user/`. That endpoint returns every user record the API key can
    see, including people whose org membership has been revoked — so it reports
    departed reps as active. The 2026-08-07 reclaim found only the one person
    hard-coded into RECLAIM_EXTRA and skipped ~5,300 leads held by leavers,
    because every one of those owners looked "active".

    Organisation memberships are the real source of truth: revoking someone's
    access removes them here, which is exactly the signal reclaim needs.
    """
    me = _req("GET", f"{BASE}/me/").json()
    orgs = me.get("organizations") or []
    if not orgs:
        raise CloseError("no organisation returned by /me/ — cannot determine "
                         "active users, refusing to guess")

    out = set()
    for org in orgs:
        oid = org.get("id") if isinstance(org, dict) else org
        j = _req("GET", f"{BASE}/organization/{oid}/").json()
        for m in j.get("memberships", []):
            uid = m.get("user_id") or (m.get("user") or {}).get("id")
            if uid:
                out.add(uid)

    if not out:
        raise CloseError("organisation returned zero memberships — refusing to "
                         "treat everyone as departed")
    return out


def user_names(user_ids):
    """
    Resolve display names for reclaim reporting.

    Uses GET /user/{id}/ — the same endpoint that must NOT be used to decide who
    is active, but which is exactly right here: it still returns records for
    departed people, which is the whole point when you're naming them.
    """
    names = {}
    for uid in user_ids:
        try:
            u = _req("GET", f"{BASE}/user/{uid}/").json()
            full = f"{u.get('first_name','')} {u.get('last_name','')}".strip()
            names[uid] = full or u.get("email") or uid
        except Exception:
            names[uid] = uid
    return names


def find_stranded(active):
    """
    Leads in a workable state whose owner has left.

    Owner is set (so they're not in the pool) but that person is gone (so nobody
    works them). Clearing Lead Owner returns them to the pool for redistribution.
    """
    rows = search(
        _wrap(status_in(SUPPRESS_STATUSES, negate=True),
              state_is(PRIORITY_STATES)),
        fields=["id", "display_name", f"custom.{F_OWNER}", f"custom.{F_OVERRIDE}"])
    out = []
    for r in rows:
        owner = cf(r, F_OWNER)
        if not owner or owner in RECLAIM_NEVER:
            continue
        if cf(r, F_OVERRIDE) == "Yes":       # pinned — leave it alone
            continue
        if owner not in active or owner in RECLAIM_EXTRA:
            out.append((r, owner))
    return out


def build_deficits(counts, pool_size, target):
    """
    How many leads each rep should receive.

    Fixed target  -> top up to that number.
    No target     -> level everyone toward the same holding once the pool is
                     distributed. A rep already sitting on 5k doesn't get the
                     same share as someone starting from zero.
    """
    if target:
        return {u: max(0, target - counts.get(u, 0)) for u in SCRAPERS}, target

    level = (sum(counts.get(u, 0) for u in SCRAPERS) + pool_size) / len(SCRAPERS)
    deficits = {u: max(0, int(level) - counts.get(u, 0)) for u in SCRAPERS}
    # Rounding can leave a few unallocated — hand them to whoever is furthest behind.
    short = pool_size - sum(deficits.values())
    if short > 0:
        for u in sorted(SCRAPERS, key=lambda x: -deficits[x])[:short]:
            deficits[u] += 1
    return deficits, int(level)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write Lead Owner (default: dry run)")
    ap.add_argument("--max-queue", type=int, default=MAX_QUEUE,
                    help=f"target queue size per rep (default {MAX_QUEUE}; 0 = unlimited)")
    ap.add_argument("--no-census", action="store_true",
                    help="stop reading each bucket once deficits are filled. Faster, "
                         "but the summary can't report how much backlog is left")
    ap.add_argument("--reclaim", action="store_true",
                    help="first clear Lead Owner on leads held by departed users, "
                         "returning them to the pool")
    ap.add_argument("--max-reclaim", type=int, default=10000,
                    help="refuse to release more than this many leads in one run "
                         "(default 10000) — a backstop against a bad active-user "
                         "lookup treating everyone as departed")
    args = ap.parse_args()
    target = None if args.max_queue == 0 else args.max_queue

    if not SCRAPERS:
        sys.exit("No scrapers configured.")

    # ---- 0. reclaim from leavers ------------------------------------------
    if args.reclaim:
        print("Checking for leads stranded under departed users...", file=sys.stderr)
        active = active_user_ids()
        # Printed because it's the number that silently broke this before. If it
        # looks wrong — far bigger than the headcount, or implausibly small —
        # stop, because everything reclaim does hangs off it.
        print(f"  {len(active)} users hold a current org membership.", file=sys.stderr)
        stranded = find_stranded(active)
        if not stranded:
            print("No stranded leads found.\n")
        else:
            by_owner = Counter(o for _, o in stranded)
            names = user_names(by_owner.keys())
            print(f"\n{len(stranded):,} leads held by {len(by_owner)} departed user(s):")
            for owner, n in by_owner.most_common():
                label = RECLAIM_EXTRA.get(owner) or names.get(owner, owner)
                print(f"  {label:<32} {n:>6,}")

            if len(stranded) > args.max_reclaim:
                sys.exit(f"\nRefusing to release {len(stranded):,} leads — above the "
                         f"--max-reclaim ceiling of {args.max_reclaim:,}. If this is "
                         f"genuinely right, re-run with a higher ceiling.")

            if args.apply:
                print(f"\nReleasing {len(stranded):,} leads back to the pool...")
                ok = err = 0
                def _clear(item):
                    lid = item[0]["id"]
                    try:
                        _req("PUT", f"{BASE}/lead/{lid}/", json={f"custom.{F_OWNER}": None})
                        return True
                    except Exception:
                        return False
                with ThreadPoolExecutor(max_workers=WRITE_WORKERS) as ex:
                    for good in ex.map(_clear, stranded):
                        ok, err = (ok + 1, err) if good else (ok, err + 1)
                print(f"  {ok:,} released, {err:,} failed.\n")
            else:
                print("  (dry run — not released)\n")

    # ---- 1. what does each rep already hold? -------------------------------
    print("Counting current queues...", file=sys.stderr)
    held = search(
        _wrap(status_in(SUPPRESS_STATUSES, negate=True),
              owner_is(SCRAPERS.keys()),
              state_is(PRIORITY_STATES)),
        fields=["id", f"custom.{F_OWNER}"], limit=9000)

    counts = Counter(cf(l, F_OWNER) for l in held)

    # ---- 2. pull from the unclaimed pool, hottest first --------------------
    # Capped mode only needs as many as the deficits require; uncapped takes all.
    cap_need = sum(max(0, target - counts.get(u, 0)) for u in SCRAPERS) if target else None
    if target and cap_need == 0:
        print("\nEveryone is at target. Nothing to assign.")
        return

    # Fetch the WHOLE unowned pool per state, not just enough to fill deficits.
    #
    # Capped mode used to stop early, which was cheaper but reported a pool size
    # that was really "how many we happened to fetch" — the summary read
    # "Active-Nurture 6" when thousands remained. Reading the full pool costs a
    # slower run (this is a once-a-weekday job) and buys an honest census of what
    # is left to work. --no-census restores the early-break behaviour.
    print("Reading the unclaimed pool...", file=sys.stderr)
    pool, available, taken_from = [], Counter(), Counter()
    for state in PRIORITY_STATES:
        early_stop = args.no_census and target and len(pool) >= cap_need
        if early_stop:
            available[state] = None       # genuinely unknown — never print a guess
            continue
        remaining = (cap_need - len(pool)) if (args.no_census and target) else None
        rows = search(
            _wrap(status_in(SUPPRESS_STATUSES, negate=True),
                  owner_empty(), state_is([state])),
            fields=["id", "display_name", f"custom.{F_OVERRIDE}"],
            limit=remaining)
        # never take a lead someone has pinned
        rows = [r for r in rows if cf(r, F_OVERRIDE) != "Yes"]
        available[state] = len(rows)
        for r in rows:
            r["_state"] = state       # so we can report what came from where
        pool.extend(rows)
        print(f"  {state:<16} {len(rows):>7,} unowned", file=sys.stderr)

    if not pool:
        print("\nPool is empty — nothing unowned in a workable state.")
        return

    # ---- 3. deal them out, furthest-behind first --------------------------
    deficit, level = build_deficits(counts, len(pool), target)

    print()
    mode = f"target {level:,}/rep" if target else f"levelling to ~{level:,}/rep (uncapped)"
    print(f"{'Rep':<20} {'holds':>8} {'gets':>8} {'after':>8}   [{mode}]")
    print("-" * 52)

    plan = defaultdict(list)
    order = sorted(SCRAPERS, key=lambda u: -deficit[u])
    i = 0
    for lead in pool:
        for _ in range(len(order)):
            uid = order[i % len(order)]
            i += 1
            if deficit[uid] > 0:
                plan[uid].append(lead)
                taken_from[lead["_state"]] += 1
                deficit[uid] -= 1
                break
        else:
            break   # everyone full

    for uid in sorted(SCRAPERS, key=lambda u: -(counts.get(u, 0) + len(plan[u]))):
        have, gets = counts.get(uid, 0), len(plan[uid])
        print(f"{SCRAPERS[uid]:<20} {have:>8,} {gets:>+8,} {have+gets:>8,}")
    print("-" * 52)
    total = sum(len(v) for v in plan.values())
    print(f"{'TOTAL':<20} {sum(counts.values()):>8,} {total:>+8,} "
          f"{sum(counts.values())+total:>8,}")
    # ---- bucket census -----------------------------------------------------
    # "unowned" is the whole unclaimed pool in that bucket BEFORE this run.
    # "left" is what nobody owns once this run's assignments land — i.e. the
    # backlog still waiting for capacity.
    print()
    print(f"{'Bucket':<18} {'unowned':>10} {'assigned':>10} {'left':>10}")
    print("-" * 52)
    t_av = t_tk = t_lf = 0
    for state in PRIORITY_STATES:
        av, tk = available.get(state), taken_from.get(state, 0)
        if av is None:
            print(f"{state:<18} {'not read':>10} {tk:>10,} {'—':>10}")
            continue
        left = av - tk
        t_av += av; t_tk += tk; t_lf += left
        print(f"{state:<18} {av:>10,} {tk:>10,} {left:>10,}")
    print("-" * 52)
    print(f"{'TOTAL':<18} {t_av:>10,} {t_tk:>10,} {t_lf:>10,}")
    if args.no_census:
        print("\n(--no-census: buckets after the deficit was filled were never read.)")

    if not args.apply:
        print("\nDRY RUN — nothing written. Re-run with --apply.")
        sample = [(SCRAPERS[u], l) for u, ls in plan.items() for l in ls[:2]][:6]
        if sample:
            print("\nSample:")
            for name, l in sample:
                print(f"  {l.get('display_name','?')[:38]:<38} -> {name}")
        return

    # ---- 4. write ----------------------------------------------------------
    work = [(l["id"], uid) for uid, ls in plan.items() for l in ls]
    print(f"\nAssigning {len(work):,} leads with {WRITE_WORKERS} workers...")
    ok = err = 0
    errors = []

    def _write(item):
        lid, uid = item
        try:
            _req("PUT", f"{BASE}/lead/{lid}/", json={f"custom.{F_OWNER}": uid})
            return None
        except Exception as e:
            return f"  {lid}: {str(e)[:180]}"

    with ThreadPoolExecutor(max_workers=WRITE_WORKERS) as pool_exec:
        for n, e in enumerate(pool_exec.map(_write, work), 1):
            if e:
                err += 1
                if len(errors) < 15:
                    errors.append(e)
            else:
                ok += 1
            if n % 500 == 0:
                print(f"  {n:,}/{len(work):,}", file=sys.stderr)

    print(f"\nDone. {ok:,} assigned, {err:,} failed.")
    if errors:
        print("First errors:")
        print("\n".join(errors))
    print("\nOwner Team + Recapture State will catch up on the next reconciler run.")


if __name__ == "__main__":
    main()
