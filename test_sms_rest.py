import copy
from dataclasses import replace, asdict
from datetime import datetime,timedelta,timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from close_status_policy import Evidence,Roster,Opportunity,OWNER,NONRESPONSIVE,HOT,DNC
from sms_rest_policy import (Calendar,Snapshot,Plan,admit,transition,return_to_rep,
                             TERMINAL_SEQUENCE,NOT_INTERESTED,TIER)
from sms_rest_close import signal_from_event,opted_out,event_pages
from sms_rest_workflow import Workflow,digest
from workflow_holds import Store,custom,BATCH,OWNER_SINCE,project,blocked,guard_request,no_sales_hold_query

NOW=datetime(2026,10,23,17,0,tzinfo=timezone.utc)  # Friday, 10am Pacific
ROSTER=Roster(frozenset({'user_closer'}),NOW)
LID='lead_test'
OWNER_ID='user_rep'


def snap(**changes):
    lead={'id':LID,'organization_id':'orga_Qwbx9tyUrl7Jdjurg9xJ8N6w1MN4PD2NQOgIhd8r7rC',
          'date_created':'2026-10-01T00:00:00+00:00',
          'status_id':NONRESPONSIVE,'custom.'+OWNER:OWNER_ID,'custom.'+OWNER_SINCE:'2026-10-01T00:00:00+00:00',
          'custom.'+BATCH:'Original batch  ', 'contacts':[{'phones':[{'phone':'+15550001'}],
            'emails':[{'email':'prospect@example.com'}]}],'opportunities':[]}
    task={'id':'task_terminal','lead_id':LID,'text':'VP THE END','sequence_id':TERMINAL_SEQUENCE,
          'date_created':(NOW-timedelta(hours=1)).isoformat(),'is_complete':False}
    value=Snapshot(lead,Evidence(LID,NONRESPONSIVE,(OWNER_ID,)),task,captured_at=NOW)
    for key,value_ in changes.items():
        value=replace(value,**{key:value_})
    return value


def grace(): return admit(snap(),ROSTER,NOW).cycle


def resting():
    cycle=grace();time=NOW+timedelta(days=3)
    original=snap();plan=transition(cycle,original,replace(ROSTER,captured_at=time),time)
    lead=dict(original.lead,**plan.lead_patch)
    return plan.cycle,replace(original,lead=lead,holds=({'workflow':'sms_rest','token':cycle['token']},))


