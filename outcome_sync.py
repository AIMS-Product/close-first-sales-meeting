#!/usr/bin/env python3
"""
outcome_sync.py — Close CRM Meeting Outcome Sync
=================================================

Writes Close's native Meeting Outcome (outcome_id) on past meetings.
Evidence hierarchy (v7):

  1. Zoom attendance             (participant report via Server-to-Server OAuth)
  2. Close meeting status        (canceled -> Rescheduled/Cancelled)
  3. Phone conversation          (same-host answered Close call >=5 min overlapping
                                  the appointment -> Completed)
  4. Nothing conclusive          -> left blank + flagged in completeness report

v7 (2026-09-24) — REMOVED the "First Meeting Analysis custom activity
exists -> Completed" rung. Two reasons:
  a) avoma_to_close_first_meeting_sync.py now writes the Meeting Outcome
     DIRECTLY from Avoma's Outcome tag (Show / No-Show / Rescheduled),
     which is transcript-grounded and per-meeting — meetings it covers
     arrive here already terminal and are skipped (projection still runs).
  b) The rung became actively dangerous under Avoma: the CA is created for
     analyzed NO-SHOW meetings too (Avoma records the rep waiting), so
     "CA exists" no longer implies a conversation happened. Attention-era
     assumption, no longer true.
This sync is now the FALLBACK for meetings Avoma didn't tag.
GRACE_MINUTES default raised 90 -> 180 so Avoma's tag (which can lag the
meeting by a couple hours) usually gets first crack at the outcome before
the Zoom rung does.

FIELD PROJECTION (since v6): outcomes are the source of truth; the legacy
"First Call Show Up (Opp)" lead field is kept in lockstep as a bridge for
Smart Views / older reports. When THE first sales call (sales-titled meeting on
the lead's FSCBD date) carries outcome Completed -> field "Yes"; No Show ->
"No". Overwrites a differing value (outcome wins); runs for rep-set outcomes
too; never touches "First Call Show (Override)". This is the ONLY automated
writer of that field (the Avoma sync's direct write was removed 2026-09-24).

Designed to live in the close-first-sales-meeting repo as a SEPARATE step in
the 30-min workflow (isolated failure: a Zoom outage skips outcome sync, it
never blocks FSCBD stamping).

HARD RULES
----------
* NEVER overwrites an existing terminal outcome. Only writes when the meeting's
  current outcome is blank or "Scheduled". Human edits in the Close UI always win.
* Zoom absence ALONE never produces a No Show — the host must have been present
  (>= HOST_MIN_SECONDS) for an auto No Show, and every auto No Show is listed
  in the run report for review.
* DRY_RUN=1 (default!) logs every decision without writing to Close.
  Set DRY_RUN=0 only after reviewing a few dry runs.

ENV / SECRETS
-------------
  CLOSE_API_KEY        required (already a repo secret)
  ZOOM_ACCOUNT_ID      required for Zoom signal (Server-to-Server OAuth app)
  ZOOM_CLIENT_ID       "
  ZOOM_CLIENT_SECRET   "
  ZOOM_REQUIRED        "1" = fail before Close access if Zoom auth is unavailable
                       (set by the GitHub workflow; optional for local dry runs)
  DRY_RUN              "1" (default) = log only; "0" = write outcomes
  LOOKBACK_DAYS        how far back to scan past meetings (default 7)
  GRACE_MINUTES        skip meetings that ended less than this long ago
                       (default 180 — gives the Avoma tag first crack)
  MIN_ATTEND_SECONDS   prospect total time to count as attended (default 300)
  HOST_MIN_SECONDS     host presence required before auto No Show (default 600)
  ZOOM_AUTO_NOSHOW     "1" (default) allow auto No Show under the guard above;
                       "0" = Zoom absence only ever flags for review

Usage:
  python outcome_sync.py             # normal run (dry unless DRY_RUN=0)
  python outcome_sync.py --selftest  # run built-in decision-logic tests, no network
"""

import difflib
import json
import os
import re
import sys
import time
import unicodedata
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from close_meeting_window import read_meeting_window
from workflow_holds import guard_request

# ---------------------------------------------------------------------------
# Constants — KEEP IN SYNC with update_field.py where noted
# ---------------------------------------------------------------------------

PACIFIC = ZoneInfo("America/Los_Angeles")

CLOSE_API = "https://api.close.com/api/v1"
ZOOM_API = "https://api.zoom.us/v2"

# Meeting outcome IDs for this org (Settings > Playbooks and Outcomes).
OUTCOMES = {
    "scheduled":   "outcome_032DjlzDKpdXJZOzK4f7q3",
    "completed":   "outcome_032Djn4dfeNuEoCunojA7K",
    "rescheduled": "outcome_032Djo72GJ2Lvw3Q296wxH",
    "no_show":     "outcome_032DjoyPo9BgPBdOF6DzqH",
    "cancelled":   "outcome_032DjpoQ9otqb8rGb7SIYt",
}
TERMINAL_OUTCOME_IDS = {
    v for k, v in OUTCOMES.items() if k != "scheduled"
}
# Any other outcome id present on a meeting (call-type outcomes, future adds)
# is also treated as terminal: we never overwrite anything non-blank/non-Scheduled.

