"""
Scraper Meeting Counts Report
-------------------------------
Fetches all scraper/setter meetings within a date range and reports:
  - Total meeting count per setter (based on Reactivation - Setter Name mapping)
  - Meeting outcome breakdown per setter

Date range is set via environment variables:
  DATE_START — YYYY-MM-DD (inclusive)
  DATE_END   — YYYY-MM-DD (inclusive)

Output goes to stdout (GitHub Actions logs).
"""

import os
import re
import time
from collections import defaultdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

# ─────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────

CLOSE_API_KEY = os.environ["CLOSE_API_KEY"]
DATE_START    = os.environ["DATE_START"]   # YYYY-MM-DD
DATE_END      = os.environ["DATE_END"]     # YYYY-MM-DD
BASE_URL      = "https://api.close.com/api/v1"
PACIFIC       = ZoneInfo("America/Los_Angeles")
SLEEP         = 0.5

# ─────────────────────────────────────────────
# Scraper title → setter name map
# Mirrors SCRAPER_TITLE_MAP in update_field.py
# ─────────────────────────────────────────────

SCRAPER_TITLE_MAP = [
    (re.compile(r"vendingpren[eu]+rs?\s+-\s+next\s+steps\s+call",              re.IGNORECASE), "Charlie Ingram"),
    (re.compile(r"vendingpren[eu]+rs?\s+call\s+-\s+next\s+steps",              re.IGNORECASE), "Jacob Hepner"),
    (re.compile(r"vendingpren[eu]+rs?\s+next\s+steps\s+call",                  re.IGNORECASE), "Vince Bartolini"),
    (re.compile(r"vendingpren[eu]+rs?\s+next\s+steps\s+session",               re.IGNORECASE), "Pearl Sathekge"),
    (re.compile(r"vendingpren[eu]+rs?\s+discovery\s+-\s+next\s+steps",         re.IGNORECASE), "Kelly Schrader"),
    (re.compile(r"vendingpren[eu]+rs?\s+-\s+next\s+steps(?!\s+call)",          re.IGNORECASE), "Jacob Herbig"),
    (re.compile(r"vendingpren[eu]+r\s+next\s+steps",                           re.IGNORECASE), "William Nowak"),
    (re.compile(r"vending\s+consult\s+call",                                   re.IGNORECASE), "William Nowak"),
    (re.compile(r"vending\s+discovery\s+call\s+-\s+next\s+steps",              re.IGNORECASE), "August Young"),
    (re.compile(r"vending\s+discovery\s+-\s+next\s+steps",                    re.IGNORECASE), "Spencer Reynolds"),
    (re.compile(r"vendingpren[eu]+rs?\s+strategy\s*-?\s*next\s+steps",        re.IGNORECASE), "Amy Mulch"),
    (re.compile(r"vending\s+opportunity\s*-?\s*next\s+steps",                 re.IGNORECASE), "Cassie Caraballo"),
    (re.compile(r"vendingpren[eu]+rs?\s+connect\s*-?\s*next\s+steps",         re.IGNORECASE), "Jessica Zatkin"),
    (re.compile(r"vending\s+success\s*-?\s*next\s+steps",                     re.IGNORECASE), "Abigail Garza"),
]

EXCLUDED_OWNERS = {
    "user_5cZRqXu8kb4O1IeBVA98UMcMEhYZUhx1fnCHfSL0YMV",  # Stephen Olivas
    "user_yRF070m26JE67J6CJqzkAB3IqY7btNm1K5RisCglKa6",  # Ahmad Bukhari
}


def match_scraper_title(title: str, user_id: str) -> str | None:
    """Returns setter name if title matches a scraper pattern, else None."""
    if user_id in EXCLUDED_OWNERS:
        return None
    for pattern, setter in SCRAPER_TITLE_MAP:
        if pattern.search(title):
            return setter
    return None


# ─────────────────────────────────────────────
# API
# ─────────────────────────────────────────────

session = requests.Session()
session.auth = (CLOSE_API_KEY, "")


