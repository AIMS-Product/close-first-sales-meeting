#!/usr/bin/env python3
"""
outcome_sync.py — Close CRM Meeting Outcome Sync
=================================================

Writes Close's native Meeting Outcome (outcome_id) on past meetings.
Evidence hierarchy (v5 — per-meeting evidence outranks lead-level fields):

  1. Close meeting status        (canceled -> Rescheduled/Cancelled)
  2. Attention PER-CALL activity ("First Meeting Analysis"/"Meeting Analysis"
                                  custom activity within +/-4h of the meeting
                                  -> Completed; never overwritten by later calls)
  3. Attention lead disposition  ("Todays Call Disposition (Opp)" — guarded:
                                  latest meeting only, <=3 days old; kept as
                                  secondary because the NEXT call overwrites it)
  4. Phone conversation          (answered Close call >=5 min on the meeting's
                                  day -> Completed; 2-5 min blocks auto-no-show)
  5. Zoom attendance             (participant report via Server-to-Server OAuth)
  6. Lead status + RSVP          (status Canceled/No Show AND every external
                                  attendee noreply/declined -> Cancelled/No Show)
  7. Nothing conclusive          -> left blank + flagged in completeness report

FIELD PROJECTION (v6): outcomes are the source of truth; the legacy
"First Call Show Up (Opp)" lead field is kept in lockstep as a bridge for
Smart Views / older reports. When THE first sales call (sales-titled meeting on
the lead's FSCBD date) carries outcome Completed -> field "Yes"; No Show ->
"No". Overwrites a differing value (outcome wins); runs for rep-set outcomes
too; never touches "First Call Show (Override)".

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
  DRY_RUN              "1" (default) = log only; "0" = write outcomes
  LOOKBACK_DAYS        how far back to scan past meetings (default 7)
  MIN_ATTEND_SECONDS   prospect total time to count as attended (default 300)
  HOST_MIN_SECONDS     host presence required before auto No Show (default 600)
  ZOOM_AUTO_NOSHOW     "1" (default) allow auto No Show under the guard above;
                       "0" = Zoom absence only ever flags for review

Usage:
  python outcome_sync.py             # normal run (dry unless DRY_RUN=0)
  python outcome_sync.py --selftest  # run built-in decision-logic tests, no network
"""

import json
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

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

# Lead custom field: Attention writes its verdict here today.
CF_TODAYS_DISPOSITION = "custom.cf_n2QvikNfeZ0uWObMsyCJmnXnrbWNLGlSvYiKJTwxTqU"

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

# Attention disposition -> outcome key. Grounded in the field's actual choices.
DISPOSITION_TO_OUTCOME = {
    "new call show":              "completed",
    "follow up show":             "completed",
    "reschedule show":            "completed",
    "new call no show":           "no_show",
    "follow up no show":          "no_show",
    "reschedule no show":         "no_show",
    "discovery - no show (setter)": "no_show",
    "canceled":                   "cancelled",
    "canceled - rescheduled":     "rescheduled",
}

# Attention PER-CALL custom activity types. These are per-event records that
# later calls never overwrite — primary evidence (fixes the Dan Minton case,
# where a Tuesday cancel overwrote Monday's show in the lead-level field).
ATTENTION_MEETING_TYPE_IDS = {
    "actitype_7Hnq4Sw2S223adPFUmTarD",   # Attention - First Meeting Analysis
    "actitype_1hoSfW6deESpPKyZ4FORYV",   # Attention - Meeting Analysis
}
ATTENTION_DIALER_TYPE_ID = "actitype_6odahlx7K817nuEYi4yL32"  # Close Dialer Call Analysis
ATTENTION_MATCH_HOURS = 4  # meeting-analysis activity within +/- this of starts_at

