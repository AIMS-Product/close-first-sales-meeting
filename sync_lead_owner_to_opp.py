#!/usr/bin/env python3
"""
sync_lead_owner_to_opp.py

Keeps each Opportunity's native owner (`user_id`) in sync with its lead's
"Lead Owner" custom field, for every opportunity EXCEPT Closed/Won ones.
Lead Owner is the source of truth.

Rule set (updated 2026-08-07 per Stephen -- see decision note below):
  - ALL opportunities are checked and reassigned to match Lead Owner
    EXCEPT ones in a Won status (WON_OPP_STATUS_IDS). Won deals keep
    whichever rep actually closed them permanently -- that's revenue
    attribution and shouldn't move even if Lead Owner changes later.
  - Lost and active (open) opportunities ARE reassigned to match Lead
    Owner. This is a deliberate change from the original "active only"
    design: this org sometimes re-scrapes/reactivates Lost leads and
    reassigns ownership via bulk import, and expects the Lost
    opportunity's owner to follow along rather than staying stuck on
    whoever originally lost it.
  - Leads with no Lead Owner set are skipped (nothing to sync against).
  - Leads with the "Reassignment Override" custom field set to "Yes" are
    skipped entirely -- this is the same escape hatch your other
    reassignment automations (Lane 2, Lost Deals) already respect.
  - Re-runs are idempotent: an opportunity whose owner already matches
    Lead Owner is skipped.

Performance note: this fetches leads via ONE paginated scan with each
lead's opportunities and custom fields embedded (same call), instead of
listing active opportunities and then issuing a separate GET per lead.
At ~56k leads that's ~560 pages total, not tens of thousands of individual
API calls (this org has ~21.5k active opportunities alone, which the old
per-lead-GET design would have re-scanned individually every 30 minutes,
forever). Validate this assumption in your first --dry-run --limit 20:
check that each printed lead actually shows its opportunities and Lead
Owner value. If Close's API doesn't embed opportunities/custom fields by
default for your account, expand the _fields param in
paginate_leads_with_opportunities().

"Won" is determined from WON_OPP_STATUS_IDS (built from your Sales
Pipeline's status list) rather than trusting a status_type field on the
embedded opportunity object, since embeds don't always include every
top-level field. This set intentionally includes BOTH your Sales
Pipeline's official 🏆 Closed/Won status (status_type "won") AND the
legacy "OLD Deal Won (Prior to 2026 Migration)" status -- the latter is
typed "active" in Close, not "won", but its name says it represents an
already-won deal, and the same non-negotiable-attribution logic should
apply to it. If that legacy status actually means something else in
practice, pull it out of WON_OPP_STATUS_IDS.

Usage:
    python sync_lead_owner_to_opp.py              # live
    python sync_lead_owner_to_opp.py --dry-run     # report only, zero writes
    python sync_lead_owner_to_opp.py --limit 200   # only scan first N leads (quick test)
    python sync_lead_owner_to_opp.py --selftest    # pure-logic tests, no network

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

LEAD_OWNER_FIELD_ID = "cf_gOfS9pFwext58oberEegLyix8hZzeHrxhCZOVh3P3rd"  # "Lead Owner"
REASSIGNMENT_OVERRIDE_FIELD_ID = "cf_6PnYz6aaAkLzHMU3Faxz7kFurBEfadqlfGiBgfhjaVC"  # "Reassignment Override"

# Opportunity status_ids that represent a WON deal -- these are the ONLY
# statuses excluded from the owner sync. Everything else (active + Lost)
# gets reassigned to match Lead Owner. Pulled from Settings -> Statuses.
# Update if statuses change.
WON_OPP_STATUS_IDS = {
    "stat_WnFc0uhjcjV0cc3bVzdFVqDz7av6rbsOmOvHUsO6s03",  # 🏆 Closed / Won (status_type: won)
    "stat_ZA4DFlp3JeGCpqLVoQxIEGfd3WNxAqdM5RrHQrzrpm6",  # OLD Deal Won (Prior to 2026 Migration) (status_type: active, but named/treated as won)
}


def read_custom(lead, field_id, field_name):
    """Close can return custom fields keyed by API id ('custom.cf_xxx') or
    by display name under lead['custom'], depending on which endpoint /
    _fields you asked for. Check both so this doesn't silently no-op if
    Close's response shape differs from what we expect."""
    key_by_id = f"custom.{field_id}"
    if key_by_id in lead:
        return lead[key_by_id]
    custom = lead.get("custom") or {}
    if field_name in custom:
        return custom[field_name]
    return None


