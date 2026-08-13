#!/usr/bin/env python3
"""
find_ben_kelly_joe_meetings.py
--------------------------------
One-time audit script for `close-first-sales-meeting`.

Identifies which "Ben Kelly Lead" leads (custom field cf_0ab1Co4MuyTH6IrjjAPIMejbK4V4oA7iUxO89Ly74km
= "Yes") have real MEETING activity with Joe Dysert -- as distinct from the recruiting/hiring
interview meetings that also land on his calendar and get synced into Close as lead meeting
activity.

WHY THIS EXISTS
----------------
The org's existing "first sales call" classifier (update_field.py, see
meeting-title-classification.md) works by matching meeting TITLE strings (e.g. "Vending Strategy
Call"). That doesn't work for Joe Dysert's meetings: they come through a generic Calendly link
that titles every meeting "{Invitee Name} and Joe Dysert" -- there is no Vendingpreneurs-style
title to pattern-match on, and Joe was not consistently wired into the title-classification
automations to begin with. So this script does NOT use title classification. Instead it:

  1. Bulk-fetches every lead where Ben Kelly Lead = Yes (the field is the trusted signal here --
     the underlying business-line field is known to be unreliable, per Stephen 2026-08-13).
  2. Bulk-fetches every meeting activity owned by Joe Dysert (all-time, one paginated scan).
  3. Intersects the two by lead_id.
  4. Classifies each surviving meeting as a real sales meeting vs. a hiring/interview meeting
     using the Calendly note text (see CLASSIFICATION below) -- Joe also uses Close/Calendly to
     interview candidates for a "Sales Closer" role, and those meetings land on lead records too.
  5. Emits a CSV: one row per qualifying lead, with the earliest qualifying meeting ("first booked
     sales call" with Joe), plus a separate CSV of everything that was excluded or flagged, so
     nothing is silently dropped.

This is a READ-ONLY audit script. It does not write anything back to Close. Run it, review the
CSVs (especially the "flagged for review" bucket), and decide from there whether/how to persist
the result as a Close field.

CLASSIFICATION (see FIND_BEN_KELLY_JOE_MEETINGS_SETUP.md for the full writeup + evidence)
-------------------------------------------------------------------------------------------
Real sales-prospect meetings booked through Joe's link carry plain Calendly boilerplate with no
custom question. The recruiting/interview booking flow asks an extra Calendly custom question
("Please share anything that will help prepare for our meeting."). So:

  - EXCLUDE (high confidence, hiring/interview): note contains an explicit hiring/candidate
    keyword (interview, candidate, resume, sales closer, closer position, hiring, applicant,
    job opening, cover letter).
  - FLAG (needs human review): note contains the recruiting Calendly custom-question marker
    ("Please share anything that will help prepare for our meeting") but NONE of the explicit
    keywords above -- i.e. it looks like the recruiting link, but the answer text didn't trip an
    explicit keyword (e.g. a candidate who wrote something vague/enthusiastic instead of saying
    "interview").
  - INCLUDE (high confidence, sales): no custom-question marker at all -- plain Calendly
    boilerplate, matching the pattern seen on confirmed real prospect meetings.

This is a heuristic, not a guarantee. Spot-check the FLAG bucket and a sample of the INCLUDE
bucket before treating this as final. If Joe's recruiting link changes or a new booking flow is
added, update INTERVIEW_KEYWORDS / CUSTOM_QUESTION_MARKER accordingly.

USAGE
-----
    python find_ben_kelly_joe_meetings.py --selftest              # 12 pure-logic tests, no network
    python find_ben_kelly_joe_meetings.py --dry-run --limit 20     # time + eyeball this first
    python find_ben_kelly_joe_meetings.py                          # full run, writes CSVs, no Close writes ever

ENV
---
    CLOSE_API_KEY   required (same secret used by the other scripts in this repo)
"""

import argparse
import csv
import os
import re
import sys
import time
from datetime import datetime, timezone

import requests

try:
    from zoneinfo import ZoneInfo
    PACIFIC = ZoneInfo("America/Los_Angeles")
except ImportError:  # pragma: no cover
    PACIFIC = None

