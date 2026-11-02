#!/usr/bin/env python3
"""
Sales Lane - Close CRM Field Updater
====================================

Sets the lead's "Sales Lane" dropdown (Lane 1 / Lane 2) based ENTIRELY on who
the lead's *Lead Owner* is.

  - "Lead Owner" is the CUSTOM field cf_gOfS... (NOT Close's built-in
    `assigned_to`, which controls opportunity/task ownership in this org).
  - Owner is a Lane 1 rep -> Sales Lane = "Lane 1"
  - Owner is a Lane 2 rep -> Sales Lane = "Lane 2"
  - Owner is anyone else (or unset) -> lead is never returned, never touched.

Design (and why it is the way it is)
------------------------------------
We query Close's Advanced Filtering API (`POST /api/v1/data/search/`) ONCE PER
OWNER, filtering on the Lead Owner custom field with a `reference` condition.
Reasons:

  * Close's data/search has a HARD 10,000-object pagination cap. Scanning every
    lead in the org would hit that wall. Per-owner result sets stay well under
    it.
  * Pagination uses the `cursor` field in the request body (NOT `_cursor`, which
    is the Events API). When the response `cursor` is null, you're done.
  * Cursors expire after 30s, so we fetch ALL pages for an owner first (no
    writes in between), then do any PATCH writes.

Idempotent: reads the current Sales Lane and only PATCHes leads whose value is
wrong/blank. No state cache needed.

CLI
---
    python update_sales_lane.py             # live run
    python update_sales_lane.py --dry-run   # report only, zero writes
    python update_sales_lane.py --limit 25  # cap leads PER OWNER (quick test)

Requires env var: CLOSE_API_KEY
"""

import argparse
import os
import sys
import time

import requests
from workflow_holds import guard_request

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CLOSE_API_KEY = os.environ.get("CLOSE_API_KEY")
BASE_URL = "https://api.close.com/api/v1"

SALES_LANE_FIELD = "cf_UD9Hm3dpLGtcUd37tX8Y9GAK1Lhc3BdtDX769ffFvyB"  # "Sales Lane" (written)
LEAD_OWNER_FIELD = "cf_gOfS9pFwext58oberEegLyix8hZzeHrxhCZOVh3P3rd"  # "Lead Owner" (filtered/read)

LANE_1_VALUE = "Lane 1"
LANE_2_VALUE = "Lane 2"

PAGE_SIZE = 200          # Close data/search page size
MAX_PAGES_PER_OWNER = 60 # safety backstop (60*200 = 12k; Close caps at 10k anyway)

# ---------------------------------------------------------------------------
# Rep rosters - EDIT THESE when reps move lanes / join / leave.
# Names are only for log readability; the user_id is what matters.
# ---------------------------------------------------------------------------

LANE_1_REPS = {
    "user_F0VeLnOQlWpkDncNW8rBl1V2QJ08fnDt6DcUjNATUJK": "Scott Seymour",
    "user_7F059xEinVentOEvkRMP77fWZyvwUiTRTUOuhD11J0e": "Robin Perkins",
    "user_wF5aATmDljO6g6AHqehRPVmfCmH5j9VszbO6Q6Pjzm4": "Eric Piccione",
    "user_1xDZSeOa8omjfxHXD80twTf8OieXfQ6tNCaYbVygtv1": "Dubem Adindu",
    "user_fYWHvOuCKDuaQxSp6lROlv2rmvZZYq1kzjGvaF7OrAL": "Jake Skinner",
    "user_Ap8we63okFA5Cw9pvr5xgccvqDlIfisKVtFKt6oBe6p": "Luis Galarza",
    "user_XEbPgLixZy4dhuLp34WogOzCIChkKEnrffDnHlxOnA7": "Danny Santolaya",
    "user_7HSxi55O8q5jO11khvrTcAGoL2nlcoa3kZ6loAY6i78": "Joe Vaughn",
    "user_1TKtkacQ7ZMKkcqnmCERikTYWwGltp5XUjEE9Hshple": "Shreya Bechra",
    "user_wHm1vcLde4RExd3vv9UOjnms5Oz8ssXg8600mQuxMPb": "Christian Hartwell",
    "user_lUjlATIIgFg8mELa0GFzZUj0lG4Cs7PwQsxbi34I6Su": "Joe Dysert",
}