# Attention verdict is a LEAD-level "today's" field, so it is only trusted for
# a meeting when it unambiguously refers to it (see attention_signal()).
ATTENTION_MAX_AGE_DAYS = 3

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
GRACE_MINUTES = env_int("GRACE_MINUTES", 90)  # skip meetings that ended < this long ago
MIN_ATTEND_SECONDS = env_int("MIN_ATTEND_SECONDS", 300)
HOST_MIN_SECONDS = env_int("HOST_MIN_SECONDS", 600)
ZOOM_AUTO_NOSHOW = os.environ.get("ZOOM_AUTO_NOSHOW", "1") != "0"
MIN_PHONE_SHOW_SECONDS = env_int("MIN_PHONE_SHOW_SECONDS", 300)     # answered call = show
MIN_PHONE_REVIEW_SECONDS = env_int("MIN_PHONE_REVIEW_SECONDS", 120)  # blocks auto-no-show

# ---------------------------------------------------------------------------
# Close API helpers
# ---------------------------------------------------------------------------

def close_session():
    s = requests.Session()
    s.auth = (os.environ["CLOSE_API_KEY"], "")
    s.headers["Content-Type"] = "application/json"
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
    """
    Paginate /activity/meeting/. NOTE (SYNC WITH update_field.py): Close
    silently ignores date filters on this endpoint, so we paginate newest-first
    and stop once a full page is older than the window.
    """
    fields = ("id,lead_id,user_id,title,starts_at,ends_at,duration,status,"
              "outcome_id,attendees,note,location,source,date_created")
    out, skip = [], 0
    while True:
        data = close_get(s, "/activity/meeting/",
                         {"_skip": skip, "_limit": 100, "_fields": fields})
        rows = data["data"]
        if not rows:
            break
        page_all_old = True
        for m in rows:
            st = parse_dt(m.get("starts_at") or m.get("date_created"))
            if st is None:
                continue
            if st >= since_dt:
                page_all_old = False
                if st <= until_dt:
                    out.append(m)
        if page_all_old or not data.get("has_more"):
            break
        skip += 100
    return out


def fetch_lead_brief(s, lead_id):
    return close_get(
        s, f"/lead/{lead_id}/",
        {"_fields": f"id,display_name,status_label,{CF_TODAYS_DISPOSITION},"
                    f"{CF_FSCBD},{CF_FIRST_CALL_SHOW}"}
    )


def set_lead_field(s, lead_id, field_key, value):
    return close_put(s, f"/lead/{lead_id}/", {field_key: value})


def fetch_attention_acts(s, lead_id):
    """Attention custom-activity instances on a lead -> [{'type_id','at'}]."""
    data = close_get(s, "/activity/custom/", {"lead_id": lead_id, "_limit": 100})
    acts = []
    for a in data.get("data", []):
        tid = a.get("custom_activity_type_id")
        if tid not in ATTENTION_MEETING_TYPE_IDS and tid != ATTENTION_DIALER_TYPE_ID:
            continue
        at = parse_dt(a.get("activity_at") or a.get("date_created"))
        if at:
            acts.append({"type_id": tid, "at": at})
    return acts


def fetch_lead_calls(s, lead_id):
    """Native Close call activities on a lead -> [{'at','duration','disposition'}]."""
    data = close_get(s, "/activity/call/", {
        "lead_id": lead_id, "_limit": 100,
        "_fields": "id,duration,disposition,activity_at,date_created"})
    calls = []
    for c in data.get("data", []):
        at = parse_dt(c.get("activity_at") or c.get("date_created"))
        if at:
            calls.append({"at": at, "duration": int(c.get("duration") or 0),
                          "disposition": c.get("disposition") or ""})
    return calls


def set_meeting_outcome(s, meeting_id, outcome_id):
    return close_put(s, f"/activity/meeting/{meeting_id}/", {"outcome_id": outcome_id})

# ---------------------------------------------------------------------------
# Zoom API helpers
# ---------------------------------------------------------------------------