# --- First Call Show Up projection (outcome -> legacy field bridge) ---------
# Outcomes are the source of truth; this keeps the legacy field in lockstep so
# Smart Views / older reports keep working. Completed -> "Yes", No Show -> "No".
# Only for THE first sales call: sales-titled meeting on the lead's FSCBD date.
# Never touches "First Call Show (Override)".
CF_FSCBD = "custom.cf_LFdYEQ6bsgp49YjZzefypDmdVx8iwuakWDSLPLpVrBq"
CF_FIRST_CALL_SHOW = "custom.cf_OPyvpU45RdvjLqfm8V1VWwNxrGKogEH2IBJmfCj0Uhq"

# SYNC WITH update_field.py / backfill_outcomes.py — qualifying sales titles.
SALES_TITLE_RE = re.compile("|".join([
    r"vending strategy call",
    r"vendingpren[eu]+rs?\s+consultation",
    r"vendingpren[eu]+rs?\s+strategy call",
    r"new vendingpreneur strategy call",
    r"vending consult",
    r"post masterclass strategy call",
    r"vending route consultation",
    r"cash[- ]?flowing vending route advisory interview",
    r"vending route advisory call",
    r"vendingpren[eu]+rs?.{0,12}next steps",   # scraper closer calls (title variants)
    r"vendingpreneur next steps",
    r"vending (?:opportunity|discovery call)\s*-\s*next steps",
]), re.IGNORECASE)
FOLLOWUP_TITLE_RE = re.compile(r"follow[\s-]?up|fallow up|f/u", re.IGNORECASE)
OUTCOME_TITLE_RE = re.compile(r"vending|vendhub|acquisition[ -]?ace|\bAA\b|\bTLG\b|land[ -]?geek|\bPPA\b|publishing[ -]?profits", re.I)

# v7: the "First Meeting Analysis CA exists -> Completed" rung was REMOVED
# (see docstring). The CA type is still created — by the Avoma sync, which
# reuses the Attention-era type — but its existence no longer proves a
# conversation happened (Avoma analyzes no-show recordings too). Only the
# dialer CA type is still consumed here, as weak phone evidence.
ATTENTION_DIALER_TYPE_ID = "actitype_6odahlx7K817nuEYi4yL32"  # Close Dialer Call Analysis

# SYNC WITH update_field.py — owners whose meetings are always ignored.
EXCLUDED_OWNER_NAMES = {"stephen olivas", "ahmad bukhari"}

ZOOM_JOIN_RE = re.compile(r"zoom\.us/j/(\d{9,12})", re.IGNORECASE)
# Calendly hides the real conferencing URL behind a redirect link:
#   calendly.com/events/{uuid}/zoom          -> redirects to zoom.us/j/...
#   calendly.com/events/{uuid}/google_meet   -> Google Meet
CALENDLY_CONF_RE = re.compile(
    r"(https?://(?:www\.)?calendly\.com/events/[0-9a-fA-F-]+/(zoom|google_meet))",
    re.IGNORECASE)

_calendly_cache = {}

def resolve_calendly_zoom(url):
    """Follow a calendly .../zoom redirect to the real Zoom join URL."""
    if url in _calendly_cache:
        return _calendly_cache[url]
    zoom_id = None
    try:
        r = requests.get(url, allow_redirects=True, timeout=15)
        chain = [h.headers.get("Location", "") for h in r.history] + [r.url, r.text[:2000]]
        for piece in chain:
            m = ZOOM_JOIN_RE.search(piece or "")
            if m:
                zoom_id = m.group(1)
                break
    except requests.RequestException:
        pass
    _calendly_cache[url] = zoom_id
    return zoom_id


def video_meeting_id(meeting):
    """Return (provider, Zoom meeting ID or None) for a Close meeting."""
    blob = f"{meeting.get('note') or ''} {meeting.get('location') or ''}"
    direct = ZOOM_JOIN_RE.search(blob)
    if direct:
        return "zoom", direct.group(1)
    calendly = CALENDLY_CONF_RE.search(blob)
    if calendly and calendly.group(2).lower() == "google_meet":
        return "google-meet", None
    if calendly:
        zoom_id = resolve_calendly_zoom(calendly.group(1))
        provider = "zoom" if zoom_id else "zoom-calendly-unresolved"
        return provider, zoom_id
    if "meet.google.com" in blob.lower():
        return "google-meet", None
    if "zoom.us" in blob.lower():
        return "zoom-link-unparsed", None
    return "no-video-link", None

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default

DRY_RUN = os.environ.get("DRY_RUN", "1") != "0"
LOOKBACK_DAYS = env_int("LOOKBACK_DAYS", 7)
# v7: 90 -> 180. Avoma's Outcome tag (written to the meeting by
# avoma_to_close_first_meeting_sync.py) can lag the meeting by a couple of
# hours; waiting longer lets that transcript-grounded verdict land first,
# so the Zoom rung here only judges meetings Avoma didn't cover.
GRACE_MINUTES = env_int("GRACE_MINUTES", 180)
MIN_ATTEND_SECONDS = env_int("MIN_ATTEND_SECONDS", 300)
HOST_MIN_SECONDS = env_int("HOST_MIN_SECONDS", 600)
ZOOM_AUTO_NOSHOW = os.environ.get("ZOOM_AUTO_NOSHOW", "1") != "0"
NAME_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}
NAME_METADATA_SUFFIXES = {"yo", "old"}
MIN_PHONE_SHOW_SECONDS = env_int("MIN_PHONE_SHOW_SECONDS", 300)     # answered call = show
MIN_PHONE_REVIEW_SECONDS = env_int("MIN_PHONE_REVIEW_SECONDS", 120)  # blocks auto-no-show

# ---------------------------------------------------------------------------
# Close API helpers
# ---------------------------------------------------------------------------

