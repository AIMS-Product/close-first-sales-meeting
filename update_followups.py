#!/usr/bin/env python3
"""
update_followups.py — Close CRM Follow-Up Date Field Updater

Scans all meeting activity in Close CRM and stamps the earliest three
follow-up meeting dates per lead into three custom date fields.

Follow-ups only count if they occur AFTER the lead's qualifying first
sales call (using the same classification rules as update_field.py).

If a backdated follow-up later appears that pushes an existing entry
out of the top 3, all three field values shift down to reflect the
actual earliest-3. Likewise, if a previously-counted follow-up is
removed (canceled / rescheduled / deleted), the affected fields are
cleared.

Runs every 30 minutes via GitHub Actions (offset to :15 / :45 so it
doesn't collide with update_field.py at :00 / :30).
"""

import json
import os
import re
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from workflow_holds import guard_request

# ────────────────────────────────────────────────────────────────────────────
# Configuration
# ────────────────────────────────────────────────────────────────────────────

CLOSE_API_KEY = os.environ.get("CLOSE_API_KEY")
if not CLOSE_API_KEY:
    sys.exit("ERROR: CLOSE_API_KEY environment variable not set.")

BASE_URL = "https://api.close.com/api/v1"
STATE_CACHE_FILE = "followups_state_cache.json"
PACIFIC = ZoneInfo("America/Los_Angeles")

# Lead custom field IDs — the three date-only Follow Up Call fields
# (Original date+time fields were replaced with date-only ones on 2026-05-27)
FIELD_FOLLOWUP_1 = "cf_2BzXSnYW94EQJxtedp3E6xxUaxnpZ4e0K1MNrFvNjIG"
FIELD_FOLLOWUP_2 = "cf_N9VyFcSUGaYx9VIFg08Uafm7C0GiKiDgQKOLc21M9SV"
FIELD_FOLLOWUP_3 = "cf_42swTFCof7d1J96AA2BNvgT9Y9vk7KO9q7Hs0xtCJ3R"
FOLLOWUP_FIELDS = [FIELD_FOLLOWUP_1, FIELD_FOLLOWUP_2, FIELD_FOLLOWUP_3]

# Carry-over from update_field.py
EXCLUDED_OWNERS = {"Stephen Olivas", "Ahmad Bukhari"}
SETTER_OWNERS = {"Kristin Nelson", "Spencer Reynolds"}

REQUEST_TIMEOUT = 60

# ────────────────────────────────────────────────────────────────────────────
# Title classification patterns
# ────────────────────────────────────────────────────────────────────────────

# Follow-up title indicators (case-insensitive unless noted)
FOLLOWUP_PATTERNS = [
    re.compile(r"follow[\s-]?up", re.IGNORECASE),     # follow-up / follow up / followup
    re.compile(r"fallow[\s-]?up", re.IGNORECASE),     # known misspelling
    re.compile(r"\bF/U\b", re.IGNORECASE),
    re.compile(r"\bFU\b"),                            # word-bounded, case-sensitive
]

# Hard excludes — applied to follow-up matching to skip canceled/rescheduled
FOLLOWUP_HARD_EXCLUDES = [
    re.compile(r"^\s*Canceled:?\s", re.IGNORECASE),
    re.compile(r"Rescheduled|reschedule", re.IGNORECASE),
]

# Scraper "Next Steps" patterns — these qualify as CLOSER calls.
# Most specific first (Step 2 in the classification doc).
SCRAPER_NEXT_STEPS = [
    re.compile(r"Vendingpren[eu]+rs?\s*-\s*Next Steps Call", re.IGNORECASE),
    re.compile(r"Vendingpren[eu]+rs?\s+Next Steps Call",     re.IGNORECASE),
    re.compile(r"Vendingpren[eu]+rs?\s*-\s*Next Steps",      re.IGNORECASE),
    re.compile(r"Vendingpren[eu]+r\s+Next Steps",            re.IGNORECASE),
]

