#!/usr/bin/env python3
"""
sync_opp_lead_status.py

Keeps each Lead's native status in sync with its Opportunity status.
Opportunity is the source of truth.

Rule set:
  - If a lead has more than one opportunity, the opportunity that was
    updated most recently (date_updated) wins.
  - 17 Opportunity statuses have a direct label-matched Lead status
    (see STATUS_MAP) and are synced automatically.
  - 8 Opportunity statuses have no Lead-status equivalent (see
    ORPHAN_OPP_STATUS_IDS) -- when the winning opportunity is in one of
    these, the lead's status is left untouched and the lead is logged
    as a flag for manual review.
  - Leads with zero opportunities are skipped entirely (nothing to sync
    against).
  - Re-runs are idempotent: a lead whose status already matches the
    mapped value is skipped (no write, no log noise beyond a debug line).

Performance note: this fetches leads via ONE paginated scan with each
lead's opportunities embedded (same call), instead of listing opportunities
and then issuing a separate GET per lead. At ~56k leads that's ~560 pages
total, not tens of thousands of individual API calls. Validate this
assumption in your first --dry-run --limit 20: check that each printed
lead actually has its opportunities populated. If Close's API doesn't
embed opportunities by default for your account, add
"opportunities.status_id,opportunities.date_updated,opportunities.id" to
the _fields param in paginate_leads_with_opportunities().

LOOKBACK_DAYS (optional) skips evaluating leads whose winning opportunity
hasn't been updated recently -- it doesn't reduce API calls (the bulk
fetch already scans everything cheaply), it just cuts down repeat FLAG/SET
log noise on records that haven't changed. Leave it at 0 (default: no
filter) for the first run so any pre-existing mismatches get caught, then
optionally set it once you've done that initial pass.

Usage:
    python sync_opp_lead_status.py                    # live, full scan
    python sync_opp_lead_status.py --dry-run           # report only, zero writes
    python sync_opp_lead_status.py --limit 200         # only scan first N leads (quick test)
    python sync_opp_lead_status.py --lookback-days 3   # only evaluate recently-changed opportunities
    python sync_opp_lead_status.py --selftest          # pure-logic tests, no network

Env vars:
    CLOSE_API_KEY   required (unless --selftest)
    DRY_RUN         "1" or "0" -- same effect as --dry-run, workflow-friendly
    LOOKBACK_DAYS   same effect as --lookback-days, workflow-friendly (default 0 = no filter)
"""

import os
import sys
import argparse
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import requests

BASE_URL = "https://api.close.com/api/v1"

# ---------------------------------------------------------------------------
# Status maps -- built from the actual Sales Pipeline / Lead status lists.
# If you rename, delete, or add statuses in Close, this table goes stale.
# Re-pull both lists (Settings -> Statuses) and update here.
# ---------------------------------------------------------------------------

