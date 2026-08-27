#!/usr/bin/env python3
"""
no_show_recovery.py

Keeps a temporary "No show recovery" flag on Close leads.

Rules:
  - Look at meetings whose start time is in the last WINDOW_HOURS hours.
  - If any of those meetings has Close's native Meeting Outcome = No Show,
    and the lead is not Do Not Contact / Closed Won / Disqualified /
    Outside the US, and the lead has no upcoming meetings booked, set:
      * No show recovery = Yes
      * Lead Owner = Reactivation Setter User when present
      * Lead Owner = the Close user matching Reactivation - Setter Name as fallback
      * Lead Owner = blank when both setter fields are blank/unmapped
  - If a lead currently has No show recovery = Yes but no longer qualifies,
    set No show recovery = No so it can reappear on the normal SOP lists.
    Booking a new meeting makes the lead no longer qualify immediately.

The owner reassignment is deliberately one-way. When a lead ages out of the
window, this script clears only the recovery flag; it does not guess the prior
owner.

Usage:
  python no_show_recovery.py                       # dry run
  python no_show_recovery.py --apply               # write changes
  python no_show_recovery.py --window-hours 48
  python no_show_recovery.py --discover-lane2-users
  python no_show_recovery.py --selftest            # pure logic, no network

Env vars:
  CLOSE_API_KEY                 required unless --selftest
  NO_SHOW_RECOVERY_FIELD_ID     optional; defaults to the known field below
  NO_SHOW_RECOVERY_FIELD_NAME   optional; default "No show recovery"
  REACTIVATION_SETTER_ID        optional fallback user id if explicitly desired
  REACTIVATION_SETTER_NAME      optional fallback user name if explicitly desired
  DRY_RUN                       "1" keeps dry-run, "0" applies
  WINDOW_HOURS                  default 48
"""

import argparse
import base64
import json
import os
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

BASE = "https://api.close.com/api/v1"

OUTCOME_NO_SHOW = "outcome_032DjoyPo9BgPBdOF6DzqH"

LEAD_OWNER_FIELD = "cf_gOfS9pFwext58oberEegLyix8hZzeHrxhCZOVh3P3rd"
NO_SHOW_RECOVERY_FIELD = "cf_ye2V9PkBCqUZLfNo2AVoe82m7riZUTCbBYPVhvhou6x"
REACTIVATION_SETTER_FIELD = "cf_vz6kNiu4ItFxRA8Y9HKlWIoQMq3TsdaQqKekQ2YuxVk"
REACTIVATION_SETTER_USER_FIELD = "cf_7W3UCpJWWaIQsniF1upSxGO7rMX1yDT5qppHXBGJIhO"

STATUS_CLOSED_WON = "stat_0oW3iRpVp9z5DJq0cuwI1HgR0XhHAhykEPPIq4TFsxd"
STATUS_DNC = "stat_U9MI7pqsvIjceTv3pCU7b1EghO8Q83h1HUcL6fGVyi6"
STATUS_DQ = "stat_p3oblSTnbsyDAw4rWqZDePGYMOlKBgV2FjbqIMDrfvF"
STATUS_OUTSIDE_US = "stat_YV4ZngDB4IGjLjlOf0YTFEWuKZJ6fhNxVkzQkvKYfdB"
SUPPRESSED_STATUSES = {
    STATUS_CLOSED_WON,
    STATUS_DNC,
    STATUS_DQ,
    STATUS_OUTSIDE_US,
}

DEFAULT_FIELD_NAME = "No show recovery"
DEFAULT_WINDOW_HOURS = 48
WRITE_WORKERS = 8

LANE2_REP_NAMES = [
    "Charlie Ingram",
    "Jacob Hepner",
    "Vince Bartolini",
    "Pearl Sathekge",
    "Kelly Schrader",
    "Jacob Herbig",
    "William Nowak",
    "August Young",
    "Spencer Reynolds",
    "Amy Mulch",
    "Cassie Caraballo",
    "Jessica Zatkin",
    "Abigail Garza",
    "Connor George",
    "Dana Lesiuk",
    "Naria Torres",
    "Melia King",
]


