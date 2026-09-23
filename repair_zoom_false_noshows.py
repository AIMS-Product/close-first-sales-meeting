#!/usr/bin/env python3
"""Repair a reviewed allowlist of false Zoom No Show outcomes.

Every target is re-fetched and re-evaluated with the live production matcher.
The script only changes a meeting that is still No Show and now has qualifying
Zoom attendance. It is read-only unless APPLY_REPAIR=1.
"""

import json
import os
import sys

import outcome_sync as production


REPORT_PATH = "zoom_outcome_repair_report.json"
APPLY_REPAIR = os.environ.get("APPLY_REPAIR", "0") == "1"

# Reviewed 2026-09-23. Trevor Workman and Ashley Trybus are intentionally absent.
REPAIR_TARGETS = {
    "acti_ZAqFRw9DebD8e6VXgreWbumTvyAVQX2fbPNwHjAWATw": "lead_MM3CXDh0rdP0Sl2saHds7vhTlqoO9mJua2VHozOHklP",
    "acti_MNW0jmKlDpm1xApg8xAlHSy2XCqj7SptbtfasTVH5wz": "lead_yTPYS1vUTzuZfHQA0vBRaAOqXMmqOFAnqUkG1nHUFbq",
    "acti_Zt4Vh2zXwLXrzsIxdsoKJL6kNv4YPWyAngKO2TRTupB": "lead_mIMQEyMtp9wvsCL5Pv1BERdsbEKqL6StUaSTanJt6xi",
    "acti_vm4pnlxtOaitI1sQaAUDQ1BX40ZNz2u0oSOK5xkTQQC": "lead_CpVwYEYX4aGCsOdGD4nRQiLfIZIiao5UbJ3xaSyX3sd",
    "acti_B0XtE9XRIv5RlVtUWYzJk3eJQgjp10ZpJB3cFDokXbm": "lead_0otvDkw7joxfs3mjC0xygYOLJFbzLPP7NB1Xte4huVy",
    "acti_eGWV6YGs6KkeNGYKeipmatPY2cKPYsOJ8M7mMTZrtl5": "lead_A1aNTyd0TztmPeEBYYZTf6fMYcI8hdqsxBw4mWOohI4",
    "acti_bO1gMjZhkqYqk5MgVjIXg8zVUdxJ0KkiJjOPgZbl1Q8": "lead_2VQEbeRrvzxry0tFFsLDI4KcUTJ5UtqXPtjirOAmgJh",
    "acti_Dae5DVeLzvMxUSltwvIPIIR6I8HouWU9xZ7ISo60pGl": "lead_924zWMkR7jVYm7cziBVlSXL9ksRjIyyIwHlUSElvo0s",
    "acti_DMzo0OdIeZGkWUBokNHGj89KQRbkZNPXs8rUhmVPYSB": "lead_X8l6YHYUWUWfEIfqtxs7dffXXV0AV5xmKjGEZdR9ijI",
}

MEETING_FIELDS = (
    "id,lead_id,title,starts_at,duration,status,outcome_id,attendees,note,location"
)


def fetch_meeting(session, meeting_id):
    return production.close_get(
        session,
        f"/activity/meeting/{meeting_id}/",
        {"_fields": MEETING_FIELDS},
    )


def prospect_emails_for(meeting, org_emails):
    return {
        (attendee.get("email") or "").lower()
        for attendee in meeting.get("attendees") or []
        if attendee.get("email")
        and (attendee.get("email") or "").lower() not in org_emails
    }


