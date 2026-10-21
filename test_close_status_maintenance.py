import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch
from close_status_policy import *
from close_status_reader import CloseClient, SnapshotReader, EXACT_STOP
from close_status_maintenance import StatusExecutor, main, follow_up_ids

NOW = datetime(2026, 10, 20, 22, 30, tzinfo=timezone.utc)
ROSTER = Roster(frozenset({'user_closer'}), NOW)
OPP = Opportunity('oppo_test', OPP_HOT, NOW - timedelta(days=1), 'active', 'HOT - Follow Up', 'user_setter', SALES_PIPELINE)
BASE = Evidence('lead_test', HOT, ('user_setter',), (OPP,))


class PolicyTests(unittest.TestCase):
    def decision(self, **kwargs):
        return decide(replace(BASE, **kwargs), ROSTER, frozenset(), NOW)

    def test_exact_boundary_expires(self):
        self.assertEqual(self.decision(latest_inbound=NOW - WINDOW).lead_status, GENERAL)

    def test_one_microsecond_before_expiry_stays_hot(self):
        self.assertEqual(self.decision(latest_inbound=NOW - WINDOW + timedelta(microseconds=1)).kind, 'keep')

    def test_expiry_changes_controlling_opportunity_first(self):
        result = self.decision()
        self.assertEqual((result.opportunity_id, result.opportunity_status, result.lead_status), ('oppo_test', OPP_GENERAL, GENERAL))

    def test_held_beats_everything(self):
        result = decide(BASE, Roster(frozenset(), NOW), {held_hash(BASE.lead_id)}, NOW)
        self.assertEqual(result.reason, 'explicit_held_lead_exclusion')

    def test_active_closer_excluded(self):
        self.assertEqual(self.decision(owners=('user_closer',)).reason, 'active_closer_owner')

    def test_unowned_is_supported(self):
        self.assertTrue(self.decision(owners=()).changes)

    def test_multiple_owners_hold(self):
        self.assertEqual(self.decision(owners=('user_a', 'user_b')).kind, 'hold')

    def test_incomplete_fails_closed(self):
        self.assertEqual(self.decision(complete=False).kind, 'hold')

    def test_every_suppression_blocks_even_recent_reply(self):
        for reason in ('historical_stop', 'historical_dnc_status', 'email_unsubscribed', 'blocked_phone'):
            with self.subTest(reason=reason):
                self.assertEqual(self.decision(suppressions=(reason,), latest_inbound=NOW).kind, 'hold')

    def test_review_and_future_meeting_hold(self):
        self.assertEqual(self.decision(review_reasons=('callback_task',)).kind, 'hold')
        self.assertEqual(self.decision(future_meeting=True).kind, 'hold')

    def test_booked_and_won_native_statuses_never_promoted(self):
        for status in ('stat_lGHxEKwhbVswuchbpRo6XcMMSXz0fV4CID9qFWT8KCO', DNC, 'stat_won', 'stat_lost'):
            with self.subTest(status=status):
                self.assertEqual(self.decision(status=status, latest_inbound=NOW).kind, 'hold')

    def test_any_won_opportunity_blocks(self):
        self.assertEqual(self.decision(opportunities=(OPP, replace(OPP, id='oppo_old', status_type='won'))).kind, 'hold')

    def test_mistyped_legacy_won_label_blocks(self):
        self.assertEqual(self.decision(opportunities=(replace(OPP, label='OLD Deal Won'),)).kind, 'hold')

    def test_active_closer_opportunity_blocks(self):
        self.assertEqual(self.decision(opportunities=(replace(OPP, owner='user_closer'),)).kind, 'hold')

    def test_tied_or_unrelated_winner_blocks(self):
        self.assertEqual(self.decision(opportunities=(OPP, replace(OPP, id='oppo_other'))).kind, 'hold')
        self.assertEqual(self.decision(opportunities=(replace(OPP, pipeline='pipe_other'),)).kind, 'hold')
        self.assertEqual(self.decision(opportunities=(replace(OPP, status='stat_closed_lost'),)).kind, 'hold')

    def test_stale_roster_blocks(self):
        self.assertEqual(decide(BASE, replace(ROSTER, captured_at=NOW-timedelta(hours=24)), set(), NOW).kind, 'hold')

    def test_future_signal_blocks(self):
        self.assertEqual(self.decision(latest_inbound=NOW+timedelta(seconds=1)).kind, 'hold')

    def test_fresh_general_and_nonresponsive_promote(self):
        for status in (GENERAL, NONRESPONSIVE):
            result = self.decision(status=status, opportunities=(), latest_inbound=NOW)
            self.assertEqual(result.lead_status, HOT)
            self.assertIsNone(result.opportunity_id)

    def test_general_outbound_cannot_keep_hot(self):
        # There is no rep/outbound clock in the policy contract.
        self.assertEqual(self.decision(status=GENERAL, opportunities=()).kind, 'keep')

    def test_legacy_normalization_and_no_opportunity_creation(self):
        result = self.decision(status=LEGACY, opportunities=(), latest_inbound=NOW)
        self.assertEqual(result.lead_status, NONRESPONSIVE)
        self.assertIsNone(result.opportunity_id)

    def test_latent_hot_opportunity_repaired_without_reactivation(self):
        result = self.decision(status=NONRESPONSIVE)
        self.assertIsNone(result.lead_status)
        self.assertEqual(result.opportunity_status, OPP_NONRESPONSIVE)

    def test_only_latest_opportunity_changes(self):
        older = replace(OPP, id='oppo_old', status='stat_unrelated', updated_at=NOW-timedelta(days=5))
        self.assertEqual(self.decision(opportunities=(older, OPP)).opportunity_id, OPP.id)

    def test_legacy_does_not_overwrite_an_existing_hot_or_general_opportunity(self):
        self.assertEqual(self.decision(status=LEGACY).kind, 'hold')

    def test_expiry_does_not_overwrite_nonresponsive_opportunity(self):
        self.assertEqual(self.decision(opportunities=(replace(OPP,status=OPP_NONRESPONSIVE),)).kind, 'hold')

    def test_nonhot_mismatch_is_left_to_existing_sync(self):
        self.assertEqual(self.decision(status=GENERAL, opportunities=(replace(OPP,status=OPP_NONRESPONSIVE),)).kind, 'hold')

    def test_manifest_sealed_to_all_75_leads(self):
        hashes = load_held_leads()
        self.assertEqual(len(hashes), 75)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'holds.json'
            path.write_text(json.dumps({'count':75, 'lead_id_sha256':list(hashes)[:-1]}))
            with self.assertRaises(ValueError):load_held_leads(path)


