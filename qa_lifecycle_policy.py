"""QA lifecycle decisions. No credentials, network requests or CRM writes."""
import hashlib
import re
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from route_released_leads import (BATCH_FIELD, COMPLETED_OUTCOME, DOM_ID, NARIA_ID,
    ORGANIZATION_ID, OWNER_FIELD, OVERRIDE_FIELD, STATE_FIELD, WON_OPPORTUNITY_STATUSES,
    custom, has_release_marker, instant)

QA_ASSIGNED = 'cf_PTKwT43eUFf8fVIwd4ScmyyWNxCcU5ftss3pjq3GLje'
QA_OUTCOME = 'cf_HUTa2ZuyQSz5aft9Ob5Tu4wTXn3iAJTXZo3052tvEOM'
QA_CONTACTED = 'cf_YOCYZ08NZRvvZnT7BiQd20HMbyXK8zRT6Ewz82lYZN4'
LOST_REASON = 'cf_R4i05fLNOQP8yveAs4ofTMMYGAQnkLLklunP4lov2Bt'
EXCLUSION = 'cf_dOgbJRRRYimvIh9POsEsQpGhX7rI3Dm8gz0RheQeL73'
LEGACY_DNC = 'cf_L1HjUOMsqq6sSADgSMn4aH2bma8ohho5enH7jeL1yRJ'
CENTRAL = ZoneInfo('America/Chicago')
COOLING_PERIOD = timedelta(hours=48)
QA_DAYS = 5
POLICY_VERSION = 'qa-lost-48h-five-federal-business-days-v1'
ALLOWED_LOST_REASONS = frozenset((
    'Price- "Thats more than I can afford to pay"', 'Timing', 'Need to think',
    'Support- "Spouse/partner won\'t get onboard"', 'Unresponsive'))
QA_OUTCOMES = frozenset(('Education', 'No connection', 'No longer interested in vending',
                         'Qualified and ready to book', 'Still researching'))


def _weekday(year, month, weekday, occurrence):
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (occurrence - 1))


def federal_holidays(year):
    """Nationwide federal holidays, including Friday/Monday observance."""
    days = set()
    # Adjacent years cover a Saturday New Year's Day observed on December 31.
    for y in (year - 1, year, year + 1):
        fixed = [date(y, 1, 1), date(y, 7, 4), date(y, 11, 11), date(y, 12, 25)]
        if y >= 2021:
            fixed.append(date(y, 6, 19))
        for day in fixed:
            days.add(day + timedelta(days=-1 if day.weekday() == 5 else 1 if day.weekday() == 6 else 0))
        days.update((_weekday(y, 1, 0, 3), _weekday(y, 2, 0, 3),
                     date(y, 5, 31) - timedelta(days=date(y, 5, 31).weekday()),
                     _weekday(y, 9, 0, 1), _weekday(y, 10, 0, 2), _weekday(y, 11, 3, 4)))
    return frozenset(day for day in days if day.year == year)


def qa_deadline(entered_at):
    """Entry is day zero; preserve Central wall-clock time across DST."""
    entered = instant(entered_at).astimezone(CENTRAL)
    day, counted = entered.date(), 0
    while counted < QA_DAYS:
        day += timedelta(days=1)
        if day.weekday() < 5 and day not in federal_holidays(day.year):
            counted += 1
    deadline = datetime.combine(day, entered.timetz().replace(tzinfo=None), CENTRAL)
    return deadline.astimezone(timezone.utc)


def held(reason):
    return {'destination': 'held', 'reason': reason, 'changes': {}}


def receipt_id(lead_id, signal_id):
    return hashlib.sha256(f'{POLICY_VERSION}|{lead_id}|{signal_id}'.encode()).hexdigest()[:24]


def possible_call_connection(calls):
    """False is complete non-connection evidence; None is an unknown call."""
    unknown = False
    for call in calls:
        duration, disposition = call.get('duration'), call.get('disposition')
        if disposition == 'answered' or (isinstance(duration, (int, float)) and not isinstance(duration, bool) and duration > 0):
            return True
        if disposition not in ('busy', 'no-answer', 'failed', 'canceled'):
            unknown = True
    return None if unknown else False