def close_session():
    s = requests.Session()
    s.auth = (os.environ["CLOSE_API_KEY"], "")
    s.headers["Content-Type"] = "application/json"
    s.request = guard_request(s.request)
    return s


def close_get(s, path, params=None):
    for attempt in range(5):
        r = s.get(f"{CLOSE_API}{path}", params=params, timeout=60)
        if r.status_code == 429:
            time.sleep(float(r.headers.get("Retry-After", 2)) + 0.5)
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"Close GET {path}: rate-limited after retries")


def close_put(s, path, payload):
    if path.startswith('/lead/') or path.startswith('/opportunity/'):
        r = s.put(f"{CLOSE_API}{path}", json=payload, timeout=60)
        r.raise_for_status()
        return r.json()
    for attempt in range(5):
        r = s.put(f"{CLOSE_API}{path}", json=payload, timeout=60)
        if r.status_code == 429:
            time.sleep(float(r.headers.get("Retry-After", 2)) + 0.5)
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"Close PUT {path}: rate-limited after retries")


def fetch_org_users(s):
    """user_id -> {name, email}; also a set of org emails (reps/hosts)."""
    users, skip = {}, 0
    while True:
        data = close_get(s, "/user/", {"_skip": skip, "_limit": 100})
        for u in data["data"]:
            users[u["id"]] = {
                "name": ((u.get("first_name") or "") + " " + (u.get("last_name") or "")).strip(),
                "email": (u.get("email") or "").lower(),
            }
        if not data.get("has_more"):
            break
        skip += 100
    return users


def fetch_meetings_window(s, since_dt, until_dt):
    fields = ("id,lead_id,user_id,title,starts_at,ends_at,duration,status,"
              "outcome_id,attendees,note,location,source,date_created")
    def search_page(payload):
        for attempt in range(6):
            response = s.post(f"{CLOSE_API}/data/search/", json=payload, timeout=60)
            if response.status_code == 429:
                time.sleep(min(30, float(response.headers.get("Retry-After", 2 ** attempt))))
                continue
            response.raise_for_status()
            time.sleep(0.6)
            return response.json()
        raise RuntimeError("Close outcome search remained rate limited")
    meetings = read_meeting_window(search_page, fields, since_dt, until_dt)
    return [meeting for meeting in meetings
            if (start := parse_dt(meeting.get("starts_at"))) is not None
            and since_dt <= start <= until_dt]


def fetch_all_lead_meetings(s, lead_id):
    meetings, skip = [], 0
    while True:
        data = close_get(s, "/activity/meeting/", {"lead_id": lead_id, "_limit": 100, "_skip": skip})
        page = data.get("data", [])
        meetings.extend(page)
        if not data.get("has_more"):
            return meetings
        if not page:
            raise RuntimeError("Close lead meeting pagination incomplete")
        skip += len(page)


def fetch_lead_brief(s, lead_id):
    return close_get(
        s, f"/lead/{lead_id}/",
        {"_fields": f"id,display_name,status_label,contacts,{CF_FSCBD},{CF_FIRST_CALL_SHOW}"}
    )


def set_lead_field(s, lead_id, field_key, value):
    return close_put(s, f"/lead/{lead_id}/", {field_key: value})


def fetch_attention_acts(s, lead_id):
    """Dialer-analysis custom-activity instances on a lead -> [{'type_id','at'}].
    (v7: meeting-analysis CAs are no longer evidence — see docstring.)"""
    data = close_get(s, "/activity/custom/", {"lead_id": lead_id, "_limit": 100})
    acts = []
    for a in data.get("data", []):
        tid = a.get("custom_activity_type_id")
        if tid != ATTENTION_DIALER_TYPE_ID:
            continue
        at = parse_dt(a.get("activity_at") or a.get("date_created"))
        if at:
            acts.append({"type_id": tid, "at": at})
    return acts


def fetch_lead_calls(s, lead_id):
    calls, skip = [], 0
    while True:
        data = close_get(s, "/activity/call/", {
            "lead_id": lead_id, "_limit": 100, "_skip": skip,
            "_fields": "id,user_id,duration,disposition,activity_at,date_created"})
        page = data.get("data", [])
        for call in page:
            at = parse_dt(call.get("activity_at") or call.get("date_created"))
            if at:
                calls.append({"at": at, "duration": int(call.get("duration") or 0),
                              "user_id": call.get("user_id"),
                              "disposition": call.get("disposition") or ""})
        if not data.get("has_more"):
            return calls
        if not page:
            raise RuntimeError("Close call pagination incomplete")
        skip += len(page)


def meeting_change_messages(s, meeting, *, zoom_ids=None):
    """Prospect change requests veto an automated negative; a reviewer settles meaning."""
    start = parse_dt(meeting.get("starts_at"))
    if start is None:
        return []
    references = []
    for kind in ("sms", "email"):
        skip = 0
        while True:
            data = close_get(s, f"/activity/{kind}/", {
                "lead_id": meeting["lead_id"], "_limit": 100, "_skip": skip,
                "date_created__gte": (start-timedelta(hours=24)).isoformat(),
                "date_created__lte": (start+timedelta(hours=4)).isoformat()})
            page = data.get("data", [])
            for activity in page:
                at = parse_dt(activity.get("activity_at") or activity.get("date_created"))
                end = parse_dt(meeting.get("ends_at")) or start+timedelta(hours=1)
                if zoom_ids is not None and kind == "sms" and activity.get("direction") == "outbound" and at is not None and start-timedelta(minutes=15) <= at <= end+timedelta(hours=1):
                    zoom_ids.update(ZOOM_JOIN_RE.findall(activity.get("text") or ""))
                if activity.get("direction") not in {"inbound", "incoming"}:
                    continue
                text = activity.get("text") or activity.get("body_text") or ""
                # Ignore quoted reminders and calendar automation; they contain Cancel links.
                text = re.split(r"\nOn .{10,200}wrote:|\n>", text, maxsplit=1)[0]
                sender = (activity.get("sender") or "").casefold()
                if kind == "email" and ("calendly" in sender or "calendar-notification" in sender):
                    continue
                if re.search(r"reschedul|cancel|can.?t (attend|make|join)|won.?t (be available|work)|could not attend|push it|free the spot", text, re.I):
                    references.append({"activity_id": activity["id"], "type": kind})
            if not data.get("has_more"):
                break
            if not page:
                raise RuntimeError("Meeting change-message pagination incomplete")
            skip += len(page)
    return references


