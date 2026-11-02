import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from close_status_policy import ORG, DNC
from sales_dispatch_holds import marketing_sequence_verified
from workflow_holds import BATCH, Store, PostgresStore, guard_request
from sms_rest_close import Reader

BASE = 'https://api.close.com/api/v1'


class DispatchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.db = Path(temporary.name) / 'state.sqlite'; Store(self.db).close()
        environment = patch.dict(os.environ, {'WORKFLOW_HOLD_DB': str(self.db),
            'WORKFLOW_HOLDS_REQUIRED': '1', 'WORKFLOW_HOLD_BACKEND': 'sqlite'})
        environment.start(); self.addCleanup(environment.stop)
        self.lead = {'id': 'lead_test', 'organization_id': ORG, 'status_id': 'normal'}
        self.sequence = {'id': 'seq_marketing', 'organization_id': ORG, 'steps': [{'step_type': 'email'}]}
        self.subscription = {'id': 'sub_test', 'lead_id': 'lead_test', 'sequence_id': 'seq_marketing', 'status': 'paused'}
        self.calls = []; self.created = {}; self.timeout = False
        self.guarded = guard_request(self.transport)

    def transport(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if method == 'GET':
            if '/lead/' in url: result = self.lead
            elif '/contact/' in url: result = {'id': 'cont_test', 'lead_id': 'lead_test'}
            elif '/sequence/' in url: result = self.sequence
            elif url.endswith('/sub_test/'): result = self.subscription
            else: result = self.created
        else:
            if self.timeout: raise TimeoutError('Dispatch response lost')
            result = {'id': 'created_test', **kwargs['json']}
            self.created = result
        return Mock(json=lambda: copy.deepcopy(result))

    def hold(self, workflow='sms_rest'):
        store = Store(self.db)
        try: store.acquire('lead_test', workflow, 'token', '2026-10-23')
        finally: store.close()

    def approve_marketing(self):
        now = datetime.now(timezone.utc)
        policy = {'schemaVersion': 1, 'purpose': 'marketing_only', 'reviewedBy': 'Reviewer',
            'reviewedAt': (now - timedelta(minutes=1)).isoformat(), 'expiresAt': (now + timedelta(days=1)).isoformat(),
            'stepsSha256': hashlib.sha256(json.dumps(self.sequence['steps'], sort_keys=True, separators=(',', ':')).encode()).hexdigest()}
        store = Store(self.db)
        try: store.connection.execute('INSERT INTO workflow_meta VALUES(?,?)', ('marketing_sequence:seq_marketing', json.dumps(policy)))
        finally: store.close()

    def assert_no_mutations(self):
        self.assertFalse(any(method != 'GET' for method, _, _ in self.calls))

    def test_sms_call_email_task_and_enrollment_block_on_each_hold(self):
        for workflow in ('sms_rest', 'qa', 'education', 'recovery'):
            self.hold(workflow)
            for path in ('activity/sms', 'activity/call', 'activity/email', 'task', 'sequence_subscription'):
                with self.subTest(workflow=workflow, path=path), self.assertRaisesRegex(RuntimeError, 'Sales hold'):
                    self.guarded('POST', BASE + '/' + path + '/', json={'lead_id': 'lead_test'})
                self.assert_no_mutations()

    def test_subscription_resume_is_blocked(self):
        self.hold()
        with self.assertRaisesRegex(RuntimeError, 'Sales hold'):
            self.guarded('PUT', BASE + '/sequence_subscription/sub_test/', json={'status': 'active'})
        self.assert_no_mutations()

    def test_pause_is_allowed_to_stop_sales(self):
        self.hold()
        self.guarded('PUT', BASE + '/sequence_subscription/sub_test/', json={'status': 'paused'})
        self.assertTrue(any(method == 'PUT' for method, _, _ in self.calls))

    def test_email_only_without_review_is_not_assumed_marketing(self):
        self.hold()
        with self.assertRaisesRegex(RuntimeError, 'Sales hold'):
            self.guarded('POST', BASE + '/sequence_subscription/', json={'lead_id': 'lead_test', 'sequence_id': 'seq_marketing'})
        self.assert_no_mutations()

    def test_reviewed_marketing_stays_allowed_during_sms_rest(self):
        self.hold(); self.approve_marketing()
        self.guarded('POST', BASE + '/sequence_subscription/', json={'lead_id': 'lead_test', 'sequence_id': 'seq_marketing'})
        self.assertEqual(sum(method == 'POST' for method, _, _ in self.calls), 1)

    def test_marketing_cannot_override_a_qa_hold(self):
        self.hold(); self.hold('qa'); self.approve_marketing()
        with self.assertRaisesRegex(RuntimeError, 'Sales hold'):
            self.guarded('POST', BASE + '/sequence_subscription/', json={'lead_id': 'lead_test', 'sequence_id': 'seq_marketing'})
        self.assert_no_mutations()

    def test_sequence_changed_after_marketing_review_blocks(self):
        self.hold(); self.approve_marketing(); self.sequence['steps'].append({'step_type': 'sms'})
        with self.assertRaisesRegex(RuntimeError, 'Sales hold'):
            self.guarded('POST', BASE + '/sequence_subscription/', json={'lead_id': 'lead_test', 'sequence_id': 'seq_marketing'})
        self.assert_no_mutations()

    def test_malformed_marker_does_not_allow_marketing(self):
        self.hold(); self.approve_marketing(); self.lead['custom.' + BATCH] = '[STH1 invalid'
        with self.assertRaisesRegex(RuntimeError, 'Sales hold'):
            self.guarded('POST', BASE + '/sequence_subscription/', json={'lead_id': 'lead_test', 'sequence_id': 'seq_marketing'})
        self.assert_no_mutations()

    def test_dnc_cannot_send_even_reviewed_marketing(self):
        self.hold(); self.approve_marketing(); self.lead['status_id'] = DNC
        with self.assertRaisesRegex(RuntimeError, 'DNC'):
            self.guarded('POST', BASE + '/sequence_subscription/', json={'lead_id': 'lead_test', 'sequence_id': 'seq_marketing'})
        self.assert_no_mutations()

    def test_contact_scoped_dispatch_resolves_lead(self):
        self.hold()
        with self.assertRaisesRegex(RuntimeError, 'Sales hold'):
            self.guarded('POST', BASE + '/activity/sms/', json={'contact_id': 'cont_test'})
        self.assert_no_mutations()

    def test_contact_lead_mismatch_blocks(self):
        with self.assertRaisesRegex(ValueError, 'mismatch'):
            self.guarded('POST', BASE + '/activity/sms/', json={'contact_id': 'cont_test', 'lead_id': 'lead_other'})
        self.assert_no_mutations()

    def test_uncertain_post_cannot_be_retried(self):
        self.timeout = True
        with self.assertRaises(TimeoutError):
            self.guarded('POST', BASE + '/activity/sms/', json={'lead_id': 'lead_test'})
        with self.assertRaisesRegex(RuntimeError, 'Uncertain write'):
            self.guarded('POST', BASE + '/activity/sms/', json={'lead_id': 'lead_test'})
        self.assertEqual(sum(method == 'POST' for method, _, _ in self.calls), 1)

    def test_auth_is_forwarded_to_fresh_reads(self):
        self.guarded('PUT', BASE + '/lead/lead_test/', json={'status_id': 'normal'}, auth=('fake', ''))
        self.assertTrue(all(kwargs.get('auth') == ('fake', '') for _, _, kwargs in self.calls))

    def test_native_subscription_scope_mismatch_blocks_admission(self):
        client = Mock(); client.list.return_value = [{'id':'sub_wrong','contact_id':'cont_other'}]
        store = Store(self.db)
        try:
            reader = Reader(client, None, store)
            with self.assertRaisesRegex(ValueError,'scope mismatch'):
                reader._subscriptions({'id':'lead_test','contacts':[{'id':'cont_test'}]})
        finally: store.close()

    def test_reviewed_marketing_is_preserved_while_sales_is_paused(self):
        self.approve_marketing()
        client = Mock(); client.list.return_value = [
            {'id':'sub_marketing','contact_id':'cont_test','sequence_id':'seq_marketing','status':'active'},
            {'id':'sub_sales','contact_id':'cont_test','sequence_id':'seq_sales','status':'active'}]
        store = Store(self.db)
        try:
            reader = Reader(client, None, store, sequences=[self.sequence,
                {'id':'seq_sales','steps':[{'step_type':'sms'}]}])
            sales, unknown = reader._subscriptions({'id':'lead_test','contacts':[{'id':'cont_test'}]})
            self.assertEqual(sales, ('sub_sales',)); self.assertEqual(unknown, ())
        finally: store.close()

    def test_unreviewed_email_sequence_blocks_rest_as_unknown(self):
        client = Mock(); client.list.return_value = [
            {'id':'sub_marketing','contact_id':'cont_test','sequence_id':'seq_marketing','status':'active'}]
        store = Store(self.db)
        try:
            reader = Reader(client, None, store, sequences=[self.sequence])
            sales, unknown = reader._subscriptions({'id':'lead_test','contacts':[{'id':'cont_test'}]})
            self.assertEqual(sales, ()); self.assertEqual(unknown, ('sub_marketing',))
        finally: store.close()

    def test_sms_cli_apply_cannot_use_isolated_local_state(self):
        import sms_rest
        with patch('sys.argv',['sms_rest.py','apply','--output','unused','--approval','unused','--proposal','unused']), \
                patch.dict(os.environ,{'SMS_REST_ENABLE_WRITES':'1','WORKFLOW_HOLD_BACKEND':'sqlite'}), \
                patch.object(sms_rest,'CloseClient') as client:
            with self.assertRaises(SystemExit): sms_rest.main()
        client.assert_not_called()


class PostgresFenceTests(unittest.TestCase):
    def connection(self, version='1'):
        connection = Mock(autocommit=True)
        def execute(sql, params=()):
            value = PostgresStore.TENANT if "key='tenant'" in sql else version if "key='hold_contract_version'" in sql else None
            return Mock(fetchone=lambda: {'value': value} if value is not None else None, rowcount=1)
        connection.execute.side_effect = execute
        return connection

    def test_old_schema_contract_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'version mismatch'):
            PostgresStore(self.connection(version='0'))

    def test_acquire_and_release_require_the_tenant_fence(self):
        connection = self.connection(); store = PostgresStore(connection)
        for operation in (lambda: store.acquire('lead_test', 'qa', 'token', 'now'), lambda: store.release('lead_test', 'qa', 'token')):
            with self.assertRaisesRegex(RuntimeError, 'advisory fence'): operation()
        with store.serialized():
            store.acquire('lead_test', 'qa', 'token', 'now')
            with store.serialized(): store.release('lead_test', 'qa', 'token')
        lock_calls = [call for call in connection.execute.call_args_list if 'pg_advisory_' in call.args[0]]
        self.assertEqual(len(lock_calls), 2)
        self.assertTrue(all(call.args[1] == (PostgresStore.TENANT,) for call in lock_calls))


if __name__ == '__main__': unittest.main()