# Opportunity status_id -> Lead status_id, matched by identical label text.
STATUS_MAP = {
    "stat_S4cxvnfOWWi8jOqTPtGoq34BzuU8ah9fKTPPfO5P9Yi": "stat_lGHxEKwhbVswuchbpRo6XcMMSXz0fV4CID9qFWT8KCO",  # Call Booked
    "stat_cqwPKIAezUGp8sJ4May80zRjJAlzCQzVXtAc4P9xyPd": "stat_2SmOUMCp1vDFJF0TcJ011hNnpLYWDGwugyo4JyiRMEP",  # Reschedule
    "stat_mApYfeCdMszhTCCp6TJMWBRCRtmMilVyvaMpVlzbZZR": "stat_kY1aMGKOui3jjlgniY2LQWMadXN78cr7vTHVMPDCliy",  # Follow Up
    "stat_csQPyVHTXpTFBSDAK8kx10yGCtX40H0ONbD4QNXbJBI": "stat_vL6LDuMPhQHcpNvvT6bA6Ofc0soHWDBks1azdq8UTJk",  # Contract Sent
    "stat_NCXVjokjo3VXirJx2eSAcRoKlEDg1WsO1sjeLfU8udO": "stat_5CqIgNJnGYO357zXjSnH6BAkKyoCvYUOBxVvpYfDMZn",  # No Show
    "stat_9ae2fCnLhMKoWq15dKkAEb5drDFPXgV1PZHJegl3fuq": "stat_hWIGHjzyNpl4YjIFSFz3VK4fp2ny10SFJLKAihmo4KT",  # Canceled (by Lead)
    "stat_bBWcww9IflskaleadKuK2E4SGFF4qy3IuBucrqo7H4u": "stat_aR2jBa8YnTNZmHAnPsnlQuinBdaXpSBCkZGP3UvoBlV",  # Lost
    "stat_E9LE4YrRUQvQIIs7GoaWA4eOFqzs1GtsoV4qKWmvbYN": "stat_YV4ZngDB4IGjLjlOf0YTFEWuKZJ6fhNxVkzQkvKYfdB",  # Outside the US
    "stat_WnFc0uhjcjV0cc3bVzdFVqDz7av6rbsOmOvHUsO6s03": "stat_0oW3iRpVp9z5DJq0cuwI1HgR0XhHAhykEPPIq4TFsxd",  # Closed / Won
    "stat_oyR6irMMbv9KIigS8VEmX44DmXMhUut3EL57pFsExPF": "stat_mRxbAkfEqzEcmEF2Z5CkucMQocXAbwzs0hSlD0SzHEh",  # Long Term Follow Up
    "stat_bhmaw9aSACxeEIW9wjdE9dnSBMyQEWpZBSZZRdg0fcK": "stat_l8ATqabgtlrL7EKL5o0EL8ufBg8UMPxMJJP1AXI9V9i",  # Non-responsive
    "stat_P9TLh4NNjKZztUlaUI533dcxhSCXmfoL7DuC2gIuIzM": "stat_U9MI7pqsvIjceTv3pCU7b1EghO8Q83h1HUcL6fGVyi6",  # Do Not Contact
    "stat_gJjDwCgfdPqVaAgVY64CI8XwxjNuwi3yrKlIXnBdF5l": "stat_nN6uLtk05n1MZML0gDFUVvP7zPpQOqOLQm4SsRuvdiW",  # Webinar Lead
    "stat_XdEFMMZI2kSfeHSb1DxS08WZCQAROeSOElayjYKawPU": "stat_i9BrzV5VmrNsy1FlHJKg4NV0Fb5Hxo5qRTOD9vMEWeT",  # Scraper Show
    "stat_SJL06q8F8GOcHyX6RBclkQHLDh1tHaRm9KR4R35nOXA": "stat_FRlJpzeReKIGHnEHmf5UYcwUgCJJsGj465UsM5heCi9",  # Scraper No-Show
    "stat_FP0Obs2SMeD84gKU2WsjpRQH9JYEiArHniVdbc5drsc": "stat_5vONRb3xsup9faV8qqesxLTRhmQTw94YJMTJ5R2ZvON",  # Scraper Canceled
    "stat_oPfsZCwCCEadif8uzCSCcb6TroB3Gu5FKxnUPdJlWTM": "stat_3RYd0LpypFaOBbRBMSw9fnTdyooJZTVcCVCKS4fXi5z",  # Scraper Rescheduled
}

# Opportunity statuses with no Lead-status equivalent. When the winning
# opportunity is in one of these, we do NOT touch the lead's status.
ORPHAN_OPP_STATUS_IDS = {
    "stat_ZA4DFlp3JeGCpqLVoQxIEGfd3WNxAqdM5RrHQrzrpm6": "OLD Deal Won (Prior to 2026 Migration)",
    "stat_xJYl3faVfshDeuaL1w1Rogh3JA64eLJqNVJL1navPWf": "Meeting Scheduled",
    "stat_Nr7cBCXTx67umK73a6KnaFoIDqARapYUgZ5ApUUs41x": "Discovery Completed",
    "stat_NGfzbFQkI46nagRbqrRPyaAm8z8gdqHa8AhJnjEwdQq": "Proposal Presented",
    "stat_lIge79IrriROLh2ocwM1Xm6oS74vSPJx0epwL465hxN": "Commitment Pending",
    "stat_8XbH25IjXYGT3b1g07d9AFNCb6WbqVjd6Yjz0ykd3NQ": "Nurture",
    "stat_LbsuNBimzj2Fa3RCBpTz6kEDeokz3O97LwUorSpDhVp": "Closed Lost",
    "stat_G331FCVhaJeCFYRDNEfRBwOqnx13hNo2o6p2cvwrWwe": "Discovery Call",
}


