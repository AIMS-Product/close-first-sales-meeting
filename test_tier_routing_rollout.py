import argparse
import copy
from dataclasses import replace
from datetime import timedelta
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from close_status_policy import OWNER
from test_tier_assignment import facts, rep, NOW, ROSTER
from tier_routing_rollout import (PilotScope, add_arguments, dispatch, build_plan,
    authorize, digest, execute, INTEGRATIONS)
from workflow_holds import Store
from test_hold_rollout import fake_ready_manifest


def arguments(*extra):
    parser = argparse.ArgumentParser()
    parser.set_defaults(apply=False, reclaim=False, actionable_queue=False,
        actionable_owner=None, no_census=False, max_queue=1000)
    add_arguments(parser)
    return parser.parse_args(extra)


def valid_args():
    return arguments('--tier-routing', '--tier-rep', 'user_ramp', '--tier-lead', 'lead_test',
        '--tier-max-assignments', '1', '--tier-output', 'unused', '--tier-tagger', 'existing.py')


class FlagTests(unittest.TestCase):
    def test_default_off_does_not_enter_tier_runner(self):
        runner = Mock()
        with patch.dict(os.environ, {'LANE2_TIER_ROUTING_ENABLED': '0'}):
            self.assertFalse(dispatch(arguments(), runner))
        runner.assert_not_called()

    def test_env_flag_alone_does_not_change_legacy_entrypoint(self):
        runner = Mock()
        with patch.dict(os.environ, {'LANE2_TIER_ROUTING_ENABLED': '1'}):
            self.assertFalse(dispatch(arguments(), runner))
        runner.assert_not_called()

    def test_preview_is_read_only_even_with_write_switch_on(self):
        runner = Mock()
        with patch.dict(os.environ, {'LANE2_TIER_ROUTING_ENABLED': '1'}):
            self.assertTrue(dispatch(valid_args(), runner))
        self.assertFalse(runner.call_args.args[0].apply)

    def test_default_disabled_switch_rejects_tier_apply(self):
        args = valid_args(); args.apply = True
        runner = Mock()
        with patch.dict(os.environ, {'LANE2_TIER_ROUTING_ENABLED': '0'}):
            with self.assertRaises(PermissionError): dispatch(args, runner)
        runner.assert_not_called()

    def test_write_switch_is_not_itself_approval(self):
        args = valid_args(); args.apply = True
        with patch.dict(os.environ, {'LANE2_TIER_ROUTING_ENABLED': '1'}):
            with self.assertRaisesRegex(PermissionError, 'approved tier preview'): dispatch(args, Mock())

    def test_pilot_requires_mandatory_shared_postgres(self):
        args = valid_args(); args.apply = True; args.tier_approval = Path('approval.json')
        with patch.dict(os.environ, {'LANE2_TIER_ROUTING_ENABLED': '1', 'WORKFLOW_HOLD_BACKEND': 'sqlite'}):
            with self.assertRaisesRegex(PermissionError, 'PostgreSQL'): dispatch(args, Mock())

    def test_staged_permissions_cannot_be_used_for_apply(self):
        args = valid_args(); args.apply = True; args.tier_use_staged_permissions = True
        with self.assertRaisesRegex(PermissionError, 'preview-only'): dispatch(args, Mock())

    def test_stray_tier_option_does_not_fall_back_to_legacy(self):
        with self.assertRaises(ValueError): dispatch(arguments('--tier-rep', 'user_ramp'), Mock())

    def test_tier_failure_propagates_without_legacy_fallback(self):
        with self.assertRaisesRegex(RuntimeError, 'hold unavailable'):
            dispatch(valid_args(), Mock(side_effect=RuntimeError('hold unavailable')))

    def test_legacy_reclaim_and_queue_modes_are_rejected(self):
        for key in ('reclaim', 'actionable_queue', 'no_census'):
            args = valid_args(); setattr(args, key, True)
            with self.subTest(key=key), self.assertRaises(ValueError): dispatch(args, Mock())

    def test_scope_cannot_be_unbounded(self):
        for args in (((), ('lead_test',), 1, 1000), (('user_rep',), (), 1, 1000),
                     (('user_rep',), ('lead_test',), None, 1000),
                     (('user_rep',), ('lead_test',), 11, 1000),
                     (('user_rep',), ('lead_test',), 1, 0),
                     (('user_rep',), ('lead_test', 'lead_test'), 1, 1000),
                     (('user_rep', 'user_rep'), ('lead_test',), 1, 1000)):
            with self.subTest(args=args), self.assertRaises(ValueError): PilotScope(*args)