class PolicyTests(unittest.TestCase):
    def test_friday_has_monday_deadline(self):
        self.assertEqual(Calendar().deadline(NOW),NOW+timedelta(days=3))
    def test_holiday_skipped(self):
        self.assertEqual(Calendar(holidays=frozenset({'2026-10-26'})).deadline(NOW),NOW+timedelta(days=4))
    def test_dst_preserves_local_hour(self):
        t=datetime(2026,10,30,17,tzinfo=timezone.utc)
        self.assertEqual(Calendar().deadline(t),datetime(2026,11,2,18,tzinfo=timezone.utc))
    def test_old_terminal_gets_new_grace(self):
        s=snap();s.task['date_created']='2026-08-01T00:00:00+00:00'
        self.assertEqual(admit(s,ROSTER,NOW).cycle['grace_expires_at'],(NOW+timedelta(days=3)).isoformat())
    def test_prelaunch_backlog_is_excluded_when_configured(self):
        self.assertEqual(admit(snap(),ROSTER,NOW,launch_at=NOW).reason,'prelaunch_backlog')
    def test_wrong_sequence_is_excluded(self):
        s=snap();s.task['sequence_id']='seq_other'
        self.assertEqual(admit(s,ROSTER,NOW).action,'excluded')
    def test_completed_task_is_not_admission(self):
        s=snap();s.task['is_complete']=True
        self.assertEqual(admit(s,ROSTER,NOW).action,'excluded')
    def test_closer_owner_excluded(self):
        s=snap();s.lead['custom.'+OWNER]='user_closer'
        self.assertEqual(admit(s,ROSTER,NOW).reason,'active_closer_owner')
    def test_closer_opportunity_excluded(self):
        o=Opportunity('opp',NONRESPONSIVE,NOW,'active','Nonresponsive','user_closer','pipe')
        self.assertEqual(admit(replace(snap(),evidence=replace(snap().evidence,opportunities=(o,))),ROSTER,NOW).reason,
                         'active_closer_opportunity_owner')
    def test_legacy_won_opp_is_protected(self):
        o=Opportunity('opp',NONRESPONSIVE,NOW,'active','OLD Deal Won','user_rep','pipe')
        self.assertEqual(admit(replace(snap(),evidence=replace(snap().evidence,opportunities=(o,))),ROSTER,NOW).action,'excluded')
    def test_other_hold_blocks_admission(self):
        self.assertEqual(admit(snap(holds=({'workflow':'qa','token':'other'},)),ROSTER,NOW).reason,'another_workflow_hold')
    def test_marker_only_blocks_admission(self):
        s=snap();s.lead['custom.'+BATCH]+=' [STH1 education abc]'
        self.assertEqual(admit(s,ROSTER,NOW).reason,'another_workflow_marker')
    def test_malformed_marker_fails_closed(self):
        s=snap();s.lead['custom.'+BATCH]+=' [STH1 broken]'
        self.assertEqual(admit(s,ROSTER,NOW).reason,'malformed_hold_marker')
    def test_reply_during_grace_keeps_rep(self):
        plan=transition(grace(),snap(signal_at=NOW),ROSTER,NOW)
        self.assertEqual(plan.action,'rep_working');self.assertFalse(plan.lead_patch)
    def test_before_deadline_no_owner_write(self):
        plan=transition(grace(),snap(),ROSTER,NOW+timedelta(hours=1))
        self.assertEqual(plan.action,'keep');self.assertFalse(plan.lead_patch)
    def test_rest_exact_90_days_and_saved_original(self):
        cycle,s=resting();self.assertEqual(cycle['original_batch'],'Original batch  ')
        self.assertEqual(datetime.fromisoformat(cycle['rest_expires_at'])-datetime.fromisoformat(cycle['rest_started_at']),timedelta(days=90))
        self.assertIsNone(custom(s.lead,OWNER));self.assertEqual(s.lead['status_id'],NONRESPONSIVE)
    def test_owner_generation_fence(self):
        s=snap();s.lead['custom.'+OWNER_SINCE]='2026-10-22T00:00:00+00:00'
        self.assertEqual(transition(grace(),s,ROSTER,NOW).reason,'owner_generation_changed')
    def test_stop_blocks_later_reply(self):
        cycle,s=resting();t=datetime.fromisoformat(cycle['rest_started_at'])+timedelta(days=1)
        s=replace(s,signal_at=t,evidence=replace(s.evidence,suppressions=('historical_stop',)))
        self.assertEqual(transition(cycle,s,replace(ROSTER,captured_at=t),t).reason,'stop_dnc_or_unsubscribe')
    def test_dnc_blocks_expiry(self):
        cycle,s=resting();s.lead['status_id']=DNC;t=datetime.fromisoformat(cycle['rest_expires_at'])
        self.assertEqual(transition(cycle,s,replace(ROSTER,captured_at=t),t).reason,'protected_status')
    def test_new_response_requests_early_return(self):
        cycle,s=resting();t=datetime.fromisoformat(cycle['rest_started_at'])+timedelta(days=1)
        p=transition(cycle,replace(s,signal_at=t),replace(ROSTER,captured_at=t),t)
        self.assertEqual(p.action,'return_requested');self.assertEqual(p.reason,'qualifying_signal')
    def test_old_response_does_not_end_rest(self):
        cycle,s=resting();t=datetime.fromisoformat(cycle['rest_started_at'])+timedelta(days=1)
        self.assertEqual(transition(cycle,replace(s,signal_at=NOW),replace(ROSTER,captured_at=t),t).action,'keep')
    def test_expiry_requests_lane2(self):
        cycle,s=resting();t=datetime.fromisoformat(cycle['rest_expires_at'])
        self.assertEqual(transition(cycle,s,replace(ROSTER,captured_at=t),t).reason,'ninety_days_elapsed')
    def test_reactivation_handoff_retains_other_text(self):
        cycle,s=resting();t=datetime.fromisoformat(cycle['rest_expires_at'])
        cycle=dict(cycle,phase='return_requested',return_reason='ninety_days_elapsed')
        s.lead['custom.'+BATCH]+=' [STL1 lane1_release qa123]'
        p=return_to_rep(cycle,s,replace(ROSTER,captured_at=t),t,'user_rep',['user_rep'])
        self.assertTrue(p.release_hold);self.assertEqual(p.lead_patch['custom.'+TIER],['Proven'])
        self.assertIn('[STL1 lane1_release qa123]',p.lead_patch['custom.'+BATCH])
        self.assertNotIn('STH1',p.lead_patch['custom.'+BATCH])
    def test_other_hold_never_released(self):
        cycle,s=resting();t=datetime.fromisoformat(cycle['rest_expires_at'])
        cycle=dict(cycle,phase='return_requested',return_reason='ninety_days_elapsed')
        p=return_to_rep(cycle,replace(s,holds=s.holds+({'workflow':'education','token':'x'},)),replace(ROSTER,captured_at=t),t,'user_rep',['user_rep'])
        self.assertEqual(p.action,'excluded');self.assertFalse(p.release_hold)
    def test_not_interested_preserves_rep_disposition(self):
        s=snap();s.lead['status_id']=NOT_INTERESTED
        self.assertEqual(transition(grace(),s,ROSTER,NOW).action,'excluded')
    def test_future_meeting_protected(self):
        self.assertEqual(admit(snap(evidence=replace(snap().evidence,future_meeting=True)),ROSTER,NOW).reason,'booked')
    def test_stale_roster(self):
        self.assertEqual(admit(snap(),replace(ROSTER,captured_at=NOW-timedelta(days=2)),NOW).action,'excluded')


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'state.sqlite';self.store=Store(self.path)
    def tearDown(self): self.store.close();self.temp.cleanup()
    def test_hold_tokens_are_owned(self):
        with self.store.serialized():
            self.store.acquire(LID,'qa','a',NOW.isoformat());self.store.acquire(LID,'sms_rest','b',NOW.isoformat())
            with self.assertRaises(ValueError):self.store.release(LID,'qa','b')
            self.store.release(LID,'sms_rest','b')
        self.assertEqual([h['workflow'] for h in self.store.holds(LID)],['qa'])
    def test_expired_hold_is_still_hold(self):
        self.store.acquire(LID,'sms_rest','a','2020-01-01','2020-02-01')
        self.assertTrue(blocked(snap().lead,self.store))
    def test_original_and_other_markers_preserved(self):
        original='  Exact original [STH1 qa abc] [STL1 lane1_release q]  '
        value=project(original,'sms_rest','new',acquire=True)
        self.assertEqual(project(value,'sms_rest','new',acquire=False),original)
    def test_crash_keeps_write_intent(self):
        with self.assertRaises(RuntimeError):
            with self.store.serialized():
                self.store.intent(LID,'token',{'path':'/lead/'+LID+'/','patch':{'status_id':HOT}})
                raise RuntimeError('simulated process failure')
        self.store.close();self.store=Store(self.path)
        self.assertEqual(len(self.store.uncertain(LID)),1)
    def test_due_index(self):
        c=grace();self.store.save(c)
        self.assertEqual(self.store.due(NOW.isoformat()),[])
        self.assertEqual(len(self.store.due((NOW+timedelta(days=4)).isoformat())),1)
    def test_nonexistent_configured_db_fails_closed(self):
        from workflow_holds import configured_store
        with patch.dict(os.environ,{'WORKFLOW_HOLD_DB':str(self.path)+'missing','WORKFLOW_HOLD_BACKEND':'sqlite'}):
            with self.assertRaises(RuntimeError):configured_store()