API_BASE = "https://api.close.com/api/v1"
BEN_KELLY_FIELD_ID = "cf_0ab1Co4MuyTH6IrjjAPIMejbK4V4oA7iUxO89Ly74km"  # "Ben Kelly Lead" (choices: No/Yes)
JOE_DYSERT_USER_ID = "user_lUjlATIIgFg8mELa0GFzZUj0lG4Cs7PwQsxbi34I6Su"

PAGE_SIZE = 100
MAX_RETRIES = 5

# --- Classification constants -----------------------------------------------------------------

# Explicit hiring/interview signals. Case-insensitive substring match against the meeting's note
# text (title is useless here -- see module docstring).
INTERVIEW_KEYWORDS = [
    "sales closer",
    "closer position",
    "interview for",
    "interview with",
    "job interview",
    "candidate",
    "resume",
    "cover letter",
    "job opening",
    "applicant",
    "applying for",
    "hiring",
    "recruiting",
    "recruiter",
]

# The Calendly custom question that (per observed data) only appears on the recruiting/hiring
# booking link, not on the standard prospect booking link. Presence of this marker without an
# explicit keyword above = FLAG, not auto-exclude.
CUSTOM_QUESTION_MARKER = "please share anything that will help prepare for our meeting"

# Internal / non-prospect meeting titles to always ignore outright (recurring internal team
# meetings that show up in Joe's /activity/meeting/ feed). Belt-and-suspenders -- the Ben Kelly
# lead_id intersection should already exclude these, since internal placeholder leads won't carry
# the Ben Kelly Lead custom field, but we filter explicitly too in case that ever changes.
INTERNAL_MEETING_TITLES = {
    "sales huddle",
}

INTERVIEW_RE = re.compile("|".join(re.escape(k) for k in INTERVIEW_KEYWORDS), re.IGNORECASE)


def classify_meeting(title, note):
    """Returns (bucket, reason) where bucket is 'include' | 'flag' | 'exclude'."""
    title = (title or "").strip()
    note = (note or "").strip()

    if title.lower() in INTERNAL_MEETING_TITLES:
        return "exclude", "internal meeting title"

    m = INTERVIEW_RE.search(note)
    if m:
        return "exclude", f"note contains hiring keyword: '{m.group(0)}'"

    if CUSTOM_QUESTION_MARKER in note.lower():
        return "flag", "recruiting-link custom question present, no explicit keyword matched"

    return "include", "no recruiting-link signal found"


# --- Self-test ------------------------------------------------------------------------------

def _run_selftest():
    cases = [
        # (title, note, expected_bucket)
        ("Jordan Smith and Joe Dysert", "", "include"),
        (
            "Jordan Smith and Joe Dysert",
            "Event Name\n30 Minute Meeting\n\nLocation: This is a Google Meet web conference.\n"
            "https://calendly.com/events/abc/google_meet\n\nPowered by Calendly.com",
            "include",
        ),
        (
            "Taylor Reed and Joe Dysert",
            "Please share anything that will help prepare for our meeting.: This meeting is "
            "specifically to review your needs for a sales closer. I'm currently interested in "
            "the opportunity.",
            "exclude",
        ),
        (
            "Taylor Reed and Joe Dysert",
            "Please share anything that will help prepare for our meeting.: For a closer position",
            "exclude",
        ),
        (
            "Taylor Reed and Joe Dysert",
            "Please share anything that will help prepare for our meeting.: Interview for Sales "
            "Closer for Vendingpreneurs",
            "exclude",
        ),
        (
            "Taylor Reed and Joe Dysert",
            "Please share anything that will help prepare for our meeting.: excited to learn more "
            "about the company and potentially help people transform their lives!",
            "flag",
        ),
        ("Sales Huddle", "", "exclude"),
        ("sales huddle", "some note", "exclude"),
        ("Alex Kim and Joe Dysert", "Please bring my resume and references", "exclude"),
        ("Alex Kim and Joe Dysert", "I am a candidate for the closer role", "exclude"),
        ("Alex Kim and Joe Dysert", "Looking forward to discussing the vending opportunity!", "include"),
        (
            "Alex Kim and Joe Dysert",
            "Please share anything that will help prepare for our meeting.: n/a",
            "flag",
        ),
    ]
    failures = 0
    for i, (title, note, expected) in enumerate(cases, 1):
        bucket, reason = classify_meeting(title, note)
        ok = bucket == expected
        status = "OK" if ok else "FAIL"
        if not ok:
            failures += 1
        print(f"[{status}] case {i}: expected={expected} got={bucket} ({reason})")
    print(f"\n{len(cases) - failures}/{len(cases)} passed")
    return failures == 0