class TransportTests(unittest.TestCase):
    def test_preview_transport_blocks_every_mutation(self):
        session = Mock()
        client = CloseClient('fake', session=session, pause=lambda _:None)
        for method,path in [('PUT','/lead/lead_test/'),('DELETE','/lead/lead_test/'),('POST','/activity/sms/')]:
            with self.assertRaises((ValueError,PermissionError)):
                client.request(method,path,{'status_id':GENERAL})
        session.request.assert_not_called()

    def test_writer_rejects_owner_and_custom_field_patches(self):
        client = CloseClient('fake', write_enabled=True, session=Mock(), pause=lambda _:None)
        for body in ({'custom.field':'x'}, {'status_id':GENERAL,'owner':None}, {'status_id':None}):
            with self.assertRaises(ValueError):client.request('PUT','/lead/lead_test/',body)

    def test_pagination_does_not_treat_failure_or_empty_page_as_no_signal(self):
        client = CloseClient('fake', session=Mock(), pause=lambda _:None)
        client.request=Mock(return_value={'data':[],'has_more':True})
        with self.assertRaises(ValueError):client.list('/activity/sms/',{})

    def test_duplicate_activity_pages_fail_closed(self):
        client=CloseClient('fake',pause=lambda _:None)
        client.request=Mock(side_effect=[{'data':[{'id':'a'}],'has_more':True},{'data':[{'id':'a'}],'has_more':False}])
        with self.assertRaises(ValueError):client.list('/activity/sms/',{})

    def test_no_unknown_write_retry(self):
        import requests
        session=Mock();session.request.side_effect=requests.ConnectionError()
        client=CloseClient('fake',write_enabled=True,session=session,pause=lambda _:None)
        with self.assertRaises(requests.ConnectionError):client.request('PUT','/lead/lead_test/',{'status_id':GENERAL})
        self.assertEqual(session.request.call_count,1)

    def test_apply_requires_two_explicit_controls_before_credentials_or_reads(self):
        with patch.dict('os.environ',{},clear=True):
            with patch('sys.stderr', __import__('io').StringIO()):
                with self.assertRaises(SystemExit):main(['--scan-follow-up','--output','unused','--apply'])


