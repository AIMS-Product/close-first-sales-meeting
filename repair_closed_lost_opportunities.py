#!/usr/bin/env python3
"""
repair_closed_lost_opportunities.py

ONE-TIME REPAIR -- not a recurring job.

Context: 9,102 opportunities (26% of the org, as of 2026-07-27) are
currently sitting in the Opportunity-only "🪦 Closed Lost" status, believed
to be an accidental bulk mis-set rather than a real reflection of each
deal's state. Lead status was NOT affected by whatever happened, so for
every opportunity currently in Closed Lost, this script sets the
OPPORTUNITY's status to mirror whatever its LEAD's status currently is
(the reverse direction of sync_opp_lead_status.py's ongoing job, and
scoped only to this one status -- it does not touch any other opportunity).

Once this has run clean (0 opportunities left in Closed Lost, confirmed via
a dry-run count), the plan is to delete the Closed Lost status option in
Close entirely. Do that AFTER confirming this script found nothing left to
fix, not before -- Close may block deleting a status that's still
referenced by live records, or reassign them to some default rather than
what the lead reflects, which would undo the point of this script.

Mapping logic:
  - Uses the same 17-entry label-matched STATUS_MAP as
    sync_opp_lead_status.py, but inverted (Lead status_id -> Opportunity
    status_id) since we're going the opposite direction here. Keep this
    file's copy of STATUS_MAP in sync with that script's copy if either
    changes -- they are intentionally duplicated rather than imported, so
    this script stays drop-in runnable on its own.
  - If a lead's current status has no Opportunity equivalent (the 6
    Lead-only statuses: New, Contacted, Engaged, Unresponsive, Disqualified,
    Off Ramp), the opportunity is left in Closed Lost and flagged for
    manual review -- there's nothing sensible to set it to automatically.

Usage:
    python repair_closed_lost_opportunities.py              # live
    python repair_closed_lost_opportunities.py --dry-run     # report only, zero writes -- run this first
    python repair_closed_lost_opportunities.py --limit 20    # quick smoke test
    python repair_closed_lost_opportunities.py --selftest    # pure-logic tests, no network

Env vars:
    CLOSE_API_KEY   required (unless --selftest)
    DRY_RUN         "1" or "0" -- same effect as --dry-run, workflow-friendly
"""

import os
import sys
import argparse
import time

import requests

BASE_URL = "https://api.close.com/api/v1"

CLOSED_LOST_STATUS_ID = "stat_LbsuNBimzj2Fa3RCBpTz6kEDeokz3O97LwUorSpDhVp"  # Opp-only "🪦 Closed Lost"

# Duplicated from sync_opp_lead_status.py -- keep in sync if either changes.
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

# Inverted: Lead status_id -> Opportunity status_id. Safe to invert directly
# because STATUS_MAP is a verified 1:1 mapping (see selftest in the sibling
# script). Lead-only statuses (New, Contacted, Engaged, Unresponsive,
# Disqualified, Off Ramp) are absent here on purpose -- there's no
# Opportunity equivalent to set.
LEAD_STATUS_TO_OPP_STATUS = {v: k for k, v in STATUS_MAP.items()}