LANE_2_REPS = {
    "user_MrBLkl5wCqTm7QxHxPo2ydNV5KxMllg6YZDVc12Aqzj": "Jason Aaron",
    "user_WquWudQN7dghZsAPiNY80eJUmg1EadQg2UCQdvgbif7": "Kelly Schrader",
    "user_Bov31jjnHhENBy8uWNTTL8KKax8VX7o6DugLzBYOHBG": "Lyle Hubbard",
    "user_I0cHZ04mBXXBvbFcnwmsc2KrcMsLsKxqjW8DtJ783Hr": "Elvis Ellis",
    "user_UpJb11fzX2TuFHf7fFyWpfXr84lg2Ui7i7p5CtQkIaW": "Cameron Caswell",
}

# owner_id -> (desired_lane_value, owner_name)
OWNER_TO_LANE = {}
for _uid, _name in LANE_1_REPS.items():
    OWNER_TO_LANE[_uid] = (LANE_1_VALUE, _name)
for _uid, _name in LANE_2_REPS.items():
    OWNER_TO_LANE[_uid] = (LANE_2_VALUE, _name)

# ---------------------------------------------------------------------------
# Close API helpers
# ---------------------------------------------------------------------------


def make_session():
    if not CLOSE_API_KEY:
        sys.exit("ERROR: CLOSE_API_KEY environment variable is not set.")
    s = requests.Session()
    s.auth = (CLOSE_API_KEY, "")  # Close HTTP Basic: key as username, blank password
    s.headers.update({"Content-Type": "application/json"})
    return s


def request_with_retry(session, method, url, max_retries=5, **kwargs):
    """Issue a request, backing off on 429 / 5xx. Returns the final Response."""
    if method.upper() == "PUT":
        return guard_request(session.request)(method, url, timeout=60, **kwargs)
    for attempt in range(max_retries):
        resp = session.request(method, url, timeout=60, **kwargs)
        if resp.status_code == 429 or resp.status_code >= 500:
            wait = float(resp.headers.get("Retry-After", 2 ** attempt))
            wait = min(wait, 30)
            print(f"    (rate/5xx {resp.status_code}, retrying in {wait:.0f}s)")
            time.sleep(wait)
            continue
        return resp
    return resp  # last response (caller handles non-200)


def get_custom(lead, cf_id, display_name=None):
    """
    Defensive accessor for a custom field value on a lead payload. Close keys
    custom fields differently across endpoints, so check every shape:
      nested by cf id      -> lead["custom"]["cf_xxx"]
      flattened top-level  -> lead["custom.cf_xxx"]
      nested by display    -> lead["custom"]["Sales Lane"]
    """
    flat_key = f"custom.{cf_id}"
    if flat_key in lead:
        return lead[flat_key]
    custom = lead.get("custom") or {}
    if cf_id in custom:
        return custom[cf_id]
    if display_name and display_name in custom:
        return custom[display_name]
    return None


def search_leads_for_owner(session, owner_id, owner_name, per_owner_limit=None):
    """
    Fetch ALL leads owned by `owner_id` (Lead Owner custom field) via data/search.
    Returns a list of lead dicts. Pagination is fully consumed up front so cursors
    don't expire mid-write. Hard guards prevent any runaway loop.
    """
    url = f"{BASE_URL}/data/search/"
    leads = []
    cursor = None
    seen_cursors = set()
    pages = 0

    while True:
        body = {
            "query": {
                "type": "and",
                "queries": [
                    {"type": "object_type", "object_type": "lead"},
                    {
                        "type": "field_condition",
                        "field": {"type": "custom_field", "custom_field_id": LEAD_OWNER_FIELD},
                        "condition": {
                            "type": "reference",
                            "reference_type": "user",
                            "object_ids": [owner_id],
                        },
                    },
                ],
            },
            "_fields": {"lead": ["id", "display_name", "custom"]},
            "_limit": PAGE_SIZE,
            # Stable sort so paging can't miss/duplicate rows if data shifts mid-run.
            "sort": [{
                "direction": "asc",
                "field": {"object_type": "lead", "type": "regular_field", "field_name": "date_created"},
            }],
        }
        if cursor:
            body["cursor"] = cursor

        resp = request_with_retry(session, "POST", url, json=body)
        if resp.status_code != 200:
            print(f"  ! search failed for {owner_name} ({resp.status_code}): {resp.text[:300]}")
            break

        payload = resp.json()
        leads.extend(payload.get("data", []))
        pages += 1

        if per_owner_limit and len(leads) >= per_owner_limit:
            return leads[:per_owner_limit]

        cursor = payload.get("cursor")
        if not cursor:               # null cursor => last page
            break
        if cursor in seen_cursors:   # GUARD: repeated cursor => bail, never loop
            print(f"  ! repeated cursor for {owner_name} - breaking to avoid a loop")
            break
        seen_cursors.add(cursor)
        if pages >= MAX_PAGES_PER_OWNER:  # GUARD: hard page cap
            print(f"  ! hit {MAX_PAGES_PER_OWNER}-page cap for {owner_name} "
                  f"(>{MAX_PAGES_PER_OWNER * PAGE_SIZE} leads) - may need date-range chunking")
            break
        time.sleep(0.1)

    return leads