def api_get(path: str, params: dict = None, retry: int = 5) -> dict:
    url = f"{BASE_URL}{path}"
    for _ in range(retry):
        time.sleep(SLEEP)
        resp = session.get(url, params=params, timeout=30)
        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", 10))
            print(f"  [rate limit] sleeping {wait}s ...", flush=True)
            time.sleep(wait)
            continue
        resp.raise_for_status()
        return resp.json()
    raise RuntimeError(f"GET {path} failed")


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def pacific_date(starts_at: str) -> str | None:
    if not starts_at:
        return None
    dt_utc = datetime.fromisoformat(starts_at.replace("Z", "+00:00"))
    return dt_utc.astimezone(PACIFIC).strftime("%Y-%m-%d")


def normalize_outcome(raw: str | None) -> str:
    """Normalize Close meeting status/outcome to a readable label."""
    if not raw:
        return "No Outcome Set"
    mapping = {
        "attended":        "Attended",
        "no_show":         "No Show",
        "no-show":         "No Show",
        "canceled":        "Canceled",
        "cancelled":       "Canceled",
        "rescheduled":     "Rescheduled",
        "upcoming":        "Upcoming",
        "completed":       "Attended",
    }
    return mapping.get(raw.lower(), raw.title())


# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────

def main():
    start_ts = datetime.now(timezone.utc)
    print(
        f"═══════════════════════════════════════════════\n"
        f"Scraper Meeting Counts Report\n"
        f"Date range: {DATE_START} → {DATE_END} (Pacific)\n"
        f"Started: {start_ts.strftime('%Y-%m-%d %H:%M:%S UTC')}\n"
        f"═══════════════════════════════════════════════\n",
        flush=True,
    )

    # Validate date range
    if DATE_START > DATE_END:
        print("ERROR: DATE_START must be before DATE_END.", flush=True)
        return

    # ── Paginate all meetings ─────────────────────────────────────────────────
    print("Fetching all meetings (Close ignores date filters — must paginate all)...", flush=True)

    # { setter_name: { outcome: count } }
    counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    totals: dict[str, int] = defaultdict(int)

    skip          = 0
    limit         = 100
    total_fetched = 0
    matched       = 0

    while True:
        data  = api_get("/activity/meeting/", params={
            "_skip":   skip,
            "_limit":  limit,
            "_fields": "id,title,starts_at,user_id,status,lead_id",
        })
        batch = data.get("data", [])
        if not batch:
            break

        for meeting in batch:
            date = pacific_date(meeting.get("starts_at", ""))
            if not date:
                continue

            # Filter to date range
            if date < DATE_START or date > DATE_END:
                continue

            title   = (meeting.get("title") or "").strip()
            user_id = meeting.get("user_id") or ""
            setter  = match_scraper_title(title, user_id)

            if not setter:
                continue

            outcome = normalize_outcome(meeting.get("status"))
            counts[setter][outcome] += 1
            totals[setter] += 1
            matched += 1

        total_fetched += len(batch)
        print(f"  Fetched {total_fetched} meetings ({matched} matched so far) ...", flush=True)

        if not data.get("has_more"):
            break
        skip += limit

    # ── Print report ──────────────────────────────────────────────────────────
    print(
        f"\n═══════════════════════════════════════════════\n"
        f"RESULTS — {DATE_START} to {DATE_END}\n"
        f"Total scraper meetings found: {matched}\n"
        f"═══════════════════════════════════════════════",
        flush=True,
    )

    if not totals:
        print("\nNo scraper meetings found in this date range.", flush=True)
        return

    # Sort by meeting count descending
    sorted_setters = sorted(totals.items(), key=lambda x: x[1], reverse=True)

    for setter, total in sorted_setters:
        print(f"\n{setter} — {total} meeting{'s' if total != 1 else ''}", flush=True)
        for outcome, count in sorted(counts[setter].items(), key=lambda x: x[1], reverse=True):
            print(f"    {outcome}: {count}", flush=True)

    # Summary totals
    print(
        f"\n───────────────────────────────────────────────\n"
        f"OUTCOME TOTALS (all setters combined)\n"
        f"───────────────────────────────────────────────",
        flush=True,
    )
    all_outcomes: dict[str, int] = defaultdict(int)
    for setter_outcomes in counts.values():
        for outcome, count in setter_outcomes.items():
            all_outcomes[outcome] += count
    for outcome, count in sorted(all_outcomes.items(), key=lambda x: x[1], reverse=True):
        print(f"  {outcome}: {count}", flush=True)

    elapsed = (datetime.now(timezone.utc) - start_ts).total_seconds()
    print(f"\nRuntime: {elapsed:.1f}s", flush=True)


if __name__ == "__main__":
    main()