def set_meeting_outcome(s, meeting, outcome_id, *, reviewed_from=None):
    path = f"/activity/meeting/{meeting['id']}/"
    fresh = close_get(s, path)
    if any(fresh.get(key) != meeting.get(key)
           for key in ("id", "lead_id", "user_id", "starts_at", "title", "status")):
        raise RuntimeError("Meeting identity changed before outcome write")
    current = fresh.get("outcome_id")
    if current and current != OUTCOMES["scheduled"]:
        reviewed_upgrade = (reviewed_from == OUTCOMES["no_show"] == current
                            and outcome_id == OUTCOMES["completed"])
        if not reviewed_upgrade:
            return current
    close_put(s, path, {"outcome_id": outcome_id})
    verified = close_get(s, path)
    if verified.get("outcome_id") != outcome_id:
        raise RuntimeError("Meeting outcome readback did not match")
    return outcome_id

# ---------------------------------------------------------------------------
# Zoom API helpers
# ---------------------------------------------------------------------------

class Zoom:
    def __init__(self):
        self.credential_names = ("ZOOM_ACCOUNT_ID", "ZOOM_CLIENT_ID", "ZOOM_CLIENT_SECRET")
        self.missing_credentials = [k for k in self.credential_names if not os.environ.get(k)]
        self.enabled = not self.missing_credentials
        self._token = None
        self._token_exp = 0

    def token(self):
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        r = requests.post(
            "https://zoom.us/oauth/token",
            params={"grant_type": "account_credentials",
                    "account_id": os.environ["ZOOM_ACCOUNT_ID"]},
            auth=(os.environ["ZOOM_CLIENT_ID"], os.environ["ZOOM_CLIENT_SECRET"]),
            timeout=30,
        )
        if r.status_code >= 400:
            # requests' default exception only prints the URL. Zoom's structured
            # OAuth error identifies bad credentials vs. a bad account ID, while
            # avoiding a raw response body (which may contain sensitive values).
            details = []
            try:
                body = r.json()
            except ValueError:
                body = {}
            if isinstance(body, dict):
                for key in ("error", "reason", "code"):
                    value = body.get(key)
                    if isinstance(value, (str, int)):
                        value = str(value)
                        for name in self.credential_names:
                            secret = os.environ.get(name)
                            if secret:
                                value = value.replace(secret, "[redacted]")
                        value = " ".join(value.split())
                        details.append(f"{key}={value[:200]}")
            suffix = f" ({', '.join(details)})" if details else ""
            raise RuntimeError(f"Zoom OAuth HTTP {r.status_code}{suffix}")
        d = r.json()
        self._token = d["access_token"]
        self._token_exp = time.time() + d.get("expires_in", 3600)
        return self._token

    def _get(self, path, params=None):
        for attempt in range(6):
            response = requests.get(f"{ZOOM_API}{path}", params=params,
                                    headers={"Authorization": f"Bearer {self.token()}"}, timeout=30)
            if response.status_code == 404:
                return None
            if response.status_code == 429:
                time.sleep(min(30, max(1, int(response.headers.get("Retry-After", 2 ** attempt)))))
                continue
            response.raise_for_status()
            return response.json()
        raise RuntimeError("Zoom read remained rate limited after six attempts")

    def participants_for(self, zoom_meeting_id, target_start_utc):
        """Use one occurrence, all participant pages, and elapsed (not device) time."""
        instances = self._get(f"/past_meetings/{zoom_meeting_id}/instances")
        matches = [occ for occ in (instances or {}).get("meetings", [])
                   if parse_dt(occ.get("start_time")) is not None
                   and abs((parse_dt(occ["start_time"]) - target_start_utc).total_seconds()) <= 1800]
        if len(matches) != 1 or not matches[0].get("uuid"):
            return None
        uuid = matches[0]["uuid"]
        ident = requests.utils.quote(uuid, safe="")
        if uuid.startswith("/") or "//" in uuid:
            ident = requests.utils.quote(ident, safe="")
        rows, token, seen = [], "", set()
        while True:
            data = self._get(f"/report/meetings/{ident}/participants",
                             {"page_size": 300, "next_page_token": token})
            if data is None:
                return None
            rows.extend(data.get("participants", []))
            token = data.get("next_page_token") or ""
            if not token:
                break
            if token in seen:
                raise RuntimeError("Zoom participant pagination repeated a token")
            seen.add(token)
        return aggregate_participants(rows, target_start_utc)

# ---------------------------------------------------------------------------
# Decision logic (pure — covered by --selftest)
# ---------------------------------------------------------------------------

