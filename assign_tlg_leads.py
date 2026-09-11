#!/usr/bin/env python3
"""
The Land Geek (TLG) lead assignment — top-up round robin for the TLG setters.

TLG is a SEPARATE OFFER from Vendingpreneurs. It shares the Close instance, the
Recapture State machinery and the reconciler, and nothing else: its own reps, its
own lists, its own pool.

    assign_lane2_leads.py   -> VP Scrapers   (excludes TLG)
    assign_setter_leads.py  -> VP Setters    (excludes TLG)
    assign_tlg_leads.py     -> TLG Setters   (TLG only)          <- this file

The separation is enforced on BOTH sides, not just this one. Every VP assigner
and all 18 `L2 ·` views carry `not_excluded_business_line()`; this script and the
`TLG ·` views carry `business_line_is([BL_TLG])`. A lead cannot appear in both.

    python3 assign_tlg_leads.py                  # dry run
    python3 assign_tlg_leads.py --apply
    python3 assign_tlg_leads.py --apply --max-queue 1000
"""

import argparse
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

from lane2_state import (
    BASE, F_OWNER, F_OVERRIDE, TLG_SETTERS, SUPPRESS_STATUSES, BL_TLG,
    CloseError, WRITE_WORKERS, SKIP_CAP, PROBE_CAP,
    _wrap, _req, cf, search, status_in, business_line_is,
)

# ============================================================================
# CONFIG
# ============================================================================

# Who gets dealt TLG leads. Mirrors the roster in lane2_state.TLG_SETTERS, which
# drives Owner Team stamping — this one drives dealing. Same split as every other
# lane, and the same reason: the two can legitimately differ.
#
# Beatrice is ALSO a VP Scraper (see lane2_state.TLG_SETTERS). She is in both on
# purpose. Her VP book and her TLG book never mix, because each assigner and each
# view set filters on business line.
ROTATION = dict(TLG_SETTERS)

# Target book per rep. Stephen, 2026-09-11: "to begin, we want to give both Josh
# and Beatrice 1000 leads to start."
#
# Same semantics as the Scraper assigner: a TOP-UP target, not a ceiling on what
# is already held. The cap stops giving; it never claws back.
#
# In code rather than only in the workflow input, because a bodyless cron dispatch
# sends no max_queue and the no-argument behaviour has to be the safe one.
MAX_QUEUE = 1000

# Hottest first, same ladder as the Scraper assigner. Hot-Inbound is included here
# — unlike the VP Scraper lane — because TLG has no separate inbound setter team to
# hand fresh hand-raises to. These two reps work the whole TLG book.
#
# BE CAREFUL READING THIS LADDER FOR TLG. On 2026-09-11 15,915 of the 18,275
# imported leads stamped Hot-Inbound, which sounds like a wall of hand-raises and
# is nothing of the sort: bucket #6 is "created in the last 14 days, no completed
# meeting", and a bulk import satisfies that by definition. Expect them to age out
# of the 14-day window together and land in Deep-Nurture about two weeks after the
# import. The ladder only starts carrying real signal once TLG generates its own
# inbound.
PRIORITY_STATES = ["Hot-Inbound", "Blitz", "Active-Nurture", "Deep-Nurture"]

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


def state_is(values):
    from lane2_state import F_STATE
    return {"type": "field_condition", "negate": False,
            "field": {"type": "custom_field", "custom_field_id": F_STATE},
            "condition": {"type": "term", "values": list(values)}}


