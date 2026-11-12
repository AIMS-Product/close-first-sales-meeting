"""Lane 2 policy decisions. Pure functions; no credentials or CRM writes.

Event evidence is a verified source envelope, not a Close field name. Adapters
must supply exact lead/event/rep identities and complete reads before routing.
"""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

PACIFIC = ZoneInfo("America/Los_Angeles")
F_OWNER = "cf_gOfS9pFwext58oberEegLyix8hZzeHrxhCZOVh3P3rd"
F_STATE = "cf_hKcyx4tQSMvHd7llfLX363bx3LGXMxVEmFHEtjpR5C2"
F_ENTRY = "cf_FI1V2Zw8M0aoSg7yJKJo0wb8NbRU8LafnkgHrpLcfYA"
F_OVERRIDE = "cf_6PnYz6aaAkLzHMU3Faxz7kFurBEfadqlfGiBgfhjaVC"
LEAD_TIER_FIELD = "cf_Mpz1pnxLOSjscFDAErSDYOV0PSjlWKXSgBN4jWd73sM"
WARM_STATUS = "stat_fOwpEPCfmjC2LhrJEZE5T1btfKJRcgkMinycBxLPwS5"
REAL_OPT_IN_SOURCES = {"Website-Apply", "Typeform", "Webinar", "Lead-Magnet"}
NORMAL_TIER_ACCESS = {"Ramp": {"Ramp"}, "Proven": {"Ramp", "Proven"},
                      "Top": {"Ramp", "Proven", "Top"}}
CANCELED = {"canceled", "cancelled", "declined-by-lead", "declined-by-org"}
SUPPRESS_STATUSES = {
    "stat_0oW3iRpVp9z5DJq0cuwI1HgR0XhHAhykEPPIq4TFsxd",
    "stat_U9MI7pqsvIjceTv3pCU7b1EghO8Q83h1HUcL6fGVyi6",
    "stat_p3oblSTnbsyDAw4rWqZDePGYMOlKBgV2FjbqIMDrfvF",
    "stat_YV4ZngDB4IGjLjlOf0YTFEWuKZJ6fhNxVkzQkvKYfdB",
}
CANCELLED_OUTCOME = "outcome_032DjpoQ9otqb8rGb7SIYt"
REACTIVATION_MIN_DIALS = 100
REACTIVATION_WINDOW_DAYS = 90
REACTIVATION_MAX_STALENESS_DAYS = 7


def custom(lead, field):
    return lead.get("custom." + field, (lead.get("custom") or {}).get(field))


