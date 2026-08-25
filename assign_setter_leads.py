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
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor

from lane2_state import (
    BASE, F_OWNER, F_OVERRIDE, SETTERS, HYBRID_SETTERS, SUPPRESS_STATUSES,
    CloseError,
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


# ============================================================================
# ACTIVE SEQUENCE AWARENESS  (--actionable-queue)
#
# A lead sitting in a live Close sequence is already being worked by automation.
# Counting it toward a Setter's live load makes them look busier than they are
# and starves them of new hand-raises. With --actionable-queue, balancing (and
# the MIN_QUEUE floor) is computed from leads that are NOT in an active sequence.
#
# DESIGN NOTE — this is deliberately NOT one API call per lead.
# The obvious implementation asks "is THIS lead in a sequence?" for every held
# lead. Against the Scraper rotation's ~25,000 leads that is 25,000+ requests:
# ~84 minutes at 5 req/s, past the 60-minute workflow timeout. Instead we sweep
# subscriptions once and build a set — there are only ~16,000 active
# subscriptions org-wide across ~20 active sequences, so ~160 paged requests
# cover the same ground. Roughly 158x fewer calls, and the cost stops scaling
# with roster size. Port this shape back to assign_lane2_leads.py.
# ============================================================================

def close_paginate_skip(path, params=None):
    """Yield items from Close endpoints that use _skip pagination, with backoff."""
    params = dict(params or {})
    params.setdefault("_limit", 100)
    skip = 0
    while True:
        page = dict(params)
        page["_skip"] = skip
        last_exc = None
        for attempt in range(5):
            try:
                data = _req("GET", f"{BASE}{path}", params=page).json()
                break
            except Exception as exc:
                last_exc = exc
                time.sleep(min(2 ** attempt, 20))
        else:
            raise last_exc
        rows = data.get("data", [])
        for row in rows:
            yield row
        if not data.get("has_more") or not rows:
            return
        skip += len(rows)


def _active_sequence_ids():
    """Ids of sequences currently running.

    Verified against the live org 2026-08-24: /sequence/ returns `status`, and
    active ones carry status == "active".

    HARD FAIL on an empty result rather than returning one. An empty set makes
    every subscription look inactive, which makes every lead look actionable,
    which inflates every deficit — the run would over-assign and look completely
    normal doing it. Same failure shape as the 9,000-row census truncation.
    """
    seqs = list(close_paginate_skip("/sequence/", {"_fields": "id,name,status"}))
    active = {x["id"] for x in seqs if x.get("status") == "active" and x.get("id")}
    if not active:
        raise CloseError(
            f"/sequence/ returned {len(seqs)} sequence(s) but none with "
            'status == "active". Refusing to continue: an empty active set would '
            "mark every lead actionable and over-assign. Check whether the "
            "endpoint still returns a `status` field.")
    return active


def active_sequence_lead_ids():
    """Lead ids with a live subscription to a live sequence.

    Probes once to find out whether /sequence_subscription/ honours a
    `sequence_id` filter. If it does, we page per sequence (bounded, tidy). If
    the filter is ignored — the response comes back carrying other sequences —
    we fall back to one global sweep. Detecting this beats assuming it: an
    ignored filter would otherwise be invisible and we would just re-read the
    whole table once per sequence.
    """
    active = _active_sequence_ids()
    print(f"  {len(active)} active sequence(s).", file=sys.stderr)

    FIELDS = "id,lead_id,sequence_id,status"
    probe_id = sorted(active)[0]
    probe = list(close_paginate_skip(
        "/sequence_subscription/",
        {"sequence_id": probe_id, "status": "active", "_fields": FIELDS, "_limit": 100}))
    honoured = all(r.get("sequence_id") in (None, probe_id) for r in probe)

    lead_ids, subs = set(), 0

    def _take(rows):
        nonlocal subs
        for r in rows:
            if r.get("status") != "active":
                continue
            if r.get("sequence_id") not in active:
                continue
            lid = r.get("lead_id")
            if lid:
                subs += 1
                lead_ids.add(lid)

    if honoured:
        _take(probe)
        for sid in sorted(active):
            if sid == probe_id:
                continue
            _take(close_paginate_skip(
                "/sequence_subscription/",
                {"sequence_id": sid, "status": "active", "_fields": FIELDS, "_limit": 100}))
    else:
        print("  (sequence_id filter not honoured — falling back to one global "
              "sweep)", file=sys.stderr)
        lead_ids.clear(); subs = 0
        _take(close_paginate_skip(
            "/sequence_subscription/", {"status": "active", "_fields": FIELDS, "_limit": 100}))

    print(f"  {subs:,} active subscription(s) across {len(lead_ids):,} lead(s).",
          file=sys.stderr)
    return lead_ids


def not_lane1():
    """No Closer actively working a deal on this lead. Live, not state-based."""
    return negated(has_opp_status(LANE1_OPP_STATUSES))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="write Lead Owner (default: dry run)")
    ap.add_argument("--max-assign", type=int, default=MAX_PER_RUN,
                    help=f"safety ceiling for this run (default {MAX_PER_RUN})")
    ap.add_argument("--actionable-queue", action="store_true",
                    help="balance on hot leads NOT already in an active Close "
                         "sequence — a lead automation is working should not "
                         "count against a Setter's live load")
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
    total_counts = Counter(cf(l, F_OWNER) for l in held)

    sequence_counts = Counter()
    if args.actionable_queue:
        print("Reading active sequence subscriptions...", file=sys.stderr)
        in_sequence = active_sequence_lead_ids()
        actionable = []
        for lead in held:
            if lead.get("id") in in_sequence:
                sequence_counts[cf(lead, F_OWNER)] += 1
            else:
                actionable.append(lead)
        counts = Counter(cf(l, F_OWNER) for l in actionable)

        # Sanity check on the sweep itself. Zero matches across a whole rotation
        # is far more likely to mean the subscription read silently returned
        # nothing than that not one hot lead is sequenced. Left as a warning, not
        # an abort: on a small or freshly-drained queue it can legitimately be 0.
        if held and not sequence_counts:
            print("\n  ⚠️  No held lead matched an active subscription. If that "
                  "looks wrong, re-check the sweep before trusting these deficits.",
                  file=sys.stderr)

        print(f"\nACTIONABLE QUEUE — balancing on hot leads NOT in an active sequence.")
        print(f"  Held hot leads:        {len(held):,}")
        print(f"  In an active sequence: {sum(sequence_counts.values()):,}")
        print(f"  Actionable:            {sum(counts.values()):,}")
    else:
        counts = total_counts

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
    wide = args.actionable_queue
    if wide:
        print(f"{'Setter':<20} {'total':>8} {'in seq':>8} {'action':>8} "
              f"{'gets':>8} {'after':>8}")
        print("-" * 74)
    else:
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
        if wide:
            print(f"{ROTATION[uid]:<20} {total_counts.get(uid, 0):>8,} "
                  f"{sequence_counts.get(uid, 0):>8,} {have:>8,} "
                  f"{gets:>+8,} {have + gets:>8,}{note}")
        else:
            print(f"{ROTATION[uid]:<20} {have:>8,} {gets:>+8,} {have + gets:>8,}{note}")
    print("-" * (74 if wide else 48))
    dealt = sum(len(v) for v in plan.values())
    if wide:
        print(f"{'TOTAL':<20} {sum(total_counts.values()):>8,} "
              f"{sum(sequence_counts.values()):>8,} {sum(counts.values()):>8,} "
              f"{dealt:>+8,} {sum(counts.values()) + dealt:>8,}")
        print("\n'after' is the ACTIONABLE count. Real holdings are 'total' + gets — "
              "a Setter whose sequenced leads finish will see actionable rise on its own.")
    else:
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