def pick_winning_opportunity(opps):
    """Given a list of opportunity dicts (each with date_updated), return
    the one updated most recently. Pure function -- no network -- so it's
    covered by --selftest."""
    return max(opps, key=lambda o: o["date_updated"])


# ---------------------------------------------------------------------------
# Close API helpers
# ---------------------------------------------------------------------------

class CloseClient:
    def __init__(self, api_key):
        self.session = requests.Session()
        self.session.auth = (api_key, "")

    def _request(self, method, path, **kwargs):
        url = f"{BASE_URL}{path}"
        for attempt in range(5):
            resp = self.session.request(method, url, **kwargs)
            if resp.status_code == 429:
                wait = float(resp.headers.get("Retry-After", 2 * (attempt + 1)))
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json() if resp.content else {}
        resp.raise_for_status()

    def paginate_leads_with_opportunities(self, limit=None):
        """Yields every lead dict with id, status_id, display_name, and its
        opportunities embedded (each with at least id, status_id,
        date_updated). One paginated scan -- no per-lead follow-up call."""
        skip = 0
        page_size = 100
        fetched = 0
        while True:
            params = {
                "_skip": skip,
                "_limit": page_size,
                "_fields": "id,status_id,display_name,opportunities",
            }
            data = self._request("GET", "/lead/", params=params)
            for lead in data.get("data", []):
                yield lead
                fetched += 1
                if limit and fetched >= limit:
                    return
            if not data.get("has_more"):
                return
            skip += page_size

    def set_lead_status(self, lead_id, status_id):
        return self._request(
            "PUT", f"/lead/{lead_id}/", json={"status_id": status_id}
        )


# ---------------------------------------------------------------------------
# Main run
# ---------------------------------------------------------------------------

def run(dry_run, limit, lookback_days):
    api_key = os.environ.get("CLOSE_API_KEY")
    if not api_key:
        sys.exit("CLOSE_API_KEY environment variable is required")

    client = CloseClient(api_key)
    cutoff = None
    if lookback_days:
        cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
        print(f"Lookback active: only evaluating opportunities updated since {cutoff.isoformat()}")

    print(f"Fetching leads (with opportunities embedded){f', limit {limit}' if limit else ''}...")
    leads_scanned = 0
    leads_with_opps = 0
    updated = already_correct = orphan_skipped = stale_skipped = failed = 0

    for lead in client.paginate_leads_with_opportunities(limit=limit):
        leads_scanned += 1
        opps = lead.get("opportunities") or []
        if not opps:
            continue
        leads_with_opps += 1

        winner = pick_winning_opportunity(opps)
        opp_status_id = winner["status_id"]

        if cutoff is not None:
            winner_updated = datetime.fromisoformat(winner["date_updated"].replace("Z", "+00:00"))
            if winner_updated < cutoff:
                stale_skipped += 1
                continue

        lead_id = lead["id"]

        if opp_status_id in ORPHAN_OPP_STATUS_IDS:
            orphan_skipped += 1
            label = ORPHAN_OPP_STATUS_IDS[opp_status_id]
            print(f"FLAG  lead {lead_id}: opp status '{label}' has no Lead equivalent -- left untouched "
                  f"(https://app.close.com/lead/{lead_id}/)")
            continue

        mapped_status_id = STATUS_MAP.get(opp_status_id)
        if mapped_status_id is None:
            # Should not happen unless Close statuses changed since this
            # table was built -- treat like an orphan but call it out louder.
            orphan_skipped += 1
            print(f"WARN  lead {lead_id}: opp status_id {opp_status_id} not in STATUS_MAP or "
                  f"ORPHAN_OPP_STATUS_IDS -- status lists may have changed in Close. Skipped.")
            continue

        if lead["status_id"] == mapped_status_id:
            already_correct += 1
            continue

        if dry_run:
            print(f"WOULD SET lead {lead_id} ({lead.get('display_name', '')}): "
                  f"{lead['status_id']} -> {mapped_status_id}")
            updated += 1
            continue

        try:
            result = client.set_lead_status(lead_id, mapped_status_id)
        except requests.HTTPError as e:
            failed += 1
            print(f"ERROR updating lead {lead_id}: {e}")
            continue

        if result.get("status_id") != mapped_status_id:
            failed += 1
            print(f"ERROR lead {lead_id}: write did not take (verify failed)")
            continue

        updated += 1
        print(f"SET   lead {lead_id} ({lead.get('display_name', '')}) -> {mapped_status_id}")

    print("\n--- Summary ---")
    print(f"Leads scanned:                     {leads_scanned}")
    print(f"Leads with >=1 opportunity:        {leads_with_opps}")
    print(f"Updated:                           {updated}")
    print(f"Already correct:                   {already_correct}")
    print(f"Orphan / unmapped status (flag):   {orphan_skipped}")
    print(f"Skipped (outside lookback window): {stale_skipped}")
    print(f"Failed:                            {failed}")
    if dry_run:
        print("\nDRY RUN -- no writes were made.")


