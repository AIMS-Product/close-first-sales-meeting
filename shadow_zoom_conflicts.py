#!/usr/bin/env python3
"""Read-only shadow audit for Zoom outcome conflicts.

The production sync treats an identified Zoom attendee as strong positive
evidence. This shadow keeps that behavior, but tests two safeguards before a
host-only report becomes a No Show:

* exact aliases derived from an external meeting attendee's email address;
* meeting-specific Attention or answered-phone evidence that contradicts the
  negative Zoom signal;
* explicit, guarded no-show dispositions that prevent a bot-generated
  Attention analysis from turning a host-only call into Completed.

The script only calls GET endpoints. It never updates Close.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import outcome_sync as production


REPORT_PATH = "zoom_conflict_shadow_report.json"
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "7"))

ROLE_EMAIL_ALIASES = {
    "admin",
    "contact",
    "hello",
    "info",
    "office",
    "owner",
    "sales",
    "support",
    "team",
}

# Markus has deterministic human-reviewed post-call correspondence, but no
# meeting-specific Attention record. The shadow reports the general classifier
# result separately from this reviewed repair scenario.
REVIEWED_COMPLETED_TARGETS = {
    "acti_u2K0L4f7JXyl5wzfFxytiKRvgahNngjJ6IbGoGKGexA":
        "reviewed one-word lead with exact meeting analysis confirms attendance",
    "acti_uIMvr0JeN1pm9fMEJbVyn47L4g4VLu3dhY90wo6gCuo":
        "reviewed post-call email exchange confirms attendance",
}

FOCUS_TARGETS = {
    "acti_D08co5c2zYTpGB1TCzcaRqvf35OEwL8jwdAzlWhasVo": "Isaac Dodds",
    "acti_u2K0L4f7JXyl5wzfFxytiKRvgahNngjJ6IbGoGKGexA": "Terri",
    "acti_uIMvr0JeN1pm9fMEJbVyn47L4g4VLu3dhY90wo6gCuo": "Markus Specks",
}

FOCUS_LEADS = {
    "lead_r1zzkiRh2KTAqXC3E9AXABKOOKCPOrTWQm27wt9zCjQ": "Haoua Kabore",
}


def email_alias(email: str) -> str | None:
    """Return a conservative exact-match alias from one attendee email."""
    local_part = str(email or "").partition("@")[0].casefold()
    alias = re.sub(r"[^a-z]+", "", local_part)
    if len(alias) < 3 or alias in ROLE_EMAIL_ALIASES:
        return None
    return alias


def attendee_email_aliases(meeting: dict, org_emails: set[str]) -> set[str]:
    aliases: set[str] = set()
    for attendee in meeting.get("attendees") or []:
        email = (attendee.get("email") or "").casefold()
        if not email or email in org_emails:
            continue
        alias = email_alias(email)
        if alias:
            aliases.add(alias)
    return aliases


def zoom_name_aliases(name: str) -> set[str]:
    tokens = re.findall(r"[^\W_]+", str(name or "").casefold(), flags=re.UNICODE)
    alpha_tokens = {re.sub(r"[^a-z]+", "", token) for token in tokens}
    alpha_tokens.discard("")
    collapsed = "".join(re.sub(r"[^a-z]+", "", token) for token in tokens)
    if collapsed:
        alpha_tokens.add(collapsed)
    return alpha_tokens


def alias_attendance_seconds(
    participants: list[dict], aliases: set[str], org_emails: set[str]
) -> int:
    seconds = 0
    for participant in participants or []:
        email = (participant.get("email") or "").casefold()
        if email and email in org_emails:
            continue
        if aliases.intersection(zoom_name_aliases(participant.get("name") or "")):
            seconds += int(participant.get("seconds") or 0)
    return seconds


def verified_identity_attendance(
    participants: list[dict],
    aliases: set[str],
    org_emails: set[str],
    prospect_names: list[str],
) -> tuple[int, set[str]]:
    """Match only identities anchored to this meeting's verified attendees."""
    seconds = 0
    reasons: set[str] = set()
    for participant in participants or []:
        email = (participant.get("email") or "").casefold()
        if email and email in org_emails:
            continue
        participant_name = participant.get("name") or ""
        participant_aliases = zoom_name_aliases(participant_name)
        reason = None
        if aliases.intersection(participant_aliases):
            reason = "attendee-email-alias"
        else:
            participant_tokens = re.findall(
                r"[^\W_]+", participant_name.casefold(), flags=re.UNICODE
            )
            for prospect_name in prospect_names:
                if production.same_surname(participant_name, prospect_name):
                    reason = "same-surname"
                    break
                prospect_tokens = re.findall(
                    r"[^\W_]+", prospect_name.casefold(), flags=re.UNICODE
                )
                if (
                    len(participant_tokens) == 1
                    and prospect_tokens
                    and participant_tokens[0] == prospect_tokens[0]
                    and any(alias.startswith(prospect_tokens[0]) for alias in aliases)
                ):
                    reason = "first-name-plus-email-alias"
                    break
        if reason:
            seconds += int(participant.get("seconds") or 0)
            reasons.add(reason)
    return seconds, reasons