def common_protection(snapshot, suppressed_statuses, *, allow_target_active=False):
    lead = snapshot['lead']
    if lead.get('organization_id') != ORGANIZATION_ID:
        return 'organization_mismatch'
    if snapshot.get('complete') is not True:
        return 'incomplete_source_read'
    if snapshot.get('shared_holds_verified') is not True or snapshot.get('competing_hold') is not False:
        return 'competing_or_unverified_shared_hold'
    if snapshot.get('suppression_evidence_complete') is not True or snapshot.get('contact_suppressed') is not False:
        return 'contact_suppression_or_unknown_consent'
    if custom(lead, LEGACY_DNC):
        return 'legacy_do_not_contact'
    if custom(lead, OVERRIDE_FIELD) not in (None, '', 'No') or custom(lead, EXCLUSION) == 'Yes':
        return 'manual_override_or_exclusion'
    batch = custom(lead, BATCH_FIELD) or ''
    if not isinstance(batch, str) or 'STH1' in batch or 'STL1' in batch:
        return 'workflow_hold_or_separate_qa_episode'
    if snapshot.get('workflow_active') is not False:
        return 'active_or_unverified_workflow'
    if lead.get('status_id') in suppressed_statuses:
        return 'protected_lead_status'
    if lead.get('num_upcoming_meetings') is None:
        return 'upcoming_meeting_count_missing'
    if lead['num_upcoming_meetings'] > 0:
        return 'future_meeting'
    now = snapshot['as_of']
    for meeting in snapshot['meetings']:
        try:
            starts = instant(meeting.get('starts_at'))
            ends = instant(meeting['ends_at']) if meeting.get('ends_at') else None
        except (TypeError, ValueError):
            return 'meeting_timestamp_unverified'
        if meeting.get('status') not in ('canceled', 'declined-by-lead', 'declined-by-org'):
            if starts >= now or meeting.get('status') == 'in-progress' or (ends and ends > now):
                return 'future_or_in_progress_meeting'
    for opportunity in snapshot['opportunities']:
        if opportunity.get('status_type') == 'won' or opportunity.get('status_id') in WON_OPPORTUNITY_STATUSES:
            return 'won_opportunity'
        if re.search(r'do not contact|disqualified|vend\s*hub', str(opportunity.get('status_label', '')), re.I):
            return 'protected_opportunity_status'
        if opportunity.get('status_type') not in ('active', 'lost', 'won'):
            return 'opportunity_status_unverified'
        if opportunity.get('status_type') == 'active' and not allow_target_active:
            return 'active_deal_protection'
    return None


def manual_lost_signal(snapshot, closer_id, lost_status, since):
    """Use the latest transition per current Lost object, never an older human one.

    api_key_id must be explicitly null. An API key owned by the closer still is
    automation. Event consolidation uses date_updated as the conservative clock.
    """
    if snapshot.get('events_complete') is not True:
        return None
    lead = snapshot['lead']
    objects = {lead['id']: lost_status} if lead.get('status_id') == lost_status else {}
    for opportunity in snapshot['opportunities']:
        if opportunity.get('status_type') == 'lost' and opportunity.get('user_id') == closer_id:
            objects[opportunity['id']] = opportunity['status_id']
    latest = {}
    for event in snapshot['events']:
        if event.get('object_id') not in objects or 'status_id' not in event.get('changed_fields', []):
            continue
        key = event['object_id']
        if key not in latest or instant(event['date_updated']) > instant(latest[key]['date_updated']):
            latest[key] = event
    valid = []
    for key, event in latest.items():
        data, previous = event.get('data') or {}, event.get('previous_data') or {}
        if (event.get('organization_id') != ORGANIZATION_ID or event.get('lead_id') != lead['id']
                or event.get('action') != 'updated' or not str(event.get('id', '')).startswith('ev_')
                or event.get('user_id') != closer_id or 'api_key_id' not in event or event['api_key_id'] is not None
                or (event.get('meta') or {}).get('bulk_action_id') or data.get('updated_by_agent_id')
                or data.get('status_id') != objects[key] or 'status_id' not in previous
                or previous['status_id'] == data['status_id']):
            continue
        owner_at_event = custom(data, OWNER_FIELD) if key == lead['id'] else data.get('user_id')
        if owner_at_event != closer_id:
            continue
        at = max(instant(event['date_created']), instant(event['date_updated']))
        if not since <= at <= snapshot['as_of']:
            continue
        # Away-and-back reassignment after Lost cannot borrow the old closer episode.
        owner_changes = [e for e in snapshot['events'] if e.get('object_type') == 'lead' and
            f'custom.{OWNER_FIELD}' in e.get('changed_fields', []) and
            instant(e['date_updated']) > at and
            (e.get('previous_data') or {}).get(f'custom.{OWNER_FIELD}') != custom(e.get('data') or {}, OWNER_FIELD)]
        known_staging = (snapshot.get('verified_staged_closer_id') == closer_id
            and custom(lead, OWNER_FIELD) == DOM_ID and len(owner_changes) == 1
            and (owner_changes[0].get('previous_data') or {}).get(f'custom.{OWNER_FIELD}') == closer_id
            and custom(owner_changes[0].get('data') or {}, OWNER_FIELD) == DOM_ID)
        if not owner_changes or known_staging:
            valid.append({'id': event['id'], 'at': at.isoformat(), 'closer_id': closer_id,
                          'object_id': key, 'status_id': objects[key]})
    return max(valid, key=lambda item: instant(item['at'])) if valid else None