def owner_needs_update(opp_user_id, lead_owner_user_id):
    """Pure function: does this opportunity's owner need to change?
    Covered by --selftest."""
    if not lead_owner_user_id:
        return False
    return opp_user_id != lead_owner_user_id


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
        """Yields every lead dict with id, display_name, the two custom
        fields we need, and its opportunities embedded (each with at least
        id, status_id, user_id). One paginated scan -- no per-lead
        follow-up call."""
        skip = 0
        page_size = 100
        fetched = 0
        while True:
            params = {
                "_skip": skip,
                "_limit": page_size,
                "_fields": f"id,display_name,opportunities,"
                           f"custom.{LEAD_OWNER_FIELD_ID},"
                           f"custom.{REASSIGNMENT_OVERRIDE_FIELD_ID}",
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

    def set_opportunity_owner(self, opp_id, user_id):
        return self._request(
            "PUT", f"/opportunity/{opp_id}/", json={"user_id": user_id}
        )


def run(dry_run, limit):
    api_key = os.environ.get("CLOSE_API_KEY")
    if not api_key:
        sys.exit("CLOSE_API_KEY environment variable is required")

    client = CloseClient(api_key)

    print(f"Fetching leads (with opportunities embedded){f', limit {limit}' if limit else ''}...")
    leads_scanned = 0
    opps_scanned = 0
    updated = already_correct = no_owner_set = override_skipped = failed = 0

    for lead in client.paginate_leads_with_opportunities(limit=limit):
        leads_scanned += 1
        opps = lead.get("opportunities") or []
        syncable_opps = [o for o in opps if o.get("status_id") not in WON_OPP_STATUS_IDS]
        if not syncable_opps:
            continue

        lead_id = lead["id"]

        override = read_custom(lead, REASSIGNMENT_OVERRIDE_FIELD_ID, "Reassignment Override")
        if override == "Yes":
            override_skipped += len(syncable_opps)
            continue

        lead_owner_user_id = read_custom(lead, LEAD_OWNER_FIELD_ID, "Lead Owner")
        if not lead_owner_user_id:
            no_owner_set += len(syncable_opps)
            continue

        for opp in syncable_opps:
            opps_scanned += 1

            if not owner_needs_update(opp.get("user_id"), lead_owner_user_id):
                already_correct += 1
                continue

            if dry_run:
                print(f"WOULD SET opp {opp['id']} (lead {lead_id}, {lead.get('display_name', '')}): "
                      f"{opp.get('user_id')} -> {lead_owner_user_id}")
                updated += 1
                continue

            try:
                result = client.set_opportunity_owner(opp["id"], lead_owner_user_id)
            except requests.HTTPError as e:
                failed += 1
                print(f"ERROR updating opp {opp['id']}: {e}")
                continue

            if result.get("user_id") != lead_owner_user_id:
                failed += 1
                print(f"ERROR opp {opp['id']}: write did not take (verify failed)")
                continue

            updated += 1
            print(f"SET   opp {opp['id']} (lead {lead_id}, {lead.get('display_name', '')}) "
                  f"-> owner {lead_owner_user_id}")

    print("\n--- Summary ---")
    print(f"Leads scanned:                     {leads_scanned}")
    print(f"Non-Won opportunities checked:     {opps_scanned}")
    print(f"Updated:                           {updated}")
    print(f"Already correct:                   {already_correct}")
    print(f"No Lead Owner set (skipped):       {no_owner_set}")
    print(f"Reassignment Override (skip):      {override_skipped}")
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

    check("mismatched owner needs update", owner_needs_update("user_A", "user_B") is True)
    check("matching owner needs no update", owner_needs_update("user_A", "user_A") is False)
    check("blank lead owner never triggers update", owner_needs_update("user_A", None) is False)
    check("blank lead owner never triggers update (opp also blank)", owner_needs_update(None, None) is False)
    check("opp with no owner + lead owner set needs update", owner_needs_update(None, "user_B") is True)

    check("read_custom finds value by custom.<id> key",
          read_custom({"custom.cf_x": "user_A"}, "cf_x", "Some Field") == "user_A")
    check("read_custom finds value by display name under 'custom'",
          read_custom({"custom": {"Some Field": "user_A"}}, "cf_x", "Some Field") == "user_A")
    check("read_custom returns None when field absent",
          read_custom({}, "cf_x", "Some Field") is None)

    check("WON_OPP_STATUS_IDS has 2 entries", len(WON_OPP_STATUS_IDS) == 2)

    # Simulate the inclusion filter used in run(): everything except Won.
    sample_opps = [
        {"id": "o1", "status_id": "stat_S4cxvnfOWWi8jOqTPtGoq34BzuU8ah9fKTPPfO5P9Yi"},  # Call Booked (active)
        {"id": "o2", "status_id": "stat_bBWcww9IflskaleadKuK2E4SGFF4qy3IuBucrqo7H4u"},  # Lost
        {"id": "o3", "status_id": "stat_LbsuNBimzj2Fa3RCBpTz6kEDeokz3O97LwUorSpDhVp"},  # Closed Lost
        {"id": "o4", "status_id": "stat_WnFc0uhjcjV0cc3bVzdFVqDz7av6rbsOmOvHUsO6s03"},  # Closed/Won -- excluded
        {"id": "o5", "status_id": "stat_ZA4DFlp3JeGCpqLVoQxIEGfd3WNxAqdM5RrHQrzrpm6"},  # OLD Deal Won -- excluded
    ]
    syncable_ids = {o["id"] for o in sample_opps if o["status_id"] not in WON_OPP_STATUS_IDS}
    check("active opportunities are syncable", "o1" in syncable_ids)
    check("Lost opportunities are syncable", "o2" in syncable_ids)
    check("Closed Lost opportunities are syncable", "o3" in syncable_ids)
    check("Closed/Won opportunities are excluded", "o4" not in syncable_ids)
    check("OLD Deal Won opportunities are excluded", "o5" not in syncable_ids)

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