class Zoom:
    def __init__(self):
        self.enabled = all(os.environ.get(k) for k in
                           ("ZOOM_ACCOUNT_ID", "ZOOM_CLIENT_ID", "ZOOM_CLIENT_SECRET"))
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
        r.raise_for_status()
        d = r.json()
        self._token = d["access_token"]
        self._token_exp = time.time() + d.get("expires_in", 3600)
        return self._token

    def _get(self, path, params=None):
        r = requests.get(f"{ZOOM_API}{path}", params=params,
                         headers={"Authorization": f"Bearer {self.token()}"}, timeout=30)
        if r.status_code == 404:
            return None
        if r.status_code == 429:
            time.sleep(2)
            return self._get(path, params)
        r.raise_for_status()
        return r.json()

    def participants_for(self, zoom_meeting_id, target_start_utc):
        """
        Participant list for the occurrence nearest target_start_utc.
        Handles reused/recurring meeting IDs via /past_meetings/{id}/instances.
        Returns list of {name, email, seconds} aggregated per participant, or None.
        """
        inst = self._get(f"/past_meetings/{zoom_meeting_id}/instances")
        uuid = None
        if inst and inst.get("meetings"):
            best, best_gap = None, None
            for occ in inst["meetings"]:
                st = parse_dt(occ.get("start_time"))
                if st is None:
                    continue
                gap = abs((st - target_start_utc).total_seconds())
                if best_gap is None or gap < best_gap:
                    best, best_gap = occ, gap
            if best is not None and best_gap is not None and best_gap <= 6 * 3600:
                uuid = best.get("uuid")
        ident = uuid if uuid else str(zoom_meeting_id)
        if uuid and (uuid.startswith("/") or "//" in uuid):
            ident = requests.utils.quote(requests.utils.quote(uuid, safe=""), safe="")
        data = self._get(f"/report/meetings/{ident}/participants",
                         {"page_size": 300})
        if data is None:
            return None
        agg = defaultdict(lambda: {"name": "", "email": "", "seconds": 0})
        for p in data.get("participants", []):
            key = (p.get("user_email") or p.get("name") or "?").lower()
            agg[key]["name"] = p.get("name") or agg[key]["name"]
            agg[key]["email"] = (p.get("user_email") or agg[key]["email"]).lower()
            agg[key]["seconds"] += int(p.get("duration") or 0)
        return list(agg.values())

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
    return meeting.get("status") == "canceled" or title.startswith("canceled")


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


def attention_signal(meeting, disposition, lead_meetings, now_utc):
    """
    Trust the lead-level 'Todays Call Disposition' for THIS meeting only when:
      * a disposition exists and maps to an outcome,
      * this meeting is the lead's MOST RECENT past meeting (the field always
        describes the latest call), and
      * the meeting is recent (<= ATTENTION_MAX_AGE_DAYS old) — beyond that the
        field may describe a newer interaction pattern we can't see.
    """
    if not disposition:
        return None
    key = disposition.strip().lower()
    outcome = DISPOSITION_TO_OUTCOME.get(key)
    if outcome is None:
        return None
    st = parse_dt(meeting.get("starts_at"))
    if st is None or (now_utc - st).days > ATTENTION_MAX_AGE_DAYS:
        return None
    past = [m for m in lead_meetings
            if parse_dt(m.get("starts_at")) and parse_dt(m["starts_at"]) <= now_utc
            and not is_canceledish(m)]
    if not past:
        return None
    latest = max(past, key=lambda m: parse_dt(m["starts_at"]))
    if latest.get("id") != meeting.get("id"):
        return None
    return outcome


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


def attention_activity_signal(meeting, acts):
    """
    Per-meeting Attention evidence: a First Meeting / Meeting Analysis custom
    activity within ATTENTION_MATCH_HOURS of the meeting start means Attention
    analyzed a real conversation for THIS event -> Completed.
    (Attention only produces a meeting analysis when a meeting actually ran.)
    """
    st = parse_dt(meeting.get("starts_at"))
    if st is None:
        return None, None
    for a in acts or ():
        if a["type_id"] in ATTENTION_MEETING_TYPE_IDS and \
                abs((a["at"] - st).total_seconds()) <= ATTENTION_MATCH_HOURS * 3600:
            return "completed", f"attention meeting-analysis at {a['at']:%m-%d %H:%M}"
    return None, None


