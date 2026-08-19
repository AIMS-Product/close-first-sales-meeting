#!/usr/bin/env python3
"""
Read-only Lane 2 meeting outcome truth audit.

Pulls Close meetings for one Pacific date, filters to Lane 2 Next Steps title
rules, reads native Meeting Outcome, lead status, Attention disposition,
attendee statuses, and optional Zoom attendance. It prints decisions and totals
without writing to Close, GitHub, Zoom, or local data files.

Required:
  CLOSE_API_KEY

Optional for Zoom-backed decisions:
  ZOOM_ACCOUNT_ID ZOOM_CLIENT_ID ZOOM_CLIENT_SECRET

Usage:
  python3 temp_lane2_live_truth_audit.py --date 2026-08-17
  python3 temp_lane2_live_truth_audit.py --date 2026-08-17 --no-zoom
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import time as time_module
from collections import defaultdict
from datetime import datetime, time, timezone
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler, urlopen
from zoneinfo import ZoneInfo


PACIFIC = ZoneInfo("America/Los_Angeles")
BASE_URL = "https://api.close.com/api/v1"
ZOOM_API = "https://api.zoom.us/v2"
ORG_EMAIL_DOMAINS = ("@modern-amenities.com",)
CF_TODAYS_DISPOSITION = "custom.cf_n2QvikNfeZ0uWObMsyCJmnXnrbWNLGlSvYiKJTwxTqU"
MIN_ATTEND_SECONDS = int(os.environ.get("MIN_ATTEND_SECONDS", "300"))
HOST_MIN_SECONDS = int(os.environ.get("HOST_MIN_SECONDS", "600"))
ZOOM_AUTO_NOSHOW = os.environ.get("ZOOM_AUTO_NOSHOW", "1") != "0"

OUTCOMES = {
    "scheduled": "outcome_032DjlzDKpdXJZOzK4f7q3",
    "completed": "outcome_032Djn4dfeNuEoCunojA7K",
    "rescheduled": "outcome_032Djo72GJ2Lvw3Q296wxH",
    "no_show": "outcome_032DjoyPo9BgPBdOF6DzqH",
    "cancelled": "outcome_032DjpoQ9otqb8rGb7SIYt",
}
OUTCOME_ID_TO_KEY = {value: key for key, value in OUTCOMES.items()}
DISPOSITION_TO_OUTCOME = {
    "new call show": "completed",
    "follow up show": "completed",
    "reschedule show": "completed",
    "new call no show": "no_show",
    "follow up no show": "no_show",
    "reschedule no show": "no_show",
    "discovery - no show (setter)": "no_show",
    "canceled": "cancelled",
    "canceled - rescheduled": "rescheduled",
}
ZOOM_JOIN_RE = re.compile(r"zoom\.us/j/(\d{9,12})", re.I)
CALENDLY_CONF_RE = re.compile(
    r"(https?://(?:www\.)?calendly\.com/events/[0-9a-fA-F-]+/(zoom|google_meet))",
    re.I,
)

TITLE_RULES: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("charlie_ingram_vendingpreneurs_dash_next_steps_call", re.compile(r"vendingpren[eu]+rs?\s+-\s+next\s+steps\s+call", re.I), "Charlie Ingram"),
    ("jacob_hepner_vendingpreneurs_call_dash_next_steps", re.compile(r"vendingpren[eu]+rs?\s+call\s+-\s+next\s+steps", re.I), "Jacob Hepner"),
    ("vince_bartolini_vendingpreneurs_next_steps_call", re.compile(r"vendingpren[eu]+rs?\s+next\s+steps\s+call", re.I), "Vince Bartolini"),
    ("pearl_sathekge_vendingpreneurs_next_steps_session", re.compile(r"vendingpren[eu]+rs?\s+next\s+steps\s+session", re.I), "Pearl Sathekge"),
    ("kelly_schrader_vendingpreneurs_discovery_dash_next_steps", re.compile(r"vendingpren[eu]+rs?\s+discovery\s+-\s+next\s+steps", re.I), "Kelly Schrader"),
    ("jacob_herbig_vendingpreneurs_dash_next_steps_not_call", re.compile(r"vendingpren[eu]+rs?\s+-\s+next\s+steps(?!\s+call)", re.I), "Jacob Herbig"),
    ("william_nowak_vendingpreneur_next_steps", re.compile(r"vendingpren[eu]+r\s+next\s+steps", re.I), "William Nowak"),
    ("august_young_vending_discovery_call_dash_next_steps", re.compile(r"vending\s+discovery\s+call\s+-\s+next\s+steps", re.I), "August Young"),
    ("spencer_reynolds_vending_discovery_dash_next_steps", re.compile(r"vending\s+discovery\s+-\s+next\s+steps", re.I), "Spencer Reynolds"),
    ("amy_mulch_vendingpreneurs_strategy_next_steps", re.compile(r"vendingpren[eu]+rs?\s+strategy\s*-?\s*next\s+steps", re.I), "Amy Mulch"),
    ("cassie_caraballo_vending_opportunity_next_steps", re.compile(r"vending\s+opportunity\s*-?\s*next\s+steps", re.I), "Cassie Caraballo"),
    ("jessica_zatkin_vendingpreneurs_connect_next_steps", re.compile(r"vendingpren[eu]+rs?\s+connect\s*-?\s*next\s+steps", re.I), "Jessica Zatkin"),
    ("abigail_garza_vending_success_next_steps", re.compile(r"vending\s+success\s*-?\s*next\s+steps", re.I), "Abigail Garza"),
    ("connor_george_vendingpreneurs_momentum_next_steps", re.compile(r"vendingpren[eu]+rs?\s+momentum\s*-?\s*next\s+steps", re.I), "Connor George"),
    ("dana_lesiuk_vendingpreneurs_launch_next_steps", re.compile(r"vendingpren[eu]+rs?\s+launch\s*-?\s*next\s+steps", re.I), "Dana Lesiuk"),
)

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True, help="Pacific date, YYYY-MM-DD")
    parser.add_argument("--no-zoom", action="store_true", help="Skip Zoom participant reports")
    return parser.parse_args()


def pt_window(day: str) -> tuple[datetime, datetime]:
    parsed = datetime.strptime(day, "%Y-%m-%d").date()
    start = datetime.combine(parsed, time.min, PACIFIC).astimezone(timezone.utc)
    end = datetime.combine(parsed, time.max, PACIFIC).astimezone(timezone.utc)
    return start, end


def match_title(title: str) -> tuple[str, str] | None:
    for rule_key, pattern, rep in TITLE_RULES:
        if pattern.search(title or ""):
            return rule_key, rep
    return None


def lead_brief(session: object, lead_id: str) -> dict:
    return close_get(
        f"/lead/{lead_id}/",
        {
            "_fields": ",".join(
                [
                    "id",
                    "display_name",
                    "status_label",
                    "status_id",
                    "opportunities",
                    CF_TODAYS_DISPOSITION,
                ]
            )
        },
    )


def attendee_summary(meeting: dict, org_emails: set[str]) -> tuple[str, list[str], list[str]]:
    contacts: list[str] = []
    organizers: list[str] = []
    prospect_emails: list[str] = []
    for attendee in meeting.get("attendees") or []:
        email = (attendee.get("email") or "").lower()
        status = attendee.get("status") or attendee.get("attendance_status") or ""
        is_org = bool(attendee.get("is_organizer")) or email in org_emails or email.endswith(ORG_EMAIL_DOMAINS)
        label = f"{email or attendee.get('name') or '?'}:{status or '?'}"
        if is_org:
            organizers.append(label)
        else:
            contacts.append(label)
            if email:
                prospect_emails.append(email)
    return ";".join(contacts) or "-", prospect_emails, organizers


def negative_fallback(meeting: dict, lead: dict, org_emails: set[str]) -> tuple[str | None, str]:
    status = normalized(lead.get("status_label"))
    if "no show" in status or "no-show" in status or "noshow" in status:
        return "no_show", "lead status says no show"
    if "resched" in status:
        return "rescheduled", "lead status says rescheduled"
    if "cancel" in status:
        return "cancelled", "lead status says canceled"

    contact_statuses: list[str] = []
    for attendee in meeting.get("attendees") or []:
        email = (attendee.get("email") or "").lower()
        is_org = bool(attendee.get("is_organizer")) or email in org_emails or email.endswith(ORG_EMAIL_DOMAINS)
        if is_org:
            continue
        contact_statuses.append(normalized(attendee.get("status") or attendee.get("attendance_status")))
    if contact_statuses and all(value in {"noreply", "no", "declined"} for value in contact_statuses):
        return "no_show", "all contact attendees are noreply/no/declined"
    return None, "no negative fallback"


def normalized(value: object) -> str:
    return str(value or "").strip().lower()


def parse_dt(value: object) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def close_get(path: str, params: dict | None = None) -> dict:
    api_key = os.environ["CLOSE_API_KEY"]
    query = f"?{urlencode(params)}" if params else ""
    auth = base64.b64encode(f"{api_key}:".encode()).decode()
    request = Request(f"{BASE_URL}{path}{query}", headers={"Authorization": f"Basic {auth}"})
    for _ in range(5):
        try:
            with urlopen(request, timeout=60) as response:
                return json.loads(response.read().decode())
        except HTTPError as error:
            if error.code == 429:
                time_module.sleep(float(error.headers.get("Retry-After", 2)) + 0.5)
                continue
            raise
    raise RuntimeError(f"GET {path}: rate-limited after retries")


def fetch_org_users() -> dict[str, dict[str, str]]:
    users: dict[str, dict[str, str]] = {}
    skip = 0
    while True:
        data = close_get("/user/", {"_skip": skip, "_limit": 100})
        for user in data["data"]:
            users[user["id"]] = {
                "name": f"{user.get('first_name') or ''} {user.get('last_name') or ''}".strip(),
                "email": (user.get("email") or "").lower(),
            }
        if not data.get("has_more"):
            break
        skip += 100
    return users


def fetch_meetings_window(since_dt: datetime, until_dt: datetime) -> list[dict]:
    fields = "id,lead_id,user_id,title,starts_at,ends_at,duration,status,outcome_id,attendees,note,location,source,date_created"
    out: list[dict] = []
    skip = 0
    while True:
        data = close_get("/activity/meeting/", {"_skip": skip, "_limit": 100, "_fields": fields})
        rows = data["data"]
        if not rows:
            break
        page_all_old = True
        for meeting in rows:
            starts_at = parse_dt(meeting.get("starts_at") or meeting.get("date_created"))
            if starts_at is None:
                continue
            if starts_at >= since_dt:
                page_all_old = False
                if starts_at <= until_dt:
                    out.append(meeting)
        if page_all_old or not data.get("has_more"):
            break
        skip += 100
    return out


def is_canceledish(meeting: dict) -> bool:
    title = normalized(meeting.get("title"))
    return normalized(meeting.get("status")) == "canceled" or title.startswith("canceled")


def later_similar_meeting_exists(meeting: dict, lead_meetings: list[dict]) -> bool:
    starts_at = parse_dt(meeting.get("starts_at"))
    if starts_at is None:
        return False
    for other in lead_meetings:
        if other.get("id") == meeting.get("id") or is_canceledish(other):
            continue
        other_starts_at = parse_dt(other.get("starts_at"))
        if other_starts_at and other_starts_at > starts_at:
            return True
    return False


def attention_signal(meeting: dict, disposition: str | None, lead_meetings: list[dict], now_utc: datetime) -> str | None:
    outcome = DISPOSITION_TO_OUTCOME.get(normalized(disposition))
    if outcome is None:
        return None
    starts_at = parse_dt(meeting.get("starts_at"))
    if starts_at is None or (now_utc - starts_at).days > 3:
        return None
    past = [
        item
        for item in lead_meetings
        if parse_dt(item.get("starts_at")) and parse_dt(item.get("starts_at")) <= now_utc and not is_canceledish(item)
    ]
    if not past:
        return None
    latest = max(past, key=lambda item: parse_dt(item.get("starts_at")))
    return outcome if latest.get("id") == meeting.get("id") else None


def zoom_signal(participants: list[dict] | None, prospect_emails: set[str], org_emails: set[str], prospect_names: list[str]) -> tuple[str | None, str]:
    if participants is None:
        return None, "no zoom data"
    prospect_seconds = 0
    host_seconds = 0
    lowered_names = [name.lower() for name in prospect_names if name]
    for participant in participants:
        email = participant["email"]
        name = (participant["name"] or "").lower()
        seconds = participant["seconds"]
        if email and email in org_emails:
            host_seconds += seconds
        elif email and email in prospect_emails:
            prospect_seconds += seconds
        elif not email and lowered_names and any(name_match(name, prospect_name) for prospect_name in lowered_names):
            prospect_seconds += seconds
    if prospect_seconds >= MIN_ATTEND_SECONDS:
        return "completed", f"prospect on for {prospect_seconds}s"
    if prospect_seconds > 0:
        return None, f"prospect joined only {prospect_seconds}s - review"
    if host_seconds >= HOST_MIN_SECONDS and ZOOM_AUTO_NOSHOW:
        return "no_show", f"host on {host_seconds}s, prospect absent"
    if host_seconds >= HOST_MIN_SECONDS:
        return None, f"host on {host_seconds}s, prospect absent - review"
    return None, "neither host nor prospect found in zoom report - review"


def name_match(a: str, b: str) -> bool:
    import difflib

    return bool(a and b and difflib.SequenceMatcher(None, a, b).ratio() >= 0.8)


def decide(meeting: dict, lead_meetings: list[dict], disposition: str | None, zoom_result: object, now_utc: datetime) -> tuple[str | None, str, str]:
    if is_canceledish(meeting):
        if later_similar_meeting_exists(meeting, lead_meetings):
            return "rescheduled", "close-status", "canceled + later booking exists"
        return "cancelled", "close-status", "canceled, no later booking"
    attention = attention_signal(meeting, disposition, lead_meetings, now_utc)
    if attention:
        return attention, "attention", f"disposition={disposition!r}"
    if zoom_result != "skip":
        participants, prospect_emails, org_emails, prospect_names = zoom_result
        zoom_outcome, detail = zoom_signal(participants, prospect_emails, org_emails, prospect_names)
        if zoom_outcome:
            return zoom_outcome, "zoom", detail
        return None, "zoom", detail
    return None, "none", "no signal available"


def resolve_calendly_zoom(url: str) -> str | None:
    try:
        opener = build_opener(HTTPRedirectHandler)
        with opener.open(url, timeout=15) as response:
            pieces = [response.geturl(), response.read(2000).decode(errors="ignore")]
            for piece in pieces:
                match = ZOOM_JOIN_RE.search(piece or "")
                if match:
                    return match.group(1)
    except Exception:
        return None
    return None


class ZoomClient:
    def __init__(self) -> None:
        self.enabled = all(os.environ.get(key) for key in ("ZOOM_ACCOUNT_ID", "ZOOM_CLIENT_ID", "ZOOM_CLIENT_SECRET"))
        self._token: str | None = None
        self._token_exp = 0.0

    def token(self) -> str:
        if self._token and time_module.time() < self._token_exp - 60:
            return self._token
        params = urlencode({"grant_type": "account_credentials", "account_id": os.environ["ZOOM_ACCOUNT_ID"]})
        auth = base64.b64encode(f"{os.environ['ZOOM_CLIENT_ID']}:{os.environ['ZOOM_CLIENT_SECRET']}".encode()).decode()
        request = Request(
            f"https://zoom.us/oauth/token?{params}",
            data=b"",
            method="POST",
            headers={"Authorization": f"Basic {auth}"},
        )
        with urlopen(request, timeout=30) as response:
            data = json.loads(response.read().decode())
        self._token = data["access_token"]
        self._token_exp = time_module.time() + int(data.get("expires_in", 3600))
        return self._token

    def get(self, path: str, params: dict | None = None) -> dict | None:
        query = f"?{urlencode(params)}" if params else ""
        request = Request(f"{ZOOM_API}{path}{query}", headers={"Authorization": f"Bearer {self.token()}"})
        try:
            with urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode())
        except HTTPError as error:
            if error.code == 404:
                return None
            if error.code == 429:
                time_module.sleep(2)
                return self.get(path, params)
            raise

    def participants_for(self, zoom_meeting_id: str, target_start_utc: datetime | None) -> list[dict] | None:
        instance_data = self.get(f"/past_meetings/{zoom_meeting_id}/instances")
        uuid = None
        if instance_data and target_start_utc and instance_data.get("meetings"):
            best = None
            best_gap = None
            for occurrence in instance_data["meetings"]:
                starts_at = parse_dt(occurrence.get("start_time"))
                if starts_at is None:
                    continue
                gap = abs((starts_at - target_start_utc).total_seconds())
                if best_gap is None or gap < best_gap:
                    best = occurrence
                    best_gap = gap
            if best is not None and best_gap is not None and best_gap <= 6 * 3600:
                uuid = best.get("uuid")
        ident = uuid or str(zoom_meeting_id)
        if uuid and (uuid.startswith("/") or "//" in uuid):
            from urllib.parse import quote

            ident = quote(quote(uuid, safe=""), safe="")
        data = self.get(f"/report/meetings/{ident}/participants", {"page_size": 300})
        if data is None:
            return None
        aggregated: dict[str, dict] = defaultdict(lambda: {"name": "", "email": "", "seconds": 0})
        for participant in data.get("participants", []):
            key = (participant.get("user_email") or participant.get("name") or "?").lower()
            aggregated[key]["name"] = participant.get("name") or aggregated[key]["name"]
            aggregated[key]["email"] = (participant.get("user_email") or aggregated[key]["email"]).lower()
            aggregated[key]["seconds"] += int(participant.get("duration") or 0)
        return list(aggregated.values())


def classify(
    meeting: dict,
    lead: dict,
    lead_meetings: list[dict],
    org_emails: set[str],
    zoom_result: object,
    now_utc: datetime,
) -> tuple[str, str, str]:
    native = OUTCOME_ID_TO_KEY.get(meeting.get("outcome_id") or "")
    if native and native != "scheduled":
        if native == "completed":
            return "shown", "native-outcome", native
        return "not_shown", "native-outcome", native

    outcome_key, source, detail = decide(meeting, lead_meetings, lead.get(CF_TODAYS_DISPOSITION), zoom_result, now_utc)
    if outcome_key:
        return "shown" if outcome_key == "completed" else "not_shown", source, detail

    fallback_key, fallback_detail = negative_fallback(meeting, lead, org_emails)
    if fallback_key:
        return "not_shown", "lead-attendee-fallback", fallback_detail

    return "unresolved", source, detail


def zoom_result_for(meeting: dict, zoom: "ZoomClient", org_emails: set[str]) -> tuple[object, str]:
    if not zoom.enabled:
        return "skip", "zoom off"
    blob = f"{meeting.get('note') or ''} {meeting.get('location') or ''}"
    direct = ZOOM_JOIN_RE.search(blob)
    calendly = CALENDLY_CONF_RE.search(blob)
    zoom_meeting_id = None
    provider = "no-video-link"
    if direct:
        zoom_meeting_id = direct.group(1)
        provider = "zoom"
    elif calendly and calendly.group(2).lower() == "google_meet":
        provider = "google-meet"
    elif calendly:
        zoom_meeting_id = resolve_calendly_zoom(calendly.group(1))
        provider = "zoom" if zoom_meeting_id else "zoom-calendly-unresolved"
    elif "meet.google.com" in blob.lower():
        provider = "google-meet"
    elif "zoom.us" in blob.lower():
        provider = "zoom-link-unparsed"

    if not zoom_meeting_id:
        return "skip", provider

    attendees = meeting.get("attendees") or []
    prospect_emails = {
        (attendee.get("email") or "").lower()
        for attendee in attendees
        if attendee.get("email")
        and (attendee.get("email") or "").lower() not in org_emails
        and not (attendee.get("email") or "").lower().endswith(ORG_EMAIL_DOMAINS)
    }
    prospect_names = [
        attendee.get("name") or ""
        for attendee in attendees
        if (attendee.get("email") or "").lower() not in org_emails
        and not (attendee.get("email") or "").lower().endswith(ORG_EMAIL_DOMAINS)
    ]
    participants = zoom.participants_for(zoom_meeting_id, parse_dt(meeting.get("starts_at")))
    return (participants, prospect_emails, org_emails, prospect_names), provider


def main() -> int:
    args = parse_args()
    start_utc, end_utc = pt_window(args.date)
    users = fetch_org_users()
    org_emails = {user["email"] for user in users.values() if user.get("email")}
    zoom = ZoomClient()
    if args.no_zoom:
        zoom.enabled = False

    meetings = fetch_meetings_window(start_utc, end_utc)
    scoped = []
    by_lead: dict[str, list[dict]] = defaultdict(list)
    for meeting in meetings:
        starts_at = parse_dt(meeting.get("starts_at"))
        if not starts_at or starts_at < start_utc or starts_at > end_utc:
            continue
        matched = match_title(meeting.get("title") or "")
        if not matched:
            continue
        rule_key, mapped_rep = matched
        meeting["_title_rule_key"] = rule_key
        meeting["_mapped_rep"] = mapped_rep
        scoped.append(meeting)
        by_lead[meeting.get("lead_id")].append(meeting)

    leads: dict[str, dict] = {}
    for lead_id in sorted({meeting.get("lead_id") for meeting in scoped if meeting.get("lead_id")}):
        leads[lead_id] = lead_brief(None, lead_id)

    print(f"Lane 2 live truth audit for {args.date} PT")
    print(f"Zoom: {'on' if zoom.enabled else 'off'}")
    print("=" * 170)
    print(
        f"{'Time PT':<8} {'Lead':<24} {'Rep':<17} {'Activity':<10} {'Native':<11} "
        f"{'Contact RSVP':<26} {'Lead status':<24} {'Decision':<11} {'Source':<22} Detail"
    )
    print("-" * 170)

    totals = {"shown": 0, "not_shown": 0, "unresolved": 0}
    now_utc = datetime.now(timezone.utc)
    for meeting in sorted(scoped, key=lambda row: row.get("starts_at") or ""):
        lead = leads.get(meeting.get("lead_id"), {})
        zoom_result, provider = zoom_result_for(meeting, zoom, org_emails)
        decision, source, detail = classify(
            meeting,
            lead,
            by_lead.get(meeting.get("lead_id"), []),
            org_emails,
            zoom_result,
            now_utc,
        )
        totals[decision] += 1
        starts_at = parse_dt(meeting.get("starts_at")).astimezone(PACIFIC)
        native = OUTCOME_ID_TO_KEY.get(meeting.get("outcome_id") or "", "blank")
        contacts, _, _ = attendee_summary(meeting, org_emails)
        lead_name = lead.get("display_name") or meeting.get("lead_id") or ""
        lead_status = lead.get("status_label") or ""
        source_label = f"{source}/{provider}" if source in {"zoom", "none"} else source
        print(
            f"{starts_at.strftime('%-I:%M %p'):<8} {lead_name[:24]:<24} "
            f"{meeting['_mapped_rep']:<17} {(meeting.get('status') or ''):<10} "
            f"{native:<11} {contacts[:26]:<26} {lead_status[:24]:<24} "
            f"{decision:<11} {source_label[:22]:<22} {detail}"
        )

    scheduled = len(scoped)
    print("-" * 170)
    print(f"scheduled={scheduled} shown={totals['shown']} not_shown={totals['not_shown']} unresolved={totals['unresolved']}")
    if scheduled:
        print(f"confirmed shown / scheduled = {totals['shown'] / scheduled * 100:.1f}%")
    terminal = totals["shown"] + totals["not_shown"]
    if terminal:
        print(f"shown / terminal classified = {totals['shown'] / terminal * 100:.1f}%")
    if totals["unresolved"]:
        print("NOTE: unresolved rows need native outcome, Attention, Zoom, or human review before the show rate is fully definitive.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
