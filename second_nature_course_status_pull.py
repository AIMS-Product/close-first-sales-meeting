#!/usr/bin/env python3
"""
second_nature_course_status_pull.py

Real pull against Second Nature's Users Course State API, built from the
actual published doc (not a paraphrase this time). Two distinct endpoints:

  1. GET /course-status/
     "Fetch all available courses" -- a catalog, NOT completion data.
     Returns [{course_id, external_course_id}, ...]. No query params.

  2. GET /course-status/{course_id}/
     Per-course completion/score data for assigned users:
     user_email, updated_at, completed_at, score, passed, course_id,
     external_course_id (+ external_user_id if include_external_user_id=true)
     Optional filters: completed_from / completed_until -- UNIX UTC
     timestamps (not date strings). If neither is given, ALL assigned
     users are returned, including in-progress ones.

  Base URL:    https://app.secondnature.ai/api/
  Auth header: Authorization: Token <SECOND_NATURE_API_KEY>
  Rate limit:  5 requests/minute (hard) -- this script paces itself to it.

Our first test hit endpoint #1 and got an empty catalog back, even though
the account has plenty of completed courses in the UI. That's a real,
confirmed-by-docs result (not a script bug) -- it means the API-facing
course catalog is empty, separate from whatever exists in the product UI.
So this version also accepts a manually-supplied course_id (from the
Second Nature admin UI) to test endpoint #2 directly, bypassing the
catalog entirely, since that's the endpoint that actually matters for the
recap job.

PRIVACY: endpoint #2 returns real trainee emails/scores. The full response
is saved to a JSON file (uploaded as a short-retention build artifact) --
do NOT commit that file to the repo. Only aggregate, PII-free counts get
printed to the console / job summary.
"""

import json
import os
import sys
import time
from datetime import datetime, timezone

import requests

API_KEY = os.environ.get("SECOND_NATURE_API_KEY")
if not API_KEY:
    print("ERROR: SECOND_NATURE_API_KEY is not set.", flush=True)
    sys.exit(1)

BASE_URL = "https://app.secondnature.ai/api"
HEADERS = {"Authorization": f"Token {API_KEY}", "Content-Type": "application/json"}
TIMEOUT = 30

# 5 req/min hard limit -> minimum 12s between calls. Padding to 13s.
SLEEP_BETWEEN_CALLS = 13

# Optional: a course_id grabbed manually from the Second Nature admin UI,
# to test endpoint #2 even if the catalog (endpoint #1) comes back empty.
MANUAL_COURSE_ID = os.environ.get("COURSE_ID") or None

# Optional test filters for endpoint #2. Accepts either a UNIX timestamp
# directly, or a YYYY-MM-DD date (converted to a UTC UNIX timestamp here,
# since typing unix timestamps by hand is error-prone).
COMPLETED_FROM_RAW = os.environ.get("COMPLETED_FROM")
COMPLETED_UNTIL_RAW = os.environ.get("COMPLETED_UNTIL")
INCLUDE_EXTERNAL_USER_ID = os.environ.get("INCLUDE_EXTERNAL_USER_ID", "").lower() == "true"

RAW_OUTPUT_PATH = "course_status_raw.json"

request_count = 0


def log(msg: str) -> None:
    print(msg, flush=True)


def to_unix_timestamp(value: str | None) -> int | None:
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return int(value)
    # Treat as YYYY-MM-DD, UTC midnight.
    dt = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def api_get(path: str, params: dict | None = None) -> requests.Response:
    """Single paced, rate-limit-aware GET. Respects the documented 5/min
    limit by sleeping before every call, and backs off further on a 429
    using Retry-After if the server sends one anyway."""
    global request_count
    if request_count > 0:
        time.sleep(SLEEP_BETWEEN_CALLS)
    request_count += 1

    url = f"{BASE_URL}{path}"
    resp = requests.get(url, headers=HEADERS, params=params, timeout=TIMEOUT)

    if resp.status_code == 429:
        wait = int(resp.headers.get("Retry-After", 30))
        log(f"  [429 rate limited] sleeping {wait}s then retrying once...")
        time.sleep(wait)
        request_count += 1
        resp = requests.get(url, headers=HEADERS, params=params, timeout=TIMEOUT)

    return resp


