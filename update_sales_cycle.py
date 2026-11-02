#!/usr/bin/env python3
"""
Sales Cycle (Days) — Close CRM Field Updater
============================================

Computes the number of days between a lead's First Sales Call and the moment the
lead flipped to "Closed / Won", and writes that number to the custom field
"Sales Cycle (Days)".

  Sales Cycle (Days) = won_date (Pacific) - first_sales_call_date (Pacific)

Source of each value:
  - first_sales_call_date  -> read directly from the existing custom field
                              `First Sales Call Booked Date` (cf_LFdYEQ...),
                              which update_field.py already maintains. Single
                              source of truth; this script never re-derives it.
  - won_date               -> the lead's won *opportunity* `date_won`. This is
                              the true close date and survived the HubSpot->Close
                              migration intact. (The migration bulk-flipped *lead
                              status* to won on one date, so the lead status-change
                              timeline is unreliable; the opportunity won date is
                              not.) Falls back to the status-change into Closed/Won
                              only when a lead has no won opportunity.

Runs a few times a day via GitHub Actions; supports a full backfill via the
--backfill flag (or BACKFILL=1). Mirrors the architecture of update_field.py:
paginate -> classify in Python -> diff against a committed JSON state cache ->
patch only what changed -> commit the cache.

Usage:
    python update_sales_cycle.py                # routine run (uses state cache)
    python update_sales_cycle.py --backfill     # ignore cache, recompute all
    python update_sales_cycle.py --dry-run      # compute + report, no writes
"""

import argparse
import json
import os
import sys
import time
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from workflow_holds import guard_request

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

CLOSE_API_KEY = os.environ.get("CLOSE_API_KEY")
BASE = "https://api.close.com/api/v1"

# Lead status that marks a closed-won deal (verified against the live org).
WON_STATUS_ID = "stat_0oW3iRpVp9z5DJq0cuwI1HgR0XhHAhykEPPIq4TFsxd"  # 🏆 Closed / Won

# Custom fields.
FIELD_FIRST_SALES_CALL = "cf_LFdYEQ6bsgp49YjZzefypDmdVx8iwuakWDSLPLpVrBq"   # read  (date)
FIELD_SALES_CYCLE_DAYS = "cf_27NpVa3rplytwPB6uJB4YJxC1qztWgOsiLM2hZUhicq"   # write (number)

# When a lead has been won more than once (won -> reopened -> won again),
# which transition into Closed/Won do we measure against?
#   "latest"   -> the most recent entry into won (reflects the current state)
#   "earliest" -> the first time it was ever won
WON_TRANSITION = "latest"

# Negative cycles mean the won-date precedes the first-call date — almost always
# a data-entry problem. By default we skip them rather than poison the averages.
# A same-day (0-day) close is legitimate and IS written.
ALLOW_NEGATIVE = False

PACIFIC = ZoneInfo("America/Los_Angeles")
STATE_CACHE = Path(__file__).with_name("sales_cycle_state_cache.json")

PAGE_SIZE = 100
HTTP_TIMEOUT = 60


# --------------------------------------------------------------------------- #
# HTTP plumbing (auth + rate-limit-aware)
# --------------------------------------------------------------------------- #

def _session() -> requests.Session:
    if not CLOSE_API_KEY:
        sys.exit("ERROR: CLOSE_API_KEY environment variable is not set.")
    s = requests.Session()
    s.auth = (CLOSE_API_KEY, "")          # Close uses the API key as basic-auth username
    s.request = guard_request(s.request)
    s.headers.update({"Content-Type": "application/json"})
    return s


SESSION = _session()


def _request(method: str, path: str, **kwargs):
    """Single request with retry on 429 / 5xx and Close's rate-limit backoff."""
    url = path if path.startswith("http") else f"{BASE}{path}"
    for attempt in range(6):
        resp = SESSION.request(method, url, timeout=HTTP_TIMEOUT, **kwargs)
        if resp.status_code == 429:
            # Close returns rate-limit info; honour it, fall back to expo backoff.
            wait = float(resp.headers.get("Retry-After", 0)) or _rate_limit_wait(resp)
            time.sleep(max(wait, 1.0))
            continue
        if 500 <= resp.status_code < 600:
            time.sleep(2 ** attempt)
            continue
        resp.raise_for_status()
        return resp.json() if resp.text else {}
    resp.raise_for_status()