# Hard excludes used only when classifying first-sales calls
# (Step 3 in the doc — these would falsely match otherwise)
FIRST_SALES_HARD_EXCLUDES = [
    re.compile(r"^\s*Canceled:?\s", re.IGNORECASE),
    re.compile(r"follow[\s-]?up", re.IGNORECASE),
    re.compile(r"fallow[\s-]?up", re.IGNORECASE),
    re.compile(r"\bF/U\b", re.IGNORECASE),
    re.compile(r"Next Steps", re.IGNORECASE),
    re.compile(r"Rescheduled|reschedule", re.IGNORECASE),
    re.compile(
        r"enrollment|Silver Start up|Bronze enrollment|questions on enrollment",
        re.IGNORECASE,
    ),
]

# Qualifying closer titles (Step 5)
CLOSER_TITLE_PATTERNS = [
    re.compile(r"Vending Strategy Call",                                re.IGNORECASE),
    re.compile(r"Vendingpren[eu]+rs?\s+Consultation",                   re.IGNORECASE),
    re.compile(r"Vendingpren[eu]+rs?\s+Strategy Call",                  re.IGNORECASE),
    re.compile(r"New Vendingpren[eu]+r\s+Strategy Call",                re.IGNORECASE),
    re.compile(r"Vending Consult",                                      re.IGNORECASE),
    re.compile(r"Post Masterclass Strategy Call",                       re.IGNORECASE),
    re.compile(r"Vending Route Consultation",                           re.IGNORECASE),
    re.compile(r"Cash[\s-]?Flowing Vending Route Advisory Interview",   re.IGNORECASE),
    re.compile(r"Vending Route Advisory Call",                          re.IGNORECASE),
]


def _is_anthony_qa(title: str) -> bool:
    return bool(
        re.search(r"Anthony", title, re.IGNORECASE)
        and re.search(r"Q&A", title, re.IGNORECASE)
    )


def is_followup_title(title: str) -> bool:
    """True if the title indicates a follow-up meeting (and is not canceled/rescheduled)."""
    if not title:
        return False
    for pat in FOLLOWUP_HARD_EXCLUDES:
        if pat.search(title):
            return False
    return any(pat.search(title) for pat in FOLLOWUP_PATTERNS)


def is_qualifying_first_sales_call(title: str, owner_name: str) -> bool:
    """
    True if a meeting qualifies as a first sales call (Closer call).
    Mirrors the classification rules used by update_field.py.
    """
    if not title:
        return False
    if owner_name in EXCLUDED_OWNERS:
        return False

    # Step 2 — Scraper Next Steps count as CLOSER (checked before hard excludes)
    for pat in SCRAPER_NEXT_STEPS:
        if pat.search(title):
            return True

    # Step 3 — Hard excludes
    for pat in FIRST_SALES_HARD_EXCLUDES:
        if pat.search(title):
            return False
    if _is_anthony_qa(title):
        return False

    # Step 4 — Setter / discovery calls don't qualify as first sales
    if owner_name in SETTER_OWNERS:
        return False
    if re.search(r"Vending Quick Discovery", title, re.IGNORECASE):
        return False

    # Step 5 — Qualifying closer titles
    return any(pat.search(title) for pat in CLOSER_TITLE_PATTERNS)


# ────────────────────────────────────────────────────────────────────────────
# Close API
# ────────────────────────────────────────────────────────────────────────────

session = requests.Session()
session.auth = (CLOSE_API_KEY, "")
session.request = guard_request(session.request)