def phone_evidence(meeting, calls, acts=()):
    """
    Phone conversation on the meeting's Pacific day (the Rashard case: prospect
    misses the Zoom link, rep reaches them by phone — that IS the show).
      answered call >= MIN_PHONE_SHOW_SECONDS   -> ("completed", detail)
      answered call >= MIN_PHONE_REVIEW_SECONDS
        or an Attention dialer analysis that day -> ("review", detail)
      else                                       -> (None, None)
    """
    st = parse_dt(meeting.get("starts_at"))
    if st is None:
        return None, None
    day = pacific_date(st)
    best = 0
    for c in calls or ():
        if pacific_date(c["at"]) == day and c["disposition"] == "answered":
            best = max(best, c["duration"])
    if best >= MIN_PHONE_SHOW_SECONDS:
        return "completed", f"answered call {best}s on meeting day"
    dialer = any(a["type_id"] == ATTENTION_DIALER_TYPE_ID
                 and pacific_date(a["at"]) == day for a in acts or ())
    if best >= MIN_PHONE_REVIEW_SECONDS or dialer:
        why = f"answered call {best}s" if best else "attention dialer analysis"
        return "review", f"{why} on meeting day — review before no-show"
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


def zoom_signal(participants, prospect_emails, org_emails, prospect_names=()):
    """
    -> ("completed" | "no_show" | None, detail)
    no_show requires host presence >= HOST_MIN_SECONDS and ZOOM_AUTO_NOSHOW.
    """
    if participants is None:
        return None, "no zoom data"
    prospect_secs, host_secs = 0, 0
    pnames = [n.lower() for n in prospect_names if n]
    for p in participants:
        email, name, secs = p["email"], (p["name"] or "").lower(), p["seconds"]
        if email and email in org_emails:
            host_secs += secs
        elif email and email in prospect_emails:
            prospect_secs += secs
        elif not email and pnames and any(
                _name_match(name, pn) for pn in pnames):
            prospect_secs += secs  # phone/renamed join matched by name
        elif not email and not pnames:
            pass
    if prospect_secs >= MIN_ATTEND_SECONDS:
        return "completed", f"prospect on for {prospect_secs}s"
    if prospect_secs > 0:
        return None, f"prospect joined only {prospect_secs}s — review"
    if host_secs >= HOST_MIN_SECONDS and ZOOM_AUTO_NOSHOW:
        return "no_show", f"host on {host_secs}s, prospect absent (auto no-show)"
    if host_secs >= HOST_MIN_SECONDS:
        return None, f"host on {host_secs}s, prospect absent — review (auto no-show off)"
    return None, "neither host nor prospect found in zoom report — review"


def _name_match(a, b):
    import difflib
    if not a or not b:
        return False
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.8


def decide(meeting, lead_meetings, disposition, zoom_result, now_utc,
           acts=(), calls=(), lead_status="", ext_attendee_statuses=()):
    """
    -> (outcome_key or None, source, detail)
    zoom_result: (participants or None) pre-fetched, or "skip" if zoom disabled/no link.
    """
    # 1. Cancel / reschedule from Close's own state — cheapest, most reliable.
    if is_canceledish(meeting):
        if later_similar_meeting_exists(meeting, lead_meetings):
            return "rescheduled", "close-status", "canceled + later booking exists"
        return "cancelled", "close-status", "canceled, no later booking"

    # 2. Attention per-call activity — per-meeting record, never overwritten.
    aa, aa_detail = attention_activity_signal(meeting, acts)
    if aa:
        return aa, "attention-activity", aa_detail

    # 3. Attention lead-level disposition (guarded; secondary during transition).
    a = attention_signal(meeting, disposition, lead_meetings, now_utc)
    if a:
        return a, "attention", f"disposition='{disposition}'"

    # 4. Phone conversation on the meeting day.
    ph, ph_detail = phone_evidence(meeting, calls, acts)
    if ph == "completed":
        return "completed", "phone", ph_detail

    # 5. Zoom attendance — with the phone guard on auto no-shows.
    zoom_detail = None
    if zoom_result != "skip":
        participants, prospect_emails, org_emails, prospect_names = zoom_result
        z, zoom_detail = zoom_signal(participants, prospect_emails, org_emails,
                                     prospect_names)
        if z == "no_show" and ph == "review":
            return None, "phone-guard", f"zoom says no-show BUT {ph_detail}"
        if z:
            return z, "zoom", zoom_detail

    # 6. Lead status + attendee RSVP negative evidence.
    sr, sr_detail = status_rsvp_signal(meeting, lead_status, ext_attendee_statuses)
    if sr:
        return sr, "status-rsvp", sr_detail

    if zoom_detail is not None:
        return None, "zoom", zoom_detail
    return None, "none", "no signal available"

