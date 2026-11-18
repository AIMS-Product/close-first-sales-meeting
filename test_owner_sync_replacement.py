from copy import deepcopy
import unittest

from close_status_policy import ORG, OWNER
from lane2_policy import F_OVERRIDE, F_STATE
from owner_sync_replacement import OPPORTUNITY_OWNER_MIRROR, legacy_opportunity_plans, replacement_plan
from workflow_holds import BATCH


class ReplacementTests(unittest.TestCase):
    def setUp(self):
        self.lead={'id':'lead_Example','organization_id':ORG,'status_id':'stat_normal',
            'custom.'+OWNER:'user_New','custom.'+OPPORTUNITY_OWNER_MIRROR:'user_Old',
            'future_meeting_index':False,'opportunities':[
                {'id':'oppo_old','lead_id':'lead_Example','organization_id':ORG,'status_id':'stat_lost',
                 'status_type':'lost','user_id':'user_Old','date_created':'2026-07-01T00:00:00Z'},
                {'id':'oppo_new','lead_id':'lead_Example','organization_id':ORG,'status_id':'stat_active',
                 'status_type':'active','user_id':'user_Old','date_created':'2026-08-01T00:00:00Z'}]}

    def plan(self,lead=None):
        return replacement_plan(self.lead if lead is None else lead,{'user_Closer'},set())

    def test_existing_nonwon_scope_and_mirror(self):
        self.assertEqual(legacy_opportunity_plans(self.lead),{'oppo_old','oppo_new'})
        self.assertEqual(self.plan(),('sync_opportunity_and_mirror',
                         {'oppo_old':'user_New','oppo_new':'user_New'},'user_New'))

    def test_empty_opportunity_is_rechecked_on_next_pass(self):
        without=deepcopy(self.lead)
        without['opportunities']=[]
        self.assertEqual(self.plan(without),('awaiting_opportunity',{},None))
        without['custom.'+OWNER]='user_Later'
        without['opportunities'].append(self.lead['opportunities'][1])
        self.assertEqual(self.plan(without),('sync_opportunity_and_mirror',{'oppo_new':'user_Later'},'user_Later'))

    def test_no_duplicate_changes_when_aligned(self):
        for opp in self.lead['opportunities']:opp['user_id']='user_New'
        self.assertEqual(self.plan(),('sync_mirror_only',{},'user_New'))
        self.lead['custom.'+OPPORTUNITY_OWNER_MIRROR]=['user_New']
        self.assertEqual(self.plan(),('aligned',{},None))

    def test_protected_lead_or_deal_blocks_both_writes(self):
        for key,value,reason in [
          ('custom.'+F_OVERRIDE,'Yes','reassignment_override'),
          ('custom.'+F_STATE,'QA Triage','protected_lead_status'),
          ('custom.'+BATCH,'existing [STH1 qa token]','shared_or_unconfirmed_write_hold'),
          ('uncertain_write',True,'shared_or_unconfirmed_write_hold'),
          ('durable_hold',True,'shared_or_unconfirmed_write_hold'),
          ('future_meeting_index',True,'upcoming_meeting'),
          ('custom.'+OWNER,'user_Closer','closer_is_lead_owner')]:
            with self.subTest(reason=reason,key=key):
                self.assertEqual(self.plan(dict(self.lead,**{key:value})),(reason,{},None))
        for key,value,reason in [('status_type','won','won_opportunity'),('user_id','user_Closer','closer_owned_opportunity')]:
            fixture=deepcopy(self.lead)
            fixture['opportunities'][0][key]=value
            with self.subTest(key=key):
                self.assertEqual(self.plan(fixture),(reason,{},None))

    def test_missing_and_ambiguous_target_evidence_blocks(self):
        self.lead['opportunities'][1]['date_created']=self.lead['opportunities'][0]['date_created']
        self.assertEqual(self.plan(),('newest_opportunity_ambiguous',{},None))
        del self.lead['opportunities'][1]['date_created']
        self.assertEqual(self.plan(),('newest_opportunity_evidence_unavailable',{},None))
        del self.lead['opportunities'][1]['lead_id']
        self.assertEqual(self.plan(),('opportunity_evidence_unavailable',{},None))


class ExecutorTests(ReplacementTests):
    def run_executor(self, lead, *, paused=True, readback=True, before_write=None):
        from contextlib import nullcontext
        from types import SimpleNamespace
        from urllib.parse import urlsplit
        from unittest.mock import Mock

        from owner_sync_replacement import WORKFLOW_ID
        from run_owner_sync_replacement import reconcile

        lead = deepcopy(lead)
        lead['num_upcoming_meetings'] = 0
        effects = []

        def get(method, path):
            self.assertEqual(method, 'GET')
            kind, record = urlsplit(path).path.strip('/').split('/')
            if kind == 'sequence':
                return {'id': WORKFLOW_ID, 'organization_id': ORG,
                        'status': 'paused' if paused else 'active',
                        'subscription_counts_by_status': {'active': 0}}
            if kind == 'lead':
                return deepcopy(lead)
            return deepcopy(next(row for row in lead['opportunities'] if row['id'] == record))

        def put(path, patch):
            effects.append((path, patch))
            if readback:
                record = path.split('/')[2]
                target = lead if record == lead['id'] else next(
                    row for row in lead['opportunities'] if row['id'] == record
                )
                target.update(patch)
            if before_write:
                before_write(lead)

        store = Mock(serialized=nullcontext)
        store.holds.return_value = []
        store.uncertain.return_value = []
        call = lambda: reconcile(SimpleNamespace(request=get), store, lead['id'],
                                 {'user_Closer'}, set(), put, budget=10)
        return call, effects, store

    def test_opportunities_verify_before_mirror(self):
        call, effects, store = self.run_executor(self.lead)
        self.assertEqual(call(), ('sync_opportunity_and_mirror', 3, 3))
        self.assertEqual([path for path, _ in effects], [
            '/opportunity/oppo_old/', '/opportunity/oppo_new/', '/lead/lead_Example/',
        ])
        self.assertEqual(store.receipt.call_count, 3)

    def test_native_active_prohibits_write(self):
        call, effects, store = self.run_executor(self.lead, paused=False)
        with self.assertRaisesRegex(RuntimeError, 'paused and drained'):
            call()
        self.assertEqual(effects, [])
        store.intent.assert_not_called()

    def test_unverified_write_retains_intent_and_stops(self):
        call, effects, store = self.run_executor(self.lead, readback=False)
        with self.assertRaisesRegex(RuntimeError, 'independently verified'):
            call()
        self.assertEqual(len(effects), 1)
        store.intent.assert_called_once()
        store.receipt.assert_not_called()

    def test_owner_changed_mid_sync_prevents_mirror(self):
        def change_owner(lead):
            lead['custom.' + OWNER] = 'user_Other'

        call, effects, store = self.run_executor(self.lead, before_write=change_owner)
        self.assertEqual(call(), ('lead_owner_changed_during_sync', 3, 1))
        self.assertEqual(len(effects), 1)
        store.receipt.assert_called_once()

    def test_no_opportunity_performs_no_write(self):
        self.lead['opportunities'] = []
        call, effects, store = self.run_executor(self.lead)
        self.assertEqual(call(), ('awaiting_opportunity', 0, 0))
        self.assertEqual(effects, [])
        store.intent.assert_not_called()