def shadow_zoom_signal(
    participants: list[dict] | None,
    prospect_emails: set[str],
    org_emails: set[str],
    prospect_names: list[str],
    aliases: set[str],
) -> tuple[str | None, str, str]:
    """Return outcome, detail, and evidence source for the shadow Zoom pass."""
    outcome, detail = production.zoom_signal(
        participants, prospect_emails, org_emails, prospect_names
    )
    if outcome == "completed" or participants is None:
        return outcome, detail, "zoom"

    identity_seconds, identity_reasons = verified_identity_attendance(
        participants, aliases, org_emails, prospect_names
    )
    reason_label = "+".join(sorted(identity_reasons))
    if identity_seconds >= production.MIN_ATTEND_SECONDS:
        return (
            "completed",
            f"verified Zoom identity ({reason_label}) for {identity_seconds}s",
            "zoom-verified-identity",
        )
    if identity_seconds > 0:
        return (
            None,
            f"verified Zoom identity ({reason_label}) joined only "
            f"{identity_seconds}s — review",
            "zoom-verified-identity",
        )
    return outcome, detail, "zoom"


def resolve_shadow_conflict(
    *,
    zoom_outcome: str | None,
    zoom_detail: str,
    zoom_source: str,
    close_status_outcome: str | None = None,
    close_status_detail: str | None = None,
    attention_outcome: str | None = None,
    attention_detail: str | None = None,
    phone_outcome: str | None = None,
    phone_detail: str | None = None,
    disposition_outcome: str | None = None,
    disposition_detail: str | None = None,
    status_outcome: str | None = None,
    status_detail: str | None = None,
) -> tuple[str, str, str]:
    """Apply asymmetric evidence: Zoom presence is stronger than absence."""
    if zoom_outcome == "completed":
        return "completed", zoom_source, zoom_detail
    if close_status_outcome:
        return close_status_outcome, "close-status", close_status_detail or ""
    if disposition_outcome in {"no_show", "cancelled", "rescheduled"}:
        return (
            disposition_outcome,
            "attention-disposition",
            disposition_detail or "explicit negative disposition",
        )
    if attention_outcome == "completed":
        source = "attention-over-zoom" if zoom_outcome == "no_show" else "attention"
        return "completed", source, attention_detail or ""
    if phone_outcome == "completed":
        source = "phone-over-zoom" if zoom_outcome == "no_show" else "phone"
        return "completed", source, phone_detail or ""
    if attention_outcome == "review":
        return "review", "attention-identity-conflict", attention_detail or ""
    if disposition_outcome:
        if zoom_outcome == "no_show" and disposition_outcome == "completed":
            return (
                "review",
                "zoom-disposition-conflict",
                f"{zoom_detail}; {disposition_detail or 'show disposition'}",
            )
        return disposition_outcome, "attention-disposition", disposition_detail or ""
    if zoom_outcome == "no_show":
        return "no_show", zoom_source, zoom_detail
    if phone_outcome == "review":
        return "review", "phone", phone_detail or ""
    if status_outcome:
        return status_outcome, "status-rsvp", status_detail or ""
    return "review", zoom_source, zoom_detail


def projected_first_call_value(
    meeting: dict, lead: dict, shadow_outcome: str
) -> str | None:
    """Project the legacy field from the shadow outcome without writing it."""
    outcome_id = production.OUTCOMES.get(shadow_outcome)
    if outcome_id is None:
        return None
    return production.first_call_field_value(
        meeting, lead.get(production.CF_FSCBD), outcome_id
    )


def close_status_signal(meeting: dict, lead_meetings: list[dict]) -> tuple[str | None, str | None]:
    if not production.is_canceledish(meeting):
        return None, None
    if production.later_similar_meeting_exists(meeting, lead_meetings):
        return "rescheduled", "canceled + later booking exists"
    return "cancelled", "canceled, no later booking"


def guard_identity_dependent_signal(
    outcome: str | None, detail: str | None, prospect_names: list[str]
) -> tuple[str | None, str | None]:
    """Do not turn another attached contact's evidence into this lead's show."""
    if outcome != "completed" or prospect_names:
        return outcome, detail
    suffix = "prospect identity is not verified against the Close lead"
    return "review", f"{detail}; {suffix}" if detail else suffix


