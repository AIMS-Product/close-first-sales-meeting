"""Pure SMS completion/rest policy. All datetimes are explicit UTC instants."""
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import hashlib
from zoneinfo import ZoneInfo

from close_status_policy import OWNER, NONRESPONSIVE, HOT, DNC, timestamp
from workflow_holds import BATCH, OWNER_SINCE, MARKER, custom, project
from lead_tier_policy import classify_age

TIER = 'cf_Mpz1pnxLOSjscFDAErSDYOV0PSjlWKXSgBN4jWd73sM'
OVERRIDE = 'cf_6PnYz6aaAkLzHMU3Faxz7kFurBEfadqlfGiBgfhjaVC'
BUSINESS = 'cf_aJlNlilQZIgLLuhcymNN8fiOzewnFxrbWjLZFPmsucO'
TERMINAL_SEQUENCE = 'seq_0t2QdSrHmlBOCNpUEr3nVD'
NOT_INTERESTED = 'stat_QaUUQJHyfv4tyvA6G8lmyIP6btkoiNsLBQ3rGxTybAm'
PROTECTED = frozenset((DNC, NOT_INTERESTED,
    'stat_0oW3iRpVp9z5DJq0cuwI1HgR0XhHAhykEPPIq4TFsxd',
    'stat_p3oblSTnbsyDAw4rWqZDePGYMOlKBgV2FjbqIMDrfvF',
    'stat_YV4ZngDB4IGjLjlOf0YTFEWuKZJ6fhNxVkzQkvKYfdB'))
REST_DAYS = 90


def owners(lead):
    value = custom(lead, OWNER)
    if value is None: return ()
    if isinstance(value, str): return (value,)
    if isinstance(value, list) and all(isinstance(x, str) for x in value): return tuple(sorted(value))
    raise ValueError('Invalid owner field')


@dataclass(frozen=True)
class Calendar:
    timezone: str = 'America/Los_Angeles'
    holidays: frozenset = frozenset()

    def deadline(self, entered):
        local = entered.astimezone(ZoneInfo(self.timezone)) + timedelta(days=1)
        while local.weekday() >= 5 or local.date().isoformat() in self.holidays:
            local += timedelta(days=1)
        return local.astimezone(entered.tzinfo)


@dataclass(frozen=True)
class Snapshot:
    lead: dict
    evidence: object
    task: dict | None = None
    holds: tuple = ()
    signal_at: datetime | None = None
    signal_id: str | None = None
    sales_subscriptions: tuple = ()
    unknown_subscriptions: tuple = ()
    captured_at: datetime | None = None


@dataclass(frozen=True)
class Plan:
    action: str
    reason: str
    cycle: dict | None = None
    lead_patch: dict = field(default_factory=dict)
    task_patch: dict = field(default_factory=dict)
    pause_subscriptions: tuple = ()
    release_hold: bool = False


def protection(snapshot, roster, now, cycle=None):
    lead, evidence = snapshot.lead, snapshot.evidence
    if not roster.valid(now): return 'active_closer_roster_unavailable_or_stale'
    if set(owners(lead)) & roster.closer_ids: return 'active_closer_owner'
    if any(o.owner in roster.closer_ids for o in evidence.opportunities):
        return 'active_closer_opportunity_owner'
    if any(o.status_type == 'won' or 'won' in o.label.lower() or 'do not contact' in o.label.lower() for o in evidence.opportunities):
        return 'won_or_dnc_opportunity'
    if not evidence.complete: return 'incomplete_evidence'
    if evidence.suppressions: return 'stop_dnc_or_unsubscribe'
    if lead['status_id'] in PROTECTED: return 'protected_status'
    if evidence.future_meeting: return 'booked'
    if custom(lead, OVERRIDE) == 'Yes': return 'reassignment_override'
    if custom(lead, BUSINESS) not in (None, '', 'Vendingpreneurs (VP)'): return 'different_offer'
    for hold in snapshot.holds:
        if not cycle or (hold['workflow'], hold['token']) != ('sms_rest', cycle['token']):
            return 'another_workflow_hold'
    batch = str(custom(lead, BATCH) or '')
    for workflow, token in MARKER.findall(batch):
        if not cycle or (workflow, token) != ('sms_rest', cycle['token']): return 'another_workflow_marker'
    cleaned = MARKER.sub('', batch)
    if 'STH1' in cleaned: return 'malformed_hold_marker'
    if evidence.review_reasons: return 'evidence_requires_review'
    return None


