"""Exercise the shared writer fence without provider requests or production state."""
import copy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from close_status_policy import OWNER, ORG
from workflow_holds import BATCH, Store, guard_request

BASE = 'https://api.close.com/api/v1'
LEAD_URL = BASE + '/lead/lead_test/'
OPP_URL = BASE + '/opportunity/oppo_test/'


class SharedWriterGuardTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.db = Path(temporary.name) / 'holds.sqlite'
        Store(self.db).close()
        environment = patch.dict(os.environ, {
            'WORKFLOW_HOLD_BACKEND': 'sqlite', 'WORKFLOW_HOLDS_REQUIRED': '1',
            'WORKFLOW_HOLD_DB': str(self.db)})
        environment.start()
        self.addCleanup(environment.stop)
        self.lead = {'id': 'lead_test', 'organization_id': ORG, 'status_id': 'old', 'custom.' + OWNER: None}
        self.opportunity = {'id': 'oppo_test', 'lead_id': 'lead_test', 'status_id': 'old'}
        self.calls = []
        self.uncertain_put = False
        self.drop_patch = False
        self.guarded = guard_request(self.transport)

    def transport(self, method, url, **kwargs):
        self.calls.append((method, url))
        target = self.lead if url == LEAD_URL else self.opportunity
        if method == 'PUT':
            if not self.drop_patch:
                target.update(kwargs['json'])
            if self.uncertain_put:
                raise TimeoutError('Response lost after provider accepted the write')
        return Mock(json=lambda: copy.deepcopy(target))

    def acquire(self, workflow):
        store = Store(self.db)
        try:
            store.acquire('lead_test', workflow, 'token', '2000-01-01', '2000-01-02')
        finally:
            store.close()

    def unresolved(self):
        store = Store(self.db)
        try:
            return store.uncertain('lead_test')
        finally:
            store.close()

    def assert_no_write(self):
        self.assertFalse(any(method == 'PUT' for method, _ in self.calls))

    def test_each_workflow_blocks_even_after_its_due_time(self):
        for workflow in ('sms_rest', 'qa', 'education', 'recovery'):
            with self.subTest(workflow=workflow):
                self.acquire(workflow)
                with self.assertRaisesRegex(RuntimeError, 'Sales hold'):
                    self.guarded('PUT', LEAD_URL, json={'custom.' + OWNER: 'user_rep'})
                self.assert_no_write()

    def test_opportunity_write_checks_parent_lead_hold(self):
        self.acquire('qa')
        with self.assertRaisesRegex(RuntimeError, 'Sales hold'):
            self.guarded('PUT', OPP_URL, json={'status_id': 'next'})
        self.assertEqual(self.calls, [('GET', OPP_URL), ('GET', LEAD_URL)])

    def test_marker_blocks_when_durable_row_is_missing(self):
        self.lead['custom.' + BATCH] = 'original [STH1 education token]'
        with self.assertRaisesRegex(RuntimeError, 'Sales hold'):
            self.guarded('PUT', LEAD_URL, json={'status_id': 'next'})
        self.assert_no_write()

    def test_malformed_marker_fails_closed(self):
        self.lead['custom.' + BATCH] = 'original [STH1 broken'
        with self.assertRaisesRegex(RuntimeError, 'Sales hold'):
            self.guarded('PUT', LEAD_URL, json={'status_id': 'next'})
        self.assert_no_write()

    def test_missing_registry_blocks_before_provider_request(self):
        with patch.dict(os.environ, {'WORKFLOW_HOLD_DB': ''}):
            with self.assertRaisesRegex(RuntimeError, 'database required'):
                self.guarded('PUT', LEAD_URL, json={'status_id': 'next'})
        self.assertEqual(self.calls, [])

    def test_changed_owner_blocks_before_write(self):
        self.lead['custom.' + OWNER] = 'user_other'
        with self.assertRaisesRegex(RuntimeError, 'Owner changed'):
            self.guarded('PUT', LEAD_URL, json={'custom.' + OWNER: 'user_rep'}, expected_owner=None)
        self.assert_no_write()

    def test_wrong_organization_blocks_before_write(self):
        self.lead['organization_id'] = 'other'
        with self.assertRaisesRegex(ValueError, 'tenant/scope mismatch'):
            self.guarded('PUT', LEAD_URL, json={'status_id': 'next'})
        self.assert_no_write()

    def test_clear_registry_allows_write_and_verifies_readback(self):
        self.guarded('PUT', LEAD_URL, json={'custom.' + OWNER: 'user_rep'}, expected_owner=None)
        self.assertEqual(self.calls, [('GET', LEAD_URL), ('PUT', LEAD_URL), ('GET', LEAD_URL)])
        self.assertEqual(self.lead['custom.' + OWNER], 'user_rep')
        self.assertEqual(self.unresolved(), [])

    def test_ambiguous_write_cannot_be_retried(self):
        self.uncertain_put = True
        with self.assertRaises(TimeoutError):
            self.guarded('PUT', LEAD_URL, json={'status_id': 'next'})
        self.assertEqual(len(self.unresolved()), 1)
        with self.assertRaisesRegex(RuntimeError, 'Uncertain write'):
            self.guarded('PUT', LEAD_URL, json={'status_id': 'next'})
        self.assertEqual(sum(method == 'PUT' for method, _ in self.calls), 1)

    def test_readback_mismatch_keeps_uncertain_intent(self):
        self.drop_patch = True
        with self.assertRaisesRegex(RuntimeError, 'readback differs'):
            self.guarded('PUT', LEAD_URL, json={'status_id': 'next'})
        self.assertEqual(len(self.unresolved()), 1)

    def test_clearing_sms_hold_does_not_clear_qa_hold(self):
        self.acquire('sms_rest')
        self.acquire('qa')
        store = Store(self.db)
        try:
            store.release('lead_test', 'sms_rest', 'token')
        finally:
            store.close()
        with self.assertRaisesRegex(RuntimeError, 'Sales hold'):
            self.guarded('PUT', LEAD_URL, json={'custom.' + OWNER: 'user_rep'})
        self.assert_no_write()


if __name__ == '__main__':
    unittest.main()