def admission_changes(lead, entered_at, *, original_closer=None, signal_id=None):
    batch = custom(lead, BATCH_FIELD) or ''
    if original_closer:
        tag = f'Released - {original_closer}'
        if not has_release_marker(batch):
            batch = f'{batch} | {tag}' if batch else tag
    if not has_release_marker(batch):
        raise ValueError('original_closer_marker_required')
    episode = receipt_id(lead['id'], signal_id or entered_at)
    marker = f'[QA2 {episode}]'
    return {f'custom.{OWNER_FIELD}': NARIA_ID, f'custom.{STATE_FIELD}': 'QA Triage',
            f'custom.{QA_ASSIGNED}': entered_at, f'custom.{QA_OUTCOME}': None,
            f'custom.{QA_CONTACTED}': None, f'custom.{BATCH_FIELD}': f'{batch} | {marker}'}


def plan_backlog_window(snapshot, *, backlog_lead_ids, launched_at):
    """One explicit launch cohort; preserve prior outcome and contact evidence."""
    lead = snapshot['lead']
    if (lead.get('organization_id') != ORGANIZATION_ID or lead.get('id') not in backlog_lead_ids
            or custom(lead, OWNER_FIELD) != NARIA_ID or custom(lead, STATE_FIELD) != 'QA Triage'):
        return held('not_launch_backlog')
    if snapshot.get('complete') is not True or snapshot.get('shared_holds_verified') is not True:
        return held('backlog_source_unverified')
    if snapshot.get('competing_hold') is not False:
        return held('competing_shared_hold')
    batch = custom(lead, BATCH_FIELD)
    if not has_release_marker(batch) or 'STH1' in batch or 'STL1' in batch:
        return held('backlog_marker_or_hold_unverified')
    if '[QA2 ' in batch:
        return held('qa_window_already_started')
    launch = instant(launched_at)
    if launch > snapshot['as_of']:
        return held('launch_not_started')
    entered = launch.isoformat()
    marker = receipt_id(lead['id'], 'backlog:' + entered)
    return {'destination': 'backlog_window', 'reason': 'one_time_fresh_backlog_window',
            'deadline': qa_deadline(entered).isoformat(),
            'changes': {f'custom.{QA_ASSIGNED}': entered,
                        f'custom.{BATCH_FIELD}': f'{batch} | [QA2 {marker}]'}}


def plan_lost_admission(snapshot, *, closers, lost_status, suppressed_statuses, activation_at):
    lead, now = snapshot['lead'], snapshot['as_of']
    closer_id = snapshot.get('verified_staged_closer_id') or custom(lead, OWNER_FIELD)
    if closer_id not in closers or lead.get('status_id') != lost_status:
        return held('not_current_closer_lost')
    if custom(lead, STATE_FIELD) == 'QA Triage' or '[QA2 ' in str(custom(lead, BATCH_FIELD)):
        return held('existing_qa_episode')
    reason = common_protection(snapshot, suppressed_statuses)
    if reason:
        return held(reason)
    if custom(lead, LOST_REASON) not in ALLOWED_LOST_REASONS:
        return held('lost_reason_not_qa_workable')
    # A referral may be recorded separately from Lost Reason. Full custom data is required.
    if snapshot.get('referral_evidence_complete') is not True:
        return held('referral_evidence_unverified')
    if snapshot.get('vendhub_referral') is not False:
        return held('vendhub_referral')
    signal = manual_lost_signal(snapshot, closer_id, lost_status, instant(activation_at))
    if not signal:
        return held('closer_manual_lost_evidence_unverified')
    lost_at = instant(signal['at'])
    if now < lost_at + COOLING_PERIOD:
        return held('lost_cooling_period_open')
    if not any(m.get('outcome_id') == COMPLETED_OUTCOME and instant(m['starts_at']) <= lost_at
               for m in snapshot['meetings']):
        return held('shown_meeting_before_lost_required')
    entered = now.isoformat()
    return {'destination': 'qa', 'reason': 'closer_lost_48_hours', 'signal': signal,
            'deadline': qa_deadline(entered).isoformat(),
            'changes': admission_changes(lead, entered, original_closer=closers[closer_id], signal_id=signal['id'])}


