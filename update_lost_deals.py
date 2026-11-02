"""
update_lost_deals.py

Reassigns Lost leads to their intended reviewer the morning after the first
sales call, based on Lost Reason:

  - "DIY-..." or "Price-..." → Ryan Jones
  - Everything else          → Jason Aaron

Match criteria:
  - First Sales Call Booked Date falls in the lookback window (Pacific time)
  - Lead Status = 💔 Lost

Lookback window:
  - Monday: Friday + Saturday + Sunday (covers the weekend)
  - Tue-Fri: yesterday only
  - Sat/Sun: yesterday only (the scheduler skips weekends; manual triggers
    still behave sensibly)

For each matched lead, sets:
  - Lead Owner (custom field) → the routed assignee
  - Lane 2 Handraiser → "Prior Day Lost Deals"
  - Creates a Close task for the routed assignee, due today

Idempotent: skips leads whose Lead Owner is already the routed assignee.

Environment variables:
  CLOSE_API_KEY   (required)
  DRY_RUN         (optional, "true" to log without writing)
  SKIP_TASKS      (optional, "true" to update lead fields only, no tasks)
"""

import json
import os
import sys
import requests
from workflow_holds import guard_request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

CLOSE_API_KEY = os.environ["CLOSE_API_KEY"]
DRY_RUN    = os.environ.get("DRY_RUN", "false").lower() == "true"
SKIP_TASKS = os.environ.get("SKIP_TASKS", "false").lower() == "true"

BASE = "https://api.close.com/api/v1"
AUTH = (CLOSE_API_KEY, "")

# --- Routing ---
DEFAULT_ASSIGNEE = {
    "user_id": "user_MrBLkl5wCqTm7QxHxPo2ydNV5KxMllg6YZDVc12Aqzj",
    "name":    "Jason Aaron",
}
RYAN_ASSIGNEE = {
    "user_id": "user_3nrtuEmgPYd5VA15NvrxgQxDVNWbhrNSzitEKGwi8s6",
    "name":    "Ryan Jones",
}
# Lost Reason values that should route to Ryan. EXACT string match, case-sensitive.
# If Close changes these labels (dash spacing, quote style, wording), update here.
RYAN_LOST_REASONS = {
    'DIY- "I can do this on my own"',
    'Price- "Thats more than I can afford to pay"',
}

# --- Field IDs ---
FIRST_SALES_CALL_FIELD  = "cf_LFdYEQ6bsgp49YjZzefypDmdVx8iwuakWDSLPLpVrBq"
LEAD_OWNER_FIELD        = "cf_gOfS9pFwext58oberEegLyix8hZzeHrxhCZOVh3P3rd"
LANE_2_HANDRAISER_FIELD = "cf_Q1hRv8It46xsAEmpv4PRKdI1y0sPJnrnQrgRbIlF8uL"
LOST_REASON_FIELD       = "cf_R4i05fLNOQP8yveAs4ofTMMYGAQnkLLklunP4lov2Bt"

LOST_STATUS_LABEL       = "💔 Lost"
HANDRAISER_VALUE        = "Prior Day Lost Deals"

# Close returns custom fields under `custom`, keyed by display name.
# If any of these field names get renamed in Close, this dict needs updating.
FIELD_DISPLAY_NAMES = {
    FIRST_SALES_CALL_FIELD:  "First Sales Call Booked Date",
    LEAD_OWNER_FIELD:        "Lead Owner",
    LANE_2_HANDRAISER_FIELD: "Lane 2 Handraiser",
    LOST_REASON_FIELD:       "Lost Reason (Opp)",
}

PACIFIC = ZoneInfo("America/Los_Angeles")


def get_lookback_dates():
    """Monday → Fri/Sat/Sun. Every other day → yesterday only."""
    today = datetime.now(PACIFIC).date()
    weekday = today.weekday()  # Mon=0 ... Sun=6
    offsets = (3, 2, 1) if weekday == 0 else (1,)
    return [(today - timedelta(days=n)).strftime("%Y-%m-%d") for n in offsets]


def get_lost_status_id():
    r = requests.get(f"{BASE}/status/lead/", auth=AUTH)
    r.raise_for_status()
    for s in r.json()["data"]:
        if s["label"] == LOST_STATUS_LABEL:
            return s["id"]
    sys.exit(f"Could not find lead status: {LOST_STATUS_LABEL}")


def get_custom_field(lead, field_id):
    """Look up a custom field's value. Primary path: lead.custom[<display name>].
    Falls back to cf_xxx-keyed paths in case the response shape ever changes."""
    custom = lead.get("custom") or {}
    name = FIELD_DISPLAY_NAMES.get(field_id)
    if name and name in custom:
        return custom[name]
    if field_id in lead:
        return lead[field_id]
    if field_id in custom:
        return custom[field_id]
    return None


def route_assignee(lead):
    """Pick the assignee based on Lost Reason. Ryan gets DIY/Price; Jason gets
    everything else (including null/blank Lost Reason)."""
    lost_reason = get_custom_field(lead, LOST_REASON_FIELD)
    if lost_reason in RYAN_LOST_REASONS:
        return RYAN_ASSIGNEE
    return DEFAULT_ASSIGNEE


def format_call_date(iso_str):
    """Convert '2026-05-28' → 'Thu May 28'. Falls back gracefully."""
    try:
        d = datetime.strptime(iso_str, "%Y-%m-%d").date()
        return d.strftime("%a %b %d")
    except (ValueError, TypeError):
        return iso_str or "recently"


def date_clause(date_str):
    return {
        "type": "field_condition",
        "field": {
            "type": "custom_field",
            "custom_field_id": FIRST_SALES_CALL_FIELD,
        },
        "condition": {"type": "term", "values": [date_str]},
    }