# ---------------------------------------------------------------------------
# Main run
# ---------------------------------------------------------------------------

def run():
    now_utc = datetime.now(timezone.utc)
    since = now_utc - timedelta(days=LOOKBACK_DAYS)
    s = close_session()
    zoom = Zoom()

    print(f"outcome_sync: window={since:%Y-%m-%d}..{now_utc:%Y-%m-%d} "
          f"dry_run={DRY_RUN} zoom={'on' if zoom.enabled else 'OFF'}")

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

    def get_brief(lead_id):
        if lead_id not in brief_cache:
            brief_cache[lead_id] = fetch_lead_brief(s, lead_id)
        return brief_cache[lead_id]

    def get_evidence(lead_id):
        if lead_id not in evidence_cache:
            evidence_cache[lead_id] = {"acts": fetch_attention_acts(s, lead_id),
                                       "calls": fetch_lead_calls(s, lead_id)}
        return evidence_cache[lead_id]

    def project_first_call_field(meeting, lead_id, outcome_id):
        """Outcome -> First Call Show Up bridge. Overwrites (outcome is truth)."""
        brief = get_brief(lead_id)
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

    for m in meetings:
        st = parse_dt(m.get("starts_at"))
        if st is None or st > now_utc:
            continue  # future meetings keep their Scheduled default
        # Grace period: don't judge a meeting that is in progress or just
        # ended — Attention hasn't written its verdict and Zoom's participant
        # report lags meeting end. The next 30-min run will pick it up.
        est_end = st + timedelta(seconds=int(m.get("duration") or 3600))
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
                project_first_call_field(m, lead_id, current)
            except Exception as e:
                report["errors"].append({"meeting": m.get("id"), "error": str(e)})
                print(f"  ERROR {m.get('id')}: {e}", file=sys.stderr)
            continue

        try:
            lead = get_brief(lead_id)
            ev = get_evidence(lead_id)
            acts, calls = ev["acts"], ev["calls"]
            disposition = lead.get(CF_TODAYS_DISPOSITION)
            lead_status = lead.get("status_label") or ""
            ext_attendee_statuses = [
                a.get("status") for a in (m.get("attendees") or [])
                if (a.get("email") or "").lower() not in org_emails
            ]

            blob = f"{m.get('note') or ''} {m.get('location') or ''}"
            zoom_meeting_id = None
            direct = ZOOM_JOIN_RE.search(blob)
            calendly = CALENDLY_CONF_RE.search(blob)
            if direct:
                provider, zoom_meeting_id = "zoom", direct.group(1)
            elif calendly and calendly.group(2).lower() == "google_meet":
                provider = "google-meet"
            elif calendly:  # calendly .../zoom redirect — resolve to real ID
                zoom_meeting_id = resolve_calendly_zoom(calendly.group(1))
                provider = "zoom" if zoom_meeting_id else "zoom-calendly-unresolved"
            elif "meet.google.com" in blob.lower():
                provider = "google-meet"
            elif "zoom.us" in blob.lower():
                provider = "zoom-link-unparsed"  # /my/ vanity or webinar link
            else:
                provider = "no-video-link"

            zoom_result = "skip"
            if zoom.enabled:
                if zoom_meeting_id:
                    attendees = m.get("attendees") or []
                    prospect_emails = {
                        (a.get("email") or "").lower() for a in attendees
                        if (a.get("email") or "").lower() not in org_emails
                        and a.get("email")
                    }
                    prospect_names = [a.get("name") or "" for a in attendees
                                      if (a.get("email") or "").lower()
                                      not in org_emails]
                    participants = zoom.participants_for(zoom_meeting_id, st)
                    zoom_result = (participants, prospect_emails,
                                   org_emails, prospect_names)

            outcome_key, source, detail = decide(
                m, by_lead[lead_id], disposition, zoom_result, now_utc,
                acts=acts, calls=calls, lead_status=lead_status,
                ext_attendee_statuses=ext_attendee_statuses)

            label = f"{m['id']} '{(m.get('title') or '')[:40]}' {st:%m-%d %H:%M}"
            if outcome_key:
                if not DRY_RUN:
                    set_meeting_outcome(s, m["id"], OUTCOMES[outcome_key])
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
                     "note_head": (m.get("note") or "")[:100],
                     "reason": f"{source}: {detail}"})
                print(f"  FLAG               {label} "
                      f"({source}: {detail}) [provider={provider} age={age_days}d]")
        except Exception as e:  # keep the run alive; report the failure
            report["errors"].append({"meeting": m.get("id"), "error": str(e)})
            print(f"  ERROR {m.get('id')}: {e}", file=sys.stderr)

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

    # Non-zero exit only on hard errors, so the workflow step can alert.
    return 1 if report["errors"] else 0