class CloseError(Exception):
    pass


class CloseClient:
    def __init__(self, api_key):
        self.api_key = api_key
        try:
            import requests
        except ModuleNotFoundError:
            requests = None
        self.requests = requests
        self.session = None
        if requests:
            self.session = requests.Session()
            self.session.auth = (api_key, "")
            self.session.headers["Content-Type"] = "application/json"

    def request(self, method, path, **kwargs):
        url = f"{BASE}{path}"
        if not self.requests:
            return self.urllib_request(method, url, **kwargs)

        last = None
        for attempt in range(6):
            resp = self.session.request(method, url, timeout=60, **kwargs)
            last = resp
            if resp.status_code == 429:
                time.sleep(float(resp.headers.get("Retry-After", 2)) + attempt)
                continue
            if resp.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            if resp.status_code >= 400:
                try:
                    detail = resp.json()
                except ValueError:
                    detail = resp.text[:1000]
                raise CloseError(f"{resp.status_code} {method} {path}: {detail}")
            return resp.json() if resp.content else {}
        raise CloseError(f"gave up after retries: {method} {path} ({last.status_code})")

    def urllib_request(self, method, url, **kwargs):
        from urllib.error import HTTPError
        from urllib.parse import urlencode
        from urllib.request import Request, urlopen

        params = kwargs.get("params")
        if params:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}{urlencode(params)}"

        body = kwargs.get("json")
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")

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
                    if not raw:
                        return {}
                    return json.loads(raw.decode("utf-8"))
            except HTTPError as exc:
                last_status = exc.code
                if exc.code == 429:
                    retry = exc.headers.get("Retry-After")
                    time.sleep(float(retry or 2) + attempt)
                    continue
                if exc.code >= 500:
                    time.sleep(2 ** attempt)
                    continue
                detail = exc.read().decode("utf-8", errors="replace")[:1000]
                raise CloseError(f"{exc.code} {method} {url}: {detail}")
        raise CloseError(f"gave up after retries: {method} {url} ({last_status})")

    def get(self, path, params=None):
        return self.request("GET", path, params=params)

    def put(self, path, payload):
        return self.request("PUT", path, json=payload)

    def post(self, path, payload):
        return self.request("POST", path, json=payload)


def parse_dt(value):
    if not value:
        return None
    text = str(value).replace(" ", "T").replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def custom_value(lead, field_id):
    direct = lead.get(f"custom.{field_id}")
    if direct is not None:
        return direct
    custom = lead.get("custom") or {}
    return custom.get(field_id) or custom.get(f"custom.{field_id}")


def user_field_id(value):
    if isinstance(value, dict):
        return value.get("id") or None
    if isinstance(value, list):
        ids = [user_field_id(item) for item in value]
        ids = [uid for uid in ids if uid]
        return ",".join(ids) if ids else None
    if isinstance(value, str):
        return value.strip() or None
    return None


def normalize_name(value):
    return " ".join(str(value or "").lower().split())


def env_value(name, default=None):
    value = os.environ.get(name)
    return value if value else default


def find_custom_field_id(client, field_name):
    skip = 0
    while True:
        data = client.get("/custom_field/lead/", {"_skip": skip, "_limit": 100})
        for field in data.get("data", []):
            if normalize_name(field.get("name")) == normalize_name(field_name):
                return field["id"]
        if not data.get("has_more"):
            break
        skip += 100
    return None


def fetch_users(client):
    users, skip = [], 0
    while True:
        data = client.get("/user/", {"_skip": skip, "_limit": 100})
        users.extend(data.get("data", []))
        if not data.get("has_more"):
            break
        skip += 100
    return users