def _rate_limit_wait(resp) -> float:
    try:
        return float(json.loads(resp.headers.get("ratelimit", "{}")).get("reset", 1))
    except Exception:
        return 1.0


# --------------------------------------------------------------------------- #
# Data fetching
# --------------------------------------------------------------------------- #

def _read_cf(lead: dict, field_id: str):
    """
    Read a custom field off a lead object. Close can surface custom fields a
    couple of ways depending on endpoint, so check each shape.
    """
    key = f"custom.{field_id}"
    if key in lead:
        return lead[key]
    custom = lead.get("custom")
    if isinstance(custom, dict):
        return custom.get(field_id) or custom.get(key)
    return None


def get_won_leads() -> list[dict]:
    """
    All leads currently in Closed/Won, each with its First Sales Call date.

    Uses Close's Advanced Filtering endpoint (POST /data/search/), which filters
    leads by status_id reliably. The simple `/lead/?query=` endpoint treats
    `lead_status_id:...` as free text and silently matches nothing.

    The first-call date rides along in the payload (via _fields), so the only
    per-lead call we make later is for the won-date status change.
    """
    body = {
        "_limit": PAGE_SIZE,
        "query": {
            "type": "and",
            "queries": [
                {"type": "object_type", "object_type": "lead"},
                {
                    "type": "field_condition",
                    "field": {
                        "type": "regular_field",
                        "object_type": "lead",
                        "field_name": "status_id",
                    },
                    "condition": {"type": "term", "values": [WON_STATUS_ID]},
                },
            ],
        },
        "_fields": {
            "lead": ["id", "display_name", "status_id", f"custom.{FIELD_FIRST_SALES_CALL}"]
        },
    }

    leads, cursor = [], None
    while True:
        if cursor:
            body["cursor"] = cursor
        page = _request("POST", "/data/search/", json=body)
        for lead in page.get("data", []):
            leads.append({
                "id": lead["id"],
                "name": lead.get("display_name", ""),
                "first_call_date": _read_cf(lead, FIELD_FIRST_SALES_CALL),
            })
        cursor = page.get("cursor")
        if not cursor:
            break
    return leads


def get_won_date(lead_id: str) -> tuple[date | None, str | None]:
    """
    The date this deal was actually won, plus which source it came from.

    Primary: the lead's won opportunity `date_won` — the real close date, which
    the HubSpot->Close migration left intact. Fallback: the lead status-change
    into Closed/Won, used only when a lead has no dated won opportunity. Returns
    (None, None) when neither exists.

    Why not lead status: the migration bulk-flipped lead status to Closed/Won on
    a single date (2026-04-16), so ~98 already-closed deals would otherwise get a
    cycle measured to that import date instead of their true close.
    """
    won = _won_date_from_opportunity(lead_id)
    if won is not None:
        return won, "opportunity"
    won = _won_date_from_status_change(lead_id)
    if won is not None:
        return won, "status_fallback"
    return None, None


def _won_date_from_opportunity(lead_id: str) -> date | None:
    """Won date from the lead's won opportunity (`date_won`, else `close_at`)."""
    page = _request(
        "GET",
        "/opportunity/",
        params={"lead_id": lead_id, "_fields": "status_type,date_won,close_at", "_limit": PAGE_SIZE},
    )
    dates = [
        (opp.get("date_won") or opp.get("close_at"))
        for opp in page.get("data", [])
        if opp.get("status_type") == "won" and (opp.get("date_won") or opp.get("close_at"))
    ]
    if not dates:
        return None
    chosen = max(dates) if WON_TRANSITION == "latest" else min(dates)
    return _parse_close_date(chosen)


def _won_date_from_status_change(lead_id: str) -> date | None:
    """Fallback: Pacific date the lead's status changed into Closed/Won."""
    page = _request(
        "GET",
        "/activity/status_change/lead/",
        params={"lead_id": lead_id, "_fields": "new_status_id,date_created", "_limit": PAGE_SIZE},
    )
    transitions = [
        a["date_created"]
        for a in page.get("data", [])
        if a.get("new_status_id") == WON_STATUS_ID and a.get("date_created")
    ]
    if not transitions:
        return None
    chosen = max(transitions) if WON_TRANSITION == "latest" else min(transitions)
    return _to_pacific_date(chosen)


