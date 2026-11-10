"""Protect booked QA episodes from the initial no-contact release clock."""
from datetime import timedelta

from route_released_leads import custom, has_qa_history, instant, QA_ASSIGNED, QA_CONTACTED

RECOVERY_WINDOW = timedelta(days=30)
RECOVERY_LEAD_STATUSES = frozenset((
    'stat_5CqIgNJnGYO357zXjSnH6BAkKyoCvYUOBxVvpYfDMZn',  # No Show
    'stat_hWIGHjzyNpl4YjIFSFz3VK4fp2ny10SFJLKAihmo4KT',  # Canceled
    'stat_aP51Sa9Qv3IthtjKGhvtEriEiLgEB9WS7uvX1w5RP08',  # Reschedule Requested
))
RECOVERY_OUTCOMES = frozenset((
    'outcome_032DjoyPo9BgPBdOF6DzqH',  # No Show
    'outcome_032DjpoQ9otqb8rGb7SIYt',  # Cancelled
    'outcome_032Djo72GJ2Lvw3Q296wxH',  # Rescheduled
))
RESCHEDULE_OPPORTUNITY_STATUS = 'stat_fiEyGOvVizCBUKbW691nwHwpDcKCQJMRy3I7h76js3T'
CANCELED_MEETINGS = frozenset(('canceled', 'cancelled', 'declined-by-lead', 'declined-by-org'))


def recovery_hold(snapshot):
    """Return a hold for a booked QA episode, or None for initial outreach.

    Native booking creation and scheduled starts establish separate clocks.
    Missing legacy provenance holds for review instead of guessing that a
    canceled booking was an uncontacted QA intake. This function never releases
    a recovery lead: day 30 requires the separate Non-responsive/rest handoff.
    """
    lead = snapshot['lead']
    if not has_qa_history(lead):
        return None
    meetings = snapshot.get('meetings', [])
    signaled = (lead.get('status_id') in RECOVERY_LEAD_STATUSES
        or any(o.get('status_id') == RESCHEDULE_OPPORTUNITY_STATUS
               for o in snapshot.get('opportunities', []))
        or any(m.get('outcome_id') in RECOVERY_OUTCOMES or m.get('status') in CANCELED_MEETINGS
               for m in meetings))
    if not signaled:
        return None
    result = {'destination': 'held', 'changes': {}}
    if snapshot.get('complete') is not True:
        return {**result, 'reason': 'qa_booking_recovery_evidence_incomplete'}
    try:
        entered = instant(custom(lead, QA_ASSIGNED))
        # The launch backlog intentionally has a fresh outreach clock. A prior
        # connection cannot be erased by that clock or treated as no contact.
        connected = custom(lead, QA_CONTACTED) == 'Yes'
        booked = []
        for meeting in meetings:
            created = instant(meeting.get('date_created'))
            starts = instant(meeting.get('starts_at'))
            if created > snapshot['as_of']:
                raise ValueError('future_booking_creation')
            if created >= entered or connected:
                booked.append(starts)
    except (ValueError, TypeError):
        return {**result, 'reason': 'qa_booking_recovery_provenance_unverified'}
    if not booked:
        if connected:
            return {**result, 'reason': 'qa_booking_recovery_provenance_unverified'}
        return None
    deadline = max(booked) + RECOVERY_WINDOW
    return {**result, 'reason': 'qa_booking_recovery_window' if snapshot['as_of'] < deadline
            else 'qa_booking_recovery_terminal_handoff_required',
            'recovery_deadline': deadline.isoformat()}
