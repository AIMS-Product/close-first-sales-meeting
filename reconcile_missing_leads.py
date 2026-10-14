"""
Reconciliation Script — First Sales Call for Unmapped Leads
------------------------------------------------------------
Reads a CSV of leads missing the "First Sales Call Booked Date" field,
looks up each lead in Close CRM by name, fetches all their meetings,
and produces a report showing:
  - Every meeting on the lead (date, title, owner)
  - Whether it qualifies under current rules
  - Whether it was excluded and why
  - A best guess at the first sales call date if one exists

Output: reconciliation_report.csv
"""

import csv
import os
import re
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

from lane2_roster import MISSING_LEAD_RECONCILIATION_TITLE_MAP as SCRAPER_TITLE_MAP

# ─────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────

CLOSE_API_KEY = os.environ["CLOSE_API_KEY"]
BASE_URL      = "https://api.close.com/api/v1"
PACIFIC       = ZoneInfo("America/Los_Angeles")
SLEEP         = 0.5

INPUT_CSV  = "Vendingpreneurs_leads_2026-04-01_17-17.csv"
OUTPUT_CSV = "reconciliation_report.csv"

# ─────────────────────────────────────────────
# The 64 leads missing First Sales Call date
# ─────────────────────────────────────────────

MISSING_LEADS = [
    "Quentin", "Erick Aguero", "Eddie preciado", "Derrick Boddie",
    "Sean Davy", "Daniel (DK) Kim", "Allen Burt", "Andy Woodward",
    "Austin Rhamy", "Joshua Ryan", "Nathan Mercado", "Terra Holt",
    "Sophia Marnell", "Matthew Byrne", "ben", "Trevor Sparrow",
    "Chad DeYoung", "Mike McGee", "Sarah Parry", "Alex Solis",
    "Bilawal Singh Gill", "Aziz Shuvo", "Oakley", "Marilin Sanchez",
    "David Arnold", "Derrick cheroti", "Sean G", "Matt",
    "Erich Suehs", "Brandon Nicholas", "Kelly terracciano", "Emerito Lapid",
    "Jason Duncan", "Jason", "Luke Herman", "Dustin Geyer",
    "Casey Chandler", "Danyelle Grant", "Valicia Davis", "Michael",
    "Kerri Lewis", "Christian Bonifacio", "Raymond Concepcion", "Michael Ross",
    "Frank", "Christopher Dean", "Mike", "Chuk Dim",
    "Jake Frederick", "Curtis Hoffman", "Robin Totome", "Malik Excalibur",
    "Max Jackson", "Scott Wright", "Vinnie Ducharme", "Cory Howell",
    "Gugu Sikhakhane", "Paulina Nolina", "Lore Soto", "Tanner Mcinally",
    "Griffin Tinkey", "Bailey Jedrzejewski", "Ronnell Reed", "Stephen Daley",
]

# ─────────────────────────────────────────────
# User IDs
# ─────────────────────────────────────────────

EXCLUDED_OWNERS = {
    "user_5cZRqXu8kb4O1IeBVA98UMcMEhYZUhx1fnCHfSL0YMV",  # Stephen Olivas
    "user_yRF070m26JE67J6CJqzkAB3IqY7btNm1K5RisCglKa6",  # Ahmad Bukhari
}

SETTER_OWNERS = {
    "user_EmhqCmaHERTfgfWnPADiLGEqQw3ENvRYd3u1VEmblIp",  # Kristin Nelson
    "user_4sfuKGMbv0LQZ4hpS8ipASv406kKTSNP5Xx79jOwSqM",  # Spencer Reynolds
}

# ─────────────────────────────────────────────
# Classification Rules (same as update_field.py)
# ─────────────────────────────────────────────

RE_CANCELED      = re.compile(r"^canceled[\s:]?", re.IGNORECASE)
RE_FOLLOWUP      = re.compile(r"follow[\-\s]?up|fallow\s+up|f/u\b|next\s+steps|reschedul", re.IGNORECASE)
RE_ENROLLMENT    = re.compile(r"enrollment|silver\s+start\s*up|bronze\s+enrollment|questions\s+on\s+enrollment", re.IGNORECASE)
RE_DISCOVERY     = re.compile(r"vending\s+quick\s+discovery", re.IGNORECASE)
RE_POSTWEBINAR   = re.compile(r"post\s+masterclass\s+strategy\s+call", re.IGNORECASE)

