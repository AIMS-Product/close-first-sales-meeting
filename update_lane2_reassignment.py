"""
update_lane2_reassignment.py

Daily (weekday) round-robin reassignment of two Lane 2 lead buckets in Close CRM.

Buckets (driven by Close Smart Views, owner-filtered to Lane 1 reps):
  - Bucket 1  "L2 Handoff: 14-days No Comms"  -> Lane 2 Handraiser = "No Activity / Past 14 Days"
  - Bucket 2  "L2 Handoff: 30 Days since Booking" -> RELEASE: clears Lead Owner only
              (skips Recapture State Booked / Suppressed). The Lane 2 reconciler
              and assign_lane2_leads.py handle everything after that.

For each lead pulled from a round-robin view (Bucket 1), the script:
  1. Assigns the lead to the next Lane 2 rep in a round-robin rotation
     (each bucket keeps its OWN rotation pointer, persisted in lane2_state_cache.json).
  2. Sets the Lane 2 Handraiser custom field for that bucket.
  3. Reassigns every ACTIVE opportunity on the lead to the same rep (opp.user_id).
  4. Creates a HIGH-PRIORITY task for the rep, due today, with the bucket's text.

Why no churn / idempotency cache is needed:
  Both Smart Views are filtered to leads owned by Lane 1 reps. The moment a lead is
  reassigned to a Lane 2 rep it drops off the view, so it can't be picked up again.
  That makes re-runs on the same day safe (a second run finds the view already cleared)
  and means failed leads are simply retried on the next run.

Bucket precedence:
  Bucket 2 is processed FIRST. Any lead that somehow appears in both views is handled
  by Bucket 2 and skipped in Bucket 1. (By design the views shouldn't overlap.)

Modes (CLI flags or env vars set by the GitHub workflow):
  --dry-run   / DRY_RUN=true    Read + report only. No writes, no tasks. Shows the
                                exact round-robin distribution. Does NOT persist pointers.
  --skip-tasks/ SKIP_TASKS=true Do the field + opportunity updates, but create no tasks
                                (useful for backfilling without alerting reps).
"""

import os
import copy
import json
import time
import argparse
import requests
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CLOSE_API_KEY = os.environ["CLOSE_API_KEY"]
BASE = "https://api.close.com/api/v1"
AUTH = (CLOSE_API_KEY, "")
PACIFIC = ZoneInfo("America/Los_Angeles")

STATE_FILE = "lane2_state_cache.json"

# Exported "Copy Filters" JSON for each Smart View, keyed by bucket. These are
# the real /data/search/-format queries (Close's saved-search s_query is an
# internal dialect that does NOT replay reliably, so we use the exported form).
# To refresh after editing a view in Close: open the view, ⋯ menu -> Copy
# Filters, and paste the result under the matching bucket key in this file.
VIEW_FILTERS_FILE = "lane2_view_filters.json"

# Lane 2 reps, in round-robin order.
REPS = [
    ("Lyle Hubbard",    "user_Bov31jjnHhENBy8uWNTTL8KKax8VX7o6DugLzBYOHBG"),
    ("Kelley Schrader", "user_WquWudQN7dghZsAPiNY80eJUmg1EadQg2UCQdvgbif7"),
]
REP_IDS = {rid for _, rid in REPS}

# Custom field API IDs.
LEAD_OWNER_FIELD       = "cf_gOfS9pFwext58oberEegLyix8hZzeHrxhCZOVh3P3rd"
LANE2_HANDRAISER_FIELD = "cf_Q1hRv8It46xsAEmpv4PRKdI1y0sPJnrnQrgRbIlF8uL"
LEAD_OWNER_DISPLAY     = "Lead Owner"  # how Close keys it on reads