class FakeSource:
    def __init__(self):
        self.items = [facts(), replace(facts(), lead={**facts().lead, 'id': 'lead_second'})]
        self.reps = [rep('ramp')]
        self.read_scopes = []
        self.capacity = 0
        self.changed_before_put = False

    def read(self, scope, **kwargs):
        self.read_scopes.append(scope)
        return {'items': [item for item in self.items if item.lead['id'] in scope.lead_ids],
            'reps': self.reps, 'closers': ROSTER, 'memberships': {'user_ramp'},
            'counts': {'user_ramp': self.capacity}, 'rep_permission_source': 'stored'}

    def readback(self, lead_id):
        lead = copy.deepcopy(next(item.lead for item in self.items if item.lead['id'] == lead_id))
        if self.changed_before_put: lead['custom.' + OWNER] = 'user_other'
        return lead


class PilotTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {'LANE2_TIER_ROUTING_ENABLED': '1'})
        environment.start(); self.addCleanup(environment.stop)
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.store = Store(Path(temporary.name) / 'state.sqlite'); self.addCleanup(self.store.close)
        self.source = FakeSource()
        self.scope = PilotScope(('user_ramp',), ('lead_test', 'lead_second'), 2, 1000)
        self.plan = build_plan(self.scope, self.source.read(self.scope), NOW)
        self.approval = {'approved_by': 'Dom Ellis', 'approved_plan_sha256': digest(self.plan),
            'hold_deployment_manifest': fake_ready_manifest(NOW),
            'expires_at': (NOW + timedelta(minutes=5)).isoformat(),
            'verified_integrations': {key: True for key in INTEGRATIONS}}
        self.records = []
        self.writer = Mock()
        def write(lead_id, owner):
            item = next(item for item in self.source.items if item.lead['id'] == lead_id)
            item.lead['custom.' + OWNER] = owner
            self.source.capacity += 1
        self.writer.put_owner.side_effect = write

    def execute(self):
        return execute(self.plan, self.approval, self.scope, source=self.source, store=self.store,
            writer=self.writer, clock=lambda: NOW, record=self.records.append)

    def approve_changes(self):
        self.approval['approved_plan_sha256'] = digest(self.plan)

    def test_assignment_cap_is_hard(self):
        scope = replace(self.scope, max_assignments=1)
        plan = build_plan(scope, self.source.read(scope), NOW)
        self.assertEqual(sum(bool(p['target_owner']) for p in plan['proposals']), 1)
        self.assertTrue(plan['proposals'][1]['pilot_cap_excluded'])

    def test_unselected_reps_never_receive_leads(self):
        self.source.reps.append(rep('other', 'Top'))
        plan = build_plan(self.scope, self.source.read(self.scope), NOW)
        self.assertTrue(all(p['eligible_reps'] == ['ramp'] for p in plan['proposals']))

    def test_missing_requested_identity_stops_plan(self):
        self.source.reps = []
        with self.assertRaises(ValueError): build_plan(self.scope, self.source.read(self.scope), NOW)

    def test_success_checks_only_current_lead_before_each_write(self):
        self.assertEqual(len(self.execute()), 2)
        self.assertEqual(self.writer.put_owner.call_count, 2)
        self.assertTrue(all(len(scope.lead_ids) == 1 for scope in self.source.read_scopes[1:]))
        self.assertFalse(self.store.uncertain('lead_test'))

    def test_staged_roster_preview_cannot_authorize_writes(self):
        self.plan['rep_permission_source'] = 'candidate'; self.approve_changes()
        with self.assertRaises(PermissionError): self.execute()
        self.writer.put_owner.assert_not_called()

    def test_missing_integration_receipt_blocks(self):
        self.approval['verified_integrations']['all_dial_queues'] = False
        with self.assertRaises(PermissionError): self.execute()
        self.writer.put_owner.assert_not_called()

    def test_true_integration_flags_cannot_replace_deployment_evidence(self):
        del self.approval['hold_deployment_manifest']
        with self.assertRaisesRegex(PermissionError, 'manifest required'): self.execute()
        self.writer.put_owner.assert_not_called()

    def test_changed_plan_cannot_use_old_approval(self):
        self.plan['proposals'][0]['target_owner'] = 'user_other'
        with self.assertRaises(PermissionError): self.execute()
        self.writer.put_owner.assert_not_called()

    def test_expired_approval_blocks(self):
        self.approval['expires_at'] = NOW.isoformat()
        with self.assertRaises(PermissionError): self.execute()

    def test_old_or_future_preview_blocks(self):
        for offset in (timedelta(minutes=-11), timedelta(seconds=1)):
            self.plan['proposed_at'] = (NOW + offset).isoformat(); self.approve_changes()
            with self.subTest(offset=offset), self.assertRaises(PermissionError): self.execute()

    def test_duplicate_or_out_of_scope_approved_writes_block(self):
        self.plan['proposals'][1]['lead_id'] = 'lead_test'; self.approve_changes()
        with self.assertRaises(PermissionError): self.execute()
        self.writer.put_owner.assert_not_called()

    def test_approved_patch_cannot_touch_other_fields(self):
        self.plan['proposals'][1]['field_changes']['status_id'] = 'changed'; self.approve_changes()
        with self.assertRaises(PermissionError): self.execute()
        self.writer.put_owner.assert_not_called()

    def test_new_hold_blocks_write(self):
        self.source.items = [replace(item, holds=({'workflow': 'qa'},)) for item in self.source.items]
        with self.assertRaisesRegex(RuntimeError, 'no longer eligible'): self.execute()
        self.writer.put_owner.assert_not_called()

    def test_lost_certification_blocks(self):
        self.source.reps[0]['certified'] = False
        with self.assertRaises(RuntimeError): self.execute()
        self.writer.put_owner.assert_not_called()

    def test_capacity_is_rechecked(self):
        self.source.capacity = 1000
        with self.assertRaisesRegex(RuntimeError, 'capacity'): self.execute()
        self.writer.put_owner.assert_not_called()

    def test_owner_change_immediately_before_put_blocks(self):
        self.source.changed_before_put = True
        with self.assertRaisesRegex(RuntimeError, 'immediately before write'): self.execute()
        self.writer.put_owner.assert_not_called()

    def test_uncertain_write_is_not_retried(self):
        self.writer.put_owner.side_effect = TimeoutError('Response lost')
        with self.assertRaises(TimeoutError): self.execute()
        self.assertEqual(len(self.store.uncertain('lead_test')), 1)
        with self.assertRaisesRegex(RuntimeError, 'Uncertain write'): self.execute()
        self.writer.put_owner.assert_called_once()

    def test_owner_readback_mismatch_retains_uncertain_intent(self):
        self.writer.put_owner.side_effect = None
        with self.assertRaisesRegex(RuntimeError, 'readback differs'): self.execute()
        self.assertEqual(len(self.store.uncertain('lead_test')), 1)

    def test_kill_switch_between_leads_stops_next_assignment(self):
        original = self.writer.put_owner.side_effect
        def write_then_disable(lead_id, owner):
            original(lead_id, owner)
            os.environ['LANE2_TIER_ROUTING_ENABLED'] = '0'
        self.writer.put_owner.side_effect = write_then_disable
        with self.assertRaises(PermissionError): self.execute()
        self.writer.put_owner.assert_called_once()
        self.assertEqual(len(self.records), 1)


class EntrypointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with patch.dict(os.environ, {'CLOSE_API_KEY': 'fake'}):
            import assign_lane2_leads
        cls.assigner = assign_lane2_leads

    def test_tier_apply_off_never_reaches_legacy_reads_or_writes(self):
        argv = ['assign_lane2_leads.py', '--tier-routing', '--apply', '--tier-rep', 'user_ramp',
            '--tier-lead', 'lead_test', '--tier-max-assignments', '1', '--tier-output', 'unused', '--tier-tagger', 'existing.py']
        with patch('sys.argv', argv), patch.dict(os.environ, {'LANE2_TIER_ROUTING_ENABLED': '0'}), \
                patch.object(self.assigner, 'search') as search, patch.object(self.assigner, '_req') as request, \
                patch.object(self.assigner, 'release_vp_end_leads') as release:
            with self.assertRaises(PermissionError): self.assigner.main()
        search.assert_not_called(); request.assert_not_called(); release.assert_not_called()

    def test_flag_off_still_reaches_existing_queue_census(self):
        with patch('sys.argv', ['assign_lane2_leads.py']), \
                patch.object(self.assigner, 'search', side_effect=RuntimeError('legacy census reached')) as search:
            with self.assertRaisesRegex(RuntimeError, 'legacy census reached'): self.assigner.main()
        search.assert_called_once()

    def test_legacy_apply_still_reaches_existing_queue_census(self):
        with patch('sys.argv', ['assign_lane2_leads.py', '--apply']), \
                patch.object(self.assigner, 'search', side_effect=RuntimeError('legacy census reached')) as search:
            with self.assertRaisesRegex(RuntimeError, 'legacy census reached'): self.assigner.main()
        search.assert_called_once()


if __name__ == '__main__':
    unittest.main()