# --- Close API helpers ------------------------------------------------------------------------

def _session():
    api_key = os.environ.get("CLOSE_API_KEY")
    if not api_key:
        sys.exit("CLOSE_API_KEY environment variable is required")
    s = requests.Session()
    s.auth = (api_key, "")
    s.headers.update({"Content-Type": "application/json"})
    return s


def _get_with_retry(session, url, params):
    for attempt in range(MAX_RETRIES):
        resp = session.get(url, params=params, timeout=60)
        if resp.status_code == 429:
            wait = float(resp.headers.get("Retry-After", 2 * (attempt + 1)))
            print(f"  rate limited, sleeping {wait}s...")
            time.sleep(wait)
            continue
        resp.raise_for_status()
        return resp.json()
    resp.raise_for_status()


def fetch_ben_kelly_leads(session, limit=None):
    """One paginated scan of every lead where Ben Kelly Lead = Yes.

    Returns dict: lead_id -> {"display_name": str, "status_label": str}
    """
    leads = {}
    skip = 0
    query = f'custom.{BEN_KELLY_FIELD_ID}:"Yes"'
    while True:
        params = {
            "query": query,
            "_fields": "id,display_name,status_label",
            "_limit": PAGE_SIZE,
            "_skip": skip,
        }
        data = _get_with_retry(session, f"{API_BASE}/lead/", params)
        for lead in data.get("data", []):
            leads[lead["id"]] = {
                "display_name": lead.get("display_name", ""),
                "status_label": lead.get("status_label", ""),
            }
            if limit and len(leads) >= limit:
                return leads
        if not data.get("has_more"):
            break
        skip += PAGE_SIZE
        print(f"  ...{len(leads)} Ben Kelly leads fetched so far")
    return leads


def fetch_joe_meetings(session):
    """One paginated scan of every meeting activity owned by Joe Dysert, all-time.

    Returns list of dicts: {lead_id, title, note, starts_at, activity_id}
    """
    meetings = []
    skip = 0
    while True:
        params = {
            "user_id": JOE_DYSERT_USER_ID,
            "_limit": PAGE_SIZE,
            "_skip": skip,
        }
        data = _get_with_retry(session, f"{API_BASE}/activity/meeting/", params)
        for m in data.get("data", []):
            meetings.append(
                {
                    "activity_id": m.get("id"),
                    "lead_id": m.get("lead_id"),
                    "title": m.get("title") or "",
                    "note": m.get("note") or "",
                    "starts_at": m.get("starts_at"),
                }
            )
        if not data.get("has_more"):
            break
        skip += PAGE_SIZE
        print(f"  ...{len(meetings)} Joe Dysert meetings fetched so far")
    return meetings


def to_pacific_date(starts_at_iso):
    if not starts_at_iso:
        return ""
    dt = datetime.fromisoformat(starts_at_iso.replace("Z", "+00:00")).astimezone(timezone.utc)
    if PACIFIC:
        dt = dt.astimezone(PACIFIC)
    return dt.strftime("%Y-%m-%d")


def note_excerpt(note, length=140):
    note = (note or "").replace("\n", " ").strip()
    return note[:length] + ("..." if len(note) > length else "")


# --- Main ---------------------------------------------------------------------------------------