# Bucket definitions. Processed in PROCESS_ORDER (Bucket 2 wins overlaps).
BUCKETS = {
    "bucket1": {
        # TEMPORARILY DISABLED — flip back to True to re-enable. The query and
        # all other settings are preserved.
        "enabled":       False,
        "label":         "14-Day No Activity",
        "smart_view_id": "save_usGcGnOy1f5wIxt9jKGkACrfuQXgsKRwbzZSWhV2T8q",
        "handraiser":    "No Activity / Past 14 Days",
        "task_text":     "New Lead Assigned: No Activity within Past 14 days- Please Review",
        "index_key":     "bucket1_index",
        # /data/search/ can't filter on last_communication_date, so we strip that
        # condition from the query and apply it in Python: keep leads whose last
        # communication is older than this many days (or who have none).
        "no_comms_days": 14,
    },
    "bucket2": {
        # RE-ENABLED 2026-09-24 as a RELEASE bucket. It no longer round-robins,
        # writes Handraiser, moves opps or creates tasks. It only clears Lead
        # Owner. From there the Lane 2 system takes over:
        #   lane2_state.py            -> Owner Team = None (Recapture State is
        #                                already owner-independent)
        #   assign_lane2_leads.py     -> deals it from the unclaimed pool
        #   sync_lead_owner_to_opp.py -> moves the opp once the new owner lands
        # handraiser / task_text / index_key below are unused in release mode.
        "enabled":       True,
        "action":        "release",
        "label":         "30-Day Aged",
        "smart_view_id": "save_vUj7qzI7VqAcOj0kiYJVoSPGtTQVRXB9nqFNjPfMxXU",
        "handraiser":    "30 Day Aged Deals",
        "task_text":     "New Lead Assigned: 30 Day Aged Deal - Please Review",
        "index_key":     "bucket2_index",
    },
}
PROCESS_ORDER = ["bucket2", "bucket1"]  # Bucket 2 takes precedence on overlap

# Release mode leaves these Recapture States alone. The assigner never deals
# them, so clearing the owner would strand the lead with nobody on it:
#   Booked     - a closer is actively working a live opp (Contract Sent /
#                Follow Up / Reschedule + activity in 30 days). The 30-day view
#                requires activity in the last 14 days, so every Follow Up /
#                Reschedule lead in it lands here.
#   Suppressed - DNC / Won / DQ. Nothing to hand off.
RECAPTURE_STATE_FIELD = "cf_hKcyx4tQSMvHd7llfLX363bx3LGXMxVEmFHEtjpR5C2"
RELEASE_SKIP_STATES   = {"Booked", "Suppressed"}


# ---------------------------------------------------------------------------
# State cache (round-robin pointers)
# ---------------------------------------------------------------------------

def load_state():
    try:
        with open(STATE_FILE) as f:
            s = json.load(f)
    except FileNotFoundError:
        s = {}
    s.setdefault("bucket1_index", 0)
    s.setdefault("bucket2_index", 0)
    return s


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ---------------------------------------------------------------------------
# Close API helpers
# ---------------------------------------------------------------------------

def close_request(method, url, max_retries=6, **kwargs):
    """
    Wrapper around requests that retries on HTTP 429 (rate limit) with backoff.

    Close returns 429 with a Retry-After header (and/or a rate_reset value in the
    JSON body). We honor it, falling back to exponential backoff. This matters
    both for the diagnostic (which bursts several searches) and for production
    runs that issue many PUT/POST calls while reassigning leads.
    """
    kwargs.setdefault("auth", AUTH)
    for attempt in range(max_retries + 1):
        r = requests.request(method, url, **kwargs)
        if r.status_code != 429:
            r.raise_for_status()
            return r
        # Rate limited — figure out how long to wait.
        wait = None
        ra = r.headers.get("Retry-After")
        if ra:
            try:
                wait = float(ra)
            except ValueError:
                wait = None
        if wait is None:
            try:
                wait = float(r.json().get("error", {}).get("rate_reset", 0)) or None
            except Exception:
                wait = None
        if wait is None:
            wait = min(2 ** attempt, 30)   # exponential backoff, capped
        if attempt < max_retries:
            print(f"  [rate-limit] 429 received; waiting {wait:.1f}s (attempt {attempt + 1})")
            time.sleep(wait + 0.25)
        else:
            r.raise_for_status()
    raise RuntimeError("close_request: exhausted retries")


def load_view_filters():
    """Load the exported 'Copy Filters' JSON for both buckets."""
    with open(VIEW_FILTERS_FILE) as f:
        return json.load(f)


