#!/usr/bin/env python3
"""Read-only verification for the user-provided lead names."""

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


def exact_lead(session, name):
    response = production.close_get(
        session,
        "/lead/",
        {
            "query": f'"{name}"',
            "_limit": 20,
            "_fields": (
                f"id,display_name,{production.CF_FSCBD},"
                f"{production.CF_FIRST_CALL_SHOW}"
            ),
        },
    )
    exact = [
        lead for lead in response.get("data", [])
        if (lead.get("display_name") or "").strip().casefold() == name.casefold()
    ]
    return exact[0] if len(exact) == 1 else None, len(exact)


def first_sales_meeting(session, lead):
    response = production.close_get(
        session,
        "/activity/meeting/",
        {
            "lead_id": lead["id"],
            "_limit": 100,
            "_fields": "id,title,starts_at,outcome_id,status",
        },
    )
    fscbd = str(lead.get(production.CF_FSCBD) or "")[:10]
    candidates = []
    for meeting in response.get("data", []):
        starts_at = production.parse_dt(meeting.get("starts_at"))
        title = meeting.get("title") or ""
        if not starts_at or str(production.pacific_date(starts_at)) != fscbd:
            continue
        if not production.SALES_TITLE_RE.search(title):
            continue
        if production.FOLLOWUP_TITLE_RE.search(title):
            continue
        candidates.append(meeting)
    return min(candidates, key=lambda meeting: meeting.get("starts_at") or "") \
        if candidates else None


def run():
    session = production.close_session()
    failures = 0
    for name in NAMES:
        lead, exact_count = exact_lead(session, name)
        if lead is None:
            print(f"{name} | exact_leads={exact_count} | unable_to_verify")
            failures += 1
            continue
        meeting = first_sales_meeting(session, lead)
        field_value = lead.get(production.CF_FIRST_CALL_SHOW) or "blank"
        outcome = OUTCOME_LABELS.get((meeting or {}).get("outcome_id"), "missing")
        shown = str(field_value).casefold() == "yes" and outcome == "completed"
        print(
            f"{name} | first_call_show={field_value} | "
            f"meeting_outcome={outcome} | shown={'yes' if shown else 'no'}"
        )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(run())