def parse_dt(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def pacific_date(dt_utc):
    return dt_utc.astimezone(PACIFIC).date()


def is_canceledish(meeting):
    title = (meeting.get("title") or "").strip().lower()
    return meeting.get("status") in {"canceled", "declined-by-lead", "declined-by-org"} or title.startswith("canceled")


def later_similar_meeting_exists(meeting, lead_meetings):
    """A later, non-canceled meeting on the same lead => the cancel was a reschedule."""
    st = parse_dt(meeting.get("starts_at"))
    if st is None:
        return False
    for other in lead_meetings:
        if other.get("id") == meeting.get("id") or is_canceledish(other):
            continue
        ost = parse_dt(other.get("starts_at"))
        if ost and ost > st:
            return True
    return False


def first_call_field_value(meeting, fscbd_str, outcome_id):
    """
    If this meeting is THE first sales call (sales title, not a follow-up, not
    canceled, Pacific date == lead's FSCBD) and its outcome is projectable:
    Completed -> "Yes", No Show -> "No". Anything else -> None (no field write).
    """
    if not fscbd_str or is_canceledish(meeting):
        return None
    title = meeting.get("title") or ""
    if not SALES_TITLE_RE.search(title) or FOLLOWUP_TITLE_RE.search(title):
        return None
    st = parse_dt(meeting.get("starts_at"))
    if st is None or str(pacific_date(st)) != str(fscbd_str)[:10]:
        return None
    if outcome_id == OUTCOMES["completed"]:
        return "Yes"
    if outcome_id == OUTCOMES["no_show"]:
        return "No"
    return None


def phone_evidence(meeting, calls, acts=()):
    """Only the same host's answered call overlapping this appointment is evidence."""
    start = parse_dt(meeting.get("starts_at"))
    if start is None:
        return None, None
    end = parse_dt(meeting.get("ends_at")) or start + timedelta(seconds=int(meeting.get("duration") or 3600))
    best = 0
    for call in calls or ():
        call_end = call["at"] + timedelta(seconds=call["duration"])
        same_host = meeting.get("user_id") and call.get("user_id") == meeting["user_id"]
        if same_host and call["disposition"] == "answered" and (
            call["at"] <= end + timedelta(minutes=15)
            and call_end >= start - timedelta(minutes=10)
        ):
            best = max(best, call["duration"])
    if best >= MIN_PHONE_SHOW_SECONDS:
        return "completed", f"same-host answered call {best}s overlapping appointment"
    if best >= MIN_PHONE_REVIEW_SECONDS:
        return "review", f"same-host answered call {best}s overlapping appointment"
    return None, None


def status_rsvp_signal(meeting, lead_status, ext_attendee_statuses):
    """
    Negative evidence pair (the Ruben case): lead status says canceled/no-show
    AND every external attendee never accepted the invite.
    """
    if not lead_status or not ext_attendee_statuses:
        return None, None
    if not all((x or "noreply").lower() in ("noreply", "no", "declined")
               for x in ext_attendee_statuses):
        return None, None
    ls = lead_status.lower()
    if "cancel" in ls:
        return "cancelled", f"lead status '{lead_status}' + attendees never accepted"
    if "no show" in ls or "👻" in lead_status:
        return "no_show", f"lead status '{lead_status}' + attendees never accepted"
    return None, None


def prospect_names_for(meeting, lead, org_emails):
    """Return external attendee names whose surname agrees with the lead."""
    lead_name = lead.get("display_name") or ""
    contacts = lead.get("contacts") or []
    contacts_by_id = {
        contact.get("id"): contact for contact in contacts if contact.get("id")
    }
    contacts_by_email = {}
    for contact in contacts:
        for email_item in contact.get("emails") or []:
            email = (email_item.get("email") or "").lower()
            if email:
                contacts_by_email[email] = contact

    names = []
    for attendee in meeting.get("attendees") or []:
        email = (attendee.get("email") or "").lower()
        if email in org_emails:
            continue
        attendee_name = attendee.get("name") or ""
        if same_surname(attendee_name, lead_name):
            names.append(attendee_name)
        contact = contacts_by_id.get(attendee.get("contact_id"))
        if contact is None:
            contact = contacts_by_email.get(email)
        contact_name = (contact or {}).get("name") or ""
        if same_surname(contact_name, lead_name):
            names.append(contact_name)
        if contact is not None and not contact_name and name_surname(lead_name):
            names.append(lead_name)
    return unique_names(names)


def unique_names(names):
    unique = []
    seen = set()
    for name in names:
        cleaned = str(name or "").strip()
        key = cleaned.casefold()
        if cleaned and key not in seen:
            unique.append(cleaned)
            seen.add(key)
    return unique


def same_surname(left, right):
    left_surname = name_surname(left)
    right_surname = name_surname(right)
    return bool(left_surname and right_surname and left_surname == right_surname)


def name_surname(name):
    tokens = re.findall(r"[^\W_]+", str(name or "").casefold(), flags=re.UNICODE)
    while tokens and tokens[-1] in NAME_METADATA_SUFFIXES:
        tokens.pop()
    while tokens and tokens[-1].isdigit():
        tokens.pop()
    while tokens and tokens[-1] in NAME_SUFFIXES:
        tokens.pop()
    return tokens[-1] if len(tokens) >= 2 else ""


BOT_RE = re.compile(r"avoma|notetaker|notetaking|attention ai|fireflies|fathom|read[ .]ai|ai agent|calendly|otter[ .]ai", re.I)


def is_automation(name):
    # Known bot display names use confusable Cyrillic letters as well as ASCII.
    normalized = unicodedata.normalize("NFKC", name or "").casefold()
    normalized = normalized.translate(str.maketrans("аеорсхуікԁтн", "aeopcxyikdth"))
    return bool(BOT_RE.search(normalized))


def union_seconds(intervals):
    merged = []
    for start, end in sorted(intervals):
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(end, merged[-1][1])
        else:
            merged.append([start, end])
    return sum(end - start for start, end in merged)


def aggregate_participants(rows, target_start):
    groups = {}
    for row in rows:
        if is_automation(row.get("name")) or row.get("status") == "in_waiting_room":
            continue
        start, end = parse_dt(row.get("join_time")), parse_dt(row.get("leave_time"))
        if start is None or end is None:
            # Missing timestamps cannot support negative inference.
            return None
        email = (row.get("user_email") or row.get("email") or "").casefold()
        name = (row.get("name") or "").casefold()
        key = email or name or row.get("user_id") or "unknown"
        person = groups.setdefault(key, {"name": row.get("name") or "", "email": email, "intervals": []})
        person["intervals"].append((max(start, target_start).timestamp(), end.timestamp()))
    return [{**person, "seconds": union_seconds(person["intervals"])} for person in groups.values()]


def zoom_signal(participants, prospect_emails, org_emails, prospect_names=(), host_names=()):
    if participants is None:
        return None, "missing or ambiguous Zoom occurrence/report — review"
    prospect, host = [], []
    prospect_seconds, host_seconds, unknown = 0, 0, False
    for participant in participants:
        if is_automation(participant.get("name")):
            continue
        email = (participant.get("email") or "").casefold()
        name = (participant.get("name") or "").casefold()
        seconds = participant.get("seconds", 0)
        if (email and email in org_emails) or (name and name in {n.casefold() for n in host_names}):
            host_seconds = max(host_seconds, seconds)
            host.extend(participant.get("intervals", []))
        elif (email and email in prospect_emails) or any(_name_match(name, pn.casefold()) for pn in prospect_names):
            prospect_seconds = max(prospect_seconds, seconds)
            prospect.extend(participant.get("intervals", []))
        elif seconds > 0:
            unknown = True
    if prospect:
        prospect_seconds = union_seconds(prospect)
    if host:
        host_seconds = union_seconds(host)
    if prospect_seconds >= MIN_ATTEND_SECONDS:
        return "completed", f"verified prospect attended {int(prospect_seconds)}s"
    if prospect_seconds > 0:
        return None, f"prospect joined only {int(prospect_seconds)}s — review"
    if unknown:
        return None, "unidentified human participant — review before no-show"
    if host_seconds >= HOST_MIN_SECONDS and ZOOM_AUTO_NOSHOW:
        return "no_show", f"identified host waited {int(host_seconds)}s after appointment start; no prospect/human"
    return None, "insufficient identified host/prospect attendance — review"


def _name_match(a, b):
    if not a or not b or not same_surname(a, b):
        return False
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.8


def decide(meeting, lead_meetings, zoom_result, now_utc,
           acts=(), calls=(), lead_status="", ext_attendee_statuses=()):
    """
    -> (outcome_key or None, source, detail)
    zoom_result: (participants or None) pre-fetched, or "skip" if zoom disabled/no link.

    v7 note: Avoma-tagged meetings normally never reach this function —
    avoma_to_close_first_meeting_sync.py writes their outcome directly and
    they get skipped as already-terminal. This is the fallback path.
    """
    # 1. Zoom attendance.
    zoom_detail = None
    if zoom_result != "skip":
        participants, prospect_emails, org_emails, prospect_names, *host_names = zoom_result
        z, zoom_detail = zoom_signal(participants, prospect_emails, org_emails,
                                     prospect_names, host_names[0] if host_names else ())
        if z == "completed":
            return z, "zoom", zoom_detail
    else:
        z = None
    ph, ph_detail = phone_evidence(meeting, calls, acts)
    if ph == "completed":
        return "completed", "phone", ph_detail

    # 2. Cancel / reschedule from Close's own state.
    if is_canceledish(meeting):
        if later_similar_meeting_exists(meeting, lead_meetings):
            return "rescheduled", "close-status", "canceled + later booking exists"
        return "cancelled", "close-status", "canceled, no later booking"

    if ph == "review":
        return None, "phone", ph_detail
    if z == "no_show":
        return z, "zoom", zoom_detail
    # Current lead status/RSVP cannot prove attendance at a historical meeting.
    if zoom_detail is not None:
        return None, "zoom", zoom_detail
    return None, "none", "no signal available"

# ---------------------------------------------------------------------------
# Main run
# ---------------------------------------------------------------------------

def run():
    now_utc = datetime.now(timezone.utc)
    if LOOKBACK_DAYS <= 0:
        raise ValueError("LOOKBACK_DAYS must be positive; unbounded historical scans are disabled")
    since = now_utc - timedelta(days=LOOKBACK_DAYS)
    zoom = Zoom()

    print(f"outcome_sync: window={since:%Y-%m-%d}..{now_utc:%Y-%m-%d} "
          f"dry_run={DRY_RUN} zoom={'on' if zoom.enabled else 'OFF'}")

    # The workflow requires Zoom evidence. If its credentials fail, stop before
    # any Close writes; otherwise weaker evidence could decide a Zoom meeting.
    if os.environ.get("ZOOM_REQUIRED") == "1":
        try:
            if zoom.missing_credentials:
                raise RuntimeError("missing Zoom credentials: " +
                                   ", ".join(zoom.missing_credentials))
            zoom.token()
        except (requests.RequestException, RuntimeError, ValueError, KeyError) as e:
            error = f"Zoom authentication failed: {e}"
            print(f"  ERROR {error}", file=sys.stderr)
            with open("outcome_sync_report.json", "w") as fh:
                json.dump({"generated_at": now_utc.isoformat(), "dry_run": DRY_RUN,
                           "written": [], "errors": [{"error": error}]}, fh, indent=2)
            return 1

    s = close_session()

    users = fetch_org_users(s)
    org_emails = {u["email"] for u in users.values() if u["email"]}

    meetings = fetch_meetings_window(s, since, now_utc)
    by_lead = defaultdict(list)
    for m in meetings:
        if m.get("lead_id"):
            by_lead[m["lead_id"]].append(m)

    report = {"written": [], "skipped_terminal": 0, "flagged": [],
              "auto_noshow": [], "field_writes": [], "errors": []}

    brief_cache, evidence_cache = {}, {}
    first_projection_meeting = {}

    def get_brief(lead_id):
        if lead_id not in brief_cache:
            brief_cache[lead_id] = fetch_lead_brief(s, lead_id)
        return brief_cache[lead_id]

    def get_evidence(lead_id):
        if lead_id not in evidence_cache:
            evidence_cache[lead_id] = {"acts": [],
                                       "calls": fetch_lead_calls(s, lead_id)}
        return evidence_cache[lead_id]

    def project_first_call_field(meeting, lead_id, outcome_id):
        """Outcome -> First Call Show Up bridge. Overwrites (outcome is truth)."""
        brief = get_brief(lead_id)
        if lead_id not in first_projection_meeting:
            eligible = [candidate for candidate in by_lead[lead_id]
                        if first_call_field_value(candidate, brief.get(CF_FSCBD), OUTCOMES["completed"]) is not None]
            first_projection_meeting[lead_id] = min(eligible, key=lambda candidate: (candidate["starts_at"], candidate["id"]))["id"] if eligible else None
        if meeting["id"] != first_projection_meeting[lead_id]:
            return
        value = first_call_field_value(meeting, brief.get(CF_FSCBD), outcome_id)
        if value is None:
            return
        current_val = brief.get(CF_FIRST_CALL_SHOW)
        if (current_val or "").strip().lower() == value.lower():
            return  # already in lockstep
        if not DRY_RUN:
            set_lead_field(s, lead_id, CF_FIRST_CALL_SHOW, value)
        brief[CF_FIRST_CALL_SHOW] = value  # avoid duplicate writes this run
        report["field_writes"].append(
            {"lead": lead_id, "meeting": meeting["id"],
             "field": "First Call Show Up (Opp)",
             "old": current_val, "new": value})
        print(f"  {'DRY ' if DRY_RUN else ''}FIELD First Call Show Up: "
              f"{current_val or '(blank)'} -> {value}  "
              f"(lead {lead_id}, mtg {meeting['id']})")

    for m in sorted(meetings, key=lambda row: row.get("starts_at") or "", reverse=True):
        if not OUTCOME_TITLE_RE.search(m.get("title") or ""):
            continue
        st = parse_dt(m.get("starts_at"))
        if st is None or st > now_utc:
            continue  # future meetings keep their Scheduled default
        # Grace period: don't judge a meeting that is in progress or just
        # ended — Avoma's Outcome tag hasn't been written yet and Zoom's
        # participant report lags meeting end. A later run picks it up.
        est_end = parse_dt(m.get("ends_at")) or st + timedelta(seconds=int(m.get("duration") or 3600))
        if now_utc < est_end + timedelta(minutes=GRACE_MINUTES):
            continue
        owner = users.get(m.get("user_id"), {})
        if owner.get("name", "").lower() in EXCLUDED_OWNER_NAMES:
            continue
        lead_id = m["lead_id"]
        current = m.get("outcome_id")
        if current and current != OUTCOMES["scheduled"]:
            # HARD RULE: never overwrite a terminal/manual outcome. But DO keep
            # the legacy First Call Show Up field in lockstep with it.
            report["skipped_terminal"] += 1
            try:
                if st >= now_utc - timedelta(days=7):
                    project_first_call_field(m, lead_id, current)
            except Exception as e:
                report["errors"].append({"meeting": m.get("id"), "error": str(e)})
                print(f"  ERROR {m.get('id')}: {e}", file=sys.stderr)
            continue

        try:
            lead = get_brief(lead_id)
            ev = get_evidence(lead_id)
            acts, calls = ev["acts"], ev["calls"]
            lead_status = lead.get("status_label") or ""
            ext_attendee_statuses = [
                a.get("status") for a in (m.get("attendees") or [])
                if (a.get("email") or "").lower() not in org_emails
            ]

            provider, zoom_meeting_id = video_meeting_id(m)

            zoom_result = "skip"
            if zoom.enabled:
                if zoom_meeting_id:
                    attendees = m.get("attendees") or []
                    prospect_emails = {
                        (a.get("email") or "").lower() for a in attendees
                        if (a.get("email") or "").lower() not in org_emails
                        and a.get("email")
                    }
                    prospect_names = prospect_names_for(m, lead, org_emails)
                    participants = zoom.participants_for(zoom_meeting_id, st)
                    zoom_result = (participants, prospect_emails,
                                   org_emails, prospect_names, [owner.get("name", "")])

            lead_history = by_lead[lead_id]
            if is_canceledish(m):
                lead_history = fetch_all_lead_meetings(s, lead_id)
            outcome_key, source, detail = decide(
                m, lead_history, zoom_result, now_utc,
                acts=acts, calls=calls, lead_status=lead_status,
                ext_attendee_statuses=ext_attendee_statuses)

            change_messages = []
            if outcome_key in {None, "no_show"}:
                room_ids = set()
                change_messages = meeting_change_messages(s, m, zoom_ids=room_ids)
                alternate_rooms = room_ids - ({zoom_meeting_id} if zoom_meeting_id else set())
                if change_messages or alternate_rooms:
                    outcome_key, source, detail = None, "prospect-message", "appointment change or replacement room requires review"
                neighboring_booking = any(other["id"] != m["id"] and parse_dt(other.get("starts_at")) is not None
                                          and abs((parse_dt(other["starts_at"])-st).total_seconds()) <= 3600
                                          for other in by_lead[lead_id])
                if zoom.enabled and alternate_rooms and len(alternate_rooms) <= 3 and not neighboring_booking:
                    emails = {(a.get("email") or "").casefold() for a in m.get("attendees", [])
                              if a.get("email") and not a.get("user_id")}
                    names = prospect_names_for(m, lead, org_emails)
                    for room_id in sorted(alternate_rooms):
                        participants = zoom.participants_for(room_id, st)
                        candidate, candidate_detail = zoom_signal(participants, emails, org_emails, names, [owner.get("name", "")])
                        if candidate == "completed":
                            outcome_key, source, detail = candidate, "zoom-replacement-room", candidate_detail
                            break
            label = f"{m['id']} '{(m.get('title') or '')[:40]}' {st:%m-%d %H:%M}"
            if outcome_key:
                if not DRY_RUN:
                    actual = set_meeting_outcome(s, m, OUTCOMES[outcome_key])
                    if actual != OUTCOMES[outcome_key]:
                        report["skipped_terminal"] += 1
                        project_first_call_field(m, lead_id, actual)
                        continue
                report["written"].append(
                    {"meeting": m["id"], "lead": lead_id,
                     "outcome": outcome_key, "source": source, "detail": detail})
                if source == "zoom" and outcome_key == "no_show":
                    report["auto_noshow"].append(
                        {"meeting": m["id"], "lead": lead_id, "detail": detail})
                print(f"  {'DRY ' if DRY_RUN else ''}SET {outcome_key:<11} "
                      f"[{source}] {label} ({detail})")
                project_first_call_field(m, lead_id, OUTCOMES[outcome_key])
            else:
                age_days = (now_utc - st).days
                report["flagged"].append(
                    {"meeting": m["id"], "lead": lead_id,
                     "title": m.get("title"), "starts_at": m.get("starts_at"),
                     "provider": provider, "age_days": age_days,
                     "owner_id": m.get("user_id"),
                     "change_message_references": change_messages,
                     "reason": f"{source}: {detail}"})
                print(f"  FLAG               {label} "
                      f"({source}: {detail}) [provider={provider} age={age_days}d]")
        except Exception as e:  # keep the run alive; report the failure
            report["errors"].append({"meeting": m.get("id"), "error": str(e)})
            print(f"  ERROR {m.get('id')}: {e}", file=sys.stderr)
        with open("outcome_sync_report.json", "w") as stream:
            json.dump({"generated_at": now_utc.isoformat(), "dry_run": DRY_RUN, **report}, stream, indent=2)

    # Completeness monitor: the logging guarantee.
    print("\n=== OUTCOME COMPLETENESS ===")
    print(f"written this run : {len(report['written'])}"
          f"{' (dry run — nothing persisted)' if DRY_RUN else ''}")
    print(f"already terminal : {report['skipped_terminal']}")
    print(f"needs review     : {len(report['flagged'])}")
    print(f"auto no-shows    : {len(report['auto_noshow'])} (verify these)")
    print(f"field writes     : {len(report['field_writes'])} "
          f"(First Call Show Up kept in lockstep with outcomes)")
    print(f"errors           : {len(report['errors'])}")
    prov_counts = defaultdict(int)
    fresh_counts = defaultdict(int)
    for f in report["flagged"]:
        prov_counts[f["provider"]] += 1
        fresh_counts["fresh (<=3d)" if f["age_days"] <= 3 else "backlog (>3d)"] += 1
    if report["flagged"]:
        print("flagged by provider : " + ", ".join(
            f"{k}={v}" for k, v in sorted(prov_counts.items(), key=lambda x: -x[1])))
        print("flagged by age      : " + ", ".join(
            f"{k}={v}" for k, v in sorted(fresh_counts.items())))
    report["flagged_by_provider"] = dict(prov_counts)
    report["flagged_by_age"] = dict(fresh_counts)
    for f in report["flagged"]:
        print(f"  REVIEW: https://app.close.com/lead/{f['lead']}/ "
              f"'{(f['title'] or '')[:50]}' — {f['reason']}")

    with open("outcome_sync_report.json", "w") as fh:
        json.dump({"generated_at": now_utc.isoformat(), "dry_run": DRY_RUN,
                   **report}, fh, indent=2)
    print("\nreport: outcome_sync_report.json")

    # Ambiguous cases stay unchanged; only source/write errors fail the run.
    return 1 if report["errors"] else 0

# ---------------------------------------------------------------------------
# Selftest — pure decision logic, no network
# ---------------------------------------------------------------------------

def selftest():
    import unittest
    suite = unittest.defaultTestLoader.loadTestsFromNames(["test_outcome_sync", "test_outcome_coverage"])
    return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    sys.exit(run())
