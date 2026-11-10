"""Native Close adapters for the Lost cooling period and QA work window.

The shared production fence, committed intents and readback coordinate writes.
An uncertain PUT is never retried. Observe mode performs no provider/DB writes.
"""
from collections import Counter
from contextlib import nullcontext
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
from types import SimpleNamespace

import requests

from qa_lifecycle_policy import (ALLOWED_LOST_REASONS, EXCLUSION, LOST_REASON,
    LEGACY_DNC, POLICY_VERSION, QA_ASSIGNED, QA_CONTACTED, QA_OUTCOME, QA_OUTCOMES,
    admission_changes, plan_backlog_window, plan_lost_admission, plan_qa_exit,
    held, qa_deadline, qa_field_time, possible_call_connection)
from route_released_leads import (BATCH_FIELD, HandoffReader, NARIA_ID, ORGANIZATION_ID,
    DOM_ID, OWNER_FIELD, STATE_FIELD, custom, instant, handoff_plan, has_qa_history)

MAX_WRITES = 50
MAX_SOURCE_AGE = timedelta(seconds=60)
MAX_EVENT_PAGES = 100
MAX_EXAMINED = 100
MAX_PASS_TIME = timedelta(minutes=4)


def native_event_time(value):
    """Match the existing owner-history adapter's legacy Close UTC envelope.

    Close's event envelope can omit the offset; meeting/QA/user dates remain
    strict. Normalize only those two provider-generated envelope timestamps.
    """
    if not isinstance(value, str):
        raise ValueError('qa_event_timestamp_missing')
    if not re.search(r'(Z|[+-]\d{2}:\d{2})$', value):
        value += 'Z'
    return instant(value).isoformat()


def railway_owns_qa():
    path = Path(__file__).with_name('qa_lifecycle_deployment.json')
    if not path.exists():
        return False
    value = json.loads(path.read_text())
    if (value.get('policy_version') != POLICY_VERSION or value.get('organization_id') != ORGANIZATION_ID
            or value.get('execution_owner') != 'railway' or not isinstance(value.get('enabled'), bool)):
        raise ValueError('qa_deployment_contract_invalid')
    return value['enabled']


def load_configuration(env=None):
    env = os.environ if env is None else env
    source = env.get('QA_LIFECYCLE_CONFIG')
    if not source:
        return None
    config = json.loads(source)
    if (config.get('policy_version') != POLICY_VERSION or config.get('organization_id') != ORGANIZATION_ID
            or not isinstance(config.get('enabled'), bool) or not config.get('authorization_reference')
            or config.get('calendar') != 'entry_day_zero_same_central_time_observed_us_federal'
            or type(config.get('max_writes')) is not int or not 1 <= config['max_writes'] <= MAX_WRITES
            or not isinstance(config.get('backlog_lead_ids'), list)
            or len(set(config['backlog_lead_ids'])) != len(config['backlog_lead_ids'])
            or any(not re.fullmatch(r'lead_\w+', key) for key in config['backlog_lead_ids'])):
        raise ValueError('invalid_qa_lifecycle_configuration')
    if not config['enabled']:
        return config
    config['launched_at'] = qa_field_time(config['launched_at'])
    instant(config['lost_events_since'])
    if instant(config['lost_events_since']) > instant(config['launched_at']):
        raise ValueError('lost_cutoff_after_launch')
    if type(config.get('max_examined', MAX_EXAMINED)) is not int or not 1 <= config.get('max_examined', MAX_EXAMINED) <= MAX_EXAMINED:
        raise ValueError('invalid_qa_examined_budget')
    return config


def active_workflow_query():
    return {'type': 'has_related', 'this_object_type': 'lead', 'related_object_type': 'contact',
            'related_query': {'type': 'has_related', 'this_object_type': 'contact',
                'related_object_type': 'sequence_subscription', 'related_query': {
                    'type': 'field_condition', 'field': {'type': 'regular_field',
                        'object_type': 'sequence_subscription', 'field_name': 'status'},
                    'condition': {'type': 'term', 'values': ['active']}}}}


