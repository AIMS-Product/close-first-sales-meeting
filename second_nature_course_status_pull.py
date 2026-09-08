#!/usr/bin/env python3
"""
second_nature_course_status_pull.py

Real test pull against Second Nature's confirmed API (per their support
chatbot response, 2026-09-09) -- not a guess like the earlier discovery
script. This hits the Users Course State API to confirm we actually get
per-user course completion/score data back, ahead of building the real
manager recap job.

  Base URL:    https://app.secondnature.ai/api/
  Auth header: Authorization: Token <SECOND_NATURE_API_KEY>
  Endpoint:    GET /course-status/
               GET /course-status/{course_id}/
  Documented params: includeexternaluserid, completed_from, completed_until

What's NOT confirmed yet (their help docs are behind robots.txt, so this
was built from the chatbot summary, not the full article): exact pagination
shape, the full list of fields on each record beyond score/passed/
completed_at/course_id, and how to list valid course_ids. This script is
deliberately defensive about pagination and just dumps whatever comes back
so we can inspect the real shape rather than assume it.

PRIVACY NOTE: a real pull returns real trainee names/emails/scores. The
full response is saved to a JSON file (uploaded as a short-retention build
artifact) -- do NOT commit that file to the repo. Only aggregate,
PII-free counts get printed to the console / job summary.
"""

import json
import os
import sys
from datetime import datetime, timezone

import requests

API_KEY = os.environ.get("SECOND_NATURE_API_KEY")
if not API_KEY:
    print("ERROR: SECOND_NATURE_API_KEY is not set.", flush=True)
    sys.exit(1)

BASE_URL = "https://app.secondnature.ai/api"
HEADERS = {"Authorization": f"Token {API_KEY}"}
TIMEOUT = 30

# Optional test filters -- leave unset to pull everything. Set these as
# workflow inputs / env vars if you want to narrow a first test run.
COMPLETED_FROM = os.environ.get("COMPLETED_FROM")   # e.g. "2026-08-01"
COMPLETED_UNTIL = os.environ.get("COMPLETED_UNTIL")
INCLUDE_EXTERNAL_USER_ID = os.environ.get("INCLUDE_EXTERNAL_USER_ID")  # "true"/"false"

RAW_OUTPUT_PATH = "course_status_raw.json"
MAX_PAGES = 50  # safety cap in case pagination loops or we misread the shape


def log(msg: str) -> None:
    print(msg, flush=True)


def fetch_course_status() -> list:
    """GET /course-status/, following pagination defensively. Second
    Nature's SCIM v2 endpoint suggests a Django REST Framework backend, so
    we check for the standard DRF {count, next, previous, results} shape --
    but fall back to treating the body as a flat list if it isn't that."""
    params = {}
    if COMPLETED_FROM:
        params["completed_from"] = COMPLETED_FROM
    if COMPLETED_UNTIL:
        params["completed_until"] = COMPLETED_UNTIL
    if INCLUDE_EXTERNAL_USER_ID:
        params["includeexternaluserid"] = INCLUDE_EXTERNAL_USER_ID

    url = f"{BASE_URL}/course-status/"
    all_records = []
    page = 0

    while url and page < MAX_PAGES:
        page += 1
        log(f"GET {url} (page {page}, params={params if page == 1 else '(from next link)'})")
        resp = requests.get(url, headers=HEADERS, params=params if page == 1 else None, timeout=TIMEOUT)

        if resp.status_code != 200:
            log(f"  -> HTTP {resp.status_code}")
            log(f"  Body: {resp.text[:500]}")
            resp.raise_for_status()

        data = resp.json()

        if isinstance(data, list):
            # Flat list, no pagination envelope.
            all_records.extend(data)
            url = None
        elif isinstance(data, dict) and "results" in data:
            # DRF-style pagination.
            batch = data.get("results", [])
            all_records.extend(batch)
            log(f"  -> {len(batch)} records this page (count so far: {len(all_records)})")
            url = data.get("next")
            params = None  # `next` is already a full URL with params baked in
        else:
            # Unknown shape -- stop and dump it raw rather than guess wrong.
            log("  -> Unrecognized response shape (not a list, no 'results' key).")
            log(f"  Top-level keys: {list(data.keys()) if isinstance(data, dict) else type(data)}")
            all_records.append(data)
            url = None

    if page >= MAX_PAGES:
        log(f"WARNING: hit MAX_PAGES ({MAX_PAGES}) -- may not have fetched everything.")

    return all_records


def summarize(records: list) -> str:
    """PII-free aggregate summary -- this is what's safe to print to a
    public-ish console log / job summary. No names, emails, or individual
    records here."""
    lines = []
    lines.append("## Second Nature Course Status — Test Pull Summary")
    lines.append("")
    lines.append(f"Run at {datetime.now(timezone.utc).isoformat()}")
    lines.append(f"Total records fetched: {len(records)}")
    lines.append("")

    if not records:
        lines.append("No records returned. Either the account has no course "
                      "activity yet, the filters excluded everything, or the "
                      "endpoint/auth is wrong despite a 200 — check the raw "
                      "artifact.")
        return "\n".join(lines)

    sample = records[0]
    if isinstance(sample, dict):
        lines.append(f"Fields present on each record: {sorted(sample.keys())}")
        lines.append("")

        course_ids = {r.get("course_id") for r in records if isinstance(r, dict) and "course_id" in r}
        lines.append(f"Distinct course_id values seen: {len(course_ids)}")

        if all(isinstance(r, dict) and "passed" in r for r in records):
            passed_count = sum(1 for r in records if r.get("passed") is True)
            lines.append(f"Records with passed=true: {passed_count} / {len(records)}")

        dates = sorted(
            r.get("completed_at") for r in records
            if isinstance(r, dict) and r.get("completed_at")
        )
        if dates:
            lines.append(f"completed_at range: {dates[0]} to {dates[-1]}")
    else:
        lines.append(f"Unexpected record type: {type(sample)} — see raw artifact.")

    lines.append("")
    lines.append(
        f"Full response (including real user data) saved to `{RAW_OUTPUT_PATH}` "
        "and uploaded as a short-retention build artifact. Do not commit that "
        "file to the repo."
    )
    return "\n".join(lines)


def main() -> None:
    try:
        records = fetch_course_status()
    except requests.exceptions.RequestException as e:
        log(f"Request failed: {e}")
        sys.exit(1)

    with open(RAW_OUTPUT_PATH, "w") as f:
        json.dump(records, f, indent=2, default=str)

    summary = summarize(records)
    log("\n" + summary)

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a") as f:
            f.write(summary)


if __name__ == "__main__":
    main()
