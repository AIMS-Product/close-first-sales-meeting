#!/usr/bin/env python3
"""
Sync audit for Reactivation setter shadow user field.

Compares the existing text/dropdown lead field:
  Reactivation - Setter Name
  cf_vz6kNiu4ItFxRA8Y9HKlWIoQMq3TsdaQqKekQ2YuxVk

to the new user-type lead field:
  Reactivation Setter User
  cf_7W3UCpJWWaIQsniF1upSxGO7rMX1yDT5qppHXBGJIhO

Dry-run is the default. With --apply, this script only writes the new user field
and only for leads where that field is currently blank and the old name maps
cleanly to one Close user.

Usage:
  python3 dry_run_reactivation_setter_user_sync.py
  python3 dry_run_reactivation_setter_user_sync.py --limit 25
  python3 dry_run_reactivation_setter_user_sync.py --csv reactivation_setter_user_sync.csv
  python3 dry_run_reactivation_setter_user_sync.py --apply
"""

from __future__ import annotations

import argparse
import base64
import csv
import json
import os
import sys
import time
from collections import Counter
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BASE = "https://api.close.com/api/v1"

OLD_SETTER_NAME_FIELD = "cf_vz6kNiu4ItFxRA8Y9HKlWIoQMq3TsdaQqKekQ2YuxVk"
NEW_SETTER_USER_FIELD = "cf_7W3UCpJWWaIQsniF1upSxGO7rMX1yDT5qppHXBGJIhO"


class CloseError(Exception):
    pass


class CloseClient:
    def __init__(self, api_key: str):
        self.api_key = api_key

    def request(self, method: str, path: str, params: dict | None = None, body: dict | None = None):
        url = f"{BASE}{path}"
        if params:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}{urlencode(params)}"

        data = json.dumps(body).encode("utf-8") if body is not None else None
        token = base64.b64encode(f"{self.api_key}:".encode("utf-8")).decode("ascii")
        headers = {
            "Authorization": f"Basic {token}",
            "Content-Type": "application/json",
        }

        last_status = None
        for attempt in range(6):
            req = Request(url, data=data, headers=headers, method=method)
            try:
                with urlopen(req, timeout=60) as resp:
                    raw = resp.read()
                    return json.loads(raw.decode("utf-8")) if raw else {}
            except HTTPError as exc:
                last_status = exc.code
                if exc.code == 429:
                    time.sleep(float(exc.headers.get("Retry-After") or 2) + attempt)
                    continue
                if exc.code >= 500:
                    time.sleep(2**attempt)
                    continue
                detail = exc.read().decode("utf-8", errors="replace")[:1000]
                raise CloseError(f"{exc.code} {method} {path}: {detail}") from exc
        raise CloseError(f"gave up after retries: {method} {path} ({last_status})")

    def get(self, path: str, params: dict | None = None):
        return self.request("GET", path, params=params)

    def post(self, path: str, body: dict):
        return self.request("POST", path, body=body)

    def put(self, path: str, body: dict):
        return self.request("PUT", path, body=body)


def normalize_name(value) -> str:
    return " ".join(str(value or "").lower().split())


def user_display_name(user: dict) -> str:
    full = f"{user.get('first_name', '')} {user.get('last_name', '')}".strip()
    return full or user.get("email") or user.get("id") or ""


def custom_value(lead: dict, field_id: str):
    direct = lead.get(f"custom.{field_id}")
    if direct is not None:
        return direct
    custom = lead.get("custom") or {}
    return custom.get(field_id) or custom.get(f"custom.{field_id}")


def user_field_id(value) -> str:
    if isinstance(value, dict):
        return value.get("id") or ""
    if isinstance(value, list):
        ids = [user_field_id(item) for item in value]
        return ",".join(i for i in ids if i)
    if isinstance(value, str):
        return value.strip()
    return ""


def fetch_users(client: CloseClient) -> list[dict]:
    users, skip = [], 0
    while True:
        data = client.get("/user/", {"_skip": skip, "_limit": 100})
        batch = data.get("data", [])
        users.extend(batch)
        if not data.get("has_more") or not batch:
            return users
        skip += len(batch)


def user_maps(users: list[dict]) -> tuple[dict[str, dict], dict[str, str]]:
    by_name = {}
    duplicate_names = set()
    id_to_name = {}

    for user in users:
        display = user_display_name(user)
        uid = user.get("id") or ""
        if uid:
            id_to_name[uid] = display
        key = normalize_name(display)
        if not key:
            continue
        if key in by_name:
            duplicate_names.add(key)
        by_name[key] = user

    for key in duplicate_names:
        by_name.pop(key, None)
    return by_name, id_to_name


def field_exists(field_id: str) -> dict:
    return {
        "type": "field_condition",
        "negate": False,
        "field": {"type": "custom_field", "custom_field_id": field_id},
        "condition": {"type": "exists"},
    }


def fetch_candidate_leads(client: CloseClient, limit: int | None = None) -> list[dict]:
    query = {
        "type": "and",
        "negate": False,
        "queries": [
            {"type": "object_type", "negate": False, "object_type": "lead"},
            {
                "type": "or",
                "negate": False,
                "queries": [
                    field_exists(OLD_SETTER_NAME_FIELD),
                    field_exists(NEW_SETTER_USER_FIELD),
                ],
            },
        ],
    }
    fields = [
        "id",
        "display_name",
        f"custom.{OLD_SETTER_NAME_FIELD}",
        f"custom.{NEW_SETTER_USER_FIELD}",
    ]
    leads, seen_ids, cursor = [], set(), None
    while True:
        body = {
            "query": query,
            "_fields": {"lead": fields},
            "_limit": 200,
        }
        if cursor:
            body["cursor"] = cursor
        data = client.post("/data/search/", body)
        for row in data.get("data", []):
            lead = row.get("lead") if isinstance(row, dict) and "lead" in row else row
            lead_id = (lead or {}).get("id")
            if lead and lead_id not in seen_ids:
                seen_ids.add(lead_id)
                leads.append(lead)
                if limit and len(leads) >= limit:
                    return leads
        cursor = data.get("cursor")
        if not cursor:
            return leads