def build_query(dates, lost_status_id):
    """Structured Close search query — verified via diagnose_query.py."""
    if len(dates) == 1:
        date_filter = date_clause(dates[0])
    else:
        date_filter = {
            "type": "or",
            "queries": [date_clause(d) for d in dates],
        }

    status_filter = {
        "type": "field_condition",
        "field": {
            "type": "regular_field",
            "object_type": "lead",
            "field_name": "status_id",
        },
        "condition": {"type": "term", "values": [lost_status_id]},
    }

    return {"type": "and", "queries": [date_filter, status_filter]}


def search_lead_ids(query):
    """Search returns lead IDs only — we fetch each in full separately."""
    ids, cursor = [], None
    while True:
        payload = {
            "query": query,
            "_fields": {"lead": ["id"]},
            "results_limit": 100,
        }
        if cursor:
            payload["cursor"] = cursor
        r = requests.post(f"{BASE}/data/search/", json=payload, auth=AUTH)
        r.raise_for_status()
        data = r.json()
        ids.extend(lead["id"] for lead in data.get("data", []))
        cursor = data.get("cursor")
        if not cursor:
            break
    return ids


def get_lead(lead_id):
    """Fetch a single lead with all fields, including custom."""
    r = requests.get(f"{BASE}/lead/{lead_id}/", auth=AUTH)
    r.raise_for_status()
    return r.json()


def get_current_owner_id(lead):
    """User-type custom fields can return ID string or {id, name}."""
    val = get_custom_field(lead, LEAD_OWNER_FIELD)
    if isinstance(val, dict):
        return val.get("id")
    return val


def update_lead(lead_id, target_user_id, *, expected_owner):
    """PUT custom fields with `custom.cf_xxx` key prefix. Verify the response
    reflects the new Lead Owner; otherwise raise."""
    if DRY_RUN:
        return
    payload = {
        f"custom.{LEAD_OWNER_FIELD}":        target_user_id,
        f"custom.{LANE_2_HANDRAISER_FIELD}": HANDRAISER_VALUE,
    }
    r = guard_request(requests.request)("PUT", f"{BASE}/lead/{lead_id}/", json=payload, auth=AUTH, expected_owner=expected_owner)
    r.raise_for_status()
    updated = r.json()

    # Verify the owner actually changed before we move on
    new_owner = get_current_owner_id(updated)
    if new_owner != target_user_id:
        raise RuntimeError(
            f"Lead Owner update for {lead_id} did NOT take. "
            f"Expected {target_user_id}, got {new_owner!r}. "
            f"Payload sent: {payload}"
        )


def create_task(lead_id, lead_name, sales_call_date_iso, assignee_user_id):
    if DRY_RUN or SKIP_TASKS:
        return
    today = datetime.now(PACIFIC).strftime("%Y-%m-%d")
    pretty_date = format_call_date(sales_call_date_iso)
    payload = {
        "_type": "lead",
        "lead_id": lead_id,
        "assigned_to": assignee_user_id,
        "date": today,
        "text": (
            f"Prior Day Lost Deal — {lead_name}. "
            f"First sales call was {pretty_date}. "
            f"Lead reassigned to you for review/outreach."
        ),
    }
    r = guard_request(requests.request)("POST", f"{BASE}/task/", json=payload, auth=AUTH)
    r.raise_for_status()


def main():
    if DRY_RUN:
        print("=" * 60)
        print("  DRY RUN MODE — no writes will be made")
        print("=" * 60)
    elif SKIP_TASKS:
        print("=" * 60)
        print("  SKIP_TASKS MODE — updating lead fields only, no tasks")
        print("=" * 60)

    dates = get_lookback_dates()
    today_name = datetime.now(PACIFIC).strftime("%A")
    print(f"Today is {today_name}. Lookback window: {dates}")

    lost_status_id = get_lost_status_id()
    query = build_query(dates, lost_status_id)

    if DRY_RUN:
        print("Query JSON:")
        print(json.dumps(query, indent=2))
        print()

    lead_ids = search_lead_ids(query)
    print(f"Found {len(lead_ids)} matching leads\n")

    processed = skipped = 0
    routed_counts = {"Jason Aaron": 0, "Ryan Jones": 0}

    for lead_id in lead_ids:
        lead = get_lead(lead_id)

        name = lead.get("display_name", "(no name)")
        sales_call_iso = get_custom_field(lead, FIRST_SALES_CALL_FIELD)
        pretty_date = format_call_date(sales_call_iso)
        lost_reason = get_custom_field(lead, LOST_REASON_FIELD)
        current_owner = get_current_owner_id(lead)

        target = route_assignee(lead)
        target_user_id = target["user_id"]
        target_name = target["name"]

        if current_owner == target_user_id:
            print(f"  SKIP   {name} — Lead Owner is already {target_name}")
            skipped += 1
            continue

        if DRY_RUN:
            print(f"  WOULD  {name} → {target_name}")
            print(f"           current owner: {current_owner or 'none'}")
            print(f"           lost reason: {lost_reason!r}")
            print(f"           → Lane 2 Handraiser: {HANDRAISER_VALUE}")
            print(f"           → task: 'First sales call was {pretty_date}'")
        else:
            update_lead(lead_id, target_user_id, expected_owner=current_owner)
            create_task(lead_id, name, sales_call_iso, target_user_id)
            suffix = " (task skipped)" if SKIP_TASKS else ""
            print(f"  DONE   {name} → {target_name}{suffix}")

        processed += 1
        routed_counts[target_name] = routed_counts.get(target_name, 0) + 1

    verb = "Would process" if DRY_RUN else "Processed"
    print(f"\n{verb}: {processed}, Skipped: {skipped}")
    print(f"Routing breakdown: {routed_counts}")


if __name__ == "__main__":
    main()
