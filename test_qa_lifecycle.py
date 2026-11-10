import copy
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import qa_lifecycle_policy as policy
import qa_lifecycle_runtime as runtime
from route_released_leads import (BATCH_FIELD, COMPLETED_OUTCOME, DOM_ID, NARIA_ID, ORGANIZATION_ID,
    OWNER_FIELD, STATE_FIELD, custom)
from workflow_holds import Store

NOW = datetime(2026, 11, 9, 18, tzinfo=timezone.utc)
LAUNCH = '2026-11-01T00:00:00Z'
LOST_AT = '2026-11-07T18:00:00Z'
CLOSER = 'user_original'
LOST = 'stat_lost'
PROTECTED = {'stat_dnc', 'stat_disqualified', 'stat_won'}


def config():
    return {'policy_version': policy.POLICY_VERSION, 'organization_id': ORGANIZATION_ID,
        'enabled': True, 'authorization_reference': 'user-approved-fixture',
        'calendar': 'entry_day_zero_same_central_time_observed_us_federal',
        'launched_at': LAUNCH, 'lost_events_since': LAUNCH, 'backlog_lead_ids': [], 'max_writes': 50}


def snapshot():
    lead = {'id': 'lead_one', 'organization_id': ORGANIZATION_ID, 'status_id': LOST,
        'date_updated': LOST_AT, 'num_upcoming_meetings': 0,
        f'custom.{OWNER_FIELD}': CLOSER, f'custom.{STATE_FIELD}': 'Blitz',
        f'custom.{BATCH_FIELD}': 'Original batch', f'custom.{policy.LOST_REASON}': 'Timing'}
    event = {'id': 'ev_lost', 'organization_id': ORGANIZATION_ID, 'lead_id': 'lead_one',
        'object_id': 'lead_one', 'object_type': 'lead', 'action': 'updated',
        'changed_fields': ['status_id'], 'user_id': CLOSER, 'api_key_id': None,
        'date_created': LOST_AT, 'date_updated': LOST_AT,
        'data': copy.deepcopy(lead), 'previous_data': {'status_id': 'stat_followup'}}
    return {'lead': lead, 'meetings': [{'id': 'acti_shown', 'lead_id': 'lead_one',
        'starts_at': '2026-11-06T18:00:00Z', 'status': 'completed', 'outcome_id': COMPLETED_OUTCOME}],
        'opportunities': [], 'lane2_state': 'Blitz', 'as_of': NOW, 'complete': True,
        'events': [event], 'events_complete': True, 'workflow_active': False,
        'shared_holds_verified': True, 'competing_hold': False, 'holds': [],
        'referral_evidence_complete': True, 'vendhub_referral': False,
        'suppression_evidence_complete': True, 'contact_suppressed': False,
        'calls_complete': True, 'possible_connection': False, 'qa_outcome_verified': False,
        'lost_status': LOST, 'own_qa_hold_verified': True}


def lost_plan(value):
    return policy.plan_lost_admission(value, closers={CLOSER: 'Original Closer'}, lost_status=LOST,
        suppressed_statuses=PROTECTED, activation_at=LAUNCH)


def qa_snapshot(entered='2026-11-01T18:00:00Z'):
    value = snapshot()
    value['lead'].update(policy.admission_changes(value['lead'], entered,
        original_closer='Original Closer', signal_id='ev_lost'))
    return value


def exit_plan(value):
    return policy.plan_qa_exit(value, suppressed_statuses=PROTECTED, activation_at=LAUNCH)