def run(limit=None, out_prefix="ben_kelly_joe_meetings"):
    session = _session()

    print("Fetching Ben Kelly leads (Ben Kelly Lead = Yes)...")
    ben_kelly_leads = fetch_ben_kelly_leads(session, limit=limit)
    print(f"  {len(ben_kelly_leads)} Ben Kelly leads found\n")

    print("Fetching Joe Dysert's meeting activity (all-time)...")
    joe_meetings = fetch_joe_meetings(session)
    print(f"  {len(joe_meetings)} meetings owned by Joe Dysert found\n")

    print("Cross-referencing and classifying...")
    per_lead = {}  # lead_id -> list of (bucket, reason, meeting)
    for m in joe_meetings:
        lead_id = m["lead_id"]
        if lead_id not in ben_kelly_leads:
            continue
        bucket, reason = classify_meeting(m["title"], m["note"])
        per_lead.setdefault(lead_id, []).append((bucket, reason, m))

    qualifying_rows = []
    review_rows = []

    for lead_id, entries in per_lead.items():
        lead = ben_kelly_leads[lead_id]
        include_meetings = sorted(
            (e for e in entries if e[0] == "include"), key=lambda e: e[2]["starts_at"] or ""
        )
        flagged_meetings = [e for e in entries if e[0] == "flag"]
        excluded_meetings = [e for e in entries if e[0] == "exclude"]

        if include_meetings:
            first = include_meetings[0][2]
            qualifying_rows.append(
                {
                    "lead_id": lead_id,
                    "lead_url": f"https://app.close.com/lead/{lead_id}/",
                    "lead_name": lead["display_name"],
                    "lead_status": lead["status_label"],
                    "first_meeting_with_joe_date": to_pacific_date(first["starts_at"]),
                    "first_meeting_title": first["title"],
                    "total_include_meetings": len(include_meetings),
                    "total_flagged_meetings": len(flagged_meetings),
                    "total_excluded_meetings": len(excluded_meetings),
                }
            )

        # Anything flagged or excluded on a lead gets logged for review, even if that same lead
        # also has a qualifying meeting -- Stephen asked to see everything, not just the winners.
        for bucket, reason, m in flagged_meetings + excluded_meetings:
            review_rows.append(
                {
                    "lead_id": lead_id,
                    "lead_url": f"https://app.close.com/lead/{lead_id}/",
                    "lead_name": lead["display_name"],
                    "bucket": bucket,
                    "reason": reason,
                    "meeting_date": to_pacific_date(m["starts_at"]),
                    "meeting_title": m["title"],
                    "note_excerpt": note_excerpt(m["note"]),
                }
            )

    qualifying_rows.sort(key=lambda r: r["first_meeting_with_joe_date"])
    review_rows.sort(key=lambda r: r["meeting_date"])

    qualifying_path = f"{out_prefix}.csv"
    review_path = f"{out_prefix}_review.csv"

    with open(qualifying_path, "w", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "lead_id",
                "lead_url",
                "lead_name",
                "lead_status",
                "first_meeting_with_joe_date",
                "first_meeting_title",
                "total_include_meetings",
                "total_flagged_meetings",
                "total_excluded_meetings",
            ],
        )
        writer.writeheader()
        writer.writerows(qualifying_rows)

    with open(review_path, "w", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "lead_id",
                "lead_url",
                "lead_name",
                "bucket",
                "reason",
                "meeting_date",
                "meeting_title",
                "note_excerpt",
            ],
        )
        writer.writeheader()
        writer.writerows(review_rows)

    print("\n--- Summary ---")
    print(f"Ben Kelly leads scanned:                 {len(ben_kelly_leads)}")
    print(f"Joe Dysert meetings scanned (all-time):   {len(joe_meetings)}")
    print(f"Ben Kelly leads with >=1 Joe meeting:      {len(per_lead)}")
    print(f"Qualifying leads (real sales meeting):     {len(qualifying_rows)}  -> {qualifying_path}")
    print(f"Flagged/excluded meeting rows for review:  {len(review_rows)}  -> {review_path}")
    flag_count = sum(1 for r in review_rows if r["bucket"] == "flag")
    exclude_count = sum(1 for r in review_rows if r["bucket"] == "exclude")
    print(f"  of which FLAGGED (needs human review):   {flag_count}")
    print(f"  of which EXCLUDED (hiring/interview):    {exclude_count}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--selftest", action="store_true", help="Run pure-logic classification tests, no network")
    parser.add_argument("--dry-run", action="store_true", help="No-op flag for consistency with other scripts in this repo -- this script never writes to Close")
    parser.add_argument("--limit", type=int, default=None, help="Cap the number of Ben Kelly leads processed (for a fast first pass)")
    parser.add_argument("--out", default="ben_kelly_joe_meetings", help="Output file prefix (default: ben_kelly_joe_meetings)")
    args = parser.parse_args()

    if args.selftest:
        ok = _run_selftest()
        sys.exit(0 if ok else 1)

    if args.dry_run:
        print("NOTE: this script is read-only and never writes to Close -- --dry-run and a full run behave identically.\n")

    run(limit=args.limit, out_prefix=args.out)


if __name__ == "__main__":
    main()