def extract_query_node(blob):
    """
    Return the /data/search/ query node from a 'Copy Filters' export.

    Copy Filters wraps the query as {"limit": ..., "query": <node>,
    "results_limit": ..., "sort": [...]}. Only <node> is sent to /data/search/;
    the limit/results_limit/sort keys are display settings and are ignored here.
    """
    if isinstance(blob, dict) and "query" in blob:
        return blob["query"]
    return blob   # already a bare node


def search_lead_ids(query_node, debug=False):
    """Run a /data/search/ query node and return all matching lead IDs (paginated)."""
    if debug and os.environ.get("DUMP_QUERY", "").lower() == "true":
        # Verbose: dump the full query JSON. Off by default — it's long and tends
        # to get truncated/garbled when copied out of a terminal. Set
        # DUMP_QUERY=true only if you specifically need to inspect the raw query.
        print(f"  [debug] /data/search/ query node:\n{json.dumps(query_node, indent=2)}")
    lead_ids, cursor = [], None
    while True:
        body = {"query": query_node, "_fields": {"lead": ["id"]}, "_limit": 200}
        if cursor:
            body["cursor"] = cursor
        j = close_request("POST", f"{BASE}/data/search/", json=body).json()
        lead_ids += [row["id"] for row in j.get("data", [])]
        cursor = j.get("cursor")
        if not cursor:
            return lead_ids


def get_lead(lead_id):
    return close_request("GET", f"{BASE}/lead/{lead_id}/").json()


def read_lead_owner(lead):
    """Close returns custom fields keyed several ways depending on endpoint/version."""
    for key in (f"custom.{LEAD_OWNER_FIELD}",):
        if key in lead:
            return lead[key]
    cust = lead.get("custom", {}) or {}
    return cust.get(LEAD_OWNER_FIELD) or cust.get(LEAD_OWNER_DISPLAY)


# --- Python-side "no communication in N days" filter -----------------------
# /data/search/ can't filter on the computed last_communication_date field, so
# for that one condition we strip it from the query and evaluate it here.

COMM_ACTIVITY_TYPES = {"Email", "Call", "SMS", "WhatsAppMessage"}


def _group_references_field(group, field_name):
    """True if a condition group references the given regular-field name anywhere."""
    found = [False]

    def walk(n):
        if isinstance(n, dict):
            if n.get("field", {}).get("field_name") == field_name:
                found[0] = True
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)

    walk(group)
    return found[0]


def strip_condition_by_field(query_node, field_name):
    """Deep-copy the query with any condition referencing field_name removed."""
    node = copy.deepcopy(query_node)
    for q in node.get("queries", []):
        if q.get("type") in ("and", "or") and isinstance(q.get("queries"), list):
            q["queries"] = [c for c in q["queries"]
                            if not _group_references_field(c, field_name)]
    return node


def search_leads_with_fields(query_node, fields):
    """Like search_lead_ids but returns full lead dicts with the requested fields."""
    rows, cursor = [], None
    while True:
        body = {"query": query_node, "_fields": {"lead": fields}, "_limit": 200}
        if cursor:
            body["cursor"] = cursor
        j = close_request("POST", f"{BASE}/data/search/", json=body).json()
        rows += j.get("data", [])
        cursor = j.get("cursor")
        if not cursor:
            return rows


def _parse_dt(value):
    """Parse a Close datetime/date string to an aware UTC datetime, or None."""
    if not value:
        return None
    s = str(value).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        try:
            dt = datetime.fromisoformat(s[:10])  # date-only fallback
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def last_communication_dt_via_activities(lead_id):
    """Most recent communication activity datetime for a lead (UTC), or None."""
    j = close_request("GET", f"{BASE}/activity/",
                      params={"lead_id": lead_id, "_limit": 100}).json()
    dates = []
    for a in j.get("data", []):
        if a.get("_type") in COMM_ACTIVITY_TYPES:
            dt = _parse_dt(a.get("date") or a.get("date_created"))
            if dt:
                dates.append(dt)
    return max(dates) if dates else None