def user_display_name(user):
    full = f"{user.get('first_name', '')} {user.get('last_name', '')}".strip()
    return full or user.get("email") or user.get("id")


def lane2_user_map(client):
    users = fetch_users(client)
    by_name = {normalize_name(user_display_name(user)): user for user in users}
    out = {}
    missing = []
    for name in LANE2_REP_NAMES:
        user = by_name.get(normalize_name(name))
        if user:
            out[name] = {
                "id": user["id"],
                "name": user_display_name(user),
                "email": user.get("email") or "",
            }
        else:
            missing.append(name)
    return out, missing


def print_lane2_user_discovery(user_map, missing):
    print("Lane 2 Close user map")
    print("-" * 88)
    print(f"{'Name':<22} {'User ID':<48} Email")
    print("-" * 88)
    for name in LANE2_REP_NAMES:
        row = user_map.get(name)
        if row:
            print(f"{name:<22} {row['id']:<48} {row['email']}")
        else:
            print(f"{name:<22} {'MISSING':<48}")
    if missing:
        print("\nMissing from Close user discovery: " + ", ".join(missing))
    else:
        print("\nAll Lane 2 names matched exactly in Close.")


def resolve_reactivation_setter(client, user_id=None, user_name=None):
    if user_id:
        user = client.get(f"/user/{user_id}/")
        return user_id, user_display_name(user)

    if not user_name:
        return None, None

    want = normalize_name(user_name)
    matches = []
    for user in fetch_users(client):
        full = normalize_name(user_display_name(user))
        email = normalize_name(user.get("email"))
        if want in {full, email}:
            matches.append(user)

    if len(matches) == 1:
        user = matches[0]
        return user["id"], user_display_name(user)
    if not matches:
        raise CloseError(f"could not find reactivation setter named {user_name!r}")
    raise CloseError(
        f"reactivation setter name {user_name!r} matched multiple users: "
        + ", ".join(user_display_name(u) for u in matches)
    )


def fetch_meetings_window(client, since_dt, until_dt):
    fields = "id,lead_id,title,starts_at,date_created,outcome_id,status"
    out, skip = [], 0
    while True:
        data = client.get(
            "/activity/meeting/",
            {"_skip": skip, "_limit": 100, "_fields": fields},
        )
        rows = data.get("data", [])
        if not rows:
            break

        page_all_old = True
        for meeting in rows:
            starts_at = parse_dt(meeting.get("starts_at") or meeting.get("date_created"))
            if not starts_at:
                continue
            if starts_at >= since_dt:
                page_all_old = False
                if starts_at <= until_dt:
                    out.append(meeting)

        if page_all_old or not data.get("has_more"):
            break
        skip += 100
    return out


def fetch_leads(client, lead_ids, recovery_field_id):
    fields = (
        f"id,display_name,status_id,status_label,num_upcoming_meetings,custom.{LEAD_OWNER_FIELD},"
        f"custom.{recovery_field_id},custom.{REACTIVATION_SETTER_FIELD},custom.{REACTIVATION_SETTER_USER_FIELD}"
    )
    leads = {}
    for lead_id in sorted(lead_ids):
        leads[lead_id] = client.get(f"/lead/{lead_id}/", {"_fields": fields})
    return leads


def field_condition(field, condition, negate=False):
    return {
        "type": "field_condition",
        "negate": negate,
        "field": field,
        "condition": condition,
    }


def recovery_yes_query(recovery_field_id):
    return {
        "negate": False,
        "type": "and",
        "queries": [
            {"negate": False, "object_type": "lead", "type": "object_type"},
            field_condition(
                {"type": "custom_field", "custom_field_id": recovery_field_id},
                {"type": "term", "values": ["Yes"]},
            ),
        ],
    }


