"""The QA owner change retains the same episode, deadline and earned credit."""
import copy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from qa_identity import QA_REP_ID, LEGACY_QA_REP_ID, QA_SLACK_ID, LEGACY_QA_SLACK_ID
import qa_lifecycle_policy as policy
import qa_lifecycle_runtime as runtime
import qa_slack_notifications as alerts
from qa_rep_transfer import transfer_changes, verify_transfer, OWNER_SINCE_FIELD
from route_released_leads import OWNER_FIELD, BATCH_FIELD, STATE_FIELD, ORGANIZATION_ID, custom
from test_qa_lifecycle import qa_snapshot, exit_plan, config, NOW, LOST, PROTECTED


class QaRepHandoffTests(unittest.TestCase):
    def test_transfer_writes_only_owner_and_rejects_changed_history(self):
        before = qa_snapshot()['lead']
        before[f'custom.{OWNER_FIELD}'] = LEGACY_QA_REP_ID
        before['custom'] = {'Lead Owner': LEGACY_QA_REP_ID, 'QA Contacted': 'Yes'}
        self.assertEqual(transfer_changes(before), {f'custom.{OWNER_FIELD}': QA_REP_ID})
        after = {**before, **transfer_changes(before)}
        after['custom'] = {**before['custom'], 'Lead Owner': QA_REP_ID}
        after[f'custom.{OWNER_SINCE_FIELD}'] = NOW.isoformat()
        after['custom']['Current Owner Since'] = NOW.isoformat()
        verify_transfer(before, after)
        after[f'custom.{policy.QA_ASSIGNED}'] = NOW.isoformat()
        with self.assertRaisesRegex(RuntimeError, 'readback_mismatch'):
            verify_transfer(before, after)
        for owner, state in ((QA_REP_ID, 'QA Triage'), (LEGACY_QA_REP_ID, 'Blitz')):
            with self.assertRaisesRegex(ValueError, 'not_current_legacy_qa'):
                transfer_changes({**before, f'custom.{OWNER_FIELD}': owner, f'custom.{STATE_FIELD}': state})

    def test_owner_transfer_keeps_episode_and_deadline_and_existing_outcome(self):
        old = qa_snapshot()
        old['lead'][f'custom.{OWNER_FIELD}'] = LEGACY_QA_REP_ID
        old['lead'][f'custom.{policy.QA_OUTCOME}'] = 'Still researching'
        old['qa_outcome_verified'] = True
        new = copy.deepcopy(old)
        new['lead'][f'custom.{OWNER_FIELD}'] = QA_REP_ID
        self.assertEqual(exit_plan(old), exit_plan(new))
        self.assertEqual(policy.qa_deadline(custom(old['lead'], policy.QA_ASSIGNED)),
                         policy.qa_deadline(custom(new['lead'], policy.QA_ASSIGNED)))
        self.assertEqual(custom(old['lead'], BATCH_FIELD), custom(new['lead'], BATCH_FIELD))

    def test_connected_legacy_episode_cannot_be_expired_after_transfer(self):
        value = qa_snapshot()
        value['possible_connection'] = True
        self.assertEqual(exit_plan(value)['reason'], 'connected_without_disposition_requires_review')
        value['lead'][f'custom.{OWNER_FIELD}'] = LEGACY_QA_REP_ID
        self.assertEqual(exit_plan(value)['reason'], 'connected_without_disposition_requires_review')

    def test_booking_credit_preserves_actual_legacy_actor_after_owner_transfer(self):
        value = qa_snapshot()
        meeting = {'id': 'acti_original', 'organization_id': ORGANIZATION_ID,
                   'lead_id': value['lead']['id'], 'date_created': NOW.isoformat()}
        for rep in (QA_REP_ID, LEGACY_QA_REP_ID):
            self.assertTrue(policy.new_qa_booking_credit(value['lead'], meeting,
                booking_rep_id=rep, booking_actor_verified=True))
        self.assertFalse(policy.new_qa_booking_credit(value['lead'], meeting,
            booking_rep_id='user_other', booking_actor_verified=True))
        self.assertFalse(policy.new_qa_booking_credit(value['lead'], meeting,
            booking_rep_id=LEGACY_QA_REP_ID, booking_actor_verified=False))

    def test_candidate_query_covers_both_owners_during_cutover(self):
        lane = SimpleNamespace(S_LOST=LOST, SKIP_CAP=1000, status_in=lambda x: {},
            _wrap=lambda x: x, search=Mock(return_value=[]))
        reader = runtime.QaLifecycleReader(lane, None, config(), {'user_closer': 'Closer'})
        reader.candidates()
        query = json.dumps(lane.search.call_args.args[0])
        self.assertIn(QA_REP_ID, query)
        self.assertIn(LEGACY_QA_REP_ID, query)

    def test_native_reader_preserves_legacy_call_and_human_outcome_after_transfer(self):
        value = qa_snapshot()
        value['lead'][f'custom.{policy.QA_OUTCOME}'] = 'No connection'
        lane = SimpleNamespace(search=Mock(return_value=[]), _wrap=lambda *args: {}, FALLTHROUGH='Blitz', S_LOST=LOST)
        store = SimpleNamespace(holds=lambda _: [], lifecycle=lambda _: None,
            connection=SimpleNamespace(execute=lambda *args: SimpleNamespace(fetchall=lambda: [])))
        reader = runtime.QaLifecycleReader(lane, store, config(), {'user_closer': 'Closer'}, clock=lambda: NOW)
        reader.get = Mock(return_value=value['lead'])
        reader.contact_suppression = Mock(return_value=False)
        reader.related = Mock(side_effect=lambda path, *args: [{'id': 'acti_call', 'user_id': LEGACY_QA_REP_ID,
            'date_created': NOW.isoformat(), 'disposition': 'answered', 'duration': 60}] if path == '/activity/call/' else [])
        event = {'id': 'ev_outcome', 'object_id': value['lead']['id'], 'object_type': 'lead',
            'user_id': LEGACY_QA_REP_ID, 'api_key_id': None,
            'changed_fields': [f'custom.{policy.QA_OUTCOME}'], 'date_updated': NOW.isoformat(),
            'data': value['lead']}
        reader.events = Mock(side_effect=lambda _, kind: [event] if kind == 'lead' else [])
        with patch.object(runtime.HandoffReader, 'snapshot', return_value={'meetings': [], 'lane2_state': 'Blitz'}):
            result = reader.snapshot(value['lead']['id'])
        self.assertTrue(result['possible_connection'])
        self.assertTrue(result['qa_outcome_verified'])
        self.assertEqual(result['calls'][0]['user_id'], LEGACY_QA_REP_ID)

    def test_saved_alerts_keep_original_recipient_new_alerts_use_tara(self):
        old = {'count': 2, 'completed_at': NOW.isoformat()}
        self.assertIn(f'<@{LEGACY_QA_SLACK_ID}>', alerts.message(old))
        new = {**old, 'recipient_slack_id': QA_SLACK_ID}
        self.assertIn(f'<@{QA_SLACK_ID}>', alerts.message(new))
        with self.assertRaises(ValueError):
            alerts.message({**old, 'recipient_slack_id': 'U_WRONG'})


if __name__ == '__main__':
    unittest.main()