class QaLifecycleReader(HandoffReader):
    def __init__(self, lane2, store, config, closers, clock=None):
        super().__init__(lane2)
        self.store, self.config, self.closers = store, config, closers
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.suppression_reader, self.suppression_at = None, None

    def contact_suppression(self, lead):
        if custom(lead, LEGACY_DNC):
            return True
        from close_status_reader import CloseClient, SnapshotReader
        if self.suppression_at is None or self.clock() - self.suppression_at > MAX_SOURCE_AGE:
            self.suppression_reader = SnapshotReader(CloseClient(self.lane2.AUTH[0]))
            self.suppression_reader.refresh_unsubscribed()
            self.suppression_at = self.clock()
        contacts = lead.get('contacts')
        if not isinstance(contacts, list):
            raise RuntimeError('qa_contact_consent_read_incomplete')
        for contact in contacts:
            if not isinstance(contact.get('phones'), list) or not isinstance(contact.get('emails'), list):
                raise RuntimeError('qa_contact_consent_read_incomplete')
            if any(re.sub(r'\D', '', phone.get('phone') or '') in self.suppression_reader.blocked_phones
                   for phone in contact['phones']):
                return True
            if any(email.get('is_unsubscribed') or (email.get('email') or '').strip().lower()
                   in self.suppression_reader.unsubscribed for email in contact['emails']):
                return True
        return False

    def preflight(self):
        super().preflight()
        for fid, choices in ((QA_OUTCOME, QA_OUTCOMES), (QA_CONTACTED, {'Yes'}),
                             (LOST_REASON, ALLOWED_LOST_REASONS)):
            definition = self.get(f'/custom_field/lead/{fid}/')
            if (definition.get('type') != 'choices' or definition.get('accepts_multiple_values') is not False
                    or not choices.issubset(set(definition.get('choices', [])))):
                raise RuntimeError('qa_field_choices_require_configuration')
        definition = self.get(f'/custom_field/lead/{QA_ASSIGNED}/')
        if definition.get('type') != 'datetime':
            raise RuntimeError('qa_assigned_at_field_type_mismatch')
        if not set(self.closers) or NARIA_ID in self.closers:
            raise RuntimeError('active_closer_roster_unverified')

    def candidates(self):
        owner = {'type': 'field_condition', 'field': {'type': 'custom_field', 'custom_field_id': OWNER_FIELD},
                 'condition': {'type': 'reference', 'reference_type': 'user',
                    'object_ids': sorted(self.closers if self.config.get('observe_phase') == 'lost' else [*self.closers, DOM_ID])}}
        qa_owner = {'type': 'field_condition', 'field': {'type': 'custom_field', 'custom_field_id': OWNER_FIELD},
                    'condition': {'type': 'reference', 'reference_type': 'user', 'object_ids': [NARIA_ID]}}
        qa_state = {'type': 'field_condition', 'field': {'type': 'custom_field', 'custom_field_id': STATE_FIELD},
                    'condition': {'type': 'term', 'values': ['QA Triage']}}
        lost = self.lane2.status_in([self.lane2.S_LOST])
        reason = {'type': 'field_condition', 'field': {'type': 'custom_field', 'custom_field_id': LOST_REASON},
                  'condition': {'type': 'term', 'values': sorted(ALLOWED_LOST_REASONS)}}
        lost_query = {'type': 'and', 'queries': [owner, lost, reason]}
        staged_owner = {'type': 'field_condition', 'field': {'type': 'custom_field', 'custom_field_id': OWNER_FIELD},
                        'condition': {'type': 'reference', 'reference_type': 'user', 'object_ids': [DOM_ID]}}
        staged_marker = {'type': 'field_condition', 'field': {'type': 'custom_field', 'custom_field_id': BATCH_FIELD},
                         'condition': {'type': 'text', 'mode': 'full_words', 'value': 'Released'}}
        staged_query = {'type': 'and', 'queries': [staged_owner, staged_marker]}
        qa_query = {'type': 'and', 'queries': [qa_owner, qa_state]}
        phase = self.config.get('observe_phase')
        query = self.lane2._wrap(lost_query if phase == 'lost' else qa_query if phase == 'backlog'
            else {'type': 'or', 'queries': [lost_query, qa_query, staged_query]})
        rows = self.lane2.search(query, fields=['id', 'status_id', f'custom.{OWNER_FIELD}'], limit=self.lane2.SKIP_CAP)
        if len(rows) >= self.lane2.SKIP_CAP or len({r['id'] for r in rows}) != len(rows):
            raise RuntimeError('qa_candidate_census_incomplete')
        return rows

    def events(self, lead_id, object_type):
        rows, seen, cursor = [], set(), None
        # Close retains events for 30 days. Exact-lead reads remain bounded and
        # unknown historical actor evidence never falls back to current fields.
        for _ in range(MAX_EVENT_PAGES):
            params = {'lead_id': lead_id, 'object_type': object_type, '_limit': 50}
            if cursor:
                params['_cursor'] = cursor
            page = self.get('/event/', params)
            if not isinstance(page.get('data'), list) or 'cursor_next' not in page:
                raise RuntimeError('qa_event_pagination_unverified')
            for row in page['data']:
                if (row.get('lead_id') != lead_id or row.get('organization_id') != ORGANIZATION_ID
                        or row.get('object_type') != object_type or not row.get('id') or row['id'] in seen):
                    raise RuntimeError('qa_event_scope_or_identity_mismatch')
                seen.add(row['id'])
                row = {**row, 'date_created': native_event_time(row.get('date_created')),
                       'date_updated': native_event_time(row.get('date_updated'))}
                rows.append(row)
            next_cursor = page['cursor_next']
            if not next_cursor:
                return rows
            if next_cursor == cursor or not page['data']:
                raise RuntimeError('qa_event_cursor_stalled')
            cursor = next_cursor
        raise RuntimeError('qa_event_read_incomplete')

    def snapshot(self, lead_id):
        # Full custom values are required to detect a separately recorded VendHub
        # referral. They are never emitted in ordinary job summaries.
        fields = 'id,organization_id,status_id,date_updated,num_upcoming_meetings,date_created,custom,contacts'
        before = self.get(f'/lead/{lead_id}/', {'_fields': fields})
        if before.get('id') != lead_id or before.get('organization_id') != ORGANIZATION_ID:
            raise RuntimeError('qa_lead_scope_mismatch')
        now = self.clock()
        batch = str(custom(before, BATCH_FIELD) or '')
        if (custom(before, OWNER_FIELD) == NARIA_ID and custom(before, STATE_FIELD) == 'QA Triage'
                and lead_id in self.config['backlog_lead_ids'] and '[QA2 ' not in batch):
            # Starting an explicitly approved backlog window changes only its
            # timestamp/episode marker; prior conversations and outcomes survive.
            holds = self.store.holds(lead_id)
            after = self.get(f'/lead/{lead_id}/', {'_fields': fields})
            if before != after:
                raise RuntimeError('qa_backlog_changed_during_read')
            return {'lead': after, 'as_of': now, 'complete': True,
                    'shared_holds_verified': True, 'competing_hold': bool(holds), 'holds': holds}
        if custom(before, OWNER_FIELD) == NARIA_ID and not custom(before, QA_OUTCOME):
            entry = custom(before, QA_ASSIGNED)
            if entry and now < qa_deadline(entry):
                return {'lead': before, 'as_of': now, 'complete': False, 'inspection_reason': 'qa_window_open'}
        if custom(before, OWNER_FIELD) in self.closers and custom(before, LOST_REASON) not in ALLOWED_LOST_REASONS:
            return {'lead': before, 'as_of': now, 'complete': False,
                    'inspection_reason': 'lost_reason_not_qa_workable'}
        contact_suppressed = self.contact_suppression(before)
        owner = custom(before, OWNER_FIELD)
        if owner in (NARIA_ID, DOM_ID):
            base = super().snapshot(lead_id)
        else:
            # Lost admission does not use Lane 2's derived intake bucket. Avoid
            # recomputing every bucket or reading QA calls for closer-owned leads.
            base = {'meetings': self.related('/activity/meeting/', lead_id,
                    'id,lead_id,starts_at,ends_at,status,outcome_id'),
                    'lane2_state': custom(before, STATE_FIELD) or self.lane2.FALLTHROUGH}
        base['lead'] = before
        base['opportunities'] = self.related('/opportunity/', lead_id,
            'id,organization_id,lead_id,status_id,status_label,status_type,user_id,custom')
        events = self.events(lead_id, 'lead') + self.events(lead_id, 'opportunity')
        receipts = self.store.connection.execute(
            "SELECT payload FROM workflow_write WHERE lead_id=? AND state='verified'", (lead_id,)).fetchall()
        prior_qa = any(has_qa_history(event.get(version) or {}) for event in events
                       if event.get('object_type') == 'lead' for version in ('data', 'previous_data'))
        for receipt in receipts:
            payload = receipt['payload']
            if isinstance(payload, str):
                payload = json.loads(payload)
            prior_qa = prior_qa or (payload.get('policy_version') == POLICY_VERSION
                and payload.get('destination') in ('qa', 'backlog_window'))
        calls = self.related('/activity/call/', lead_id,
            'id,lead_id,user_id,date_created,activity_at,direction,status,disposition,duration') if owner == NARIA_ID else []
        scoped = {'type': 'id', 'value': lead_id}
        workflows = self.lane2.search(self.lane2._wrap(scoped, active_workflow_query()), fields=['id'], limit=2)
        if any(row.get('id') != lead_id for row in workflows):
            raise RuntimeError('qa_workflow_scope_mismatch')
        holds = self.store.holds(lead_id)
        markers = re.findall(r'\[QA2 ([a-f0-9]{24})\]', str(custom(before, BATCH_FIELD) or ''))
        own_token = markers[0] if len(markers) == 1 else None
        competing = any(row.get('workflow') != 'qa' or row.get('token') != own_token for row in holds)
        after = self.get(f'/lead/{lead_id}/', {'_fields': fields})
        if before != after:
            raise RuntimeError('qa_lead_changed_during_read')
        now = self.clock()
        entered_raw = custom(before, QA_ASSIGNED)
        try:
            entered = instant(entered_raw) if entered_raw else now
        except (ValueError, TypeError):
            entered = now
        # Positive-duration/answered calls are possible human connections. Only
        # exact QA-actor calls in this admission window affect the fallback.
        qa_calls = [call for call in calls if call.get('user_id') == NARIA_ID
                    and instant(call.get('activity_at') or call['date_created']) >= entered]
        possible_connection = possible_call_connection(qa_calls)
        outcome = custom(before, QA_OUTCOME)
        outcome_key = f'custom.{QA_OUTCOME}'
        outcome_events = [e for e in events if e.get('object_id') == lead_id
                          and outcome_key in e.get('changed_fields', [])]
        latest_outcome = max(outcome_events, key=lambda e: instant(e['date_updated']), default={})
        outcome_verified = bool(latest_outcome and latest_outcome.get('user_id') == NARIA_ID
            and 'api_key_id' in latest_outcome and latest_outcome['api_key_id'] is None
            and custom(latest_outcome.get('data') or {}, QA_OUTCOME) == outcome
            and entered <= instant(latest_outcome['date_updated']) <= now)
        custom_values = [value for key, value in before.items() if key.startswith('custom.')]
        custom_values += list((before.get('custom') or {}).values())
        for opportunity in base['opportunities']:
            custom_values += [value for key, value in opportunity.items() if key.startswith('custom.')]
            custom_values += list((opportunity.get('custom') or {}).values())
        vendhub = any(re.search(r'\bvend\s*hub\b', str(value), re.I) for value in custom_values)
        staged_closer = None
        staged_plan = None
        if custom(after, OWNER_FIELD) == DOM_ID:
            # The existing producer's verified journal is the authority for a
            # temporary Dom owner, not a closer name guessed from batch text.
            journal = self.store.raw.execute('''select plan from crm_release.staged_release_action
                where company_id=%s and organization_id=%s and lead_id=%s and state='verified' ''',
                (self.store.TENANT.split('/')[0], ORGANIZATION_ID, lead_id)).fetchall()
            if len(journal) == 1:
                plan = journal[0]['plan']
                if isinstance(plan, str):
                    plan = json.loads(plan)
                if all(custom(after, key[7:]) == value for key, value in plan['after'].items()):
                    staged_closer = plan.get('originalCloserId')
                    staged_plan = plan
        return {**base, 'lead': after, 'events': events, 'events_complete': True,
            'prior_qa_verified': prior_qa,
            'calls': qa_calls, 'calls_complete': True, 'possible_connection': possible_connection,
            'qa_outcome_verified': outcome_verified, 'workflow_active': bool(workflows),
            'shared_holds_verified': True, 'competing_hold': competing, 'holds': holds,
            'own_qa_hold_verified': bool(own_token and any(h.get('workflow') == 'qa' and h.get('token') == own_token for h in holds)),
            'referral_evidence_complete': True, 'vendhub_referral': vendhub,
            'suppression_evidence_complete': True, 'contact_suppressed': contact_suppressed,
            'verified_staged_closer_id': staged_closer,
            'verified_staged_plan': staged_plan,
            'lost_status': self.lane2.S_LOST, 'as_of': now, 'complete': True}

    def write_once(self, lead_id, changes):
        response = requests.put(self.lane2.BASE + f'/lead/{lead_id}/', auth=self.lane2.AUTH,
                                json=changes, timeout=45)
        response.raise_for_status()
        actual = self.get(f'/lead/{lead_id}/')
        if actual.get('id') != lead_id or actual.get('organization_id') != ORGANIZATION_ID:
            raise RuntimeError('qa_write_readback_scope_mismatch')
        for key, expected in changes.items():
            got = custom(actual, key[7:]) if key.startswith('custom.') else actual.get(key)
            if key == f'custom.{QA_ASSIGNED}' and got is not None and expected is not None:
                matches = instant(got) == instant(qa_field_time(expected))
            else:
                matches = got == expected
            if not matches:
                raise RuntimeError('qa_write_readback_mismatch')
        return actual