def search_recovery_yes_leads(client, recovery_field_id):
    out, cursor = [], None
    fields = [
        "id",
        "display_name",
        "status_id",
        "status_label",
        "num_upcoming_meetings",
        f"custom.{LEAD_OWNER_FIELD}",
        f"custom.{recovery_field_id}",
        f"custom.{REACTIVATION_SETTER_FIELD}",
        f"custom.{REACTIVATION_SETTER_USER_FIELD}",
    ]
    while True:
        payload = {
            "query": recovery_yes_query(recovery_field_id),
            "_fields": {"lead": fields},
            "_limit": 200,
        }
        if cursor:
            payload["cursor"] = cursor
        data = client.post("/data/search/", payload)
        out.extend(data.get("data", []))
        cursor = data.get("cursor")
        if not cursor:
            return out


def eligible_no_show_lead_ids(meetings):
    by_lead = defaultdict(list)
    for meeting in meetings:
        if meeting.get("lead_id") and meeting.get("outcome_id") == OUTCOME_NO_SHOW:
            by_lead[meeting["lead_id"]].append(meeting)
    return set(by_lead), by_lead


def lead_is_suppressed(lead):
    return lead.get("status_id") in SUPPRESSED_STATUSES


def lead_has_upcoming_meeting(lead):
    try:
        return int(lead.get("num_upcoming_meetings") or 0) > 0
    except (TypeError, ValueError):
        return False


def owner_target_for_lead(lead, lane2_ids_by_name, fallback_setter_id=None):
    setter_user_id = user_field_id(custom_value(lead, REACTIVATION_SETTER_USER_FIELD))
    if setter_user_id:
        return setter_user_id, "Reactivation Setter User"

    setter_name = custom_value(lead, REACTIVATION_SETTER_FIELD)
    if setter_name in lane2_ids_by_name:
        return lane2_ids_by_name[setter_name], setter_name
    if fallback_setter_id:
        return fallback_setter_id, "fallback"
    return None, setter_name


def plan_writes(
    no_show_lead_ids,
    leads_by_id,
    recovery_yes_leads,
    recovery_field_id,
    lane2_ids_by_name,
    fallback_setter_id=None,
):
    current_yes_ids = {lead["id"] for lead in recovery_yes_leads}
    eligible_ids = {
        lead_id
        for lead_id in no_show_lead_ids
        if (
            lead_id in leads_by_id
            and not lead_is_suppressed(leads_by_id[lead_id])
            and not lead_has_upcoming_meeting(leads_by_id[lead_id])
        )
    }
    released_unresolved = {}

    writes = []
    for lead_id in sorted(eligible_ids):
        lead = leads_by_id[lead_id]
        payload = {}
        if custom_value(lead, recovery_field_id) != "Yes":
            payload[f"custom.{recovery_field_id}"] = "Yes"
        owner_id, owner_label = owner_target_for_lead(lead, lane2_ids_by_name, fallback_setter_id)
        if owner_id and custom_value(lead, LEAD_OWNER_FIELD) != owner_id:
            payload[f"custom.{LEAD_OWNER_FIELD}"] = owner_id
        elif not owner_id and custom_value(lead, LEAD_OWNER_FIELD):
            payload[f"custom.{LEAD_OWNER_FIELD}"] = None
            released_unresolved[lead_id] = owner_label
        elif not owner_id:
            released_unresolved[lead_id] = owner_label
        if payload:
            writes.append((lead, payload, "activate"))

    for lead in recovery_yes_leads:
        if lead["id"] not in eligible_ids:
            writes.append((lead, {f"custom.{recovery_field_id}": "No"}, "clear"))

    return eligible_ids, writes, current_yes_ids, released_unresolved


