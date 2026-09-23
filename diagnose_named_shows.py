#!/usr/bin/env python3
"""Read-only diagnosis limited to explicitly approved fields."""

import json

import outcome_sync as production


NAMES = [
    "Stan Ferin",
    "Darnell Williams",
    "Terri",
    "Karamsho Mamadnasimov",
    "Debbie Barca",
    "Lauren Somerville",
]
OUTCOME_LABELS = {value: key for key, value in production.OUTCOMES.items()}


def exact_leads(session, name):
    response = production.close_get(
        session,
        "/lead/",
        {"query": f'"{name}"', "_limit": 50, "_fields": "id,display_name"},
    )
    return [
        lead for lead in response.get("data", [])
        if (lead.get("display_name") or "").strip().casefold() == name.casefold()
    ]


def meetings_for(session, lead_id):
    response = production.close_get(
        session,
        "/activity/meeting/",
        {
            "lead_id": lead_id,
            "_limit": 100,
            "_fields": (
                "id,title,starts_at,duration,status,outcome_id,attendees,note,location"
            ),
        },
    )
    return response.get("data", [])


def meeting_for_fscbd(meetings, fscbd):
    if not fscbd:
        return None
    same_day = []
    for meeting in meetings:
        starts_at = production.parse_dt(meeting.get("starts_at"))
        if starts_at and str(production.pacific_date(starts_at)) == fscbd:
            same_day.append(meeting)
    sales = [
        meeting for meeting in same_day
        if production.SALES_TITLE_RE.search(meeting.get("title") or "")
        and not production.FOLLOWUP_TITLE_RE.search(meeting.get("title") or "")
    ]
    candidates = sales or same_day
    return min(candidates, key=lambda meeting: meeting.get("starts_at") or "") \
        if candidates else None


def approved_diagnosis(name, lead, meeting, zoom, org_emails, attention_acts):
    fscbd = str(lead.get(production.CF_FSCBD) or "")[:10]
    native_outcome = None
    zoom_signal = None
    zoom_detail = None
    nearby_attention = False
    if meeting:
        native_outcome = OUTCOME_LABELS.get(meeting.get("outcome_id"), "blank/other")
        starts_at = production.parse_dt(meeting.get("starts_at"))
        nearby_attention = any(
            abs((activity["at"] - starts_at).total_seconds()) <= 4 * 3600
            for activity in attention_acts
            if activity["type_id"] in production.ATTENTION_MEETING_TYPE_IDS
        )
        _provider, zoom_id = production.video_meeting_id(meeting)
        if zoom_id:
            attendees = meeting.get("attendees") or []
            prospect_emails = {
                (attendee.get("email") or "").lower()
                for attendee in attendees
                if attendee.get("email")
                and (attendee.get("email") or "").lower() not in org_emails
            }
            prospect_names = production.prospect_names_for(meeting, lead, org_emails)
            participants = zoom.participants_for(zoom_id, starts_at)
            zoom_signal, zoom_detail = production.zoom_signal(
                participants, prospect_emails, org_emails, prospect_names
            )
    return {
        "name": name,
        "fscbd": fscbd or None,
        "first_call_show": lead.get(production.CF_FIRST_CALL_SHOW),
        "native_outcome": native_outcome,
        "zoom_signal": zoom_signal,
        "zoom_detail": zoom_detail,
        "nearby_attention": nearby_attention,
        "attention_disposition": lead.get(production.CF_TODAYS_DISPOSITION),
    }


def run():
    session = production.close_session()
    zoom = production.Zoom()
    users = production.fetch_org_users(session)
    org_emails = {user["email"] for user in users.values() if user.get("email")}
    for name in NAMES:
        matches = exact_leads(session, name)
        if not matches:
            print(json.dumps({"name": name, "fscbd": None,
                              "first_call_show": None, "native_outcome": None,
                              "zoom_signal": None, "zoom_detail": None,
                              "nearby_attention": False,
                              "attention_disposition": None}, sort_keys=True))
            continue
        for match in matches:
            lead = production.fetch_lead_brief(session, match["id"])
            fscbd = str(lead.get(production.CF_FSCBD) or "")[:10]
            meetings = meetings_for(session, match["id"])
            meeting = meeting_for_fscbd(meetings, fscbd)
            attention_acts = production.fetch_attention_acts(session, match["id"])
            print(json.dumps(approved_diagnosis(
                name, lead, meeting, zoom, org_emails, attention_acts
            ), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