class ExistingTierAllocator:
    """Apply existing stored-permission tier rules to a coordinated QA transfer."""
    def __init__(self, store):
        self.store = store
        self.source = None

    def target(self, snapshot, exit_plan):
        from read_tier_assignment_preview import capture_roster
        from tier_assignment import allocate_sample
        from tier_routing_pilot import PilotSource
        if self.source is None:
            self.source = PilotSource(Path(__file__).with_name('qa_tier_tag_adapter.py'))
        roster, _ = capture_roster(lead_ids=[snapshot['lead']['id']])
        rep_ids = tuple(sorted({account['provider_user_id'] for row in roster['rows']
            if row['status'] == 'active' for account in row.get('close_accounts') or []
            if account.get('provider_user_id') and account.get('provider_user_id') != NARIA_ID}))
        if not rep_ids:
            raise RuntimeError('qa_tier_active_reps_unavailable')
        scope = SimpleNamespace(rep_ids=rep_ids, lead_ids=(snapshot['lead']['id'],))
        fresh = self.source.read(scope, store=self.store, permission_source='stored')
        item = fresh['items'][0]
        for contact in item.lead.get('contacts', []):
            subscriptions = self.source.client.list('/sequence_subscription/', {'contact_id': contact['id'],
                '_fields': 'id,contact_id,lead_id,sequence_id,status'})
            if any(s.get('contact_id') != contact['id'] or s.get('lead_id') not in (None, item.lead['id']) for s in subscriptions):
                raise RuntimeError('qa_tier_workflow_scope_mismatch')
            if subscriptions:
                raise RuntimeError('qa_tier_existing_workflow_subscription')
        # Project the proposed coordinated owner/state transition into the PURE
        # allocator. The real QA owner/hold remain until a target is revalidated.
        expected = snapshot['lead']
        for key in (OWNER_FIELD, STATE_FIELD, BATCH_FIELD, QA_ASSIGNED, QA_OUTCOME, QA_CONTACTED):
            if custom(item.lead, key) != custom(expected, key):
                raise RuntimeError('qa_changed_before_tier_allocation')
        projected = {**item.lead, **exit_plan['changes']}
        token = re.search(r'\[QA2 ([a-f0-9]{24})\]', custom(expected, BATCH_FIELD)).group(1)
        other_holds = tuple(h for h in item.holds or () if h.get('workflow') != 'qa' or h.get('token') != token)
        projected_item = replace(item, lead=projected, holds=other_holds,
                                 evidence=replace(item.evidence, owners=()))
        proposal = allocate_sample([projected_item], fresh['reps'], fresh['closers'], datetime.now(timezone.utc),
            fresh['memberships'], fresh['counts'], 1000)[0]
        if not proposal.get('target_owner'):
            raise RuntimeError('qa_tier_assignment_unavailable')
        return proposal['target_owner']