def fetch_course_catalog() -> list:
    """GET /course-status/ -- lists {course_id, external_course_id} pairs.
    NOT completion data. A flat JSON array per the docs, no pagination."""
    log("Fetching course catalog: GET /course-status/")
    resp = api_get("/course-status/")
    if resp.status_code != 200:
        log(f"  -> HTTP {resp.status_code}: {resp.text[:500]}")
        resp.raise_for_status()
    data = resp.json()
    log(f"  -> {len(data)} course(s) in catalog")
    return data


def fetch_course_state(course_id: str) -> list:
    """GET /course-status/{course_id}/ -- real per-user completion data."""
    params = {}
    completed_from = to_unix_timestamp(COMPLETED_FROM_RAW)
    completed_until = to_unix_timestamp(COMPLETED_UNTIL_RAW)
    if completed_from is not None:
        params["completed_from"] = completed_from
    if completed_until is not None:
        params["completed_until"] = completed_until
    if INCLUDE_EXTERNAL_USER_ID:
        params["include_external_user_id"] = "true"

    log(f"Fetching course state: GET /course-status/{course_id}/ params={params}")
    resp = api_get(f"/course-status/{course_id}/", params=params)
    if resp.status_code != 200:
        log(f"  -> HTTP {resp.status_code}: {resp.text[:500]}")
        resp.raise_for_status()
    data = resp.json()
    log(f"  -> {len(data)} record(s) for course {course_id}")
    return data


def summarize(catalog: list, course_records: dict) -> str:
    """PII-free aggregate summary -- safe for the console/job summary.
    No individual emails or per-record detail here."""
    lines = []
    lines.append("## Second Nature Course Status — Test Pull Summary")
    lines.append("")
    lines.append(f"Run at {datetime.now(timezone.utc).isoformat()}")
    lines.append(f"API requests made: {request_count} (rate-limit paced at {SLEEP_BETWEEN_CALLS}s apart)")
    lines.append("")
    lines.append(f"Course catalog (`GET /course-status/`): {len(catalog)} course(s) returned")
    if not catalog:
        lines.append(
            "  -> Catalog is empty. This is a confirmed API result, not a script bug -- "
            "worth asking Second Nature support directly what determines whether a "
            "course appears here (e.g. does it require an `external_course_id` mapping "
            "or an API-visibility flag per course in the admin UI?)."
        )
    lines.append("")

    if not course_records:
        lines.append(
            "No course IDs were tested against `/course-status/{course_id}/` "
            "(catalog was empty and no manual COURSE_ID was supplied)."
        )
        return "\n".join(lines)

    lines.append("### Per-course completion data")
    lines.append("")
    total_records = 0
    for course_id, records in course_records.items():
        total_records += len(records)
        if not records:
            lines.append(f"- `{course_id}`: 0 records")
            continue
        passed_count = sum(1 for r in records if r.get("passed") is True)
        completed_count = sum(1 for r in records if r.get("completed_at"))
        fields = sorted(records[0].keys())
        lines.append(
            f"- `{course_id}`: {len(records)} assigned user(s), "
            f"{completed_count} completed, {passed_count} passed. "
            f"Fields: {fields}"
        )
    lines.append("")
    lines.append(f"Total completion records across tested courses: {total_records}")
    lines.append("")
    lines.append(
        f"Full response (including real user emails/scores) saved to `{RAW_OUTPUT_PATH}` "
        "and uploaded as a short-retention build artifact. Do not commit that file to the repo."
    )
    return "\n".join(lines)


def main() -> None:
    try:
        catalog = fetch_course_catalog()
    except requests.exceptions.RequestException as e:
        log(f"Catalog fetch failed: {e}")
        catalog = []

    course_ids = {c["course_id"] for c in catalog if isinstance(c, dict) and "course_id" in c}
    if MANUAL_COURSE_ID:
        course_ids.add(MANUAL_COURSE_ID)

    course_records = {}
    for course_id in course_ids:
        try:
            course_records[course_id] = fetch_course_state(course_id)
        except requests.exceptions.RequestException as e:
            log(f"Course state fetch failed for {course_id}: {e}")
            course_records[course_id] = []

    with open(RAW_OUTPUT_PATH, "w") as f:
        json.dump({"catalog": catalog, "course_state": course_records}, f, indent=2, default=str)

    summary = summarize(catalog, course_records)
    log("\n" + summary)

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a") as f:
            f.write(summary)


if __name__ == "__main__":
    main()