def admit(snapshot, roster, now, calendar=Calendar(), *, launch_at=None):
    reason = protection(snapshot, roster, now)
    if reason: return Plan('excluded', reason)
    task = snapshot.task or {}
    if ((task.get('text') or '').strip() != 'VP THE END'
            or task.get('sequence_id') != TERMINAL_SEQUENCE or task.get('is_complete')):
        return Plan('excluded', 'not_open_terminal_sms_task')
    if task.get('lead_id') != snapshot.lead['id']: return Plan('excluded', 'task_lead_mismatch')
    occurred = timestamp(task['date_created'])
    if occurred > now: return Plan('excluded', 'future_terminal_task')
    if launch_at and occurred < launch_at: return Plan('excluded', 'prelaunch_backlog')
    owner = owners(snapshot.lead)
    if len(owner) != 1 or not custom(snapshot.lead, OWNER_SINCE):
        return Plan('excluded', 'single_current_owner_and_generation_required')
    token = hashlib.sha256((snapshot.lead['id'] + ':' + task['id']).encode()).hexdigest()[:24]
    deadline = calendar.deadline(now)
    cycle = {'lead_id': snapshot.lead['id'], 'token': token, 'phase': 'grace',
             'task_id': task['id'], 'terminal_at': occurred.isoformat(),
             'admitted_at': now.isoformat(), 'grace_expires_at': deadline.isoformat(),
             'due_at': deadline.isoformat(), 'original_owner': owner[0],
             'original_owner_since': custom(snapshot.lead, OWNER_SINCE),
             'original_batch': custom(snapshot.lead, BATCH), 'rest_started_at': None,
             'rest_expires_at': None, 'signal_at': None, 'signal_id': None}
    signal = snapshot.signal_at or snapshot.evidence.latest_inbound
    if signal and occurred <= signal <= now:
        cycle.update(phase='rep_working', due_at=None, signal_at=signal.isoformat(),
                     signal_id=snapshot.signal_id)
        return Plan('rep_working', 'prospect_responded_preserve_rep_disposition', cycle)
    return Plan('grace', 'final_call_window', cycle,
                task_patch={'assigned_to': owner[0], 'date': deadline.isoformat()})


def transition(cycle, snapshot, roster, now):
    reason = protection(snapshot, roster, now, cycle)
    if reason: return Plan('excluded', reason, cycle)
    phase = cycle['phase']
    if phase in ('grace', 'rep_working'):
        if (owners(snapshot.lead) != (cycle['original_owner'],)
                or custom(snapshot.lead, OWNER_SINCE) != cycle['original_owner_since']):
            return Plan('excluded', 'owner_generation_changed', cycle)
    elif phase == 'rest':
        if owners(snapshot.lead): return Plan('excluded', 'rest_lead_manually_claimed', cycle)
        own = ('sms_rest', cycle['token'])
        if own not in MARKER.findall(str(custom(snapshot.lead, BATCH) or '')):
            return Plan('excluded', 'rest_projection_missing', cycle)
    else:
        return Plan('keep', 'terminal_cycle', cycle)
    signal = snapshot.signal_at or snapshot.evidence.latest_inbound
    threshold = timestamp(cycle['rest_started_at'] or cycle['terminal_at'])
    if signal and threshold <= signal <= now:
        updated = dict(cycle, signal_at=signal.isoformat(), signal_id=snapshot.signal_id)
        if phase in ('grace', 'rep_working'):
            updated.update(phase='rep_working', due_at=None)
            return Plan('rep_working', 'prospect_responded_preserve_rep_disposition', updated)
        updated.update(phase='return_requested', due_at=now.isoformat(), return_reason='qualifying_signal')
        return Plan('return_requested', 'qualifying_signal', updated)
    if phase == 'rep_working': return Plan('keep', 'rep_disposition_pending', cycle)
    if now < timestamp(cycle['due_at']): return Plan('keep', 'timer_not_due', cycle)
    if phase == 'rest':
        updated = dict(cycle, phase='return_requested', due_at=now.isoformat(), return_reason='ninety_days_elapsed')
        return Plan('return_requested', 'ninety_days_elapsed', updated)
    if snapshot.unknown_subscriptions: return Plan('excluded', 'unclassified_active_workflow', cycle)
    expiry = now + timedelta(days=REST_DAYS)
    updated = dict(cycle, phase='rest', rest_started_at=now.isoformat(),
                   rest_expires_at=expiry.isoformat(), due_at=expiry.isoformat())
    return Plan('rest', 'no_response_after_final_call_window', updated,
                lead_patch={'status_id': NONRESPONSIVE, 'custom.' + OWNER: None,
                            'custom.' + BATCH: project(custom(snapshot.lead, BATCH), 'sms_rest', cycle['token'], acquire=True)},
                pause_subscriptions=snapshot.sales_subscriptions)


def return_to_rep(cycle, snapshot, roster, now, target_owner, eligible_reps):
    reason = protection(snapshot, roster, now, cycle)
    if reason: return Plan('excluded', reason, cycle)
    if cycle['phase'] != 'return_requested' or owners(snapshot.lead):
        return Plan('excluded', 'return_requires_unowned_request', cycle)
    if target_owner not in eligible_reps or target_owner in roster.closer_ids:
        return Plan('excluded', 'target_not_ready_lane2_rep', cycle)
    age = classify_age(snapshot.lead, now)
    if age.tier is None:
        return Plan('excluded', age.reason, cycle)
    signal = cycle['return_reason'] == 'qualifying_signal'
    batch = project(custom(snapshot.lead, BATCH), 'sms_rest', cycle['token'], acquire=False)
    cohort = f"[STR1 sms_reactivation {cycle['token']}]"
    batch = (batch + ' ' if batch else '') + cohort
    patch = {'custom.' + OWNER: target_owner, 'custom.' + BATCH: batch,
             'custom.' + TIER: [age.tier]}
    if signal: patch['status_id'] = HOT
    updated = dict(cycle, phase='returned', due_at=None, returned_at=now.isoformat(), returned_owner=target_owner)
    return Plan('returned', cycle['return_reason'], updated, lead_patch=patch, release_hold=True)