class SnapshotTests(unittest.TestCase):
    def make_reader(self, rows=None, contacts=None):
        client=Mock()
        client.request.return_value={'id':BASE.lead_id,'organization_id':ORG,'status_id':HOT,
          'custom.'+OWNER:'user_setter','opportunities':[],
          'contacts':contacts if contacts is not None else [{'emails':[{'email':'buyer@example.com'}],'phones':[{'phone':'+15551112222'}]}]}
        def listing(path,params):return (rows or {}).get(path,[])
        client.list.side_effect=listing
        return SnapshotReader(client)

    def activity(self,**kwargs):
        return dict(id='acti_test',lead_id=BASE.lead_id,organization_id=ORG,
                    activity_at=NOW.isoformat(),date_created=NOW.isoformat(),**kwargs)

    def test_outbound_sms_email_never_refresh_clock(self):
        reader=self.make_reader({'/activity/sms/':[self.activity(direction='outbound',text='hello')],
                                 '/activity/email/':[self.activity(direction='outgoing',body_text='hello')]})
        self.assertIsNone(reader.read(BASE.lead_id,NOW,held=set(),roster=ROSTER).latest_inbound)

    def test_actual_inbound_sms_email_call_refreshes_clock(self):
        cases=[('/activity/sms/',self.activity(direction='inbound',text='hello',remote_phone='+15551112222')),
               ('/activity/email/',self.activity(direction='incoming',body_text='hello',sender='Buyer <buyer@example.com>')),
               ('/activity/call/',self.activity(direction='inbound',remote_phone='+15551112222'))]
        for path,row in cases:
            reader=self.make_reader({path:[row]})
            with self.subTest(path=path):self.assertEqual(reader.read(BASE.lead_id,NOW,held=set(),roster=ROSTER).latest_inbound,NOW)

    def test_missing_inbound_sender_is_review_not_a_crash_or_signal(self):
        for path,row in [('/activity/call/',self.activity(direction='inbound',remote_phone=None)),
                         ('/activity/sms/',self.activity(direction='inbound',text='hello',remote_phone=None)),
                         ('/activity/email/',self.activity(direction='incoming',body_text='hello',sender=None))]:
            with self.subTest(path=path):
                evidence=self.make_reader({path:[row]}).read(BASE.lead_id,NOW,held=set(),roster=ROSTER)
                self.assertIsNone(evidence.latest_inbound)
                self.assertTrue(evidence.review_reasons)

    def test_missing_inbound_body_holds_for_review(self):
        evidence=self.make_reader({'/activity/sms/':[self.activity(direction='inbound',text=None,remote_phone='+15551112222')]}).read(BASE.lead_id,NOW,held=set(),roster=ROSTER)
        self.assertIn('missing_inbound_message_body',evidence.review_reasons)

    def test_empty_contact_details_do_not_verify_empty_sender(self):
        contacts=[{'phones':[{'phone':''}],'emails':[{'email':''}]}]
        evidence=self.make_reader({'/activity/call/':[self.activity(direction='inbound',remote_phone=None)]},contacts).read(BASE.lead_id,NOW,held=set(),roster=ROSTER)
        self.assertIsNone(evidence.latest_inbound)
        self.assertIn('unverified_recent_inbound_caller',evidence.review_reasons)

    def test_unverified_recent_sender_is_review_not_absence(self):
        reader=self.make_reader({'/activity/email/':[self.activity(direction='incoming',body_text='hello',sender='other@example.com')]})
        evidence=reader.read(BASE.lead_id,NOW,held=set(),roster=ROSTER)
        self.assertIn('unverified_recent_inbound_sender',evidence.review_reasons)

    def test_historical_stop_does_not_clear_after_reply(self):
        reader=self.make_reader({'/activity/sms/':[self.activity(direction='inbound',text='STOP',remote_phone='+15551112222',date_sent=(NOW-timedelta(days=90)).isoformat())]})
        self.assertIn('historical_stop',reader.read(BASE.lead_id,NOW,held=set(),roster=ROSTER).suppressions)

    def test_historical_dnc_status_preserved(self):
        reader=self.make_reader({'/activity/status_change/lead/':[self.activity(old_status_id=DNC,new_status_id=HOT)]})
        self.assertIn('historical_dnc_status',reader.read(BASE.lead_id,NOW,held=set(),roster=ROSTER).suppressions)

    def test_callback_and_form_notes_hold_for_review(self):
        reader=self.make_reader({'/activity/note/':[self.activity(note='## Qualification completed')]})
        self.assertIn('recent_callback_or_form_note',reader.read(BASE.lead_id,NOW,held=set(),roster=ROSTER).review_reasons)

    def test_held_lead_has_zero_close_requests(self):
        reader=self.make_reader()
        evidence=reader.read(BASE.lead_id,NOW,held={held_hash(BASE.lead_id)},roster=ROSTER)
        self.assertEqual(evidence.lead_id,BASE.lead_id)
        reader.client.request.assert_not_called();reader.client.list.assert_not_called()

    def test_failed_history_read_never_authorizes_expiry(self):
        reader=self.make_reader();reader.client.list.side_effect=ValueError('incomplete')
        with self.assertRaises(ValueError):reader.read(BASE.lead_id,NOW,held=set(),roster=ROSTER)

    def test_cross_lead_activity_rejected(self):
        a=self.activity(direction='inbound',text='hello');a['lead_id']='lead_wrong'
        reader=self.make_reader({'/activity/sms/':[a]})
        with self.assertRaises(ValueError):reader.read(BASE.lead_id,NOW,held=set(),roster=ROSTER)


