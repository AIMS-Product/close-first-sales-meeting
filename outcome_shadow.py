#!/usr/bin/env python3
"""Read-only replay of current vs proposed Zoom participant matching.

This script deliberately ignores existing terminal outcomes so previously
classified meetings can be evaluated again. It never writes to Close or Zoom.
"""

import argparse
import json
import os
import re
from datetime import datetime, timedelta, timezone

import outcome_sync as production


REPORT_PATH = "outcome_shadow_report.json"
NAME_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}
NAME_METADATA_SUFFIXES = {"yo", "old"}


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--lead-id", required=True, help="Lead to highlight in the report")
    parser.add_argument("--lookback-days", type=int, default=7)
    return parser.parse_args()


def fetch_lead(session, lead_id):
    fields = f"id,display_name,contacts,{production.CF_TODAYS_DISPOSITION}"
    return production.close_get(
        session,
        f"/lead/{lead_id}/",
        {"_fields": fields},
    )


def is_external_attendee(attendee, org_emails):
    email = (attendee.get("email") or "").lower()
    return email not in org_emails


def current_prospect_names(meeting, org_emails):
    return _unique_names(
        attendee.get("name") or ""
        for attendee in meeting.get("attendees") or []
        if is_external_attendee(attendee, org_emails)
    )


def proposed_prospect_names(meeting, lead, org_emails):
    """Add attendee contact names only when their surname agrees with the lead."""
    lead_name = lead.get("display_name") or ""
    names = [
        name for name in current_prospect_names(meeting, org_emails)
        if same_surname(name, lead_name)
    ]
    contacts = lead.get("contacts") or []
    contacts_by_id = {contact.get("id"): contact for contact in contacts if contact.get("id")}
    contacts_by_email = {}
    for contact in contacts:
        for item in contact.get("emails") or []:
            email = (item.get("email") or "").lower()
            if email:
                contacts_by_email[email] = contact

    for attendee in meeting.get("attendees") or []:
        if not is_external_attendee(attendee, org_emails):
            continue
        contact = contacts_by_id.get(attendee.get("contact_id"))
        if contact is None:
            contact = contacts_by_email.get((attendee.get("email") or "").lower())
        if contact and same_surname(contact.get("name"), lead_name):
            names.append(contact["name"])
    return _unique_names(names)


def same_surname(left, right):
    left_surname = _surname(left)
    right_surname = _surname(right)
    return bool(left_surname and right_surname and left_surname == right_surname)


def _surname(name):
    tokens = re.findall(r"[^\W_]+", str(name or "").casefold(), flags=re.UNICODE)
    while tokens and tokens[-1] in NAME_METADATA_SUFFIXES:
        tokens.pop()
    while tokens and tokens[-1].isdigit():
        tokens.pop()
    while tokens and tokens[-1] in NAME_SUFFIXES:
        tokens.pop()
    return tokens[-1] if len(tokens) >= 2 else ""


def strict_name_match(participant_name, prospect_name):
    return same_surname(participant_name, prospect_name) and production._name_match(
        str(participant_name or "").casefold(),
        str(prospect_name or "").casefold(),
    )


def strictly_matchable_names(names, participants):
    participant_names = [participant.get("name") or "" for participant in participants or []]
    return [
        name for name in names
        if any(strict_name_match(participant_name, name)
               for participant_name in participant_names)
    ]


def _unique_names(names):
    unique = []
    seen = set()
    for name in names:
        cleaned = str(name or "").strip()
        key = cleaned.lower()
        if cleaned and key not in seen:
            unique.append(cleaned)
            seen.add(key)
    return unique


def zoom_meeting_id(meeting):
    blob = f"{meeting.get('note') or ''} {meeting.get('location') or ''}"
    direct = production.ZOOM_JOIN_RE.search(blob)
    if direct:
        return direct.group(1), "zoom"

    calendly = production.CALENDLY_CONF_RE.search(blob)
    if calendly and calendly.group(2).lower() == "google_meet":
        return None, "google-meet"
    if calendly:
        resolved = production.resolve_calendly_zoom(calendly.group(1))
        return resolved, "zoom" if resolved else "zoom-calendly-unresolved"
    if "meet.google.com" in blob.lower():
        return None, "google-meet"
    if "zoom.us" in blob.lower():
        return None, "zoom-link-unparsed"
    return None, "no-video-link"


def participant_summary(participants, prospect_emails, org_emails):
    summary = []
    for participant in participants or []:
        email = (participant.get("email") or "").lower()
        summary.append({
            "name": participant.get("name") or "",
            "seconds": int(participant.get("seconds") or 0),
            "email_present": bool(email),
            "email_matches_prospect": email in prospect_emails,
            "email_matches_org": email in org_emails,
        })
    return summary