class CalendarTests(unittest.TestCase):
    def test_native_qa_entry_uses_exact_stored_millisecond_precision(self):
        self.assertEqual(policy.qa_field_time('2026-11-10T03:19:12.922403Z'),
                         '2026-11-10T03:19:12.922000+00:00')
        lead = snapshot()['lead']
        changes = policy.admission_changes(lead, '2026-11-10T03:19:12.922403Z',
            original_closer='Original Closer', signal_id='ev_fixture')
        self.assertEqual(changes[f'custom.{policy.QA_ASSIGNED}'], '2026-11-10T03:19:12.922000+00:00')

    def test_backlog_launch_precision_does_not_place_entry_before_activation(self):
        value = qa_snapshot('2026-11-01T18:00:00.123456Z')
        result = policy.plan_qa_exit(value, suppressed_statuses=PROTECTED,
            activation_at='2026-11-01T18:00:00.123456Z')
        self.assertNotEqual(result['reason'], 'qa_entry_outside_activation')

    def test_entry_day_zero_skips_veterans_day(self):
        self.assertEqual(policy.qa_deadline('2026-11-09T18:00:00Z').isoformat(), '2026-11-17T18:00:00+00:00')

    def test_thanksgiving_and_weekends(self):
        self.assertEqual(policy.qa_deadline('2026-11-23T16:35:12Z').isoformat(), '2026-12-01T16:35:12+00:00')

    def test_preserves_central_wall_clock_across_both_dst_changes(self):
        self.assertEqual(policy.qa_deadline('2026-03-06T18:00:00Z').isoformat(), '2026-03-13T17:00:00+00:00')
        self.assertEqual(policy.qa_deadline('2026-10-30T17:00:00Z').isoformat(), '2026-11-06T18:00:00+00:00')

    def test_observed_weekend_holidays_and_year_boundary(self):
        self.assertIn(date(2026, 7, 3), policy.federal_holidays(2026))
        self.assertIn(date(2021, 12, 31), policy.federal_holidays(2021))
        self.assertEqual(policy.qa_deadline('2021-12-27T18:00:00Z').date(), date(2022, 1, 4))

    def test_naive_times_rejected(self):
        with self.assertRaises(ValueError):
            policy.qa_deadline('2026-11-09T18:00:00')


