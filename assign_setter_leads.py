#!/usr/bin/env python3
"""
Setter round robin — assigns fresh inbound hand-raises to a named Setter.

Sets Lead Owner ONLY. Never touches Recapture State, Owner Team, or anything else.

    python3 assign_setter_leads.py            # dry run
    python3 assign_setter_leads.py --apply     # write
    python3 assign_setter_leads.py --apply --max-assign 25

WHY THIS IS A SEPARATE SCRIPT FROM assign_lane2_leads.py
--------------------------------------------------------
Same shape, different physics. Three things differ and each one matters:

1. CADENCE. The Scraper assigner runs once a weekday morning; a dormant lead
   does not care whether it waits eight hours. A hand-raise does. This is built
   to run every ~15 minutes.

2. LIVE CRITERIA, NOT STAMPED STATE. The Scraper assigner filters on
   `Recapture State`, which the reconciler stamps hourly. That is fine for a
   day-scale queue and fatal here: a lead that arrived four minutes ago has no
   stamp yet, so a state-based filter would systematically miss exactly the
   leads this exists to route. So the pool is rebuilt from the SAME live
   conditions the Hot-Inbound bucket uses. Same rule as the SLA views:
   time-critical reads live, day-scale may read state.

3. NO QUEUE TARGET. A Scraper holds a 1,000-lead book. A Setter does not "hold"
   hot leads — they work them or lose them. So there is no top-up-to-target;
   every eligible lead is dealt, balanced by who is currently carrying less.

Balancing is by CURRENT live hot holdings, deliberately, so the script stays
stateless — no rotation pointer to persist or get out of sync. A Setter who
converts their hot leads quickly drops back down the count and receives the next
one. That rewards throughput, which is the behaviour we want on a speed-to-lead
queue. If it ever needs to be "equal count dealt per day" instead of "equal live
load", that is a different function and a deliberate decision.
"""

import argparse
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor

from lane2_state import (
    BASE, F_OWNER, F_OVERRIDE, SETTERS, HYBRID_SETTERS, SUPPRESS_STATUSES,
    LANE1_OPP_STATUSES, LANE_1, WRITE_WORKERS,
    HOT_WINDOW_DAYS, REENGAGE_DAYS,
    _wrap, _req, cf, search, status_in, num_range, within,
    has_completed_meeting, any_inbound, has_opp_status,
)

# ============================================================================
# CONFIG
# ============================================================================

# Safety ceiling per run, not a business rule. At a ~15 minute cadence this is
# ~400/hour, which drains any realistic backlog within the morning while making
# it impossible for a bad query to deal thousands of leads in one go.
MAX_PER_RUN = 100

# Hybrid Setters (Ariella) book and run their own calls rather than handing off
# to Lane 1. Hot inbound was originally the raw material for that, so they shared
# this rotation and received an equal share.
#
# OFF since 2026-08-20 on Stephen's call. Ariella now works the dormant book only
# — she stays in assign_lane2_leads.SCRAPERS and receives no fresh hot inbound.
#
# CONSEQUENCE, so nobody has to rediscover it: the "hybrid" part of Hybrid Setter
# is now dormant. She keeps the Setter Owner Team label and her existing hot leads
# (this script never takes a lead off anyone), but her incoming flow is entirely
# Scraper-side. If she should get hot inbound again, flip this back to True.
HYBRID_IN_ROTATION = False

ROTATION = {**SETTERS, **HYBRID_SETTERS} if HYBRID_IN_ROTATION else dict(SETTERS)

# Per-rep floor. Anyone below their number is topped up BEFORE the normal
# least-loaded balancing runs. Keyed by user id; absent = no floor.
#
# Spencer sat at 9 live hot leads against William's ~917 on 2026-08-20, because
# this script had never run on a schedule. Least-loaded dealing alone would fix
# that eventually, but only while he happens to be the lowest — a floor states the
# intent directly and survives a third Setter joining the rotation.
#
# This is a FLOOR, not a target or a cap. It changes who gets dealt next; it never
# takes a lead off anyone, and nobody is held back once they are above it.
MIN_QUEUE = {
    "user_4sfuKGMbv0LQZ4hpS8ipASv406kKTSNP5Xx79jOwSqM": 250,   # Spencer Reynolds
}

# Setters do not get leads another rep already owns. Same guarantee as the
# Scraper assigner: this script only ever picks up leads with NO Lead Owner.
#
# Note there are ~1,600 Hot-Inbound leads owned by SCRAPERS. Those are nurture
# leads that re-engaged and got re-stamped Hot-Inbound; they keep their existing
# owner and this script leaves them alone. Moving them to Setters would be a
# separate, deliberate decision — not something to do silently here.

# ============================================================================


