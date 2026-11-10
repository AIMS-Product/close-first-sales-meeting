from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import requests

import qa_slack_notifications as alerts
from qa_lifecycle_policy import POLICY_VERSION
from workflow_holds import Store

NOW = datetime(2026, 11, 10, 20, 37, tzinfo=timezone.utc)
CONFIG = {'channel': 'C_TEST', 'token': 'xoxb-synthetic-test-only'}


def completed(count=3, **changes):
    return {'mode': 'apply', 'policy_version': POLICY_VERSION, 'verified_writes': 50,
            'counts': {'verified_qa': count, 'verified_backlog_window': 47}, **changes}


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'holds.sqlite'
        self.store = Store(self.path)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def record(self, result=None):
        return alerts.record_completed_run(self.store, completed() if result is None else result,
                                           completed_at=NOW, config=CONFIG)

    def records(self):
        return [json.loads(row['value']) for row in self.store.connection.execute(
            'SELECT value FROM workflow_meta WHERE key LIKE ?', (alerts.PREFIX + '%',))]

    def test_zero_additions_backlog_and_tier_writes_stay_silent(self):
        self.assertIsNone(self.record(completed(0)))
        self.assertIsNone(self.record(completed(0, counts={'verified_tier_intake': 50})))
        self.assertIsNone(self.record(completed(3, mode='observe')))
        self.assertEqual(self.records(), [])

    def test_disabled_configuration_saves_nothing(self):
        with patch.dict('os.environ', {'QA_SLACK_NOTIFICATIONS_ENABLED': '0'}):
            self.assertIsNone(alerts.record_completed_run(self.store, completed(), completed_at=NOW))
        self.assertEqual(self.records(), [])

    def test_mixed_run_counts_only_verified_new_qa_admissions(self):
        self.record()
        record = self.records()[0]
        text = alerts.message(record)
        self.assertEqual(record['count'], 3)
        self.assertIn('*3 new leads* were added', text)
        self.assertNotIn('50', text)
        self.assertNotIn('47', text)
        self.assertIn(alerts.QA_LIST_URL, text)
        self.assertIn('12:37 PM PST', text)
        self.assertIn('<@' + alerts.NARIA_SLACK_ID + '>', text)

    def test_singular_count_and_malformed_completion(self):
        self.record(completed(1))
        self.assertIn('*1 new lead* was added', alerts.message(self.records()[0]))
        for result in (completed(True), completed(-1), completed(51), completed(1, verified_writes=0),
                       completed(1, policy_version='wrong-policy')):
            with self.assertRaises(ValueError):
                self.record(result)

    def test_success_is_not_repeated_after_worker_restart(self):
        self.record()
        sender = Mock(return_value={'channel': CONFIG['channel'], 'ts': '123.456'})
        self.assertEqual(alerts.deliver_pending(self.store, CONFIG, sender=sender, now=NOW)['sent'], 1)
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(alerts.deliver_pending(self.store, CONFIG, sender=sender, now=NOW)['sent'], 0)
        self.assertEqual(sender.call_count, 1)
        self.assertEqual(self.records()[0]['state'], 'sent')

    def test_timeout_is_retained_without_duplicate_retry(self):
        self.record()
        sender = Mock(side_effect=requests.Timeout('synthetic timeout'))
        self.assertEqual(alerts.deliver_pending(self.store, CONFIG, sender=sender, now=NOW)['uncertain'], 1)
        alerts.deliver_pending(self.store, CONFIG, sender=sender, now=NOW + timedelta(days=1))
        self.assertEqual(sender.call_count, 1)
        self.assertEqual(self.records()[0]['state'], 'uncertain')

    def test_interruption_after_claim_does_not_resend(self):
        run_id = self.record()
        record = self.records()[0]
        record.update(state='sending', attempted_at=NOW.isoformat())
        alerts._save(self.store, alerts.PREFIX + run_id, record)
        sender = Mock()
        alerts.deliver_pending(self.store, CONFIG, sender=sender, now=NOW + timedelta(minutes=3))
        sender.assert_not_called()
        self.assertEqual(self.records()[0]['state'], 'uncertain')

    def test_confirmed_rate_limit_defers_then_retries_the_alert_only(self):
        self.record()
        sender = Mock(side_effect=[alerts.SlackRejected('ratelimited', 60),
                                   {'channel': CONFIG['channel'], 'ts': '123.456'}])
        self.assertEqual(alerts.deliver_pending(self.store, CONFIG, sender=sender, now=NOW)['pending'], 1)
        alerts.deliver_pending(self.store, CONFIG, sender=sender, now=NOW + timedelta(seconds=59))
        self.assertEqual(sender.call_count, 1)
        alerts.deliver_pending(self.store, CONFIG, sender=sender, now=NOW + timedelta(seconds=60))
        self.assertEqual(sender.call_count, 2)
        self.assertEqual(self.records()[0]['state'], 'sent')
        self.assertEqual(self.store.connection.execute('SELECT COUNT(*) AS n FROM workflow_write').fetchone()['n'], 0)

    def test_changed_destination_does_not_retarget_saved_notifications(self):
        self.record()
        sender = Mock()
        alerts.deliver_pending(self.store, {**CONFIG, 'channel': 'C_OTHER'}, sender=sender, now=NOW)
        sender.assert_not_called()
        self.assertEqual(self.records()[0]['state'], 'pending')

    def test_separate_completions_produce_separate_notifications(self):
        self.assertNotEqual(self.record(), self.record(completed(1)))
        sender = Mock(return_value={'channel': CONFIG['channel'], 'ts': '123.456'})
        self.assertEqual(alerts.deliver_pending(self.store, CONFIG, sender=sender, now=NOW)['sent'], 2)

    def test_runtime_records_only_after_completed_apply_pass(self):
        from test_qa_lifecycle import FakeReader, snapshot
        import qa_lifecycle_runtime as runtime
        reader = FakeReader(snapshot(), self.store)
        with patch.dict('os.environ', {'WORKFLOW_HOLD_BACKEND': 'postgres'}), \
                patch.object(runtime, 'notification_configuration', return_value=CONFIG):
            runtime.run(reader, apply=True)
            runtime.run(reader, apply=True)
        self.assertEqual([r['count'] for r in self.records()], [1])

    def test_failed_qa_write_does_not_announce_success(self):
        from test_qa_lifecycle import FakeReader, snapshot
        import qa_lifecycle_runtime as runtime
        reader = FakeReader(snapshot(), self.store)
        reader.failure = requests.Timeout('uncertain CRM write')
        with patch.dict('os.environ', {'WORKFLOW_HOLD_BACKEND': 'postgres'}), \
                patch.object(runtime, 'notification_configuration', return_value=CONFIG):
            with self.assertRaises(requests.Timeout):
                runtime.run(reader, apply=True)
        self.assertEqual(self.records(), [])


class SlackBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.session = Mock()
        self.record = {'channel': CONFIG['channel'], 'run_id': 'synthetic-run', 'count': 2,
                       'completed_at': NOW.isoformat()}

    def test_single_post_and_verified_receipt(self):
        self.session.post.return_value = SimpleNamespace(status_code=200,
            json=lambda: {'ok': True, 'channel': CONFIG['channel'], 'ts': '123.456'})
        receipt = alerts.SlackSender(CONFIG, self.session)(self.record)
        self.assertEqual(receipt['ts'], '123.456')
        self.session.post.assert_called_once()
        payload = self.session.post.call_args.kwargs['json']
        self.assertEqual(payload['metadata']['event_payload'], {'run_id': 'synthetic-run', 'count': 2})
        self.assertFalse(self.session.post.call_args.kwargs['allow_redirects'])
        self.assertNotIn('lead_id', json.dumps(payload))

    def test_provider_errors_and_bad_receipts_are_uncertain(self):
        for status, body in ((500, {}), (302, {}), (200, {'ok': False, 'error': 'internal_error'}),
                             (200, {'ok': True, 'channel': 'C_OTHER', 'ts': '123.456'}),
                             (200, {'ok': True, 'channel': CONFIG['channel']})):
            self.session.post.return_value = SimpleNamespace(status_code=status, json=lambda: body)
            with self.assertRaises(alerts.SlackUncertain):
                alerts.SlackSender(CONFIG, self.session)(self.record)

    def test_confirmed_rejections_are_not_success(self):
        self.session.post.return_value = SimpleNamespace(status_code=200,
            json=lambda: {'ok': False, 'error': 'not_in_channel'})
        with self.assertRaisesRegex(alerts.SlackRejected, 'not_in_channel'):
            alerts.SlackSender(CONFIG, self.session)(self.record)
        self.session.post.return_value = SimpleNamespace(status_code=429, headers={'Retry-After': '90'})
        with self.assertRaises(alerts.SlackRejected) as caught:
            alerts.SlackSender(CONFIG, self.session)(self.record)
        self.assertEqual(caught.exception.retry_after, 90)

    def test_configuration_defaults_off_and_requires_bot_credentials(self):
        self.assertIsNone(alerts.configuration({}))
        with self.assertRaises(ValueError):
            alerts.configuration({'QA_SLACK_NOTIFICATIONS_ENABLED': '1'})
        self.assertEqual(alerts.configuration({'QA_SLACK_NOTIFICATIONS_ENABLED': '1',
            'QA_SLACK_CHANNEL_ID': 'C123456', 'QA_SLACK_BOT_TOKEN': CONFIG['token']})['channel'], 'C123456')

    def test_observe_and_failed_producer_do_not_send(self):
        import run_railway_qa_release as launch
        from contextlib import nullcontext
        with patch.dict('sys.modules', {'lane2_state': SimpleNamespace()}), \
                patch.object(launch, 'launch_mode', return_value='observe'), \
                patch.object(launch, 'cohort_fence', return_value=nullcontext(True)), \
                patch.object(launch, 'run_configured'), patch.object(launch.subprocess, 'run'), \
                patch.object(launch, 'deliver_configured') as send:
            launch.main()
            send.assert_not_called()
        with patch.dict('sys.modules', {'lane2_state': SimpleNamespace()}), \
                patch.object(launch, 'launch_mode', return_value='apply'), \
                patch.object(launch, 'cohort_fence', return_value=nullcontext(True)), \
                patch.object(launch, 'run_configured'), \
                patch.object(launch.subprocess, 'run', side_effect=RuntimeError('synthetic producer failure')), \
                patch.object(launch, 'deliver_configured') as send:
            with self.assertRaises(RuntimeError):
                launch.main()
            send.assert_not_called()


if __name__ == '__main__':
    unittest.main()