def print_plan_summary(
    meetings,
    no_show_by_lead,
    leads_by_id,
    recovery_yes_leads,
    eligible_ids,
    writes,
    recovery_field_id,
    released_unresolved_ids,
    setter_label,
    dry_run,
):
    suppressed = [
        lead
        for lead_id, lead in leads_by_id.items()
        if lead_id in no_show_by_lead and lead_is_suppressed(lead)
    ]
    booked = [
        lead
        for lead_id, lead in leads_by_id.items()
        if (
            lead_id in no_show_by_lead
            and not lead_is_suppressed(lead)
            and lead_has_upcoming_meeting(lead)
        )
    ]
    by_action = Counter(action for _, _, action in writes)
    flag_yes = sum(1 for _, payload, _ in writes if payload.get(f"custom.{recovery_field_id}") == "Yes")
    flag_no = sum(1 for _, payload, _ in writes if payload.get(f"custom.{recovery_field_id}") == "No")
    owner_moves = sum(1 for _, payload, _ in writes if f"custom.{LEAD_OWNER_FIELD}" in payload)

    print(f"Meetings scanned               : {len(meetings):,}")
    print(f"Lead(s) with no-show meeting   : {len(no_show_by_lead):,}")
    print(f"Suppressed current lead status : {len(suppressed):,}")
    print(f"Already booked upcoming meeting: {len(booked):,}")
    print(f"Eligible recovery lead(s)      : {len(eligible_ids):,}")
    print(f"Currently marked recovery Yes  : {len(recovery_yes_leads):,}")
    print(f"Will set recovery Yes          : {flag_yes:,}")
    print(f"Will clear recovery to No      : {flag_no:,}")
    print(f"Will move Lead Owner           : {owner_moves:,}")
    print(f"Release blank/unmapped owner   : {len(released_unresolved_ids):,}")
    print(f"Fallback setter                : {setter_label or '(not configured)'}")
    print(f"Mode                           : {'DRY RUN' if dry_run else 'APPLY'}")

    if released_unresolved_ids:
        counts = Counter(value or "(blank)" for value in released_unresolved_ids.values())
        print("\nReleased Reactivation - Setter Name values:")
        for value, count in counts.most_common():
            print(f"  {value}: {count:,}")

    if dry_run and writes:
        print("\nSample planned writes:")
        for lead, payload, action in writes[:10]:
            bits = []
            if f"custom.{LEAD_OWNER_FIELD}" in payload:
                bits.append("owner")
            if any(k != f"custom.{LEAD_OWNER_FIELD}" for k in payload):
                bits.append("flag")
            print(
                f"  {action:<8} {lead.get('display_name', '?')[:35]:<35} "
                f"{lead.get('id')} ({', '.join(bits)})"
            )


def apply_writes(client, writes):
    ok = err = 0
    errors = []

    def write_one(item):
        lead, payload, action = item
        try:
            client.put(f"/lead/{lead['id']}/", payload)
            return True, None
        except Exception as exc:
            return False, f"{action} {lead['id']}: {exc}"

    with ThreadPoolExecutor(max_workers=WRITE_WORKERS) as executor:
        for good, message in executor.map(write_one, writes):
            if good:
                ok += 1
            else:
                err += 1
                if len(errors) < 10:
                    errors.append(message)

    print(f"\nDone. {ok:,} lead(s) updated, {err:,} failed.")
    for message in errors:
        print(f"  {message}", file=sys.stderr)
    if err:
        raise SystemExit(1)