def instant(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def age_tier(age_days, source, imported_list=False):
    if imported_list:
        return "Ramp", "imported_list_override"
    if age_days < 0:
        return None, "future_creation_date"
    if age_days <= 7:
        return ("Top", "real_opt_in") if source in REAL_OPT_IN_SOURCES else ("Ramp", "not_real_opt_in")
    if age_days <= 29:
        return "Proven", "age_8_29"
    if age_days <= 60:
        return "Ramp", "age_30_60"
    if age_days <= 179:
        return "Proven", "age_61_179"
    if age_days <= 364:
        return "Ramp", "age_180_364"
    return "Reactivation", "age_365_plus"


def classify_tier(lead, now):
    imported = lead.get("_imported_list")
    if imported is True:
        return "Ramp", "imported_list_override"
    if imported is not False:
        return None, "import_provenance_unverified"
    created = instant(lead.get("date_created"))
    if not created:
        return None, "creation_date_unverified"
    age = (now.astimezone(PACIFIC).date() - created.astimezone(PACIFIC).date()).days
    return age_tier(age, custom(lead, F_ENTRY))


def imported_lead_query():
    # Existing saved-view schema verifies import_id on activity.created, NOT lead.
    return {"type": "has_related", "this_object_type": "lead", "related_object_type": "activity.created",
            "related_query": {"type": "field_condition", "field": {"type": "regular_field",
                "object_type": "activity.created", "field_name": "import_id"}, "condition": {"type": "exists"}}}


def reactivation_qualified(certification, now):
    approval = certification.get("reactivation_approval") or {}
    if (certification.get("reactivation") is True and approval.get("evidence_waived") is True
            and approval.get("source") and approval.get("approved_on")):
        return True
    evidence = certification.get("reactivation_evidence") or {}
    if certification.get("reactivation") is not True or evidence.get("verified") is not True:
        return False
    start, end = instant(evidence.get("window_start")), instant(evidence.get("window_end"))
    verified = instant(evidence.get("verified_at"))
    dials, bookings = evidence.get("dials"), evidence.get("bookings")
    if (evidence.get("minimum_lead_age_at_dial_days") != 365 or not evidence.get("source")
            or type(dials) is not int or type(bookings) is not int or bookings < 0
            or dials < REACTIVATION_MIN_DIALS or bookings > dials):
        return False
    if not start or not end or not verified or not start < end <= verified <= now:
        return False
    if end - start != timedelta(days=REACTIVATION_WINDOW_DAYS):
        return False
    if now - end > timedelta(days=REACTIVATION_MAX_STALENESS_DAYS):
        return False
    return bookings * 100 >= dials * 2


def tier_access(certification, now):
    if not isinstance(certification, dict):
        return set()
    access = set(NORMAL_TIER_ACCESS.get(certification.get("highest_tier"), set()))
    if access and reactivation_qualified(certification, now):
        access.add("Reactivation")
    return access


def warm_task_hold(lead, tasks, today):
    if not custom(lead, F_OWNER) or lead.get("status_id") != WARM_STATUS:
        return False
    for task in tasks:
        if (task.get("lead_id") != lead.get("id") or task.get("is_complete") is not False
                or task.get("assigned_to") != custom(lead, F_OWNER)):
            continue
        due = instant(task.get("date"))
        if not due:
            continue
        # Close date-only tasks are local calendar days; timed tasks use Pacific.
        day = due.date() if len(str(task["date"])) == 10 else due.astimezone(PACIFIC).date()
        if day <= today + timedelta(days=60) and today <= day + timedelta(days=7):
            return True
    return False


def recent_meeting_hold(meetings, now):
    for meeting in meetings:
        if meeting.get("status") in CANCELED or meeting.get("outcome_id") == CANCELLED_OUTCOME:
            continue
        start = instant(meeting.get("starts_at"))
        if not start:
            return "meeting_date_unverified"
        if now - timedelta(days=30) <= start <= now:
            return "recent_meeting_30d"
    return None


def verified_event(event, lead_id):
    return (event.get("verified") is True and event.get("lead_id") == lead_id
            and bool(event.get("id")) and bool(event.get("source")) and instant(event.get("at")) is not None)


def webinar_ownership(lead, events, now):
    registrations = [event for event in events if event.get("kind") == "webinar_registration"]
    if not registrations:
        return ("webinar_attribution_missing", None) if custom(lead, F_ENTRY) == "Webinar" else (None, None)
    seen = {}
    for event in registrations:
        if event.get("id") in seen and event != seen[event["id"]]:
            return "webinar_attribution_conflict", None
        seen[event.get("id")] = event
    if any(not verified_event(event, lead["id"]) or not instant(event.get("webinar_at"))
           or not str(event.get("rep_id", "")).startswith("user_") or not event.get("registrant_id")
           or instant(event["at"]) > now for event in registrations):
        return "webinar_attribution_unverified", None
    latest_date = max(instant(event["webinar_at"]) for event in registrations)
    latest = [event for event in registrations if instant(event["webinar_at"]) == latest_date]
    reps = {event["rep_id"] for event in latest}
    if len(reps) != 1:
        return "webinar_attribution_conflict", None
    if now < latest_date + timedelta(days=30):
        return "webinar_owner_30d", next(iter(reps))
    return None, None


def owner_hold(lead, workflow_active):
    if workflow_active is not False:
        return "active_workflow" if workflow_active else "workflow_read_unverified"
    if lead.get("status_id") in SUPPRESS_STATUSES:
        return "suppressed_status"
    if custom(lead, F_STATE) == "QA Triage":
        return "qa_triage"
    if custom(lead, F_OVERRIDE) not in (None, "", "No", False):
        return "reassignment_override"
    return None


def dial_compliance(lead, calls, *, now, window_days=7, min_dials=0):
    if window_days < 1 or min_dials < 0:
        raise ValueError("dial diagnostic needs a positive window and nonnegative minimum")
    # Only the current owner's outbound dials count, never automated SMS/email.
    dials = {call["id"] for call in calls if call.get("id") and call.get("direction") == "outbound"
             and call.get("lead_id") == lead["id"] and call.get("user_id") == custom(lead, F_OWNER)
             and instant(call.get("date_created"))
             and now - timedelta(days=window_days) <= instant(call["date_created"]) <= now}
    return {"lead_id": lead["id"], "owner_id": custom(lead, F_OWNER), "dials": len(dials),
            "window_days": window_days, "min_dials": min_dials, "insufficient_dials": len(dials) < min_dials}


def require_dry_run(apply):
    if apply:
        raise RuntimeError("Lane 2 policy writes are disabled pending tier/view, roster, event-source and dry-run reconciliation")


def active_workflow_query():
    """Match leads whose contacts have any active Close workflow subscription."""
    return {
        "type": "has_related", "negate": False,
        "this_object_type": "lead", "related_object_type": "contact",
        "related_query": {
            "type": "has_related", "negate": False,
            "this_object_type": "contact",
            "related_object_type": "sequence_subscription",
            "related_query": {
                "type": "field_condition", "negate": False,
                "field": {"type": "regular_field",
                          "object_type": "sequence_subscription",
                          "field_name": "status"},
                "condition": {"type": "term", "values": ["active"]},
            },
        },
    }