def choose_plan(reader, snapshot):
    if snapshot.get('inspection_reason'):
        return held(snapshot['inspection_reason'])
    config = reader.config
    lead = snapshot['lead']
    if custom(lead, OWNER_FIELD) == DOM_ID and lead.get('status_id') != reader.lane2.S_LOST:
        staged = snapshot.get('verified_staged_plan')
        if not staged:
            return held('verified_staging_receipt_required')
        plan = handoff_plan(snapshot, now=snapshot['as_of'],
            terminal_statuses=reader.lane2.QA_TRIAGE_RELEASE_STATUSES,
            suppressed_statuses=reader.lane2.SUPPRESS_STATUSES)
        if plan['destination'] != 'qa':
            return held('staged_' + plan['reason'])
        active = [opportunity for opportunity in snapshot['opportunities'] if opportunity.get('status_type') == 'active']
        if len(active) > 1 or any(opportunity.get('user_id') != staged['originalCloserId'] for opportunity in active):
            return held('other_active_deal')
        from qa_lifecycle_policy import common_protection
        protection = common_protection(snapshot, reader.lane2.SUPPRESS_STATUSES, allow_target_active=True)
        if protection:
            return held(protection)
        entered = snapshot['as_of'].isoformat()
        return {'destination': 'qa', 'reason': 'thirty_day_shown_release',
            'signal': {'id': staged['digest'], 'closer_id': staged['originalCloserId'], 'kind': 'staged'},
            'changes': admission_changes(lead, entered, signal_id=staged['digest'])}
    if custom(lead, OWNER_FIELD) == NARIA_ID:
        if lead.get('status_id') == getattr(reader.lane2, 'S_NOSHOW', None) and has_qa_history(lead):
            return held('qa_booking_no_show_recovery')
        if lead['id'] in config['backlog_lead_ids'] and '[QA2 ' not in str(custom(lead, BATCH_FIELD)):
            return plan_backlog_window(snapshot, backlog_lead_ids=config['backlog_lead_ids'], launched_at=config['launched_at'])
        return plan_qa_exit(snapshot, suppressed_statuses=reader.lane2.SUPPRESS_STATUSES,
                            activation_at=config['launched_at'])
    return plan_lost_admission(snapshot, closers=reader.closers, lost_status=reader.lane2.S_LOST,
        suppressed_statuses=reader.lane2.SUPPRESS_STATUSES, activation_at=config['lost_events_since'])