class LostTests(unittest.TestCase):
    def test_any_retained_prior_qa_evidence_prevents_second_pass(self):
        for field, value in ((policy.QA_ASSIGNED, '2026-10-01T18:00:00Z'),
                             (policy.QA_OUTCOME, 'Qualified and ready to book'), (policy.QA_CONTACTED, 'Yes')):
            item = snapshot(); item['lead'][f'custom.{field}'] = value
            self.assertEqual(lost_plan(item)['reason'], 'existing_qa_episode')
        item = snapshot(); item['prior_qa_verified'] = True
        self.assertEqual(lost_plan(item)['reason'], 'existing_qa_episode')
        item = snapshot(); item['lead'][f'custom.{BATCH_FIELD}'] = 'Released - Original Closer'
        self.assertEqual(lost_plan(item)['reason'], 'prior_release_history_requires_review')

    def test_at_48_hours_admits_without_thirty_day_wait(self):
        value = snapshot()
        before = copy.deepcopy(value)
        result = lost_plan(value)
        self.assertEqual(result['destination'], 'qa')
        self.assertEqual(result['changes'][f'custom.{OWNER_FIELD}'], NARIA_ID)
        self.assertEqual(result['changes'][f'custom.{policy.QA_ASSIGNED}'], NOW.isoformat())
        self.assertIn('Released - Original Closer', result['changes'][f'custom.{BATCH_FIELD}'])
        self.assertEqual(value, before)

    def test_just_under_48_hours_is_held(self):
        value = snapshot(); value['as_of'] -= timedelta(microseconds=1)
        self.assertEqual(lost_plan(value)['reason'], 'lost_cooling_period_open')

    def test_only_exact_workable_reasons(self):
        for reason in policy.ALLOWED_LOST_REASONS:
            value = snapshot(); value['lead'][f'custom.{policy.LOST_REASON}'] = reason
            self.assertEqual(lost_plan(value)['destination'], 'qa', reason)
        for reason in ('Other', 'DIY- "I can do this on my own"', 'Not Interested', 'Do Not Contact',
                       'Bad Fit- "Vending is not for me"', 'VendHub referral', None):
            value = snapshot(); value['lead'][f'custom.{policy.LOST_REASON}'] = reason
            self.assertEqual(lost_plan(value)['reason'], 'lost_reason_not_qa_workable', reason)

    def test_vendhub_referral_wins_over_workable_reason(self):
        value = snapshot(); value['vendhub_referral'] = True
        self.assertEqual(lost_plan(value)['reason'], 'vendhub_referral')

    def test_automation_wrong_actor_bulk_and_incomplete_events_never_qualify(self):
        for change in ({'api_key_id': 'apikey_automation'}, {'user_id': 'user_other'},
                       {'meta': {'bulk_action_id': 'bulk_1'}}, {'previous_data': {}},
                       {'data': {'status_id': LOST}}, {'action': 'created'}):
            value = snapshot(); value['events'][0].update(change)
            self.assertEqual(lost_plan(value)['destination'], 'held', change)
        value = snapshot(); del value['events'][0]['api_key_id']
        self.assertEqual(lost_plan(value)['destination'], 'held')
        value = snapshot(); value['events_complete'] = False
        self.assertEqual(lost_plan(value)['destination'], 'held')

    def test_newer_automated_transition_does_not_borrow_older_human_lost(self):
        value = snapshot(); newer = copy.deepcopy(value['events'][0])
        newer.update(id='ev_new', api_key_id='apikey_auto', date_updated='2026-11-08T18:00:00Z')
        value['events'].append(newer)
        self.assertEqual(lost_plan(value)['destination'], 'held')

    def test_consolidation_uses_later_update_time(self):
        value = snapshot(); value['events'][0]['date_updated'] = '2026-11-08T18:00:00Z'
        self.assertEqual(lost_plan(value)['reason'], 'lost_cooling_period_open')

    def test_reassignment_after_lost_prevents_reusing_old_episode(self):
        value = snapshot(); value['events'].append({'object_type': 'lead',
            'changed_fields': [f'custom.{OWNER_FIELD}'], 'date_updated': '2026-11-08T18:00:00Z',
            'previous_data': {f'custom.{OWNER_FIELD}': CLOSER},
            'data': {f'custom.{OWNER_FIELD}': 'user_other'}})
        self.assertEqual(lost_plan(value)['destination'], 'held')

    def test_requires_shown_call_before_lost(self):
        for patch_value in ({'outcome_id': None}, {'starts_at': '2026-11-08T18:00:00Z'}):
            value = snapshot(); value['meetings'][0].update(patch_value)
            self.assertEqual(lost_plan(value)['reason'], 'shown_meeting_before_lost_required')

    def test_protects_future_won_other_active_override_and_workflow_holds(self):
        for patch_value in ({'status_type': 'won'}, {'status_type': 'active'}, {'status_type': 'unknown'}):
            value = snapshot(); value['opportunities'] = [patch_value]
            self.assertEqual(lost_plan(value)['destination'], 'held')
        for key, val in (('workflow_active', True), ('competing_hold', True), ('shared_holds_verified', False)):
            value = snapshot(); value[key] = val
            self.assertEqual(lost_plan(value)['destination'], 'held')
        value = snapshot(); value['lead']['num_upcoming_meetings'] = 1
        self.assertEqual(lost_plan(value)['destination'], 'held')
        value = snapshot(); value['lead'][f'custom.{policy.EXCLUSION}'] = 'Yes'
        self.assertEqual(lost_plan(value)['destination'], 'held')