class SignalTests(unittest.TestCase):
    def event(self,kind,data):return {'id':'ev_x','object_type':kind,'action':'created','data':dict(activity_at=NOW.isoformat(),**data)}
    def test_native_form_qualifies(self):
        self.assertEqual(signal_from_event(self.event('activity.form_submission',{}),snap().lead),NOW)
    def test_email_sender_must_match(self):
        e=self.event('activity.email',{'direction':'incoming','sender':'other@example.com','body_text':'hello'})
        self.assertIsNone(signal_from_event(e,snap().lead));e['data']['sender']='prospect@example.com'
        self.assertEqual(signal_from_event(e,snap().lead),NOW)
    def test_sms_stop_is_suppression_not_signal(self):
        e=self.event('activity.sms',{'direction':'inbound','remote_phone':'+15550001','text':'STOP'})
        self.assertIsNone(signal_from_event(e,snap().lead));self.assertTrue(opted_out(e))
    def test_outbound_is_not_reply(self):
        e=self.event('activity.sms',{'direction':'outbound','remote_phone':'+15550001','text':'hello'})
        self.assertIsNone(signal_from_event(e,snap().lead))
    def test_invitation_is_not_registration(self):
        e=self.event('activity.custom',{'custom_activity_type_id':'invitation'})
        self.assertIsNone(signal_from_event(e,snap().lead,['registration']))
    def test_verified_registration_type_qualifies(self):
        e=self.event('activity.custom',{'custom_activity_type_id':'registration'})
        self.assertEqual(signal_from_event(e,snap().lead,['registration']),NOW)
    def test_retention_gap_blocks(self):
        with self.assertRaises(RuntimeError):list(event_pages(None,NOW-timedelta(days=30),NOW))


