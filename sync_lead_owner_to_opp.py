#!/usr/bin/env python3
"""
sync_lead_owner_to_opp.py

Keeps each active (open) Opportunity's native owner (`user_id`) in sync
with its lead's "Lead Owner" custom field. Lead Owner is the source of
truth.

Rule set:
  - Only ACTIVE (open) opportunities are checked. Won/Lost opportunities
    keep whichever rep actually closed or lost them -- matches the
    existing Lane 2 / Lost Deals reassignment scripts.
  - Leads with no Lead Owner set are skipped (nothing to sync against).
  - Leads with the "Reassignment Override" custom field set to "Yes" are
    skipped entirely -- this is the same escape hatch your other
    reassignment automations (Lane 2, Lost Deals) already respect.
  - Re-runs are idempotent: an opportunity whose owner already matches
    Lead Owner is skipped.

Usage:
    python sync_lead_owner_to_opp.py              # live
    python sync_lead_owner_to_opp.py --dry-run     # report only, zero writes
    python sync_lead_owner_to_opp.py --limit 200   # only scan first N opportunities
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

    def paginate_active_opportunities(self, limit=None):
        skip = 0
        page_size = 100
        fetched = 0
        while True:
            params = {
                "_skip": skip,
                "_limit": page_size,
                "_fields": "id,lead_id,status_id,status_type,user_id",
                "status_type": "active",
            }
            data = self._request("GET", "/opportunity/", params=params)
            for opp in data.get("data", []):
                # Belt-and-suspenders: filter client-side too, in case the
                # status_type query param isn't honored by this endpoint.
                if opp.get("status_type") == "active":
                    yield opp
                    fetched += 1
                    if limit and fetched >= limit:
                        return
            if not data.get("has_more"):
                return
            skip += page_size

    def get_lead(self, lead_id):
        return self._request(
            "GET",
            f"/lead/{lead_id}/",
            params={
                "_fields": f"id,display_name,custom.{LEAD_OWNER_FIELD_ID},"
                           f"custom.{REASSIGNMENT_OVERRIDE_FIELD_ID}"
            },
        )

    def set_opportunity_owner(self, opp_id, user_id):
        return self._request(
            "PUT", f"/opportunity/{opp_id}/", json={"user_id": user_id}
        )


def run(dry_run, limit):
    api_key = os.environ.get("CLOSE_API_KEY")
    if not api_key:
        sys.exit("CLOSE_API_KEY environment variable is required")

    client = CloseClient(api_key)

    print(f"Fetching active opportunities{f' (limit {limit})' if limit else ''}...")
    lead_cache = {}
    updated = already_correct = no_owner_set = override_skipped = failed = 0
    scanned = 0

    for opp in client.paginate_active_opportunities(limit=limit):
        scanned += 1
        lead_id = opp["lead_id"]

        if lead_id not in lead_cache:
            try:
                lead_cache[lead_id] = client.get_lead(lead_id)
            except requests.HTTPError as e:
                failed += 1
                print(f"ERROR fetching lead {lead_id}: {e}")
                continue
        lead = lead_cache[lead_id]

        override = read_custom(lead, REASSIGNMENT_OVERRIDE_FIELD_ID, "Reassignment Override")
        if override == "Yes":
            override_skipped += 1
            continue

        lead_owner_user_id = read_custom(lead, LEAD_OWNER_FIELD_ID, "Lead Owner")
        if not lead_owner_user_id:
            no_owner_set += 1
            continue

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
    print(f"Active opportunities scanned: {scanned}")
    print(f"Updated:                      {updated}")
    print(f"Already correct:              {already_correct}")
    print(f"No Lead Owner set (skipped):  {no_owner_set}")
    print(f"Reassignment Override (skip): {override_skipped}")
    print(f"Failed:                        {failed}")
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

    print(f"\n{'PASSED' if failures == 0 else f'{failures} FAILURE(S)'}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=None,
                         help="only scan first N opportunities (quick test)")
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args()

    if args.selftest:
        selftest()

    dry_run = args.dry_run or os.environ.get("DRY_RUN", "0") == "1"
    run(dry_run=dry_run, limit=args.limit)