def _parse_close_date(raw: str) -> date:
    """Opportunity won dates are plain dates ('2024-12-10'); some payloads carry
    a full timestamp. Handle both, converting any timestamp to Pacific."""
    if "T" in raw:
        return _to_pacific_date(raw)
    return date.fromisoformat(raw[:10])


def _to_pacific_date(iso_utc: str) -> date:
    """'2026-05-29T20:14:00+00:00' (UTC) -> Pacific calendar date."""
    dt = datetime.fromisoformat(iso_utc.replace("Z", "+00:00"))
    return dt.astimezone(PACIFIC).date()


# --------------------------------------------------------------------------- #
# Core logic
# --------------------------------------------------------------------------- #

def compute_cycle(first_call_iso: str, won_dt: date) -> int | None:
    """Days between first sales call and won. None if uncomputable/invalid."""
    try:
        first = date.fromisoformat(first_call_iso)
    except (TypeError, ValueError):
        return None
    days = (won_dt - first).days
    if days < 0 and not ALLOW_NEGATIVE:
        return None
    return days


def load_cache(backfill: bool) -> dict:
    if backfill or not STATE_CACHE.exists():
        return {}
    try:
        return json.loads(STATE_CACHE.read_text())
    except Exception:
        return {}


def save_cache(cache: dict) -> None:
    STATE_CACHE.write_text(json.dumps(cache, indent=2, sort_keys=True))


def patch_lead(lead_id: str, value: int) -> None:
    _request("PUT", f"/lead/{lead_id}/", json={f"custom.{FIELD_SALES_CYCLE_DAYS}": value})


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main() -> None:
    ap = argparse.ArgumentParser(description="Update Sales Cycle (Days) in Close CRM.")
    ap.add_argument("--backfill", action="store_true",
                    help="Ignore the state cache and recompute every won lead.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Compute and report, but write nothing to Close.")
    args = ap.parse_args()
    backfill = args.backfill or os.environ.get("BACKFILL") == "1"

    mode = "BACKFILL" if backfill else "routine"
    print(f"Sales Cycle updater — {mode} run{' (dry-run)' if args.dry_run else ''}")

    cache = load_cache(backfill)
    won_leads = get_won_leads()
    print(f"Found {len(won_leads)} leads in Closed / Won.")

    updated = skipped_no_first_call = skipped_no_won_date = unchanged = anomalies = 0
    won_via_fallback = 0

    for lead in won_leads:
        lid, name = lead["id"], lead["name"]
        first_call = lead["first_call_date"]
        cached = cache.get(lid)

        if not first_call:
            skipped_no_first_call += 1
            print(f"  · skip (no First Sales Call date): {name}")
            continue

        # Reuse the cached won-date when the lead is unchanged: still won, same
        # first-call date, and we already resolved a won-date for it. Only newly
        # won / changed leads trigger the per-lead opportunity fetch.
        if cached and cached.get("first_call_date") == first_call and cached.get("won_date"):
            won_dt = date.fromisoformat(cached["won_date"])
        else:
            won_dt, source = get_won_date(lid)
            if won_dt is None:
                skipped_no_won_date += 1
                print(f"  · skip (no won opportunity or status change): {name}")
                continue
            if source == "status_fallback":
                won_via_fallback += 1

        cycle = compute_cycle(first_call, won_dt)
        if cycle is None:
            anomalies += 1
            print(f"  ! anomaly (won {won_dt} is before first call {first_call}): {name}")
            continue

        # Diff against last-written value (the cache is our record of Close state).
        if cached and cached.get("cycle_days") == cycle:
            unchanged += 1
        else:
            if not args.dry_run:
                patch_lead(lid, cycle)
            updated += 1
            print(f"  ✓ {name}: {first_call} → {won_dt}  =  {cycle} days")

        cache[lid] = {
            "name": name,
            "first_call_date": first_call,
            "won_date": won_dt.isoformat(),
            "cycle_days": cycle,
        }

    if not args.dry_run:
        save_cache(cache)

    print("\nSummary")
    print(f"  updated/written : {updated}")
    print(f"  unchanged       : {unchanged}")
    print(f"  no first call   : {skipped_no_first_call}")
    print(f"  no won date     : {skipped_no_won_date}")
    print(f"  anomalies       : {anomalies}")
    print(f"  won via status fallback (no opp): {won_via_fallback}")
    if args.dry_run:
        print("  (dry-run: nothing was written to Close and the cache was not saved)")


if __name__ == "__main__":
    main()