def resolve_no_comms_bucket(query_node, no_comms_days, debug=False):
    """
    Resolve the 14-Day bucket: run the candidate query without the
    last_communication_date condition, then keep only leads whose last
    communication is older than `no_comms_days` (or who have none).

    Tries to read last_communication_date straight from the search results; if
    Close doesn't return that field, falls back to scanning each candidate's
    communication activities.
    """
    candidate_query = strip_condition_by_field(query_node, "last_communication_date")
    rows = search_leads_with_fields(candidate_query, ["id", "last_communication_date"])

    # Match Close's filter exactly: it anchors "within the last N days" to the
    # START of the day N days ago, in the org's (Pacific) timezone — not a rolling
    # now-minus-N-hours. So a lead counts as "no comms" if its last communication
    # is before midnight Pacific of (today - N days).
    now_pac = datetime.now(PACIFIC)
    cutoff_date = now_pac.date() - timedelta(days=no_comms_days)
    cutoff = datetime(cutoff_date.year, cutoff_date.month, cutoff_date.day, tzinfo=PACIFIC)

    field_present = any("last_communication_date" in r for r in rows)
    if debug:
        print(f"  [no-comms] {len(rows)} candidate(s); "
              f"last_communication_date returned by search: {field_present}; "
              f"cutoff (Pacific start-of-day) = {cutoff.isoformat()}")
        if not field_present and rows:
            print(f"  [no-comms] field not in search results — using per-lead "
                  f"activity lookup for {len(rows)} candidates")

    kept = []
    for r in rows:
        lead_id = r["id"]
        if "last_communication_date" in r:
            last_dt = _parse_dt(r.get("last_communication_date"))
        else:
            last_dt = last_communication_dt_via_activities(lead_id)
            time.sleep(0.15)
        if last_dt is None or last_dt < cutoff:
            kept.append(lead_id)
    return kept


def get_active_opportunities(lead_id):
    j = close_request(
        "GET", f"{BASE}/opportunity/",
        params={"lead_id": lead_id, "_fields": "id,user_id,status_type"},
    ).json()
    return [o for o in j.get("data", []) if o.get("status_type") == "active"]


def reassign_lead(lead_id, rep_id, handraiser_value):
    """PUT the Lead Owner + Lane 2 Handraiser, then verify the owner actually took."""
    payload = {
        f"custom.{LEAD_OWNER_FIELD}":       rep_id,
        f"custom.{LANE2_HANDRAISER_FIELD}": handraiser_value,
    }
    updated = close_request("PUT", f"{BASE}/lead/{lead_id}/", json=payload).json()
    new_owner = read_lead_owner(updated)
    if new_owner != rep_id:
        raise RuntimeError(
            f"Lead {lead_id}: owner update did not take "
            f"(got {new_owner!r}, expected {rep_id!r}). Aborting before task creation."
        )


def read_recapture_state(lead):
    key = f"custom.{RECAPTURE_STATE_FIELD}"
    if key in lead:
        return lead[key]
    cust = lead.get("custom", {}) or {}
    return cust.get(RECAPTURE_STATE_FIELD) or cust.get("Recapture State")


def release_lead(lead_id):
    """Clear Lead Owner so the Lane 2 assigner can deal the lead. Verifies it took."""
    updated = close_request("PUT", f"{BASE}/lead/{lead_id}/",
                            json={f"custom.{LEAD_OWNER_FIELD}": None}).json()
    if read_lead_owner(updated):
        raise RuntimeError(f"Lead {lead_id}: owner clear did not take "
                           f"(still {read_lead_owner(updated)!r}).")


def reassign_opportunity(opp_id, rep_id):
    close_request("PUT", f"{BASE}/opportunity/{opp_id}/", json={"user_id": rep_id})