def fetch_users() -> dict:
    """Return {user_id: display_name} for owner-name lookup."""
    url = f"{BASE_URL}/user/"
    resp = session.get(url, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    users = resp.json().get("data", [])
    out = {}
    for u in users:
        uid = u.get("id")
        if not uid:
            continue
        name = (
            u.get("display_name")
            or " ".join(filter(None, [u.get("first_name"), u.get("last_name")])).strip()
            or u.get("email")
            or ""
        )
        out[uid] = name
    return out


def fetch_all_meetings() -> list:
    """Paginate every meeting activity in the org.

    Uses offset pagination (_skip + _limit) which is what Close's
    /activity/meeting/ endpoint supports. Stops when `has_more` is false
    or a short page is returned.
    """
    meetings = []
    skip = 0
    limit = 100
    page = 0
    while True:
        page += 1
        params = {"_limit": limit, "_skip": skip}
        resp = session.get(
            f"{BASE_URL}/activity/meeting/",
            params=params,
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        batch = data.get("data", [])
        meetings.extend(batch)
        if page % 10 == 0:
            print(f"    Page {page}: {len(meetings)} meetings fetched so far")
        if not data.get("has_more") or len(batch) < limit:
            break
        skip += limit
    return meetings


def get_lead_followup_fields(lead_id: str) -> dict:
    """Fetch a lead's current values for the three follow-up date fields.

    Close returns custom field values keyed as 'custom.cf_xxx' on the
    lead GET response, so we read from that path. The returned dict
    is keyed by the bare 'cf_xxx' ID for internal consistency.
    """
    resp = session.get(f"{BASE_URL}/lead/{lead_id}/", timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    lead = resp.json()
    return {f: lead.get(f"custom.{f}") for f in FOLLOWUP_FIELDS}


def patch_lead(lead_id: str, updates: dict) -> None:
    """Patch a lead with the given custom-field updates.

    The caller passes updates keyed by bare 'cf_xxx' IDs; Close's PUT
    endpoint expects 'custom.cf_xxx' keys in the request body, so we
    rekey at the API boundary.
    """
    payload = {f"custom.{k}": v for k, v in updates.items()}
    resp = session.put(f"{BASE_URL}/lead/{lead_id}/", json=payload, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()


# ────────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────────

def parse_iso(ts):
    """Parse a Close ISO timestamp (UTC) into a tz-aware datetime, or None."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def to_pacific_date(dt: datetime) -> str:
    """Convert UTC datetime to YYYY-MM-DD in Pacific time."""
    return dt.astimezone(PACIFIC).strftime("%Y-%m-%d")


def load_state_cache() -> dict:
    if not os.path.exists(STATE_CACHE_FILE):
        return {}
    try:
        with open(STATE_CACHE_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        print("  WARNING: state cache unreadable, starting fresh")
        return {}


def save_state_cache(state: dict) -> None:
    with open(STATE_CACHE_FILE, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)


# ────────────────────────────────────────────────────────────────────────────
# Main
# ────────────────────────────────────────────────────────────────────────────

def main():
    print("=" * 72)
    print(f"Follow-Up Updater — started {datetime.now(PACIFIC).isoformat()}")
    print("=" * 72)

    print("\n[1/5] Fetching users (for owner-name lookup)...")
    user_map = fetch_users()
    print(f"      {len(user_map)} users")

    print("\n[2/5] Fetching all meetings (paginated)...")
    t0 = time.time()
    meetings = fetch_all_meetings()
    print(f"      {len(meetings)} meetings fetched in {time.time() - t0:.1f}s")

    print("\n[3/5] Classifying meetings per lead...")
    by_lead = {}
    for m in meetings:
        lead_id = m.get("lead_id")
        if lead_id:
            by_lead.setdefault(lead_id, []).append(m)
    print(f"      {len(by_lead)} leads have meetings")

    desired_state = {}
    leads_without_fsc = 0
    leads_with_fsc = 0
    leads_with_followups = 0

    for lead_id, lead_meetings in by_lead.items():
        # First pass: find earliest qualifying first sales call
        first_sales_dt = None
        for m in lead_meetings:
            title = m.get("title", "") or ""
            owner_name = user_map.get(m.get("user_id", ""), "")
            starts_at = parse_iso(m.get("starts_at"))
            if not starts_at:
                continue
            if not is_qualifying_first_sales_call(title, owner_name):
                continue
            if first_sales_dt is None or starts_at < first_sales_dt:
                first_sales_dt = starts_at

        if first_sales_dt is None:
            leads_without_fsc += 1
            continue
        leads_with_fsc += 1

        # Second pass: find follow-ups occurring strictly AFTER the first sales call
        followup_times = []
        for m in lead_meetings:
            title = m.get("title", "") or ""
            owner_name = user_map.get(m.get("user_id", ""), "")
            starts_at = parse_iso(m.get("starts_at"))
            if not starts_at:
                continue
            if owner_name in EXCLUDED_OWNERS:
                continue
            if not is_followup_title(title):
                continue
            if starts_at <= first_sales_dt:
                continue
            followup_times.append(starts_at)

        if not followup_times:
            continue
        leads_with_followups += 1

        followup_times.sort()
        earliest_three = followup_times[:3]
        dates = [to_pacific_date(dt) for dt in earliest_three]
        while len(dates) < 3:
            dates.append(None)

        desired_state[lead_id] = {
            FIELD_FOLLOWUP_1: dates[0],
            FIELD_FOLLOWUP_2: dates[1],
            FIELD_FOLLOWUP_3: dates[2],
        }

    print(f"      First sales calls found: {leads_with_fsc}")
    print(f"      Leads with no first sales call (skipped): {leads_without_fsc}")
    print(f"      Leads with at least one qualifying follow-up: {leads_with_followups}")

    print("\n[4/5] Comparing to state cache...")
    state = load_state_cache()

    # Detect leads that previously had follow-ups but no longer do
    # (e.g. all follow-ups were canceled). These need fields cleared.
    cleared_leads = []
    for lead_id, cached in state.items():
        if lead_id in desired_state:
            continue
        # If cache shows any non-null value, we need to clear those fields
        if any(cached.get(f) for f in FOLLOWUP_FIELDS):
            cleared_leads.append(lead_id)
            desired_state[lead_id] = {f: None for f in FOLLOWUP_FIELDS}

    changed = [
        (lead_id, desired)
        for lead_id, desired in desired_state.items()
        if state.get(lead_id, {}) != desired
    ]
    print(f"      Leads needing update: {len(changed)} "
          f"({len(cleared_leads)} clears, {len(changed) - len(cleared_leads)} writes)")

    print("\n[5/5] Pushing updates...")
    succeeded = 0
    failed = 0
    for lead_id, desired in changed:
        # Fetch current to send a minimal diff
        try:
            current = get_lead_followup_fields(lead_id)
        except requests.HTTPError as e:
            print(f"      ✗ {lead_id}: failed to fetch — {e}")
            failed += 1
            continue

        updates = {
            f: desired.get(f)
            for f in FOLLOWUP_FIELDS
            if current.get(f) != desired.get(f)
        }
        if not updates:
            # Cache was stale, but Close is already correct
            state[lead_id] = desired
            continue

        try:
            patch_lead(lead_id, updates)
            state[lead_id] = desired
            succeeded += 1
            summary = ", ".join(
                f"{f[-6:]}={v if v is not None else 'null'}" for f, v in updates.items()
            )
            print(f"      ✓ {lead_id}: {summary}")
        except requests.HTTPError as e:
            print(f"      ✗ {lead_id}: {e}")
            failed += 1

    # Drop fully-empty entries from the cache to keep it clean
    state = {
        lid: vals
        for lid, vals in state.items()
        if any(vals.get(f) for f in FOLLOWUP_FIELDS)
    }
    save_state_cache(state)

    print("\n" + "=" * 72)
    print(f"Done. Updated {succeeded} leads. Failures: {failed}.")
    print("=" * 72)


if __name__ == "__main__":
    main()