def selftest():
    failures = 0

    def check(name, cond):
        nonlocal failures
        status = "ok" if cond else "FAIL"
        if not cond:
            failures += 1
        print(f"[{status}] {name}")

    # pick_winning_opportunity picks the most recently updated
    opps = [
        {"id": "a", "date_updated": "2026-01-01T00:00:00.000Z", "status_id": "s1"},
        {"id": "b", "date_updated": "2026-06-01T00:00:00.000Z", "status_id": "s2"},
        {"id": "c", "date_updated": "2026-03-01T00:00:00.000Z", "status_id": "s3"},
    ]
    check("winner is most recently updated opp", pick_winning_opportunity(opps)["id"] == "b")

    check("single opp is its own winner",
          pick_winning_opportunity([opps[0]])["id"] == "a")

    # STATUS_MAP integrity
    check("STATUS_MAP has 17 entries", len(STATUS_MAP) == 17)
    check("ORPHAN_OPP_STATUS_IDS has 8 entries", len(ORPHAN_OPP_STATUS_IDS) == 8)
    check("no overlap between STATUS_MAP and ORPHAN_OPP_STATUS_IDS",
          set(STATUS_MAP.keys()).isdisjoint(ORPHAN_OPP_STATUS_IDS.keys()))
    check("all STATUS_MAP values look like status ids",
          all(v.startswith("stat_") for v in STATUS_MAP.values()))
    check("all STATUS_MAP keys look like status ids",
          all(k.startswith("stat_") for k in STATUS_MAP.keys()))
    check("no duplicate Lead status_id targets in STATUS_MAP",
          len(set(STATUS_MAP.values())) == len(STATUS_MAP))

    print(f"\n{'PASSED' if failures == 0 else f'{failures} FAILURE(S)'}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=None,
                         help="only scan first N leads (quick test)")
    parser.add_argument("--lookback-days", type=int, default=None,
                         help="only evaluate opportunities updated in the last N days "
                              "(0 or omitted = no filter, full history)")
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args()

    if args.selftest:
        selftest()

    dry_run = args.dry_run or os.environ.get("DRY_RUN", "0") == "1"
    lookback_days = args.lookback_days
    if lookback_days is None:
        lookback_days = int(os.environ.get("LOOKBACK_DAYS", "0"))
    run(dry_run=dry_run, limit=args.limit, lookback_days=lookback_days)