class FakeReader:
    def __init__(self,store,snapshot):self.store=store;self.snapshot=snapshot;self.client=self;self.registration_types=()
    def read(self,lead_id,now,**kwargs):
        return replace(self.snapshot,holds=tuple(self.store.holds(lead_id)),captured_at=now)
    def request(self,method,path,payload=None):
        if path.startswith('/task/'):return dict(self.snapshot.task)
        if path.startswith('/sequence_subscription/'):return {'status':'paused'}
        return copy.deepcopy(self.snapshot.lead)


class FakeWriter:
    enabled=True
    def __init__(self,reader):self.reader=reader;self.calls=[];self.fail=False
    def put(self,path,payload):
        self.calls.append((path,payload))
        if path.startswith('/lead/'):self.reader.snapshot.lead.update(payload)
        if path.startswith('/task/'):self.reader.snapshot.task.update(payload)
        if path.startswith('/sequence_subscription/'):self.reader.snapshot=replace(self.reader.snapshot,sales_subscriptions=())
        if self.fail:raise TimeoutError('server committed, response lost')
        return {}


class WorkflowTests(StoreTests):
    def setUp(self):
        super().setUp();self.reader=FakeReader(self.store,snap());self.writer=FakeWriter(self.reader)
        self.workflow=Workflow(self.reader,self.writer,self.store,ROSTER,clock=lambda:NOW,launch_at=NOW-timedelta(days=1))
    def approval(self,p):
        from test_hold_rollout import fake_ready_manifest
        return {'proposal_sha256':digest(p),'allowed_lead_ids':[LID],'expires_at':(NOW+timedelta(minutes=5)).isoformat(),
                'hold_deployment_manifest':fake_ready_manifest(self.workflow.clock()),
                'verified_integrations':dict.fromkeys(['shared_store_and_all_writers','all_dial_queues','sales_dispatch_and_enrollment',
                    'business_calendar','lane2_routing','schema_and_tenant'],True)}
    def test_preview_has_no_provider_writes_or_cycle_commit(self):
        p=self.workflow.propose(LID,task_id='task_terminal')
        self.assertEqual(p['plan']['action'],'grace');self.assertFalse(self.writer.calls);self.assertIsNone(self.store.cycle(LID))
    def test_apply_grace_preserves_owner_and_batch(self):
        p=self.workflow.propose(LID,task_id='task_terminal');r=self.workflow.apply(p,authorization=self.approval(p))
        self.assertEqual(r['state'],'verified');self.assertEqual(custom(self.reader.snapshot.lead,OWNER),OWNER_ID)
        self.assertEqual(custom(self.reader.snapshot.lead,BATCH),'Original batch  ')
    def test_owner_changed_after_preview_blocks(self):
        p=self.workflow.propose(LID,task_id='task_terminal');self.reader.snapshot.lead['custom.'+OWNER]='user_other'
        self.assertEqual(self.workflow.apply(p,authorization=self.approval(p))['state'],'held');self.assertFalse(self.writer.calls)
    def test_disabled_write_gate(self):
        p=self.workflow.propose(LID,task_id='task_terminal');self.writer.enabled=False
        with self.assertRaises(PermissionError):self.workflow.apply(p,authorization=self.approval(p))
    def test_missing_integration_receipt_blocks(self):
        p=self.workflow.propose(LID,task_id='task_terminal');a=self.approval(p);a['verified_integrations']['all_dial_queues']=False
        with self.assertRaises(PermissionError):self.workflow.apply(p,authorization=a)
    def test_ambiguous_write_is_not_retried(self):
        p=self.workflow.propose(LID,task_id='task_terminal');self.writer.fail=True
        with self.assertRaises(TimeoutError):self.workflow.apply(p,authorization=self.approval(p))
        self.assertEqual(len(self.writer.calls),1);self.assertEqual(len(self.store.uncertain(LID)),1)
        with self.assertRaises(RuntimeError):self.workflow.apply(p,authorization=self.approval(p))
        self.assertEqual(len(self.writer.calls),1)
    def test_readonly_reconciliation_confirms_committed_write(self):
        p=self.workflow.propose(LID,task_id='task_terminal');self.writer.fail=True
        with self.assertRaises(TimeoutError):self.workflow.apply(p,authorization=self.approval(p))
        result=self.workflow.reconcile(LID)
        self.assertTrue(result[0]['target_matches']);self.assertEqual(self.store.uncertain(LID),[])
        self.assertEqual(len(self.writer.calls),1);self.assertEqual(self.store.cycle(LID)['phase'],'recovery')
    def test_rest_holds_before_put(self):
        cycle=grace();cycle.update(evidence_baseline={'suppressions':[]},evidence_through=NOW.isoformat())
        self.store.save(cycle);due=NOW+timedelta(days=3);self.workflow.clock=lambda:due;self.workflow.roster=replace(ROSTER,captured_at=due)
        p=self.workflow.propose(LID);a=self.approval(p);a['expires_at']=(due+timedelta(minutes=5)).isoformat()
        self.workflow.apply(p,authorization=a)
        self.assertEqual(self.store.holds(LID)[0]['workflow'],'sms_rest')
        self.assertIsNone(custom(self.reader.snapshot.lead,OWNER));self.assertEqual(self.store.cycle(LID)['phase'],'rest')
    def test_event_replay_deduplicates(self):
        self.store.save(dict(grace(),evidence_baseline={'suppressions':[]}))
        event={'id':'ev_stop','date_updated':NOW.isoformat(),'object_type':'activity.sms','action':'created','lead_id':LID,
               'data':{'direction':'inbound','remote_phone':'+15550001','text':'STOP','activity_at':NOW.isoformat()}}
        self.workflow.observe([event],NOW);self.workflow.observe([event],NOW)
        self.assertEqual(self.store.cycle(LID)['evidence_baseline']['suppressions'],['stop_dnc_event'])


if __name__=='__main__':unittest.main()