def update_sales_lane(session, lead_id, desired_value):
    """PUT the Sales Lane value and verify the write took. Returns True on success."""
    url = f"{BASE_URL}/lead/{lead_id}/"
    resp = request_with_retry(session, "PUT", url, json={f"custom.{SALES_LANE_FIELD}": desired_value})
    if resp.status_code != 200:
        print(f"  ! FAILED {lead_id} ({resp.status_code}): {resp.text[:200]}")
        return False
    written = get_custom(resp.json(), SALES_LANE_FIELD, "Sales Lane")
    if written != desired_value:
        print(f"  ! VERIFY MISMATCH {lead_id}: wanted {desired_value!r}, got {written!r}")
        return False
    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def run(dry_run=False, per_owner_limit=None):
    session = make_session()
    stats = {"scanned": 0, "updated": 0, "unchanged": 0, "failed": 0}
    processed_ids = set()

    mode = "DRY RUN - no writes" if dry_run else "LIVE"
    print(f"Sales Lane updater starting [{mode}]")
    print(f"Owners: {len(LANE_1_REPS)} Lane 1 + {len(LANE_2_REPS)} Lane 2 = {len(OWNER_TO_LANE)} total\n")

    for owner_id, (desired, owner_name) in OWNER_TO_LANE.items():
        leads = search_leads_for_owner(session, owner_id, owner_name, per_owner_limit)
        owner_updates = 0

        for lead in leads:
            lead_id = lead.get("id")
            if lead_id in processed_ids:   # a lead has one owner, but stay safe
                continue
            processed_ids.add(lead_id)
            stats["scanned"] += 1

            current = get_custom(lead, SALES_LANE_FIELD, "Sales Lane")
            if current == desired:
                stats["unchanged"] += 1
                continue

            name = lead.get("display_name", "?")
            if dry_run:
                stats["updated"] += 1
                owner_updates += 1
            else:
                if update_sales_lane(session, lead_id, desired):
                    stats["updated"] += 1
                    owner_updates += 1
                else:
                    stats["failed"] += 1

        verb = "would set" if dry_run else "set"
        print(f"  {owner_name:<20} {desired} | {len(leads):>5} owned | {owner_updates:>4} {verb}")

    print("\n" + "=" * 52)
    print("SUMMARY")
    print("=" * 52)
    print(f"  Leads scanned (owned by the rosters) : {stats['scanned']}")
    print(f"  {'Would update' if dry_run else 'Updated':<36} : {stats['updated']}")
    print(f"  Already correct                      : {stats['unchanged']}")
    print(f"  Failed writes                        : {stats['failed']}")
    print("=" * 52)

    if stats["failed"]:
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Set Sales Lane from Lead Owner.")
    parser.add_argument("--dry-run", action="store_true", help="Report only, no writes.")
    parser.add_argument("--limit", type=int, default=None,
                        help="Cap leads processed PER OWNER (quick test).")
    args = parser.parse_args()
    run(dry_run=args.dry_run, per_owner_limit=args.limit)


if __name__ == "__main__":
    main()