def _stable(snapshot):
    return {key: value for key, value in snapshot.items() if key != 'as_of'}


def _read_hold_reason(error):
    response = getattr(error, 'response', None)
    if response is not None:
        return 'held_source_http_' + str(response.status_code)
    reason = str(error)
    if isinstance(error, RuntimeError) and re.fullmatch(r'[a-z_]+', reason):
        return 'held_' + reason
    if isinstance(error, ValueError) and re.fullmatch(r'[a-z_]+', reason):
        return 'held_' + reason
    return 'held_source_read'


def run(reader, *, apply=False, max_writes=None, tier_allocator=None):
    config = reader.config
    if apply and (not config['enabled'] or config.get('observe_phase')
                  or os.environ.get('WORKFLOW_HOLD_BACKEND') != 'postgres'):
        raise PermissionError('qa_apply_requires_enabled_policy_and_shared_postgres')
    reader.preflight()
    budget = min(config['max_writes'], max_writes if max_writes is not None else config['max_writes'])
    counts, written = Counter(), 0
    allocator = tier_allocator or ExistingTierAllocator(reader.store)
    started = reader.clock()
    cursor_key = 'qa-lifecycle-cursor|' + ORGANIZATION_ID
    cursor_row = reader.store.connection.execute('SELECT value FROM workflow_meta WHERE key=?', (cursor_key,)).fetchone()
    cursor = cursor_row['value'] if cursor_row else ''
    backlog = set(config['backlog_lead_ids'])
    # Start with closer Lost, then initialize the approved backlog; the saved
    # cursor rotates the complete population without restarting any QA clock.
    order_key = lambda row: ('0' if row.get('status_id') == reader.lane2.S_LOST
        and custom(row, OWNER_FIELD) in reader.closers else '1' if row['id'] in backlog else '2') + '|' + row['id']
    candidates = sorted(reader.candidates(), key=order_key)
    candidates = [row for row in candidates if order_key(row) > cursor] + [row for row in candidates if order_key(row) <= cursor]
    examined = 0

    def advance(row, *, fenced=False):
        if apply:
            with nullcontext() if fenced else reader.store.serialized():
                reader.store.connection.execute('INSERT INTO workflow_meta(key,value) VALUES(?,?) '
                    'ON CONFLICT(key) DO UPDATE SET value=excluded.value', (cursor_key, order_key(row)))

    for row in candidates:
        if apply and written >= budget:
            break
        if examined >= config.get('max_examined', MAX_EXAMINED) or reader.clock() - started >= MAX_PASS_TIME:
            break
        examined += 1
        try:
            snapshot = reader.snapshot(row['id'])
            plan = choose_plan(reader, snapshot)
        except (RuntimeError, ValueError, requests.RequestException) as error:
            counts[_read_hold_reason(error)] += 1
            advance(row)
            continue
        counts[plan['reason']] += 1
        if not apply or not plan['changes'] or written >= budget:
            advance(row)
            continue
        with reader.store.serialized():
            if reader.store.uncertain(row['id']):
                counts['held_uncertain_prior_write'] += 1
                advance(row, fenced=True)
                continue
            try:
                fresh = reader.snapshot(row['id'])
                fresh_plan = choose_plan(reader, fresh)
                if (reader.clock() - snapshot['as_of'] > MAX_SOURCE_AGE
                        or _stable(snapshot) != _stable(fresh)
                        or fresh_plan['destination'] != plan['destination']
                        or fresh_plan['reason'] != plan['reason']):
                    counts['held_changed_before_write'] += 1
                    advance(row, fenced=True)
                    continue
                changes = fresh_plan['changes']
                if fresh_plan['destination'] == 'tier_intake':
                    changes = {**changes, f'custom.{OWNER_FIELD}': allocator.target(fresh, fresh_plan)}
                    latest = reader.snapshot(row['id'])
                    if _stable(latest) != _stable(fresh):
                        counts['held_changed_during_tier_allocation'] += 1
                        advance(row, fenced=True)
                        continue
                if fresh_plan['destination'] == 'qa':
                    # Stamp at the actual write boundary, not when the census began.
                    entered = reader.clock().isoformat()
                    signal = fresh_plan['signal']
                    changes = admission_changes(fresh['lead'], entered,
                        original_closer=None if signal.get('kind') == 'staged' else reader.closers[signal['closer_id']],
                        signal_id=signal['id'])
                batch = changes.get(f'custom.{BATCH_FIELD}', custom(fresh['lead'], BATCH_FIELD))
                token = re.search(r'\[QA2 ([a-f0-9]{24})\]', batch).group(1)
            except (RuntimeError, ValueError, requests.RequestException):
                counts['held_prewrite_validation'] += 1
                advance(row, fenced=True)
                continue
            if fresh_plan['destination'] in ('qa', 'backlog_window'):
                reader.store.acquire(row['id'], 'qa', token, reader.clock().isoformat(),
                    payload={'policy_version': POLICY_VERSION, 'original_batch': custom(fresh['lead'], BATCH_FIELD)})
            intent = reader.store.intent(row['id'], token, {'policy_version': POLICY_VERSION,
                'destination': fresh_plan['destination'], 'patch': changes,
                'before': {key: custom(fresh['lead'], key[7:]) if key.startswith('custom.')
                           else fresh['lead'].get(key) for key in changes}})
            # No catch surrounds the mutation: uncertain writes stop this pass
            # and retain their durable intent and QA hold for reconciliation.
            reader.write_once(row['id'], changes)
            reader.store.receipt(intent)
            if fresh_plan['destination'] == 'tier_intake':
                reader.store.release(row['id'], 'qa', token)
            written += 1
            counts['verified_' + fresh_plan['destination']] += 1
            advance(row, fenced=True)
    result = {'policy_version': POLICY_VERSION, 'mode': 'apply' if apply else 'observe',
              'verified_writes': written, 'counts': dict(counts), 'examined': examined,
              'remaining_in_pass': len(candidates) - examined}
    print(json.dumps(result, sort_keys=True))
    return result