def replay_meeting(meeting, lead, zoom, org_emails):
    meeting_id, provider = zoom_meeting_id(meeting)
    if not meeting_id:
        return {
            "meeting_id": meeting.get("id"),
            "lead_id": meeting.get("lead_id"),
            "lead_name": lead.get("display_name") or "",
            "title": meeting.get("title") or "",
            "starts_at": meeting.get("starts_at"),
            "provider": provider,
            "status": "skipped",
            "reason": "no resolvable Zoom meeting ID",
        }

    attendees = meeting.get("attendees") or []
    prospect_emails = {
        (attendee.get("email") or "").lower()
        for attendee in attendees
        if is_external_attendee(attendee, org_emails) and attendee.get("email")
    }
    current_names = current_prospect_names(meeting, org_emails)
    proposed_names = proposed_prospect_names(meeting, lead, org_emails)
    participants = zoom.participants_for(meeting_id, production.parse_dt(meeting.get("starts_at")))
    strict_proposed_names = strictly_matchable_names(proposed_names, participants)

    current_outcome, current_detail = production.zoom_signal(
        participants, prospect_emails, org_emails, current_names
    )
    proposed_outcome, proposed_detail = production.zoom_signal(
        participants, prospect_emails, org_emails, strict_proposed_names
    )
    return {
        "meeting_id": meeting.get("id"),
        "lead_id": meeting.get("lead_id"),
        "lead_name": lead.get("display_name") or "",
        "title": meeting.get("title") or "",
        "starts_at": meeting.get("starts_at"),
        "provider": provider,
        "status": "evaluated",
        "native_outcome_id": meeting.get("outcome_id"),
        "disposition": lead.get(production.CF_TODAYS_DISPOSITION),
        "prospect_email_count": len(prospect_emails),
        "current_names": current_names,
        "proposed_names": proposed_names,
        "strictly_matched_names": strict_proposed_names,
        "participants": participant_summary(participants, prospect_emails, org_emails),
        "current": {"outcome": current_outcome, "detail": current_detail},
        "proposed": {"outcome": proposed_outcome, "detail": proposed_detail},
        "changed": current_outcome != proposed_outcome,
    }


def print_result(result, target_lead_id):
    if result.get("status") != "evaluated":
        if result.get("lead_id") == target_lead_id:
            print(f"TARGET skipped: {result.get('reason')} [{result.get('provider')}]")
        return
    marker = "TARGET" if result["lead_id"] == target_lead_id else "CHANGE"
    if result["lead_id"] != target_lead_id and not result["changed"]:
        return
    print(
        f"{marker}: {result['lead_name']} | {result['title']} | {result['starts_at']}\n"
        f"  current names={result['current_names']} -> "
        f"{result['current']['outcome']} ({result['current']['detail']})\n"
        f"  proposed names={result['proposed_names']} -> "
        f"{result['proposed']['outcome']} ({result['proposed']['detail']})\n"
        f"  participants={result['participants']}"
    )


def run(target_lead_id, lookback_days):
    if lookback_days < 1:
        raise ValueError("lookback-days must be positive")

    session = production.close_session()
    zoom = production.Zoom()
    if not zoom.enabled:
        raise RuntimeError("Zoom credentials are required for the shadow replay")

    users = production.fetch_org_users(session)
    org_emails = {user["email"] for user in users.values() if user.get("email")}
    now_utc = datetime.now(timezone.utc)
    meetings = production.fetch_meetings_window(
        session,
        now_utc - timedelta(days=lookback_days),
        now_utc,
    )

    lead_cache = {}
    results = []
    for meeting in meetings:
        lead_id = meeting.get("lead_id")
        if not lead_id:
            continue
        owner = users.get(meeting.get("user_id"), {})
        if owner.get("name", "").lower() in production.EXCLUDED_OWNER_NAMES:
            continue
        if lead_id not in lead_cache:
            lead_cache[lead_id] = fetch_lead(session, lead_id)
        try:
            result = replay_meeting(meeting, lead_cache[lead_id], zoom, org_emails)
        except Exception as error:
            result = {
                "meeting_id": meeting.get("id"),
                "lead_id": lead_id,
                "lead_name": lead_cache[lead_id].get("display_name") or "",
                "title": meeting.get("title") or "",
                "starts_at": meeting.get("starts_at"),
                "status": "error",
                "reason": f"{type(error).__name__}: {error}",
            }
        results.append(result)
        print_result(result, target_lead_id)

    evaluated = [result for result in results if result.get("status") == "evaluated"]
    changed = [result for result in evaluated if result.get("changed")]
    target = [result for result in results if result.get("lead_id") == target_lead_id]
    report = {
        "generated_at": now_utc.isoformat(),
        "read_only": True,
        "target_lead_id": target_lead_id,
        "lookback_days": lookback_days,
        "summary": {
            "meetings_scanned": len(meetings),
            "zoom_meetings_evaluated": len(evaluated),
            "changed_classifications": len(changed),
            "errors": sum(result.get("status") == "error" for result in results),
        },
        "target_results": target,
        "changed_results": changed,
    }
    with open(REPORT_PATH, "w") as report_file:
        json.dump(report, report_file, indent=2)
    print(f"SUMMARY: {json.dumps(report['summary'], sort_keys=True)}")
    print(f"REPORT: {REPORT_PATH}")
    return 1 if report["summary"]["errors"] else 0


def main():
    args = parse_args()
    return run(args.lead_id, args.lookback_days)


if __name__ == "__main__":
    raise SystemExit(main())