def run_selftest():
    global recovery_field_id
    recovery_field_id = "cf_recovery"
    leads = {
        "lead_active": {
            "id": "lead_active",
            "status_id": "stat_ok",
            "num_upcoming_meetings": 0,
            f"custom.{LEAD_OWNER_FIELD}": "old_owner",
            f"custom.{recovery_field_id}": "No",
            f"custom.{REACTIVATION_SETTER_FIELD}": "Jacob Hepner",
        },
        "lead_suppressed": {
            "id": "lead_suppressed",
            "status_id": STATUS_DNC,
            "num_upcoming_meetings": 0,
            f"custom.{recovery_field_id}": "No",
        },
        "lead_blank_setter": {
            "id": "lead_blank_setter",
            "status_id": "stat_ok",
            "num_upcoming_meetings": 0,
            f"custom.{LEAD_OWNER_FIELD}": "closer",
            f"custom.{recovery_field_id}": "No",
            f"custom.{REACTIVATION_SETTER_FIELD}": "",
        },
        "lead_user_field": {
            "id": "lead_user_field",
            "status_id": "stat_ok",
            "num_upcoming_meetings": 0,
            f"custom.{LEAD_OWNER_FIELD}": "closer",
            f"custom.{recovery_field_id}": "No",
            f"custom.{REACTIVATION_SETTER_FIELD}": "",
            f"custom.{REACTIVATION_SETTER_USER_FIELD}": "shadow_setter",
        },
        "lead_already": {
            "id": "lead_already",
            "status_id": "stat_ok",
            "num_upcoming_meetings": 0,
            f"custom.{LEAD_OWNER_FIELD}": "setter",
            f"custom.{recovery_field_id}": "Yes",
            f"custom.{REACTIVATION_SETTER_FIELD}": "Jacob Hepner",
        },
        "lead_booked": {
            "id": "lead_booked",
            "status_id": "stat_ok",
            "num_upcoming_meetings": 1,
            f"custom.{LEAD_OWNER_FIELD}": "setter",
            f"custom.{recovery_field_id}": "Yes",
            f"custom.{REACTIVATION_SETTER_FIELD}": "Jacob Hepner",
        },
    }
    current_yes = [
        leads["lead_already"],
        leads["lead_booked"],
        {"id": "lead_stale", "status_id": "stat_ok", f"custom.{recovery_field_id}": "Yes"},
    ]
    lane2_ids = {
        "Charlie Ingram": "charlie",
        "Jacob Hepner": "setter",
    }
    eligible, writes, current, released = plan_writes(
        {"lead_active", "lead_suppressed", "lead_already", "lead_blank_setter", "lead_user_field", "lead_booked"},
        leads,
        current_yes,
        recovery_field_id,
        lane2_ids,
    )
    checks = [
        ("suppressed excluded", "lead_suppressed" not in eligible),
        ("booked excluded", "lead_booked" not in eligible),
        ("active included", "lead_active" in eligible),
        ("blank setter included", "lead_blank_setter" in eligible),
        ("user field included", "lead_user_field" in eligible),
        ("already included", "lead_already" in eligible),
        ("current yes seen", current == {"lead_already", "lead_booked", "lead_stale"}),
        ("blank setter released", released == {"lead_blank_setter": ""}),
        ("active gets owner and flag", any(
            lead["id"] == "lead_active"
            and payload == {
                f"custom.{recovery_field_id}": "Yes",
                f"custom.{LEAD_OWNER_FIELD}": "setter",
            }
            and action == "activate"
            for lead, payload, action in writes
        )),
        ("blank setter clears owner and flag", any(
            lead["id"] == "lead_blank_setter"
            and payload == {
                f"custom.{recovery_field_id}": "Yes",
                f"custom.{LEAD_OWNER_FIELD}": None,
            }
            and action == "activate"
            for lead, payload, action in writes
        )),
        ("user field gets owner and flag", any(
            lead["id"] == "lead_user_field"
            and payload == {
                f"custom.{recovery_field_id}": "Yes",
                f"custom.{LEAD_OWNER_FIELD}": "shadow_setter",
            }
            and action == "activate"
            for lead, payload, action in writes
        )),
        ("already has no write", not any(lead["id"] == "lead_already" for lead, _, _ in writes)),
        ("stale clears", any(
            lead["id"] == "lead_stale"
            and payload == {f"custom.{recovery_field_id}": "No"}
            and action == "clear"
            for lead, payload, action in writes
        )),
        ("booked clears", any(
            lead["id"] == "lead_booked"
            and payload == {f"custom.{recovery_field_id}": "No"}
            and action == "clear"
            for lead, payload, action in writes
        )),
    ]
    failed = [name for name, ok in checks if not ok]
    if failed:
        raise SystemExit("SELFTEST FAILED: " + ", ".join(failed))
    print("SELFTEST PASSED")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="write changes to Close")
    parser.add_argument("--window-hours", type=int, default=int(env_value("WINDOW_HOURS", DEFAULT_WINDOW_HOURS)))
    parser.add_argument("--field-id", default=env_value("NO_SHOW_RECOVERY_FIELD_ID", NO_SHOW_RECOVERY_FIELD))
    parser.add_argument("--field-name", default=env_value("NO_SHOW_RECOVERY_FIELD_NAME", DEFAULT_FIELD_NAME))
    parser.add_argument("--reactivation-setter-id", default=env_value("REACTIVATION_SETTER_ID"))
    parser.add_argument("--reactivation-setter-name", default=env_value("REACTIVATION_SETTER_NAME"))
    parser.add_argument("--discover-lane2-users", action="store_true")
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args()

    if args.selftest:
        run_selftest()
        return

    api_key = os.environ.get("CLOSE_API_KEY")
    if not api_key:
        sys.exit("Set CLOSE_API_KEY first.")

    dry_run = not args.apply or os.environ.get("DRY_RUN", "1") != "0"
    if args.apply:
        dry_run = False

    client = CloseClient(api_key)

    lane2_users, missing_lane2_users = lane2_user_map(client)
    if args.discover_lane2_users:
        print_lane2_user_discovery(lane2_users, missing_lane2_users)
        if missing_lane2_users:
            raise SystemExit(1)
        return

    lane2_ids_by_name = {name: row["id"] for name, row in lane2_users.items()}
    if missing_lane2_users:
        print(
            "WARNING: some Lane 2 names did not match Close users: "
            + ", ".join(missing_lane2_users),
            file=sys.stderr,
        )

    recovery_field_id = args.field_id or find_custom_field_id(client, args.field_name)
    if not recovery_field_id:
        sys.exit(
            f"Could not find lead custom field {args.field_name!r}. "
            "Create it in Close or set NO_SHOW_RECOVERY_FIELD_ID."
        )

    setter_id, setter_label = resolve_reactivation_setter(
        client,
        user_id=args.reactivation_setter_id,
        user_name=args.reactivation_setter_name,
    )
    if not setter_id:
        print(
            "No fallback reactivation setter configured; leads with a blank/unknown "
            "Reactivation - Setter Name will have Lead Owner cleared.",
            file=sys.stderr,
        )

    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=args.window_hours)

    print(f"Scanning meetings from {since.isoformat()} to {now.isoformat()}...")
    meetings = fetch_meetings_window(client, since, now)
    no_show_ids, no_show_by_lead = eligible_no_show_lead_ids(meetings)
    print(f"Fetching {len(no_show_ids):,} lead(s) with recent no-show meetings...")
    leads_by_id = fetch_leads(client, no_show_ids, recovery_field_id)
    print("Fetching leads currently marked No show recovery = Yes...")
    recovery_yes_leads = search_recovery_yes_leads(client, recovery_field_id)

    eligible_ids, writes, _, unresolved = plan_writes(
        no_show_ids,
        leads_by_id,
        recovery_yes_leads,
        recovery_field_id,
        lane2_ids_by_name,
        setter_id,
    )

    print()
    print_plan_summary(
        meetings,
        no_show_by_lead,
        leads_by_id,
        recovery_yes_leads,
        eligible_ids,
        writes,
        recovery_field_id,
        unresolved,
        setter_label,
        dry_run,
    )

    if dry_run:
        print("\nDRY RUN - nothing written. Re-run with --apply.")
        return

    if not writes:
        print("\nNo changes needed.")
        return

    print(f"\nWriting {len(writes):,} lead update(s) with {WRITE_WORKERS} workers...")
    apply_writes(client, writes)


if __name__ == "__main__":
    main()