def run_configured(lane2, *, apply=False, max_writes=None):
    config = load_configuration()
    if config is None or not config['enabled']:
        return {'verified_writes': 0, 'reason': 'qa_lifecycle_not_configured'}
    if apply and not railway_owns_qa():
        raise PermissionError('qa_production_routing_delegation_not_enabled')
    from close_status_reader import load_roster
    from workflow_holds import configured_store
    if os.environ.get('WORKFLOW_HOLD_BACKEND') != 'postgres':
        raise RuntimeError('qa_shared_postgres_required_including_observe')
    now = datetime.now(timezone.utc)
    roster = load_roster(now)
    # Native organization memberships provide display names, never a guessed map.
    store = configured_store()
    if store is None:
        raise RuntimeError('qa_shared_workflow_store_required')
    try:
        store.raw.execute("SELECT set_config('app.company_id',%s,false),set_config('app.close_organization_id',%s,false)",
                          (store.TENANT.split('/')[0], ORGANIZATION_ID))
        if not apply:
            store.raw.execute('SET default_transaction_read_only = on')
        native = HandoffReader(lane2).get(f'/organization/{ORGANIZATION_ID}/')
        closers = {}
        for membership in native['memberships']:
            key = membership.get('user_id') or (membership.get('user') or {}).get('id')
            if key in roster.closer_ids:
                name = membership.get('user_full_name') or ' '.join(filter(None,
                    (membership.get('user_first_name'), membership.get('user_last_name'))))
                if isinstance(name, str) and name.strip() and not re.search(r'[\r\n|]', name):
                    closers[key] = name.strip()
        # An unavailable/unnamed closer remains outside this handoff population;
        # it must not halt independently verified closers or the current QA queue.
        if not closers:
            raise RuntimeError('native_closer_names_unverified')
        return run(QaLifecycleReader(lane2, store, config, closers), apply=apply, max_writes=max_writes)
    finally:
        store.close()