def create_task(lead_id, rep_id, text):
    today = datetime.now(PACIFIC).strftime("%Y-%m-%d")
    payload = {
        "_type":       "lead",
        "lead_id":     lead_id,
        "assigned_to": rep_id,
        "date":        today,       # due date
        "text":        text,
        "priority":    "high",      # the "high priority" checkbox
    }
    close_request("POST", f"{BASE}/task/", json=payload)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-tasks", action="store_true")
    args = parser.parse_args()

    dry_run    = args.dry_run    or os.environ.get("DRY_RUN", "").lower() == "true"
    skip_tasks = args.skip_tasks or os.environ.get("SKIP_TASKS", "").lower() == "true"

    print("=" * 64)
    if dry_run:
        print("  DRY RUN MODE — no writes will be made")
    elif skip_tasks:
        print("  SKIP-TASKS MODE — fields/opps updated, no tasks created")
    else:
        print("  LIVE RUN")
    print("=" * 64)

    state = load_state()
    view_filters = load_view_filters()
    handled = set()                 # lead IDs already assigned this run (overlap guard)
    totals = {}

    for key in PROCESS_ORDER:
        b = BUCKETS[key]
        idx_key = b["index_key"]
        print(f"\n--- {b['label']}  (view {b['smart_view_id']}) ---")

        if not b.get("enabled", True):
            print("  SKIP bucket: disabled (enabled=False)")
            totals[key] = 0
            continue

        blob = view_filters.get(key)
        if not blob or "query" not in (blob if isinstance(blob, dict) else {}):
            print(f"  SKIP bucket: no filter defined for '{key}' in {VIEW_FILTERS_FILE}")
            totals[key] = 0
            continue

        node = extract_query_node(blob)
        if b.get("no_comms_days"):
            lead_ids = resolve_no_comms_bucket(node, b["no_comms_days"], debug=dry_run)
        else:
            lead_ids = search_lead_ids(node, debug=dry_run)
        print(f"View returned {len(lead_ids)} lead(s)")

        assigned = skipped_overlap = skipped_owned = 0

        for lead_id in lead_ids:
            if lead_id in handled:
                skipped_overlap += 1
                print(f"  SKIP (overlap, Bucket 2 won)  {lead_id}")
                continue

            lead = get_lead(lead_id)
            name = lead.get("display_name", lead_id)

            if b.get("action") == "release":
                state_val = read_recapture_state(lead)
                if state_val in RELEASE_SKIP_STATES:
                    skipped_owned += 1
                    print(f"  SKIP ({state_val})  {name}")
                    continue
                if dry_run:
                    print(f"  WOULD RELEASE  {name}  (Recapture State: {state_val})")
                else:
                    release_lead(lead_id)
                    print(f"  RELEASED  {name}  (Recapture State: {state_val})")
                handled.add(lead_id)
                assigned += 1
                continue

            # Safety belt: if the view ever returns a lead already on a Lane 2 rep,
            # don't reassign it and don't consume a rotation slot.
            if read_lead_owner(lead) in REP_IDS:
                skipped_owned += 1
                print(f"  SKIP (already a Lane 2 rep)  {name}")
                continue

            rep_name, rep_id = REPS[state[idx_key] % len(REPS)]
            opps = get_active_opportunities(lead_id)

            if dry_run:
                print(f"  WOULD  {name}")
                print(f"           -> Lead Owner: {rep_name}")
                print(f"           -> Lane 2 Handraiser: {b['handraiser']}")
                print(f"           -> Active opps to reassign: {len(opps)}")
                print(f"           -> Task ({'skipped' if skip_tasks else 'high priority'}): "
                      f"{b['task_text']!r}")
            else:
                reassign_lead(lead_id, rep_id, b["handraiser"])
                for opp in opps:
                    if opp.get("user_id") != rep_id:
                        reassign_opportunity(opp["id"], rep_id)
                if not skip_tasks:
                    create_task(lead_id, rep_id, b["task_text"])
                print(f"  DONE   {name} -> {rep_name} "
                      f"({len(opps)} opp(s)"
                      f"{', task created' if not skip_tasks else ', no task'})")

            state[idx_key] += 1   # advance rotation only on a real assignment
            handled.add(lead_id)
            assigned += 1

        totals[key] = assigned
        if b.get("action") == "release":
            print(f"  {b['label']}: released {assigned}, "
                  f"overlap-skipped {skipped_overlap}, "
                  f"Booked/Suppressed-skipped {skipped_owned}")
        else:
            print(f"  {b['label']}: assigned {assigned}, "
                  f"overlap-skipped {skipped_overlap}, already-owned-skipped {skipped_owned}")

    if not dry_run:
        save_state(state)
        print(f"\nState cache saved: {state}")
    else:
        print(f"\n(dry run — pointers NOT persisted; preview ending state would be {state})")

    print("\nSummary: " + ", ".join(f"{BUCKETS[k]['label']}={v}" for k, v in totals.items()))


if __name__ == "__main__":
    main()