def owner_empty():
    return {"type": "field_condition", "negate": True,
            "field": {"type": "custom_field", "custom_field_id": F_OWNER},
            "condition": {"type": "exists"}}


def owner_is(user_ids):
    return {"type": "field_condition", "negate": False,
            "field": {"type": "custom_field", "custom_field_id": F_OWNER},
            "condition": {"type": "reference", "reference_type": "user_or_group",
                          "object_ids": list(user_ids)}}


def negated(cond):
    import json
    c = json.loads(json.dumps(cond))
    c["negate"] = not c.get("negate", False)
    return c


def hot_inbound_live():
    """
    The Hot-Inbound bucket, rebuilt from live conditions.

    Mirrors precedence rules 3 and 6 in lane2_state.BUCKETS, which are the two
    that produce Hot-Inbound:

        #3  re-engaged : inbound in the last REENGAGE_DAYS, never had a call
        #6  fresh      : created in the last HOT_WINDOW_DAYS, never had a call

    Both additionally require: not suppressed, and nothing on the calendar.
    Kept as one OR so the two paths can't drift apart.
    """
    return [
        status_in(SUPPRESS_STATUSES, negate=True),
        num_range("num_upcoming_meetings", lte=0),
        has_completed_meeting(negate=True),
        {"negate": False, "type": "or", "queries": [
            within("date_created", days=HOT_WINDOW_DAYS),
            any_inbound(REENGAGE_DAYS),
        ]},
    ]


def _norm(ts):
    """Normalise a Close timestamp so two of them can be compared as strings.

    Close is not consistent about the date/time separator — `date_created` has come
    back space-separated ("2026-08-16 22:56:53+00:00") while
    `last_communication_date` came back with a "T". Space (0x20) sorts BEFORE every
    digit and "T" (0x54) sorts after, so comparing the two raw would order them by
    format rather than by time. Normalise before any max() or sort.
    """
    return str(ts or "").replace(" ", "T").replace("Z", "+00:00")


def _waiting_since(lead):
    """When this lead raised its hand — the thing SLA should actually be measured from.

    Fresh leads (inside HOT_WINDOW_DAYS) sort on date_created: that IS the moment
    they arrived. Older leads are only in this pool because they re-engaged, so the
    most recent communication is the best available proxy for when that happened —
    Close exposes no "last INBOUND communication" field to sort on.

    Deliberately not applied to fresh leads: a bulk marketing send updates
    last_communication_date on everything it touches, and using it everywhere would
    let one campaign reshuffle the whole queue. Confining it to the re-engaged tail
    keeps that blast radius small. Falls back to date_created whenever the value is
    missing or unparseable, so a bad field can never reorder the queue silently.
    """
    created = _norm(lead.get("date_created"))
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(created)).days
    except ValueError:
        return created
    if age <= HOT_WINDOW_DAYS:
        return created
    comm = _norm(lead.get("last_communication_date"))
    try:                       # only trust it if it is actually a timestamp —
        datetime.fromisoformat(comm)   # otherwise max() would let "junk" win and
    except ValueError:                 # bury the lead at the end of the queue
        return created
    return max(created, comm)