class QaWindowTests(unittest.TestCase):
    def test_due_unconnected_requests_tier_assignment(self):
        result = exit_plan(qa_snapshot())
        self.assertEqual(result['destination'], 'tier_intake')
        self.assertNotIn(f'custom.{BATCH_FIELD}', result['changes'])

    def test_before_deadline_stays_qa(self):
        self.assertEqual(exit_plan(qa_snapshot(NOW.isoformat()))['reason'], 'qa_window_open')

    def test_calls_do_not_restart_the_deadline(self):
        value = qa_snapshot(); value['calls'] = [{'date_created': NOW.isoformat()}]
        self.assertEqual(exit_plan(value)['destination'], 'tier_intake')

    def test_connected_unknown_and_incomplete_calls_hold(self):
        for key, val in (('possible_connection', True), ('possible_connection', None), ('calls_complete', False)):
            value = qa_snapshot(); value[key] = val
            self.assertEqual(exit_plan(value)['destination'], 'held')
        value = qa_snapshot(); value['lead'][f'custom.{policy.QA_CONTACTED}'] = 'Yes'
        self.assertEqual(exit_plan(value)['destination'], 'held')

    def test_human_no_connection_can_disambiguate_voicemail(self):
        value = qa_snapshot(); value['possible_connection'] = True
        value['lead'][f'custom.{policy.QA_OUTCOME}'] = 'No connection'; value['qa_outcome_verified'] = True
        self.assertEqual(exit_plan(value)['destination'], 'tier_intake')

    def test_research_can_route_before_day_five_but_needs_actor_proof(self):
        value = qa_snapshot(NOW.isoformat()); value['lead'][f'custom.{policy.QA_OUTCOME}'] = 'Still researching'
        self.assertEqual(exit_plan(value)['reason'], 'qa_outcome_actor_or_episode_unverified')
        value['qa_outcome_verified'] = True
        self.assertEqual(exit_plan(value)['destination'], 'tier_intake')

    def test_ready_and_education_never_expire_to_uncontacted(self):
        for outcome in ('Education', 'Qualified and ready to book'):
            value = qa_snapshot(); value['lead'][f'custom.{policy.QA_OUTCOME}'] = outcome
            self.assertEqual(exit_plan(value)['changes'], {})

    def test_no_interest_ends_qa_cycle_without_returning_to_general_pool(self):
        value = qa_snapshot(); value['lead'][f'custom.{policy.QA_OUTCOME}'] = 'No longer interested in vending'
        value['qa_outcome_verified'] = True
        result = exit_plan(value)
        self.assertEqual(result['destination'], 'final_lost')
        self.assertNotIn(f'custom.{STATE_FIELD}', result['changes'])
        self.assertEqual(result['changes']['status_id'], LOST)

    def test_missing_or_legacy_entry_is_never_guessed(self):
        value = qa_snapshot(); value['lead'][f'custom.{BATCH_FIELD}'] = 'Released - Original Closer'
        self.assertEqual(exit_plan(value)['changes'], {})
        value = qa_snapshot(); value['lead'][f'custom.{policy.QA_ASSIGNED}'] = None
        self.assertEqual(exit_plan(value)['changes'], {})

    def test_fresh_backlog_window_is_exact_cohort_and_preserves_prior_dispositions(self):
        value = qa_snapshot(); value['lead'][f'custom.{BATCH_FIELD}'] = 'Released - Original Closer'
        value['lead'][f'custom.{policy.QA_CONTACTED}'] = 'Yes'
        value['lead'][f'custom.{policy.QA_OUTCOME}'] = 'Education'
        result = policy.plan_backlog_window(value, backlog_lead_ids=['lead_one'], launched_at=NOW.isoformat())
        self.assertEqual(set(result['changes']), {f'custom.{policy.QA_ASSIGNED}', f'custom.{BATCH_FIELD}'})
        value['lead'].update(result['changes'])
        self.assertEqual(policy.plan_backlog_window(value, backlog_lead_ids=['lead_one'], launched_at=NOW.isoformat())['changes'], {})
        self.assertEqual(policy.plan_backlog_window(value, backlog_lead_ids=[], launched_at=NOW.isoformat())['changes'], {})

    def test_clean_slate_applies_only_to_verified_new_booking_after_admission(self):
        value = qa_snapshot(); meeting = {'id': 'acti_rebook', 'lead_id': 'lead_one',
            'organization_id': ORGANIZATION_ID, 'date_created': '2026-11-02T18:00:00Z'}
        self.assertTrue(policy.new_qa_booking_credit(value['lead'], meeting, booking_rep_id=NARIA_ID, booking_actor_verified=True))
        self.assertFalse(policy.new_qa_booking_credit(value['lead'], meeting, booking_rep_id=CLOSER, booking_actor_verified=True))
        meeting['date_created'] = '2026-11-01T18:00:00Z'
        self.assertFalse(policy.new_qa_booking_credit(value['lead'], meeting, booking_rep_id=NARIA_ID, booking_actor_verified=True))