def plan_qa_exit(snapshot, *, suppressed_statuses, activation_at):
    lead, now = snapshot['lead'], snapshot['as_of']
    if custom(lead, OWNER_FIELD) != NARIA_ID or custom(lead, STATE_FIELD) != 'QA Triage':
        return held('not_current_naria_qa')
    batch = custom(lead, BATCH_FIELD)
    if not has_release_marker(batch) or not isinstance(batch, str) or not re.search(r'\[QA2 [a-f0-9]{24}\]', batch):
        return held('legacy_qa_entry_requires_explicit_backlog_policy')
    try:
        entered = instant(custom(lead, QA_ASSIGNED))
    except (ValueError, TypeError):
        return held('qa_entry_time_unverified')
    if not instant(activation_at) <= entered <= now:
        return held('qa_entry_outside_activation')
    if snapshot.get('own_qa_hold_verified') is not True:
        return held('qa_episode_hold_unverified')
    outcome = custom(lead, QA_OUTCOME)
    if outcome not in (None, '', *QA_OUTCOMES):
        return held('qa_outcome_unverified')
    if outcome in ('Education', 'Qualified and ready to book'):
        return held('connected_outcome_awaits_verified_downstream_handoff')
    protection = common_protection(snapshot, suppressed_statuses)
    if protection:
        return held(protection)
    if outcome and snapshot.get('qa_outcome_verified') is not True:
        return held('qa_outcome_actor_or_episode_unverified')
    if outcome == 'No longer interested in vending':
        # Retain the QA hold but remove the active QA owner: this closes the cycle
        # without silently making an uninterested prospect assignable again.
        return {'destination': 'final_lost', 'reason': 'qa_no_longer_interested',
                'changes': {'status_id': snapshot['lost_status'], f'custom.{OWNER_FIELD}': None}}
    researching = outcome == 'Still researching'
    if not researching:
        if now < qa_deadline(entered.isoformat()):
            return held('qa_window_open')
        if snapshot.get('calls_complete') is not True:
            return held('qa_calls_incomplete')
        if custom(lead, QA_CONTACTED) == 'Yes' or (outcome != 'No connection' and snapshot.get('possible_connection') is not False):
            return held('connected_without_disposition_requires_review')
    state = snapshot['lane2_state']
    if state in ('Booked', 'Suppressed', 'QA Triage'):
        return held('lane2_intake_protected')
    # The coordinator must obtain an actual tier intake acknowledgement before
    # this payload may clear QA ownership. An unowned lead alone is not a receipt.
    return {'destination': 'tier_intake', 'reason': 'qa_researching' if researching else 'qa_five_days_no_connection',
            'changes': {f'custom.{OWNER_FIELD}': None, f'custom.{STATE_FIELD}': state,
                        f'custom.{QA_OUTCOME}': 'Still researching' if researching else 'No connection'}}


def new_qa_booking_credit(lead, meeting, *, booking_rep_id, booking_actor_verified):
    """Credit eligibility only; never infer a shown/qualified meeting or payment."""
    try:
        entered, booked = instant(custom(lead, QA_ASSIGNED)), instant(meeting['date_created'])
    except (ValueError, TypeError, KeyError):
        return False
    return bool(booking_actor_verified is True and booking_rep_id == NARIA_ID
        and has_release_marker(custom(lead, BATCH_FIELD)) and '[QA2 ' in str(custom(lead, BATCH_FIELD))
        and meeting.get('organization_id') == ORGANIZATION_ID and meeting.get('lead_id') == lead.get('id')
        and isinstance(meeting.get('id'), str) and meeting['id'].startswith('acti_') and booked > entered)
