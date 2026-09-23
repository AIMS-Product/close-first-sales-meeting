#!/usr/bin/env python3
"""Read-only shadow test for single-word Close lead names.

Production remains unchanged. This compares the current prospect-name matcher
with a narrow exception: an exact attendee-linked Close contact may supply the
full name when the lead display name contains only one word and the first name
agrees. Zoom's existing exact-surname guard still makes the final match.
"""

import argparse
import re
from collections import Counter
from datetime import datetime, timedelta, timezone

import outcome_sync as production


OUTCOME_LABELS = {value: key for key, value in production.OUTCOMES.items()}


def name_tokens(name):
    """Return normalized name tokens with production metadata removed."""
    tokens = re.findall(r"[^\W_]+", str(name or "").casefold(), flags=re.UNICODE)
    while tokens and tokens[-1] in production.NAME_METADATA_SUFFIXES:
        tokens.pop()
    while tokens and tokens[-1].isdigit():
        tokens.pop()
    while tokens and tokens[-1] in production.NAME_SUFFIXES:
        tokens.pop()
    return tokens


def single_name_contact_candidates(meeting, lead, org_emails):
    """Return qualifying full names from exact attendee-linked contacts only."""
    lead_tokens = name_tokens(lead.get("display_name"))
    if len(lead_tokens) != 1:
        return []
    lead_first = lead_tokens[0]

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
        contact = contacts_by_id.get(attendee.get("contact_id"))
        if contact is None and email:
            contact = contacts_by_email.get(email)
        contact_name = (contact or {}).get("name") or ""
        contact_tokens = name_tokens(contact_name)
        if (
            contact_tokens
            and contact_tokens[0] == lead_first
            and production.name_surname(contact_name)
        ):
            names.append(contact_name)
    return production.unique_names(names)


def proposed_prospect_names_for(meeting, lead, org_emails):
    """Add only the narrow single-name exception to production candidates."""
    current = production.prospect_names_for(meeting, lead, org_emails)
    candidates = single_name_contact_candidates(meeting, lead, org_emails)
    return production.unique_names([*current, *candidates])


def prospect_emails_for(meeting, org_emails):
    return {
        (attendee.get("email") or "").lower()
        for attendee in meeting.get("attendees") or []
        if attendee.get("email")
        and (attendee.get("email") or "").lower() not in org_emails
    }


def signal_label(signal):
    return signal or "review"


def run(lookback_days):
    now_utc = datetime.now(timezone.utc)
    since = now_utc - timedelta(days=lookback_days)
    session = production.close_session()
    zoom = production.Zoom()
    if not zoom.enabled:
        raise RuntimeError("Zoom credentials are required for the shadow test")

    users = production.fetch_org_users(session)
    org_emails = {
        user["email"] for user in users.values() if user.get("email")
    }
    meetings = production.fetch_meetings_window(session, since, now_utc)
    lead_cache = {}
    summary = Counter()
    target_rows = []

    for meeting in meetings:
        starts_at = production.parse_dt(meeting.get("starts_at"))
        if starts_at is None or starts_at > now_utc:
            continue
        lead_id = meeting.get("lead_id")
        if not lead_id:
            continue
        if lead_id not in lead_cache:
            lead_cache[lead_id] = production.fetch_lead_brief(session, lead_id)
        lead = lead_cache[lead_id]
        lead_tokens = name_tokens(lead.get("display_name"))
        if len(lead_tokens) != 1:
            continue

        summary["single_name_meetings"] += 1
        provider, zoom_id = production.video_meeting_id(meeting)
        if not zoom_id:
            summary[f"skipped_{provider}"] += 1
            continue

        try:
            participants = zoom.participants_for(zoom_id, starts_at)
            prospect_emails = prospect_emails_for(meeting, org_emails)
            current_names = production.prospect_names_for(
                meeting, lead, org_emails
            )
            proposed_names = proposed_prospect_names_for(
                meeting, lead, org_emails
            )
            candidates = single_name_contact_candidates(
                meeting, lead, org_emails
            )
            current, _current_detail = production.zoom_signal(
                participants, prospect_emails, org_emails, current_names
            )
            proposed, _proposed_detail = production.zoom_signal(
                participants, prospect_emails, org_emails, proposed_names
            )
            summary["zoom_evaluated"] += 1
            summary["candidate_accepted" if candidates else "candidate_rejected"] += 1
            if current != proposed:
                summary["changed"] += 1
                summary[f"change_{signal_label(current)}_to_{signal_label(proposed)}"] += 1
                native = OUTCOME_LABELS.get(
                    meeting.get("outcome_id"), "blank_or_other"
                )
                summary[f"changed_native_{native}"] += 1
                acts = production.fetch_attention_acts(session, lead_id)
                nearby_attention = any(
                    activity["type_id"] in production.ATTENTION_MEETING_TYPE_IDS
                    and abs((activity["at"] - starts_at).total_seconds())
                    <= production.ATTENTION_MATCH_HOURS * 3600
                    for activity in acts
                )
                if nearby_attention:
                    summary["changed_with_nearby_attention"] += 1

            if (lead.get("display_name") or "").strip().casefold() == "terri":
                target_rows.append({
                    "date": str(production.pacific_date(starts_at)),
                    "native": OUTCOME_LABELS.get(
                        meeting.get("outcome_id"), "blank_or_other"
                    ),
                    "candidate": "accepted" if candidates else "rejected",
                    "current": signal_label(current),
                    "proposed": signal_label(proposed),
                })
        except Exception as error:
            summary["errors"] += 1
            print(
                "SHADOW_ERROR "
                f"type={type(error).__name__} provider={provider}"
            )

    for row in sorted(target_rows, key=lambda item: item["date"]):
        print(
            "TARGET name=Terri "
            f"date={row['date']} native={row['native']} "
            f"candidate={row['candidate']} current={row['current']} "
            f"proposed={row['proposed']}"
        )
    if not target_rows:
        print("TARGET name=Terri result=not_found_in_window")

    keys = [
        "single_name_meetings",
        "zoom_evaluated",
        "candidate_accepted",
        "candidate_rejected",
        "changed",
        "change_no_show_to_completed",
        "change_review_to_completed",
        "changed_native_no_show",
        "changed_native_completed",
        "changed_native_blank_or_other",
        "changed_with_nearby_attention",
        "errors",
    ]
    print(
        "SHADOW_SUMMARY read_only=true "
        + " ".join(f"{key}={summary[key]}" for key in keys)
    )
    return 0 if target_rows and not summary["errors"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--lookback-days", type=int, default=14)
    args = parser.parse_args()
    raise SystemExit(run(args.lookback_days))