def not_lane1():
    """No Closer actively working a deal on this lead. Live, not state-based."""
    return negated(has_opp_status(LANE1_OPP_STATUSES))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="write Lead Owner (default: dry run)")
    ap.add_argument("--max-assign", type=int, default=MAX_PER_RUN,
                    help=f"safety ceiling for this run (default {MAX_PER_RUN})")
    args = ap.parse_args()

    if not ROTATION:
        sys.exit("No setters configured in lane2_state.SETTERS / HYBRID_SETTERS.")

    overlap = set(ROTATION) & set(LANE_1)
    if overlap:
        sys.exit("CONFIG ERROR — user is both a Setter and Lane 1: "
                 + ", ".join(ROTATION[u] for u in overlap))

    # ---- 1. what does each Setter currently carry? -------------------------
    print("Counting live hot queues...", file=sys.stderr)
    held = search(
        _wrap(*hot_inbound_live(), owner_is(ROTATION.keys())),
        fields=["id", f"custom.{F_OWNER}"])
    counts = Counter(cf(l, F_OWNER) for l in held)

    # ---- 2. the unclaimed hot pool -----------------------------------------
    print("Reading unclaimed hot inbound...", file=sys.stderr)
    pool = search(
        _wrap(*hot_inbound_live(), owner_empty(), not_lane1()),
        fields=["id", "display_name", "date_created", "last_communication_date",
                f"custom.{F_OVERRIDE}"])
    pool = [r for r in pool if cf(r, F_OVERRIDE) != "Yes"]

    # Longest-waiting-since-the-hand-raise first — NOT oldest date_created.
    #
    # This pool has two kinds of lead in it and `date_created` only describes one
    # of them. A fresh hand-raise was created when it raised its hand, so
    # date_created is exactly right. A RE-ENGAGED lead is here because it replied
    # in the last REENGAGE_DAYS, and its date_created can be years old.
    #
    # Sorting the whole pool on date_created put a lead created in April 2025 that
    # replied yesterday at the very top, ahead of genuinely fresh hand-raises from
    # this week — which then sank to the bottom and were left for the next run. On
    # a speed-to-lead job that is precisely backwards. Caught 2026-08-20 from a dry
    # run whose first three rows were all 12-16 months old.
    pool.sort(key=_waiting_since)

    total_available = len(pool)
    capped = total_available > args.max_assign
    if capped:
        pool = pool[:args.max_assign]

    if not pool:
        print("\nNothing unclaimed in the hot window. Nothing to assign.")
        return

    # ---- 3. deal, always to whoever is carrying least -----------------------
    plan = defaultdict(list)
    running = {u: counts.get(u, 0) for u in ROTATION}
    for lead in pool:
        # Floors first: anyone under their MIN_QUEUE gets served before balancing
        # resumes, furthest-below-floor first. Ties break on name so a dry run and
        # the apply that follows deal identically.
        below = [u for u in ROTATION if running[u] < MIN_QUEUE.get(u, 0)]
        if below:
            uid = min(below, key=lambda u: (running[u] - MIN_QUEUE[u], ROTATION[u]))
        else:
            uid = min(ROTATION, key=lambda u: (running[u], ROTATION[u]))
        plan[uid].append(lead)
        running[uid] += 1

    # ---- 4. report ----------------------------------------------------------
    print()
    print(f"{'Setter':<20} {'holds':>8} {'gets':>8} {'after':>8}")
    print("-" * 48)
    for uid in sorted(ROTATION, key=lambda u: -(counts.get(u, 0) + len(plan[u]))):
        have, gets = counts.get(uid, 0), len(plan[uid])
        floor = MIN_QUEUE.get(uid)
        note = ""
        if floor:
            short = floor - (have + gets)
            note = (f"   floor {floor:,} — {short:,} short, next run continues"
                    if short > 0 else f"   floor {floor:,} met")
        print(f"{ROTATION[uid]:<20} {have:>8,} {gets:>+8,} {have + gets:>8,}{note}")
    print("-" * 48)
    dealt = sum(len(v) for v in plan.values())
    print(f"{'TOTAL':<20} {sum(counts.values()):>8,} {dealt:>+8,} "
          f"{sum(counts.values()) + dealt:>8,}")

    fresh = sum(1 for l in pool if str(_waiting_since(l))[:10] == (l.get("date_created") or "")[:10])
    print(f"\nOf this run: {fresh:,} fresh hand-raise(s), {len(pool) - fresh:,} re-engaged")
    print(f"\nUnclaimed hot inbound available : {total_available:,}")
    print(f"Assigned this run               : {dealt:,}")
    if capped:
        print(f"Left for the next run           : {total_available - dealt:,}"
              f"   (--max-assign {args.max_assign})")

    if not args.apply:
        print("\nDRY RUN — nothing written. Re-run with --apply.")
        print("\nSample (oldest first):")
        for uid, ls in plan.items():
            for l in ls[:3]:
                w = str(_waiting_since(l))[:16].replace("T", " ")
                c = (l.get("date_created") or "")[:10]
                kind = "fresh" if w[:10] == c else "re-eng"
                print(f"  raised {w}  ({kind}, created {c})  "
                      f"{l.get('display_name', '?')[:28]:<28} -> {ROTATION[uid]}")
        return

    # ---- 5. write -----------------------------------------------------------
    work = [(l["id"], uid) for uid, ls in plan.items() for l in ls]
    print(f"\nAssigning {len(work):,} leads with {WRITE_WORKERS} workers...")
    ok = err = 0
    errors = []

    def _write(item):
        lid, uid = item
        try:
            _req("PUT", f"{BASE}/lead/{lid}/", json={f"custom.{F_OWNER}": uid})
            return True, None
        except Exception as e:
            return False, f"{lid}: {e}"

    with ThreadPoolExecutor(max_workers=WRITE_WORKERS) as ex:
        for good, msg in ex.map(_write, work):
            if good:
                ok += 1
            else:
                err += 1
                if len(errors) < 5:
                    errors.append(msg)

    print(f"\nDone. {ok:,} assigned, {err:,} failed.")
    for m in errors:
        print(f"  {m}", file=sys.stderr)
    print("\nOwner Team will catch up on the next reconciler run.")


if __name__ == "__main__":
    main()