CLOSER_PATTERNS = [
    re.compile(r"vending\s+strategy\s+call", re.IGNORECASE),
    re.compile(r"vendingpren[eu]+rs?\s+consultation", re.IGNORECASE),
    re.compile(r"vendingpren[eu]+rs?\s+strategy\s+call", re.IGNORECASE),
    re.compile(r"new\s+vendingpren[eu]+r\s+strategy\s+call", re.IGNORECASE),
    re.compile(r"vending\s+consult\b", re.IGNORECASE),
    re.compile(r"post\s+masterclass\s+strategy\s+call", re.IGNORECASE),
]


def classify_meeting(title: str, user_id: str) -> tuple[str, str]:
    """
    Returns (result, reason) where result is:
      QUALIFIES       — counts as a first sales call
      EXCLUDED        — explicitly excluded by a rule
      NO_MATCH        — not excluded but doesn't match qualifying patterns
      OWNER_EXCLUDED  — meeting owner is always excluded (Stephen/Ahmad)
      SETTER          — setter/discovery call (not a sales call)
    """
    if user_id in EXCLUDED_OWNERS:
        return "OWNER_EXCLUDED", "Meeting owner is excluded (Stephen/Ahmad)"

    # Scraper titles — check before followup exclusion
    for pattern, setter_name in SCRAPER_TITLE_MAP:
        if pattern.search(title):
            return "QUALIFIES", f"Scraper meeting ({setter_name})"

    if RE_CANCELED.match(title):
        return "EXCLUDED", "Title starts with 'Canceled'"

    if RE_FOLLOWUP.search(title):
        return "EXCLUDED", f"Title contains follow-up/reschedule pattern"

    if re.search(r"\banthony\b", title, re.IGNORECASE) and re.search(r"\bq&a\b", title, re.IGNORECASE):
        return "EXCLUDED", "Anthony Q&A session"

    if RE_ENROLLMENT.search(title):
        return "EXCLUDED", "Enrollment/onboarding meeting"

    if user_id in SETTER_OWNERS:
        return "SETTER", "Meeting owned by setter rep (Kristin/Spencer)"

    if RE_DISCOVERY.search(title):
        return "SETTER", "Vending Quick Discovery (setter call)"

    if RE_POSTWEBINAR.search(title):
        return "QUALIFIES", "Post Masterclass Strategy Call"

    for pattern in CLOSER_PATTERNS:
        if pattern.search(title):
            return "QUALIFIES", "Matches closer pattern"

    return "NO_MATCH", "Title doesn't match any qualifying or excluded pattern"


# ─────────────────────────────────────────────
# API
# ─────────────────────────────────────────────

session = requests.Session()
session.auth = (CLOSE_API_KEY, "")


def api_get(path, params=None, retry=5):
    url = f"{BASE_URL}{path}"
    for _ in range(retry):
        time.sleep(SLEEP)
        resp = session.get(url, params=params, timeout=30)
        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", 10))
            print(f"  [rate limit] sleeping {wait}s", flush=True)
            time.sleep(wait)
            continue
        resp.raise_for_status()
        return resp.json()
    raise RuntimeError(f"GET {path} failed")


def search_lead_by_name(name: str) -> list[dict]:
    """Search for leads by name, return list of matches."""
    data = api_get("/lead/", params={
        "query": f'name:"{name}"',
        "_fields": "id,display_name",
        "_limit": 5,
    })
    return data.get("data", [])


def get_meetings_for_lead(lead_id: str) -> list[dict]:
    """Fetch all meeting activities for a specific lead."""
    data = api_get("/activity/meeting/", params={
        "lead_id": lead_id,
        "_limit": 100,
        "_fields": "id,title,starts_at,user_id,user_name,status,lead_id",
    })
    return data.get("data", [])


def pacific_date(starts_at: str) -> str:
    if not starts_at:
        return ""
    dt_utc = datetime.fromisoformat(starts_at.replace("Z", "+00:00"))
    return dt_utc.astimezone(PACIFIC).strftime("%Y-%m-%d")


# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────