class FakeReader:
    def __init__(self, value, store):
        self.value, self.store, self.config = value, store, config()
        self.closers = {CLOSER: 'Original Closer'}
        self.lane2 = SimpleNamespace(S_LOST=LOST, SUPPRESS_STATUSES=PROTECTED)
        self.preflight = Mock()
        self.clock = lambda: self.value['as_of']
        self.writes = []
        self.failure = None

    def candidates(self):
        return [{'id': self.value['lead']['id']}]

    def snapshot(self, _lead_id):
        result = copy.deepcopy(self.value)
        token_match = __import__('re').search(r'\[QA2 ([a-f0-9]{24})\]', str(custom(result['lead'], BATCH_FIELD)))
        token = token_match.group(1) if token_match else None
        result['holds'] = self.store.holds(result['lead']['id'])
        result['competing_hold'] = any(h['workflow'] != 'qa' or h['token'] != token for h in result['holds'])
        result['own_qa_hold_verified'] = bool(token and any(h['workflow'] == 'qa' and h['token'] == token for h in result['holds']))
        return result

    def write_once(self, lead_id, changes):
        self.writes.append(changes)
        if self.failure:
            raise self.failure
        self.value['lead'].update(changes)
        return self.value['lead']


class NativeAdapterTests(unittest.TestCase):
    def test_write_readback_accepts_only_close_datetime_storage_precision(self):
        lead = {'id': 'lead_one', 'organization_id': ORGANIZATION_ID,
                f'custom.{policy.QA_ASSIGNED}': '2026-11-10T03:19:12.922000Z'}
        reader = SimpleNamespace(lane2=SimpleNamespace(BASE='https://api.close.com/api/v1', AUTH=('fixture','')),
                                 get=Mock(return_value=lead))
        changes = {f'custom.{policy.QA_ASSIGNED}': '2026-11-10T03:19:12.922403Z'}
        with patch.object(runtime.requests, 'put'):
            self.assertEqual(runtime.QaLifecycleReader.write_once(reader, 'lead_one', changes), lead)
            lead[f'custom.{policy.QA_ASSIGNED}'] = '2026-11-10T03:19:12.923000Z'
            with self.assertRaisesRegex(RuntimeError, 'readback_mismatch'):
                runtime.QaLifecycleReader.write_once(reader, 'lead_one', changes)

    def test_launch_capture_retries_only_read_transport_on_rate_limit(self):
        from qa_lifecycle_setup import SetupClient
        client = SetupClient.__new__(SetupClient)
        client.session = Mock()
        limited = Mock(status_code=429, headers={'Retry-After': '1'})
        accepted = Mock(status_code=200)
        accepted.json.return_value = {'data': []}
        client.session.request.side_effect = [limited, accepted]
        with patch('qa_lifecycle_setup.time.sleep') as sleep:
            self.assertEqual(client.read('/data/search/', {'query': {}}), {'data': []})
        sleep.assert_called_once_with(1)
        self.assertEqual(client.session.request.call_count, 2)
        client.session.put.assert_not_called()

    def test_qa_booking_no_show_keeps_naria_for_recovery(self):
        value = qa_snapshot()
        value['lead']['status_id'] = 'stat_noshow'
        reader = SimpleNamespace(config=config(), lane2=SimpleNamespace(S_LOST=LOST, S_NOSHOW='stat_noshow'))
        self.assertEqual(runtime.choose_plan(reader, value)['reason'], 'qa_booking_no_show_recovery')

    def test_only_native_event_envelopes_accept_offsetless_utc(self):
        self.assertEqual(runtime.native_event_time('2026-11-09T18:00:00'), NOW.isoformat())
        self.assertEqual(runtime.native_event_time('2026-11-09T12:00:00-06:00'), NOW.isoformat())
        with self.assertRaises(ValueError):
            policy.qa_deadline('2026-11-09T18:00:00')

    def test_unknown_and_positive_duration_calls_prevent_automatic_no_connection(self):
        self.assertIsNone(policy.possible_call_connection([{'disposition': None, 'duration': 0}]))
        self.assertTrue(policy.possible_call_connection([{'disposition': 'no-answer', 'duration': 20}]))
        self.assertFalse(policy.possible_call_connection([{'disposition': 'no-answer', 'duration': 0}]))

    def test_sorted_schema_readback_preserves_existing_choices(self):
        from qa_lifecycle_setup import SetupClient
        client = SetupClient.__new__(SetupClient)
        before = {'organization_id': ORGANIZATION_ID, 'id': policy.LOST_REASON,
                  'type': 'choices', 'choices': ['Timing', 'Other']}
        client.read = Mock(side_effect=[before, copy.deepcopy(before),
            {**before, 'choices': ['Need to think', 'Other', 'Timing']}])
        client.session = Mock()
        result = client.append_need_to_think()
        self.assertTrue(result['choice_added'])
        self.assertEqual(result['existing_choices_preserved'], 2)
        client.session.put.assert_called_once()

    def test_normal_staged_qa_requires_verified_receipt_and_no_other_active_deal(self):
        value = snapshot()
        value['lead']['status_id'] = 'stat_followup'
        value['lead'][f'custom.{OWNER_FIELD}'] = DOM_ID
        value['lead'][f'custom.{BATCH_FIELD}'] = 'Released - Original Closer'
        reader = SimpleNamespace(config=config(), lane2=SimpleNamespace(S_LOST=LOST,
            QA_TRIAGE_RELEASE_STATUSES=set(), SUPPRESS_STATUSES=PROTECTED))
        self.assertEqual(runtime.choose_plan(reader, value)['reason'], 'verified_staging_receipt_required')
        value['verified_staged_plan'] = {'digest': 'abc', 'originalCloserId': CLOSER}
        self.assertEqual(runtime.choose_plan(reader, value)['destination'], 'qa')
        value['opportunities'] = [{'status_type': 'active', 'user_id': 'user_other'}]
        self.assertEqual(runtime.choose_plan(reader, value)['reason'], 'other_active_deal')

    def test_legacy_lost_router_returns_before_any_request_when_delegated(self):
        with patch.dict(os.environ, {'CLOSE_API_KEY': 'synthetic-test-key'}):
            import update_lost_deals
        with patch.object(runtime, 'railway_owns_qa', return_value=True), \
                patch.object(update_lost_deals.requests, 'get', side_effect=AssertionError('unexpected request')):
            update_lost_deals.main()

    def test_railway_wrapper_requires_matched_cutover(self):
        import run_railway_qa_release as launch
        env = {'CLOSER_RELEASE_MODE': 'apply', 'QA_LIFECYCLE_CONFIG': json.dumps(config()),
            'CLOSER_RELEASE_CONFIG': json.dumps({'policy': {'release': {'closedLostHandledByQaLifecycle': True}}})}
        with patch.object(launch, 'railway_owns_qa', return_value=False):
            with self.assertRaisesRegex(ValueError, 'coordinated_cutover'):
                launch.launch_mode(env)
        with patch.object(launch, 'railway_owns_qa', return_value=True):
            self.assertEqual(launch.launch_mode(env), 'apply')
            env['QA_LIFECYCLE_MODE'] = 'observe'
            self.assertEqual(launch.launch_mode(env), 'observe')
            del env['QA_LIFECYCLE_MODE']
            env['CLOSER_RELEASE_MODE'] = 'unknown'
            with self.assertRaisesRegex(ValueError, 'mode_invalid'):
                launch.launch_mode(env)