def compute_target_status(lead_status_id):
    """Pure function: what should a Closed-Lost opportunity's status become,
    given its lead's current status? Returns None if there's no mapping
    (caller should flag for manual review). Covered by --selftest."""
    return LEAD_STATUS_TO_OPP_STATUS.get(lead_status_id)


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
        opportunities embedded (each with at least id, status_id)."""
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

    def set_opportunity_status(self, opp_id, status_id):
        return self._request(
            "PUT", f"/opportunity/{opp_id}/", json={"status_id": status_id}
        )


def run(dry_run, limit):
    api_key = os.environ.get("CLOSE_API_KEY")
    if not api_key:
        sys.exit("CLOSE_API_KEY environment variable is required")

    client = CloseClient(api_key)

    print(f"Fetching leads (with opportunities embedded){f', limit {limit}' if limit else ''}...")
    leads_scanned = 0
    closed_lost_found = 0
    updated = unmappable_skipped = failed = 0

    for lead in client.paginate_leads_with_opportunities(limit=limit):
        leads_scanned += 1
        opps = lead.get("opportunities") or []
        closed_lost_opps = [o for o in opps if o.get("status_id") == CLOSED_LOST_STATUS_ID]
        if not closed_lost_opps:
            continue

        lead_id = lead["id"]
        lead_status_id = lead["status_id"]
        target_status_id = compute_target_status(lead_status_id)

        for opp in closed_lost_opps:
            closed_lost_found += 1

            if target_status_id is None:
                unmappable_skipped += 1
                print(f"FLAG  opp {opp['id']} (lead {lead_id}, {lead.get('display_name', '')}): "
                      f"lead status {lead_status_id} has no Opportunity equivalent -- left in "
                      f"Closed Lost, needs manual review "
                      f"(https://app.close.com/lead/{lead_id}/)")
                continue

            if dry_run:
                print(f"WOULD SET opp {opp['id']} (lead {lead_id}, {lead.get('display_name', '')}): "
                      f"Closed Lost -> {target_status_id} (mirroring lead status {lead_status_id})")
                updated += 1
                continue

            try:
                result = client.set_opportunity_status(opp["id"], target_status_id)
            except requests.HTTPError as e:
                failed += 1
                print(f"ERROR updating opp {opp['id']}: {e}")
                continue

            if result.get("status_id") != target_status_id:
                failed += 1
                print(f"ERROR opp {opp['id']}: write did not take (verify failed)")
                continue

            updated += 1
            print(f"SET   opp {opp['id']} (lead {lead_id}, {lead.get('display_name', '')}) "
                  f"-> {target_status_id}")

    print("\n--- Summary ---")
    print(f"Leads scanned:                    {leads_scanned}")
    print(f"Opportunities in Closed Lost:     {closed_lost_found}")
    print(f"Repaired:                          {updated}")
    print(f"Unmappable (flagged, left as-is): {unmappable_skipped}")
    print(f"Failed:                            {failed}")
    if dry_run:
        print("\nDRY RUN -- no writes were made.")
    elif closed_lost_found == 0:
        print("\nNothing found in Closed Lost. Safe to consider deleting the status in Close.")
    elif unmappable_skipped == 0 and failed == 0:
        print("\nAll Closed Lost opportunities repaired. Re-run with --dry-run to confirm "
              "0 remain, then it's safe to delete the status in Close.")
    else:
        print(f"\n{unmappable_skipped + failed} opportunity(ies) still need attention before "
              f"deleting the Closed Lost status -- see FLAG/ERROR lines above.")


def selftest():
    failures = 0

    def check(name, cond):
        nonlocal failures
        status = "ok" if cond else "FAIL"
        if not cond:
            failures += 1
        print(f"[{status}] {name}")

    check("LEAD_STATUS_TO_OPP_STATUS has 17 entries", len(LEAD_STATUS_TO_OPP_STATUS) == 17)
    check("inversion round-trips cleanly",
          all(LEAD_STATUS_TO_OPP_STATUS[lead_id] == opp_id
              for opp_id, lead_id in STATUS_MAP.items()))

    # A lead sitting at the Lead-side "Lost" status maps back to Opp's "Lost"
    # (not Closed Lost -- that status has no Lead-side equivalent at all).
    check("Lead 'Lost' maps back to Opp 'Lost'",
          compute_target_status("stat_aR2jBa8YnTNZmHAnPsnlQuinBdaXpSBCkZGP3UvoBlV")
          == "stat_bBWcww9IflskaleadKuK2E4SGFF4qy3IuBucrqo7H4u")

    check("Lead 'Call Booked' maps back to Opp 'Call Booked'",
          compute_target_status("stat_lGHxEKwhbVswuchbpRo6XcMMSXz0fV4CID9qFWT8KCO")
          == "stat_S4cxvnfOWWi8jOqTPtGoq34BzuU8ah9fKTPPfO5P9Yi")

    # Lead-only statuses (no Opportunity equivalent) must return None, not guess.
    for lead_only_status in [
        "stat_EwxduBOxA2CLBUrvXAyB7ZrVXKGw7v9i5xz0f2JuIY9",  # New
        "stat_6Kx9PYnpjdAjVK4fTM4QObI8dD7yPojpaJmcSGsf3th",  # Contacted
        "stat_TAUDIZOAGoyuY9lC8sUb2ETT8d0m09A0olHxZJTOy5f",  # Engaged
        "stat_Mf5jDseVsiPfAZw1VkjOAQFT9skT4xFucnHKwWDZevV",  # Unresponsive
        "stat_p3oblSTnbsyDAw4rWqZDePGYMOlKBgV2FjbqIMDrfvF",  # Disqualified
        "stat_LP7GD0DeMzhsyVzRjjIjSwCK89OiXu1auVSOPxYy17Q",  # Off Ramp
    ]:
        check(f"lead-only status {lead_only_status} has no mapping (flagged, not guessed)",
              compute_target_status(lead_only_status) is None)

    check("unknown status id returns None, not a KeyError", compute_target_status("stat_bogus") is None)

    print(f"\n{'PASSED' if failures == 0 else f'{failures} FAILURE(S)'}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=None,
                         help="only scan first N leads (quick test)")
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args()

    if args.selftest:
        selftest()

    dry_run = args.dry_run or os.environ.get("DRY_RUN", "0") == "1"
    run(dry_run=dry_run, limit=args.limit)