def evaluate_meeting(
    meeting: dict,
    lead: dict,
    lead_meetings: list[dict],
    acts: list[dict],
    calls: list[dict],
    participants: list[dict] | None,
    org_emails: set[str],
    now_utc: datetime,
) -> dict:
    attendees = meeting.get("attendees") or []
    prospect_emails = {
        (attendee.get("email") or "").casefold()
        for attendee in attendees
        if attendee.get("email")
        and (attendee.get("email") or "").casefold() not in org_emails
    }
    prospect_names = production.prospect_names_for(meeting, lead, org_emails)
    aliases = attendee_email_aliases(meeting, org_emails)
    zoom_outcome, zoom_detail, zoom_source = shadow_zoom_signal(
        participants, prospect_emails, org_emails, prospect_names, aliases
    )

    close_outcome, close_detail = close_status_signal(meeting, lead_meetings)
    attention_outcome, attention_detail = production.attention_activity_signal(
        meeting, acts
    )
    attention_outcome, attention_detail = guard_identity_dependent_signal(
        attention_outcome, attention_detail, prospect_names
    )
    phone_outcome, phone_detail = production.phone_evidence(meeting, calls, acts)
    disposition_outcome = production.attention_signal(
        meeting,
        lead.get(production.CF_TODAYS_DISPOSITION),
        lead_meetings,
        now_utc,
    )
    disposition_detail = None
    if disposition_outcome:
        disposition_detail = (
            f"disposition={lead.get(production.CF_TODAYS_DISPOSITION)!r}"
        )
    disposition_outcome, disposition_detail = guard_identity_dependent_signal(
        disposition_outcome, disposition_detail, prospect_names
    )
    external_statuses = [
        attendee.get("status")
        for attendee in attendees
        if (attendee.get("email") or "").casefold() not in org_emails
    ]
    status_outcome, status_detail = production.status_rsvp_signal(
        meeting, lead.get("status_label") or "", external_statuses
    )

    outcome, source, detail = resolve_shadow_conflict(
        zoom_outcome=zoom_outcome,
        zoom_detail=zoom_detail,
        zoom_source=zoom_source,
        close_status_outcome=close_outcome,
        close_status_detail=close_detail,
        attention_outcome=attention_outcome,
        attention_detail=attention_detail,
        phone_outcome=phone_outcome,
        phone_detail=phone_detail,
        disposition_outcome=disposition_outcome,
        disposition_detail=disposition_detail,
        status_outcome=status_outcome,
        status_detail=status_detail,
    )
    return {
        "shadow_outcome": outcome,
        "shadow_source": source,
        "shadow_detail": detail,
        "zoom_outcome": zoom_outcome,
        "zoom_detail": zoom_detail,
        "zoom_source": zoom_source,
        "prospect_names": prospect_names,
        "email_aliases": sorted(aliases),
    }


def reviewed_outcome(meeting_id: str, shadow: dict) -> dict:
    reason = REVIEWED_COMPLETED_TARGETS.get(meeting_id)
    if not reason or shadow["shadow_outcome"] == "completed":
        return shadow
    return {
        **shadow,
        "shadow_outcome": "completed",
        "shadow_source": "reviewed-override",
        "shadow_detail": reason,
    }