class CoordinatorTests(unittest.TestCase):
    def test_next_pass_resumes_remaining_lead_after_write_budget(self):
        first = snapshot()
        second = copy.deepcopy(first)
        second['lead']['id'] = 'lead_two'
        second['events'][0].update(lead_id='lead_two', object_id='lead_two')
        second['events'][0]['data']['id'] = 'lead_two'
        second['meetings'][0]['lead_id'] = 'lead_two'
        class MultipleReader(FakeReader):
            def candidates(self): return [{'id': key} for key in self.values]
            def snapshot(self, key):
                self.value = self.values[key]
                return super().snapshot(key)
        reader = MultipleReader(first, self.store)
        reader.values = {'lead_one': first, 'lead_two': second}
        self.assertEqual(runtime.run(reader, apply=True, max_writes=1)['verified_writes'], 1)
        self.assertEqual(custom(second['lead'], OWNER_FIELD), CLOSER)
        self.assertEqual(runtime.run(reader, apply=True, max_writes=1)['verified_writes'], 1)
        self.assertEqual(custom(second['lead'], OWNER_FIELD), NARIA_ID)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / 'holds.sqlite')
        self.env = patch.dict(os.environ, {'WORKFLOW_HOLD_BACKEND': 'postgres'})
        self.env.start()

    def tearDown(self):
        self.env.stop(); self.store.close(); self.temp.cleanup()

    def test_observe_has_no_mutations(self):
        reader = FakeReader(snapshot(), self.store)
        runtime.run(reader)
        self.assertEqual(reader.writes, [])
        self.assertEqual(self.store.holds('lead_one'), [])

    def test_admission_has_durable_hold_and_intent_and_cannot_restart_on_retry(self):
        reader = FakeReader(snapshot(), self.store)
        first = runtime.run(reader, apply=True)
        entry = custom(reader.value['lead'], policy.QA_ASSIGNED)
        self.assertEqual(first['verified_writes'], 1)
        self.assertEqual(len(self.store.holds('lead_one')), 1)
        reader.value['as_of'] += timedelta(hours=1)
        self.assertEqual(runtime.run(reader, apply=True)['verified_writes'], 0)
        self.assertEqual(custom(reader.value['lead'], policy.QA_ASSIGNED), entry)

    def test_uncertain_write_stops_and_is_never_retried(self):
        reader = FakeReader(snapshot(), self.store); reader.failure = TimeoutError('unknown remote outcome')
        with self.assertRaises(TimeoutError):
            runtime.run(reader, apply=True)
        self.assertEqual(len(self.store.uncertain('lead_one')), 1)
        reader.failure = None
        runtime.run(reader, apply=True)
        self.assertEqual(len(reader.writes), 1)

    def test_tier_target_unavailable_does_not_clear_qa_owner(self):
        reader = FakeReader(qa_snapshot(), self.store)
        token = __import__('re').search(r'\[QA2 ([a-f0-9]{24})\]', custom(reader.value['lead'], BATCH_FIELD)).group(1)
        self.store.acquire('lead_one', 'qa', token, LAUNCH)
        allocator = SimpleNamespace(target=Mock(side_effect=RuntimeError('unavailable')))
        result = runtime.run(reader, apply=True, tier_allocator=allocator)
        self.assertEqual(result['verified_writes'], 0)
        self.assertEqual(custom(reader.value['lead'], OWNER_FIELD), NARIA_ID)

    def test_verified_tier_transfer_preserves_release_and_releases_only_own_hold(self):
        reader = FakeReader(qa_snapshot(), self.store)
        batch = custom(reader.value['lead'], BATCH_FIELD)
        token = __import__('re').search(r'\[QA2 ([a-f0-9]{24})\]', batch).group(1)
        self.store.acquire('lead_one', 'qa', token, LAUNCH)
        result = runtime.run(reader, apply=True, tier_allocator=SimpleNamespace(target=lambda *_: 'user_tier_rep'))
        self.assertEqual(result['verified_writes'], 1)
        self.assertEqual(custom(reader.value['lead'], OWNER_FIELD), 'user_tier_rep')
        self.assertEqual(custom(reader.value['lead'], BATCH_FIELD), batch)
        self.assertEqual(self.store.holds('lead_one'), [])

    def test_zero_budget_and_disabled_config_do_not_write(self):
        reader = FakeReader(snapshot(), self.store)
        self.assertEqual(runtime.run(reader, apply=True, max_writes=0)['verified_writes'], 0)
        reader.config['enabled'] = False
        with self.assertRaises(PermissionError):
            runtime.run(reader, apply=True)
        self.assertEqual(reader.writes, [])

    def test_disabled_example_is_loadable_without_dates(self):
        source = Path(__file__).with_name('qa_lifecycle_config.example.json').read_text()
        self.assertFalse(runtime.load_configuration({'QA_LIFECYCLE_CONFIG': source})['enabled'])


if __name__ == '__main__':
    unittest.main()