def main():
    print(
        f"═══════════════════════════════════════\n"
        f"Reconciliation — Missing First Sales Calls\n"
        f"Leads to check: {len(MISSING_LEADS)}\n"
        f"═══════════════════════════════════════\n",
        flush=True,
    )

    rows = []

    for i, name in enumerate(MISSING_LEADS, 1):
        print(f"[{i}/{len(MISSING_LEADS)}] {name}", flush=True)

        # Search for lead in Close
        matches = search_lead_by_name(name)
        if not matches:
            print(f"  ⚠ No lead found in Close", flush=True)
            rows.append({
                "lead_name":           name,
                "close_lead_id":       "",
                "close_display_name":  "",
                "meeting_date":        "",
                "meeting_title":       "",
                "meeting_owner":       "",
                "meeting_status":      "",
                "classification":      "LEAD_NOT_FOUND",
                "reason":              "No matching lead found in Close",
                "suggested_first_sales_date": "",
            })
            continue

        if len(matches) > 1:
            print(f"  ⚠ Multiple leads found ({len(matches)}), using first match", flush=True)

        lead = matches[0]
        lead_id   = lead["id"]
        lead_name = lead.get("display_name", name)

        # Fetch meetings for this lead
        meetings = get_meetings_for_lead(lead_id)

        if not meetings:
            print(f"  No meetings found", flush=True)
            rows.append({
                "lead_name":           name,
                "close_lead_id":       lead_id,
                "close_display_name":  lead_name,
                "meeting_date":        "",
                "meeting_title":       "",
                "meeting_owner":       "",
                "meeting_status":      "",
                "classification":      "NO_MEETINGS",
                "reason":              "No meeting activities on this lead",
                "suggested_first_sales_date": "",
            })
            continue

        # Classify every meeting
        qualifying_dates = []
        for m in meetings:
            title    = (m.get("title") or "").strip()
            user_id  = m.get("user_id") or ""
            username = m.get("user_name") or ""
            status   = m.get("status") or ""
            date     = pacific_date(m.get("starts_at", ""))
            result, reason = classify_meeting(title, user_id)

            rows.append({
                "lead_name":           name,
                "close_lead_id":       lead_id,
                "close_display_name":  lead_name,
                "meeting_date":        date,
                "meeting_title":       title,
                "meeting_owner":       username,
                "meeting_status":      status,
                "classification":      result,
                "reason":              reason,
                "suggested_first_sales_date": "",
            })

            if result == "QUALIFIES" and date:
                qualifying_dates.append(date)

        # If qualifying meetings found, stamp the earliest on all rows for this lead
        if qualifying_dates:
            first_date = min(qualifying_dates)
            for row in rows:
                if row["close_lead_id"] == lead_id:
                    row["suggested_first_sales_date"] = first_date
            print(f"  ✓ {len(meetings)} meetings | {len(qualifying_dates)} qualifying | first: {first_date}", flush=True)
        else:
            print(f"  ✗ {len(meetings)} meetings | 0 qualifying", flush=True)

    # Write output CSV
    fieldnames = [
        "lead_name", "close_lead_id", "close_display_name",
        "meeting_date", "meeting_title", "meeting_owner", "meeting_status",
        "classification", "reason", "suggested_first_sales_date",
    ]

    with open(OUTPUT_CSV, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    # Summary
    qualifying_leads = {r["close_lead_id"] for r in rows if r["suggested_first_sales_date"]}
    no_meeting_leads = {r["lead_name"] for r in rows if r["classification"] == "NO_MEETINGS"}
    not_found_leads  = {r["lead_name"] for r in rows if r["classification"] == "LEAD_NOT_FOUND"}
    no_match_leads   = {
        r["lead_name"] for r in rows
        if r["classification"] == "NO_MATCH" and not r["suggested_first_sales_date"]
    }

    print(
        f"\n═══════════════════════════════════════\n"
        f"Summary\n"
        f"  Leads with qualifying meetings found: {len(qualifying_leads)}\n"
        f"  Leads with NO matching meeting title: {len(no_match_leads)}\n"
        f"  Leads with no meetings at all:        {len(no_meeting_leads)}\n"
        f"  Leads not found in Close:             {len(not_found_leads)}\n"
        f"\nOutput written to: {OUTPUT_CSV}\n"
        f"═══════════════════════════════════════",
        flush=True,
    )


if __name__ == "__main__":
    main()