class ExecutionTests(unittest.TestCase):
    def executor(self,evidence):
        reader=Mock();reader.client.write_enabled=True;reader.read.side_effect=evidence
        journal=[]
        return StatusExecutor(reader,ROSTER,set(),lambda:NOW,journal.append),reader,journal

    def test_opportunity_first_status_only_and_idempotent_replay(self):
        after_opp=replace(BASE,opportunities=(replace(OPP,status=OPP_GENERAL),))
        done=replace(after_opp,status=GENERAL)
        executor,reader,journal=self.executor([BASE,after_opp,done])
        reader.client.request.side_effect=[{'id':OPP.id,'lead_id':BASE.lead_id,'status_id':OPP_GENERAL},
                                           {'id':BASE.lead_id,'status_id':GENERAL}]
        result=executor.apply(BASE.lead_id,decide(BASE,ROSTER,set(),NOW))
        self.assertEqual(result['state'],'verified')
        self.assertEqual(reader.client.request.call_args_list[0].args,('PUT',f'/opportunity/{OPP.id}/',{'status_id':OPP_GENERAL}))
        self.assertEqual(reader.client.request.call_args_list[1].args,('PUT',f'/lead/{BASE.lead_id}/',{'status_id':GENERAL}))
        self.assertEqual(journal[0]['state'],'intent')
        self.assertEqual(decide(done,ROSTER,set(),NOW).kind,'keep')

    def test_opportunity_side_effect_needs_no_redundant_lead_write(self):
        reconciled=replace(BASE,status=GENERAL,opportunities=(replace(OPP,status=OPP_GENERAL),))
        executor,reader,_=self.executor([BASE,reconciled])
        reader.client.request.return_value={'id':OPP.id,'lead_id':BASE.lead_id,'status_id':OPP_GENERAL}
        result=executor.apply(BASE.lead_id,decide(BASE,ROSTER,set(),NOW))
        self.assertEqual(result['state'],'verified')
        self.assertEqual(result['reason'],'target_already_reconciled')
        self.assertEqual(len(result['writes']),1)
        reader.client.request.assert_called_once()

    def test_matching_target_with_changed_owner_stays_held(self):
        changed=replace(BASE,status=GENERAL,owners=('user_other',),opportunities=(replace(OPP,status=OPP_GENERAL),))
        executor,reader,_=self.executor([BASE,changed])
        reader.client.request.return_value={'id':OPP.id,'lead_id':BASE.lead_id,'status_id':OPP_GENERAL}
        result=executor.apply(BASE.lead_id,decide(BASE,ROSTER,set(),NOW))
        self.assertEqual(result['state'],'partial')
        reader.client.request.assert_called_once()

    def test_fresh_legacy_normalization_is_verified_before_hot_followup(self):
        initial=replace(BASE,status=LEGACY,opportunities=(),latest_inbound=NOW)
        current=replace(initial,status=NONRESPONSIVE)
        executor,reader,_=self.executor([initial,initial,current])
        reader.client.request.return_value={'id':BASE.lead_id,'status_id':NONRESPONSIVE}
        result=executor.apply(BASE.lead_id,decide(initial,ROSTER,set(),NOW))
        self.assertEqual(result['state'],'verified')
        self.assertEqual(result['reason'],'legacy_normalized_pending_hot_refresh')
        reader.client.request.assert_called_once()

    def test_owner_transfer_before_first_write_cancels(self):
        executor,reader,_=self.executor([replace(BASE,owners=('user_other',))])
        self.assertEqual(executor.apply(BASE.lead_id,decide(BASE,ROSTER,set(),NOW))['state'],'held')
        reader.client.request.assert_not_called()

    def test_new_inbound_between_writes_stops_lead_write(self):
        fresh=replace(BASE,latest_inbound=NOW,opportunities=(replace(OPP,status=OPP_GENERAL),))
        executor,reader,_=self.executor([BASE,fresh]);reader.client.request.return_value={'id':OPP.id,'lead_id':BASE.lead_id,'status_id':OPP_GENERAL}
        self.assertEqual(executor.apply(BASE.lead_id,decide(BASE,ROSTER,set(),NOW))['state'],'partial')
        self.assertEqual(reader.client.request.call_count,1)

    def test_closer_assignment_between_writes_stops_lead_write(self):
        changed=replace(BASE,owners=('user_closer',),opportunities=(replace(OPP,status=OPP_GENERAL),))
        executor,reader,_=self.executor([BASE,changed]);reader.client.request.return_value={'id':OPP.id,'lead_id':BASE.lead_id,'status_id':OPP_GENERAL}
        self.assertEqual(executor.apply(BASE.lead_id,decide(BASE,ROSTER,set(),NOW))['state'],'partial')
        self.assertEqual(reader.client.request.call_count,1)

    def test_suppression_between_writes_stops_lead_write(self):
        changed=replace(BASE,suppressions=('historical_stop',),opportunities=(replace(OPP,status=OPP_GENERAL),))
        executor,reader,_=self.executor([BASE,changed]);reader.client.request.return_value={'id':OPP.id,'lead_id':BASE.lead_id,'status_id':OPP_GENERAL}
        self.assertEqual(executor.apply(BASE.lead_id,decide(BASE,ROSTER,set(),NOW))['state'],'partial')
        self.assertEqual(reader.client.request.call_count,1)

    def test_no_network_write_without_durable_intent(self):
        executor,reader,_=self.executor([BASE]);executor.journal=Mock(side_effect=OSError('disk full'))
        with self.assertRaises(OSError):executor.apply(BASE.lead_id,decide(BASE,ROSTER,set(),NOW))
        reader.client.request.assert_not_called()

    def test_read_only_executor_cannot_apply(self):
        executor,reader,_=self.executor([]);reader.client.write_enabled=False
        with self.assertRaises(PermissionError):executor.apply(BASE.lead_id,decide(BASE,ROSTER,set(),NOW))