def classify_lead(lead: dict, users_by_name: dict[str, dict], id_to_name: dict[str, str]) -> dict:
    old_name = str(custom_value(lead, OLD_SETTER_NAME_FIELD) or "").strip()
    current_user_raw = custom_value(lead, NEW_SETTER_USER_FIELD)
    current_user_id = user_field_id(current_user_raw)
    current_user_name = id_to_name.get(current_user_id, current_user_id)

    row = {
        "lead_id": lead.get("id") or "",
        "lead_name": lead.get("display_name") or "",
        "old_setter_name": old_name,
        "current_user_id": current_user_id,
        "current_user_name": current_user_name,
        "desired_user_id": "",
        "desired_user_name": "",
        "status": "",
    }

    if not old_name:
        row["status"] = "orphan_new_user" if current_user_id else "blank"
        return row

    user = users_by_name.get(normalize_name(old_name))
    if not user:
        row["status"] = "unmapped_name"
        return row

    desired_id = user.get("id") or ""
    desired_name = user_display_name(user)
    row["desired_user_id"] = desired_id
    row["desired_user_name"] = desired_name

    if not current_user_id:
        row["status"] = "would_set"
    elif current_user_id == desired_id:
        row["status"] = "already_synced"
    else:
        row["status"] = "would_correct"
    return row


def print_report(rows: list[dict], limit_examples: int, apply: bool = False) -> None:
    counts = Counter(row["status"] for row in rows)
    mode = "APPLY" if apply else "dry run"
    print(f"Reactivation Setter User sync {mode}")
    print("-" * 72)
    print(f"Candidates scanned       : {len(rows):,}")
    print(f"Already synced           : {counts['already_synced']:,}")
    print(f"Would set blank user     : {counts['would_set']:,}")
    print(f"Would correct mismatch   : {counts['would_correct']:,}")
    print(f"Unmapped old names       : {counts['unmapped_name']:,}")
    print(f"New user set, old blank  : {counts['orphan_new_user']:,}")
    print()
    if apply:
        print("Apply mode writes only the Reactivation Setter User field.")
    else:
        print("No writes were made.")

    for status, title in [
        ("would_set", "Would set"),
        ("would_correct", "Would correct"),
        ("unmapped_name", "Unmapped names"),
        ("orphan_new_user", "New user set but old name blank"),
    ]:
        examples = [row for row in rows if row["status"] == status][:limit_examples]
        if not examples:
            continue
        print()
        print(title)
        print("-" * len(title))
        for row in examples:
            print(
                f"{row['lead_id']} | {row['lead_name']} | "
                f"old={row['old_setter_name']!r} | "
                f"current={row['current_user_name'] or '(blank)'} | "
                f"desired={row['desired_user_name'] or '(none)'}"
            )


def write_csv(path: str, rows: list[dict]) -> None:
    fieldnames = [
        "status",
        "lead_id",
        "lead_name",
        "old_setter_name",
        "current_user_id",
        "current_user_name",
        "desired_user_id",
        "desired_user_name",
    ]
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def apply_sync(client: CloseClient, rows: list[dict]) -> tuple[int, int]:
    applied = 0
    errors = 0
    to_write = []
    seen_ids = set()
    for row in rows:
        if row["status"] != "would_set":
            continue
        if row["lead_id"] in seen_ids:
            continue
        seen_ids.add(row["lead_id"])
        to_write.append(row)
    print()
    print(f"Applying {len(to_write):,} new-user-field updates...")
    for idx, row in enumerate(to_write, start=1):
        payload = {f"custom.{NEW_SETTER_USER_FIELD}": row["desired_user_id"]}
        try:
            client.put(f"/lead/{row['lead_id']}/", payload)
            applied += 1
        except CloseError as exc:
            errors += 1
            print(f"  ERROR {row['lead_id']} | {row['lead_name']}: {exc}")

        if idx % 100 == 0:
            print(f"  applied {idx:,}/{len(to_write):,}...")

    print(f"Apply complete: {applied:,} updated, {errors:,} errors.")
    return applied, errors


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, help="scan only the first N candidate leads")
    parser.add_argument("--examples", type=int, default=20, help="examples to print per non-synced status")
    parser.add_argument("--csv", help="optional CSV path for full dry-run results")
    parser.add_argument("--apply", action="store_true", help="write only blank Reactivation Setter User values")
    args = parser.parse_args()

    api_key = os.environ.get("CLOSE_API_KEY")
    if not api_key:
        sys.exit("Set CLOSE_API_KEY first.")

    client = CloseClient(api_key)
    users_by_name, id_to_name = user_maps(fetch_users(client))
    leads = fetch_candidate_leads(client, limit=args.limit)
    rows = [classify_lead(lead, users_by_name, id_to_name) for lead in leads]

    print_report(rows, args.examples, apply=args.apply)
    if args.csv:
        write_csv(args.csv, rows)
        print()
        print(f"CSV written: {args.csv}")

    if args.apply:
        apply_sync(client, rows)


if __name__ == "__main__":
    main()