def evaluate_target(session, zoom, org_emails, meeting_id, expected_lead_id):
    meeting = fetch_meeting(session, meeting_id)
    lead_id = meeting.get("lead_id")
    if lead_id != expected_lead_id:
        return None, f"lead mismatch: expected {expected_lead_id}, found {lead_id}"
    if meeting.get("outcome_id") != production.OUTCOMES["no_show"]:
        return None, f"current outcome is {meeting.get('outcome_id') or 'blank'}, not No Show"

    lead = production.fetch_lead_brief(session, lead_id)
    provider, zoom_id = production.video_meeting_id(meeting)
    if not zoom_id:
        return None, f"no resolvable Zoom ID ({provider})"

    starts_at = production.parse_dt(meeting.get("starts_at"))
    participants = zoom.participants_for(zoom_id, starts_at)
    prospect_emails = prospect_emails_for(meeting, org_emails)
    prospect_names = production.prospect_names_for(meeting, lead, org_emails)
    outcome, detail = production.zoom_signal(
        participants,
        prospect_emails,
        org_emails,
        prospect_names,
    )
    if outcome != "completed":
        return None, f"live Zoom recheck returned {outcome or 'review'}: {detail}"

    return {
        "meeting_id": meeting_id,
        "lead_id": lead_id,
        "lead_name": lead.get("display_name") or "",
        "title": meeting.get("title") or "",
        "starts_at": meeting.get("starts_at"),
        "matched_names": prospect_names,
        "zoom_detail": detail,
        "first_call_field": production.first_call_field_value(
            meeting,
            lead.get(production.CF_FSCBD),
            production.OUTCOMES["completed"],
        ),
        "current_first_call_field": lead.get(production.CF_FIRST_CALL_SHOW),
    }, None


def apply_target(session, planned):
    production.set_meeting_outcome(
        session,
        planned["meeting_id"],
        production.OUTCOMES["completed"],
    )
    field_value = planned.get("first_call_field")
    if field_value and field_value != planned.get("current_first_call_field"):
        production.set_lead_field(
            session,
            planned["lead_id"],
            production.CF_FIRST_CALL_SHOW,
            field_value,
        )

    verified_meeting = fetch_meeting(session, planned["meeting_id"])
    if verified_meeting.get("outcome_id") != production.OUTCOMES["completed"]:
        raise RuntimeError("meeting outcome verification failed")
    if field_value:
        verified_lead = production.fetch_lead_brief(session, planned["lead_id"])
        if verified_lead.get(production.CF_FIRST_CALL_SHOW) != field_value:
            raise RuntimeError("First Call Show Up verification failed")


def run():
    session = production.close_session()
    zoom = production.Zoom()
    if not zoom.enabled:
        raise RuntimeError("Zoom credentials are required")
    users = production.fetch_org_users(session)
    org_emails = {user["email"] for user in users.values() if user.get("email")}
    report = {
        "apply": APPLY_REPAIR,
        "allowlisted": len(REPAIR_TARGETS),
        "planned": [],
        "written": [],
        "skipped": [],
        "errors": [],
    }

    for target_number, (meeting_id, lead_id) in enumerate(REPAIR_TARGETS.items(), start=1):
        try:
            planned, reason = evaluate_target(
                session, zoom, org_emails, meeting_id, lead_id
            )
            if planned is None:
                report["skipped"].append({"meeting_id": meeting_id, "reason": reason})
                continue
            report["planned"].append(planned)
            print(
                f"{'APPLY' if APPLY_REPAIR else 'DRY'} candidate "
                f"{target_number}/{len(REPAIR_TARGETS)} validated"
            )
            if APPLY_REPAIR:
                apply_target(session, planned)
                report["written"].append(planned)
        except Exception as error:
            report["errors"].append({
                "meeting_id": meeting_id,
                "error": f"{type(error).__name__}: {error}",
            })

    with open(REPORT_PATH, "w") as report_file:
        json.dump(report, report_file, indent=2)
    print(
        f"SUMMARY allowlisted={len(REPAIR_TARGETS)} planned={len(report['planned'])} "
        f"written={len(report['written'])} skipped={len(report['skipped'])} "
        f"errors={len(report['errors'])}"
    )
    fully_validated = (
        len(report["planned"]) == len(REPAIR_TARGETS)
        and not report["skipped"]
        and not report["errors"]
    )
    return 0 if fully_validated else 1


if __name__ == "__main__":
    sys.exit(run())
