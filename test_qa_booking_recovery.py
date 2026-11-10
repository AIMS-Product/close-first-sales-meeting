import copy
from datetime import datetime, timedelta, timezone
import os
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import qa_booking_recovery as recovery
import qa_lifecycle_policy as policy
import qa_lifecycle_runtime as runtime
import route_released_leads as routing
from test_qa_lifecycle import qa_snapshot, exit_plan, config, LOST, PROTECTED

MISSED_AT = datetime(2026, 11, 4, 18, tzinfo=timezone.utc)


def booked_snapshot(status):
    value = qa_snapshot('2026-11-01T18:00:00Z')
    value['lead']['status_id'] = status
    value['meetings'] = [{'id': 'acti_qa_booking', 'lead_id': value['lead']['id'],
        'date_created': '2026-11-02T18:00:00Z', 'starts_at': MISSED_AT.isoformat(),
        'status': 'declined-by-org'}]
    value['as_of'] = MISSED_AT + timedelta(days=29)
    return value


class RecoveryProtectionTests(unittest.TestCase):
    def test_all_three_statuses_keep_the_booked_lead_past_day_five(self):
        for status in recovery.RECOVERY_LEAD_STATUSES:
            with self.subTest(status=status):
                result = exit_plan(booked_snapshot(status))
                self.assertEqual(result['reason'], 'qa_booking_recovery_window')
                self.assertEqual(result['changes'], {})
                self.assertEqual(result['recovery_deadline'], (MISSED_AT + timedelta(days=30)).isoformat())

    def test_exact_thirty_day_boundary_requires_terminal_flow_never_five_day_intake(self):
        value = booked_snapshot(next(iter(recovery.RECOVERY_LEAD_STATUSES)))
        for offset, expected in ((timedelta(microseconds=-1), 'qa_booking_recovery_window'),
                                 (timedelta(), 'qa_booking_recovery_terminal_handoff_required'),
                                 (timedelta(days=1), 'qa_booking_recovery_terminal_handoff_required')):
            value['as_of'] = MISSED_AT + timedelta(days=30) + offset
            result = exit_plan(value)
            self.assertEqual(result['reason'], expected)
            self.assertEqual(result['changes'], {})

    def test_edits_outcome_arrival_and_entry_clock_do_not_restart_meeting_clock(self):
        value = booked_snapshot(next(iter(recovery.RECOVERY_LEAD_STATUSES)))
        expected = recovery.recovery_hold(value)['recovery_deadline']
        value['lead']['date_updated'] = value['as_of'].isoformat()
        value['meetings'][0]['date_updated'] = value['as_of'].isoformat()
        self.assertEqual(recovery.recovery_hold(value)['recovery_deadline'], expected)

    def test_latest_replacement_meeting_advances_existing_recovery_chain(self):
        value = booked_snapshot(next(iter(recovery.RECOVERY_LEAD_STATUSES)))
        value['meetings'].append({**value['meetings'][0], 'id': 'acti_replacement',
            'starts_at': (MISSED_AT + timedelta(days=4)).isoformat()})
        self.assertEqual(recovery.recovery_hold(value)['recovery_deadline'],
                         (MISSED_AT + timedelta(days=34)).isoformat())

    def test_native_outcomes_and_opportunity_reschedule_protect_without_lead_status_sync(self):
        for outcome in recovery.RECOVERY_OUTCOMES:
            value = booked_snapshot('other_status')
            value['meetings'][0].update(status='completed', outcome_id=outcome)
            self.assertEqual(exit_plan(value)['reason'], 'qa_booking_recovery_window')
        value['meetings'][0].update(status='completed', outcome_id=None)
        value['opportunities'] = [{'status_id': recovery.RESCHEDULE_OPPORTUNITY_STATUS}]
        self.assertEqual(exit_plan(value)['reason'], 'qa_booking_recovery_window')

    def test_cancelled_uncontacted_intake_gets_full_five_business_days(self):
        value = booked_snapshot(next(iter(recovery.RECOVERY_LEAD_STATUSES)))
        value['meetings'][0]['date_created'] = '2026-10-20T18:00:00Z'
        value['meetings'][0]['starts_at'] = '2026-10-21T18:00:00Z'
        deadline = policy.qa_deadline(routing.custom(value['lead'], policy.QA_ASSIGNED))
        value['as_of'] = deadline - timedelta(microseconds=1)
        self.assertEqual(exit_plan(value)['reason'], 'qa_window_open')
        value['as_of'] = deadline
        self.assertEqual(exit_plan(value)['reason'], 'qa_five_days_no_connection')

    def test_prior_contact_is_preserved_when_backlog_outreach_clock_is_newer_than_booking(self):
        value = booked_snapshot(next(iter(recovery.RECOVERY_LEAD_STATUSES)))
        value['lead'][f'custom.{policy.QA_ASSIGNED}'] = '2026-11-10T03:16:10.934Z'
        value['lead'][f'custom.{policy.QA_CONTACTED}'] = 'Yes'
        self.assertEqual(recovery.recovery_hold(value)['reason'], 'qa_booking_recovery_window')

    def test_malformed_missing_or_incomplete_evidence_never_releases(self):
        for field, raw in (('date_created', None), ('starts_at', 'not-a-date'),
                           ('date_created', '2027-01-01T00:00:00Z')):
            value = booked_snapshot(next(iter(recovery.RECOVERY_LEAD_STATUSES)))
            value['meetings'][0][field] = raw
            self.assertEqual(exit_plan(value)['changes'], {})
        value['complete'] = False
        self.assertEqual(exit_plan(value)['reason'], 'qa_booking_recovery_evidence_incomplete')

    def test_human_no_connection_disposition_cannot_erase_a_verified_booking(self):
        value = booked_snapshot(next(iter(recovery.RECOVERY_LEAD_STATUSES)))
        value['lead'][f'custom.{policy.QA_OUTCOME}'] = 'No connection'
        value['qa_outcome_verified'] = True
        self.assertEqual(exit_plan(value)['reason'], 'qa_booking_recovery_window')

    def test_coordinator_uses_same_protection(self):
        reader = SimpleNamespace(config=config(), lane2=SimpleNamespace(S_LOST=LOST, SUPPRESS_STATUSES=PROTECTED))
        for status in recovery.RECOVERY_LEAD_STATUSES:
            self.assertEqual(runtime.choose_plan(reader, booked_snapshot(status))['reason'],
                             'qa_booking_recovery_window')

    def test_legacy_cleaner_cannot_bypass_lifecycle_even_for_closer_owned_or_unknown_qa(self):
        with patch.dict(os.environ, {'CLOSE_API_KEY': 'offline-fixture'}):
            import lane2_state
        for owner in (routing.NARIA_ID, routing.DOM_ID, 'user_closer', None):
            for status in (lane2_state.S_NOSHOW, lane2_state.S_CANCELED):
                value = booked_snapshot(status)['lead']
                value[f'custom.{routing.OWNER_FIELD}'] = owner
                with patch.object(lane2_state, 'search', return_value=[value]), \
                     patch.object(lane2_state, '_req') as transport:
                    self.assertEqual(lane2_state.reconcile_qa_triage_terminal(apply=True), 0)
                    transport.assert_not_called()

    def test_meeting_reader_requests_native_booking_creation_timestamp(self):
        reader = routing.HandoffReader(SimpleNamespace(FALLTHROUGH='Blitz', BUCKETS=[]))
        lead = {'id': 'lead_one'}
        reader.get = Mock(return_value=lead)
        reader.related = Mock(return_value=[])
        reader.snapshot('lead_one')
        self.assertIn('date_created', reader.related.call_args_list[0].args[2].split(','))


if __name__ == '__main__':
    unittest.main()