def run() -> int:
    now_utc = datetime.now(timezone.utc)
    since = now_utc - timedelta(days=LOOKBACK_DAYS)
    session = production.close_session()
    zoom = production.Zoom()
    if not zoom.enabled:
        raise RuntimeError("Zoom credentials are required for the shadow audit")

    users = production.fetch_org_users(session)
    org_emails = {user["email"] for user in users.values() if user.get("email")}
    meetings = production.fetch_meetings_window(session, since, now_utc)
    meetings_by_lead: dict[str, list[dict]] = defaultdict(list)
    for meeting in meetings:
        if meeting.get("lead_id"):
            meetings_by_lead[meeting["lead_id"]].append(meeting)

    brief_cache: dict[str, dict] = {}
    evidence_cache: dict[str, dict] = {}

    def lead_brief(lead_id: str) -> dict:
        if lead_id not in brief_cache:
            brief_cache[lead_id] = production.fetch_lead_brief(session, lead_id)
        return brief_cache[lead_id]

    def lead_evidence(lead_id: str) -> dict:
        if lead_id not in evidence_cache:
            evidence_cache[lead_id] = {
                "acts": production.fetch_attention_acts(session, lead_id),
                "calls": production.fetch_lead_calls(session, lead_id),
            }
        return evidence_cache[lead_id]

    report = {
        "generated_at": now_utc.isoformat(),
        "lookback_days": LOOKBACK_DAYS,
        "read_only": True,
        "no_show_scanned": 0,
        "completed_scanned": 0,
        "auto_completed": [],
        "auto_no_show": [],
        "needs_review": [],
        "completed_needs_review": [],
        "unchanged_no_show": [],
        "unchanged_completed": [],
        "focus_targets": [],
        "errors": [],
    }

    for meeting in meetings:
        current_outcome_id = meeting.get("outcome_id")
        current_outcome = next(
            (
                outcome
                for outcome, outcome_id in production.OUTCOMES.items()
                if outcome_id == current_outcome_id
            ),
            None,
        )
        if current_outcome not in {"no_show", "completed"}:
            continue
        meeting_id = meeting.get("id") or ""
        lead_id = meeting.get("lead_id") or ""
        owner = users.get(meeting.get("user_id"), {})
        if owner.get("name", "").casefold() in production.EXCLUDED_OWNER_NAMES:
            continue
        if not meeting_id or not lead_id:
            continue
        report[f"{current_outcome}_scanned"] += 1
        try:
            lead = lead_brief(lead_id)
            evidence = lead_evidence(lead_id)
            provider, zoom_id = production.video_meeting_id(meeting)
            participants = None
            if zoom_id:
                participants = zoom.participants_for(
                    zoom_id, production.parse_dt(meeting.get("starts_at"))
                )
            shadow = evaluate_meeting(
                meeting,
                lead,
                meetings_by_lead[lead_id],
                evidence["acts"],
                evidence["calls"],
                participants,
                org_emails,
                now_utc,
            )
            effective = reviewed_outcome(meeting_id, shadow)
            row = {
                "meeting_id": meeting_id,
                "lead_id": lead_id,
                "lead_name": lead.get("display_name") or "",
                "title": meeting.get("title") or "",
                "starts_at": meeting.get("starts_at"),
                "video_provider": provider,
                "current_outcome": current_outcome,
                **shadow,
                "effective_outcome": effective["shadow_outcome"],
                "effective_source": effective["shadow_source"],
                "effective_detail": effective["shadow_detail"],
                "current_first_call_show": lead.get(production.CF_FIRST_CALL_SHOW),
                "projected_first_call_show": projected_first_call_value(
                    meeting, lead, effective["shadow_outcome"]
                ),
            }
            if current_outcome == "no_show":
                if shadow["shadow_outcome"] == "completed":
                    report["auto_completed"].append(row)
                elif shadow["shadow_outcome"] == "review":
                    report["needs_review"].append(row)
                else:
                    report["unchanged_no_show"].append(row)
            else:
                if shadow["shadow_outcome"] == "no_show":
                    report["auto_no_show"].append(row)
                elif shadow["shadow_outcome"] == "review":
                    report["completed_needs_review"].append(row)
                else:
                    report["unchanged_completed"].append(row)
            expected_name = FOCUS_TARGETS.get(meeting_id) or FOCUS_LEADS.get(lead_id)
            if expected_name:
                report["focus_targets"].append({
                    **row,
                    "expected_name": expected_name,
                    "participants": participants,
                })
        except Exception as error:
            report["errors"].append({
                "meeting_id": meeting_id,
                "lead_id": lead_id,
                "error": f"{type(error).__name__}: {error}",
            })

    with open(REPORT_PATH, "w") as report_file:
        json.dump(report, report_file, indent=2, default=str)

    print("=== ZOOM CONFLICT SHADOW (READ ONLY) ===")
    print(f"No Shows scanned : {report['no_show_scanned']}")
    print(f"Completed scanned: {report['completed_scanned']}")
    print(f"Auto Completed   : {len(report['auto_completed'])}")
    print(f"Auto No Show     : {len(report['auto_no_show'])}")
    print(f"Needs review     : {len(report['needs_review'])}")
    print(f"Completed review : {len(report['completed_needs_review'])}")
    print(f"Unchanged        : {len(report['unchanged_no_show'])}")
    print(f"Completed same   : {len(report['unchanged_completed'])}")
    print(f"Errors           : {len(report['errors'])}")
    print("\n=== FOCUS TARGETS ===")
    for row in report["focus_targets"]:
        print(
            f"{row['expected_name']}: current={row['current_outcome']} "
            f"shadow={row['shadow_outcome']}[{row['shadow_source']}] "
            f"effective={row['effective_outcome']}[{row['effective_source']}] "
            f"first_call={row['current_first_call_show']!r}"
            f"->{row['projected_first_call_show']!r} "
            f"aliases={','.join(row['email_aliases']) or '-'} — "
            f"{row['effective_detail']}"
        )
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    sys.exit(run())