def build_deficits(counts, target):
    return {u: max(0, target - counts.get(u, 0)) for u in ROTATION}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="write Lead Owner (default: dry run)")
    ap.add_argument("--max-queue", type=int, default=MAX_QUEUE,
                    help=f"target book per rep (default {MAX_QUEUE}; 0 = unlimited)")
    args = ap.parse_args()
    target = None if args.max_queue == 0 else args.max_queue

    if not ROTATION:
        sys.exit("No TLG setters configured in lane2_state.TLG_SETTERS.")

    # ---- 1. what does each TLG rep already hold? ---------------------------
    #
    # NO `limit=` — search() only month-partitions when called without one, and a
    # limit at or below SKIP_CAP silently truncates. That bug cost a 3,000-lead
    # over-assignment on the Scraper side (2026-08-12); the tripwire below catches
    # a recurrence.
    print("Counting current TLG books...", file=sys.stderr)
    held = search(
        _wrap(status_in(SUPPRESS_STATUSES, negate=True),
              business_line_is([BL_TLG]),
              owner_is(ROTATION.keys()),
              state_is(PRIORITY_STATES)),
        fields=["id", f"custom.{F_OWNER}"])

    for cap in (SKIP_CAP, PROBE_CAP):
        if len(held) == cap:
            sys.exit(f"\nABORT — the census returned exactly {cap:,} rows, which is a "
                     f"page cap, not a count. Deficits from a truncated census "
                     f"over-assign.")

    counts = Counter(cf(l, F_OWNER) for l in held)

    # ---- 2. the unclaimed TLG pool -----------------------------------------
    print("Reading the unclaimed TLG pool...", file=sys.stderr)
    pool, available = [], Counter()
    for state in PRIORITY_STATES:
        rows = search(
            _wrap(status_in(SUPPRESS_STATUSES, negate=True),
                  business_line_is([BL_TLG]),
                  owner_empty(), state_is([state])),
            fields=["id", "display_name", "date_created", f"custom.{F_OVERRIDE}"])
        rows = [r for r in rows if cf(r, F_OVERRIDE) != "Yes"]   # pinned — leave alone
        available[state] = len(rows)
        for r in rows:
            r["_state"] = state
        pool.extend(rows)
        print(f"  {state:<16} {len(rows):>7,} unowned", file=sys.stderr)

    if not pool:
        print("\nNo unowned TLG leads in a workable state.")
        print("\nIf the import has landed, check that Lead Owner is CLEARED on it —\n"
              "this script only ever deals leads with NO owner, so leads still held\n"
              "by the importing user are invisible to it.")
        return

    # ---- 3. deal, furthest-behind first -------------------------------------
    deficit = build_deficits(counts, target) if target else {
        u: len(pool) for u in ROTATION}

    plan = defaultdict(list)
    order = sorted(ROTATION, key=lambda u: (-deficit[u], ROTATION[u]))
    i = 0
    for lead in pool:
        for _ in range(len(order)):
            uid = order[i % len(order)]
            i += 1
            if deficit[uid] > 0:
                plan[uid].append(lead)
                deficit[uid] -= 1
                break
        else:
            break   # everyone at target

    # ---- 4. report ----------------------------------------------------------
    print()
    mode = f"target {target:,}/rep" if target else "uncapped"
    print(f"{'TLG Setter':<28} {'holds':>8} {'gets':>8} {'after':>8}   [{mode}]")
    print("-" * 62)
    for uid in sorted(ROTATION, key=lambda u: -(counts.get(u, 0) + len(plan[u]))):
        have, gets = counts.get(uid, 0), len(plan[uid])
        over = f"  +{have - target:,} over" if target and have > target else ""
        print(f"{ROTATION[uid]:<28} {have:>8,} {gets:>+8,} {have + gets:>8,}{over}")
    print("-" * 62)
    dealt = sum(len(v) for v in plan.values())
    print(f"{'TOTAL':<28} {sum(counts.values()):>8,} {dealt:>+8,} "
          f"{sum(counts.values()) + dealt:>8,}")

    print()
    print(f"{'Bucket':<18} {'unowned':>10} {'assigned':>10} {'left':>10}")
    print("-" * 52)
    taken = Counter(l["_state"] for ls in plan.values() for l in ls)
    t_av = t_tk = 0
    for state in PRIORITY_STATES:
        av, tk = available.get(state, 0), taken.get(state, 0)
        t_av += av; t_tk += tk
        print(f"{state:<18} {av:>10,} {tk:>10,} {av - tk:>10,}")
    print("-" * 52)
    print(f"{'TOTAL':<18} {t_av:>10,} {t_tk:>10,} {t_av - t_tk:>10,}")

    if not args.apply:
        print("\nDRY RUN — nothing written. Re-run with --apply.")
        sample = [(ROTATION[u], l) for u, ls in plan.items() for l in ls[:3]][:6]
        if sample:
            print("\nSample:")
            for name, l in sample:
                print(f"  {l.get('display_name', '?')[:38]:<38} -> {name}")
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
            return None
        except Exception as e:
            return f"  {lid}: {str(e)[:180]}"

    with ThreadPoolExecutor(max_workers=WRITE_WORKERS) as ex:
        for n, e in enumerate(ex.map(_write, work), 1):
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