# ---------------------------------------------------------------------------
# Selftest — pure decision logic, no network
# ---------------------------------------------------------------------------

def selftest():
    now = datetime(2026, 7, 21, 20, 0, tzinfo=timezone.utc)

    def mtg(id, title, start, status="completed", outcome=None, lead="lead_1"):
        return {"id": id, "title": title, "starts_at": start, "status": status,
                "outcome_id": outcome, "lead_id": lead}

    m_first = mtg("m1", "Vending Strategy Call", "2026-07-20T16:00:00+00:00")
    m_cancel = mtg("m2", "Canceled: Vending Strategy Call",
                   "2026-07-19T16:00:00+00:00", status="canceled")
    m_later = mtg("m3", "Vending Strategy Call", "2026-07-22T16:00:00+00:00",
                  status="upcoming")
    lead_meetings = [m_first, m_cancel, m_later]

    checks = []

    # 1. canceled + later booking -> rescheduled
    r = decide(m_cancel, lead_meetings, None, "skip", now)
    checks.append(("cancel->rescheduled", r[0] == "rescheduled" and r[1] == "close-status"))

    # 2. canceled, no later booking -> cancelled
    r = decide(m_cancel, [m_cancel], None, "skip", now)
    checks.append(("cancel->cancelled", r[0] == "cancelled"))

    # 3. attention show on latest past meeting -> completed
    r = decide(m_first, lead_meetings, "New Call Show", "skip", now)
    checks.append(("attention show", r[0] == "completed" and r[1] == "attention"))

    # 4. attention no-show variants map correctly
    r = decide(m_first, lead_meetings, "Reschedule No Show", "skip", now)
    checks.append(("attention noshow", r[0] == "no_show"))

    # 5. attention ignored when meeting is NOT the latest past meeting
    m_old = mtg("m0", "Vending Strategy Call", "2026-07-18T16:00:00+00:00")
    r = decide(m_old, lead_meetings + [m_old], "New Call Show", "skip", now)
    checks.append(("attention guard: not latest", r[0] is None))

    # 6. attention ignored when stale (> ATTENTION_MAX_AGE_DAYS)
    m_stale = mtg("ms", "Vending Strategy Call", "2026-07-10T16:00:00+00:00")
    r = decide(m_stale, [m_stale], "New Call Show", "skip", now)
    checks.append(("attention guard: stale", r[0] is None))

    # 7. zoom: prospect attended -> completed
    parts = [{"name": "Rep", "email": "rep@vendingpreneurs.com", "seconds": 2400},
             {"name": "Prospect", "email": "p@x.com", "seconds": 1800}]
    z = (parts, {"p@x.com"}, {"rep@vendingpreneurs.com"}, ["Prospect"])
    r = decide(m_first, lead_meetings, None, z, now)
    checks.append(("zoom attended", r[0] == "completed" and r[1] == "zoom"))

    # 8. zoom: host present, prospect absent -> auto no_show (guarded)
    parts = [{"name": "Rep", "email": "rep@vendingpreneurs.com", "seconds": 1800}]
    z = (parts, {"p@x.com"}, {"rep@vendingpreneurs.com"}, ["Prospect"])
    r = decide(m_first, lead_meetings, None, z, now)
    checks.append(("zoom auto-noshow", r[0] == "no_show"))

    # 9. zoom: host barely present -> flag, never no_show
    parts = [{"name": "Rep", "email": "rep@vendingpreneurs.com", "seconds": 120}]
    z = (parts, {"p@x.com"}, {"rep@vendingpreneurs.com"}, ["Prospect"])
    r = decide(m_first, lead_meetings, None, z, now)
    checks.append(("zoom absent-host flag", r[0] is None))

    # 10. zoom: prospect joined 90s -> flag for review, not completed/no_show
    parts = [{"name": "Rep", "email": "rep@vendingpreneurs.com", "seconds": 1800},
             {"name": "Prospect", "email": "p@x.com", "seconds": 90}]
    z = (parts, {"p@x.com"}, {"rep@vendingpreneurs.com"}, ["Prospect"])
    r = decide(m_first, lead_meetings, None, z, now)
    checks.append(("zoom brief join flag", r[0] is None))

    # 11. phone join matched by fuzzy name -> completed
    parts = [{"name": "Rep", "email": "rep@vendingpreneurs.com", "seconds": 1800},
             {"name": "steven kelly", "email": "", "seconds": 1500}]
    z = (parts, {"p@x.com"}, {"rep@vendingpreneurs.com"}, ["Steven Kelley"])
    r = decide(m_first, lead_meetings, None, z, now)
    checks.append(("zoom name match", r[0] == "completed"))

    # 12. no signal at all -> flag
    r = decide(m_first, lead_meetings, None, "skip", now)
    checks.append(("no signal flag", r[0] is None and r[1] == "none"))

    # --- v5: per-meeting evidence ---
    MEETING_TYPE = next(iter(ATTENTION_MEETING_TYPE_IDS))
    mstart = parse_dt(m_first["starts_at"])

    # 13. attention meeting-analysis activity within window -> completed
    acts = [{"type_id": MEETING_TYPE, "at": mstart + timedelta(minutes=30)}]
    r = decide(m_first, lead_meetings, None, "skip", now, acts=acts)
    checks.append(("attention-activity match", r[0] == "completed"
                   and r[1] == "attention-activity"))

    # 14. attention activity outranks a contradicting lead disposition
    r = decide(m_first, lead_meetings, "New Call No Show", "skip", now, acts=acts)
    checks.append(("activity beats disposition", r[0] == "completed"
                   and r[1] == "attention-activity"))

    # 15. attention activity outside +/-4h window -> not matched
    acts_far = [{"type_id": MEETING_TYPE, "at": mstart + timedelta(hours=9)}]
    r = decide(m_first, lead_meetings, None, "skip", now, acts=acts_far)
    checks.append(("activity window guard", r[1] != "attention-activity"))

    # 16. phone: answered 400s call on meeting day -> completed (Rashard fix)
    calls = [{"at": mstart + timedelta(hours=2), "duration": 400,
              "disposition": "answered"}]
    parts = [{"name": "Rep", "email": "rep@vendingpreneurs.com", "seconds": 1800}]
    z = (parts, {"p@x.com"}, {"rep@vendingpreneurs.com"}, ["Prospect"])
    r = decide(m_first, lead_meetings, None, z, now, calls=calls)
    checks.append(("phone show beats zoom noshow", r[0] == "completed"
                   and r[1] == "phone"))

    # 17. phone: 150s answered call blocks auto no-show -> flagged for review
    calls_short = [{"at": mstart + timedelta(hours=2), "duration": 150,
                    "disposition": "answered"}]
    r = decide(m_first, lead_meetings, None, z, now, calls=calls_short)
    checks.append(("phone guard flags noshow", r[0] is None
                   and r[1] == "phone-guard"))

    # 18. phone: unanswered call does NOT block auto no-show
    calls_na = [{"at": mstart + timedelta(hours=2), "duration": 0,
                 "disposition": "no-answer"}]
    r = decide(m_first, lead_meetings, None, z, now, calls=calls_na)
    checks.append(("unanswered call ignored", r[0] == "no_show"))

    # 19. status+rsvp: Canceled (by Lead) + attendee noreply -> cancelled (Ruben fix)
    r = decide(m_first, lead_meetings, None, "skip", now,
               lead_status="🔻 Canceled (by Lead)",
               ext_attendee_statuses=["noreply"])
    checks.append(("status-rsvp cancelled", r[0] == "cancelled"
                   and r[1] == "status-rsvp"))

    # 20. status+rsvp: ghost/no-show status + noreply -> no_show
    r = decide(m_first, lead_meetings, None, "skip", now,
               lead_status="👻 No Show", ext_attendee_statuses=["noreply", "no"])
    checks.append(("status-rsvp noshow", r[0] == "no_show"))

    # 21. status+rsvp requires ALL-negative RSVPs — an accepted invite blocks it
    r = decide(m_first, lead_meetings, None, "skip", now,
               lead_status="🔻 Canceled (by Lead)",
               ext_attendee_statuses=["yes"])
    checks.append(("rsvp yes blocks status rung", r[0] is None))

    # --- v6: First Call Show Up projection ---
    # m_first starts 2026-07-20T16:00Z = 2026-07-20 Pacific (9am PDT)
    # 22. first sales call + Completed -> Yes
    v = first_call_field_value(m_first, "2026-07-20", OUTCOMES["completed"])
    checks.append(("project Yes", v == "Yes"))

    # 23. first sales call + No Show -> No
    v = first_call_field_value(m_first, "2026-07-20", OUTCOMES["no_show"])
    checks.append(("project No", v == "No"))

    # 24. wrong FSCBD date -> no projection (a later re-booked sales call
    #     must NOT overwrite the first call's verdict)
    v = first_call_field_value(m_first, "2026-07-01", OUTCOMES["completed"])
    checks.append(("project date guard", v is None))

    # 25. follow-up / non-sales titles -> no projection
    m_fu = mtg("mf", "Vending Strategy Call Follow-Up", "2026-07-20T16:00:00+00:00")
    v = first_call_field_value(m_fu, "2026-07-20", OUTCOMES["completed"])
    m_rand = mtg("mr", "Random sync", "2026-07-20T16:00:00+00:00")
    v2 = first_call_field_value(m_rand, "2026-07-20", OUTCOMES["completed"])
    checks.append(("project title guard", v is None and v2 is None))

    # 26. rescheduled/cancelled outcomes never project
    v = first_call_field_value(m_first, "2026-07-20", OUTCOMES["rescheduled"])
    checks.append(("project outcome guard", v is None))

    # 27. scraper title variants project too (incl. 'Call - Next Steps')
    m_scr = mtg("msc", "Vendingpreneurs Call - Next Steps with Phil",
                "2026-07-20T16:00:00+00:00")
    v = first_call_field_value(m_scr, "2026-07-20", OUTCOMES["completed"])
    checks.append(("project scraper title", v == "Yes"))

    failed = [name for name, ok in checks if not ok]
    for name, ok in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    print(f"\n{len(checks) - len(failed)}/{len(checks)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    sys.exit(run())