class InventoryTests(unittest.TestCase):
    def test_large_inventory_is_partitioned_before_cursor_limit(self):
        client=Mock()
        def request(method,path,body):
            self.assertEqual((method,path),('POST','/data/search/'))
            self.assertEqual(body['sort'][0]['field']['field_name'],'date_created')
            self.assertEqual(body['sort'][0]['direction'],'asc')
            condition=body['query']['queries'][2]['condition']
            start=timestamp(condition['on_or_after']['value']);end=timestamp(condition['before']['value'])
            if (end-start).days>40*366:
                return {'count':{'total':10000},'data':[],'cursor':None}
            status=body['query']['queries'][1]['condition']['object_ids'][0]
            return {'count':{'total':1},'data':[{'id':status+'_'+start.isoformat()}],'cursor':None}
        client.request.side_effect=request
        result=list(follow_up_ids(client))
        self.assertEqual(len(result),8)
        self.assertEqual(len(set(result)),8)

    def test_inventory_duplicate_aborts(self):
        client=Mock();client.request.return_value={'count':{'total':1},'data':[{'id':'lead_same'}],'cursor':None}
        with self.assertRaises(RuntimeError):list(follow_up_ids(client))

    def test_incomplete_inventory_count_cannot_authorize_a_run(self):
        client=Mock();client.request.return_value={'count':{'total':2},'data':[{'id':'lead_only'}],'cursor':None}
        with self.assertRaises(RuntimeError):list(follow_up_ids(client))

    def test_missing_inventory_count_cannot_authorize_a_run(self):
        client=Mock();client.request.return_value={'data':[],'cursor':None}
        with self.assertRaises(KeyError):list(follow_up_ids(client))


if __name__=='__main__':unittest.main()
