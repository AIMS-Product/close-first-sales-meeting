import unittest
from unittest.mock import Mock,patch
from datetime import datetime,timedelta,timezone
import close_status_batch as batch
import close_status_policy as policy
from close_status_maintenance import main

NOW=datetime(2026,10,21,tzinfo=timezone.utc)
ROSTER=policy.Roster(frozenset({'user_closer'}),NOW)
class BatchTests(unittest.TestCase):
 def row(self,**kwargs):
  return dict(id='acti_test',lead_id='lead_test',organization_id=policy.ORG,date_created=(NOW-timedelta(days=1)).isoformat(),**kwargs)
 def test_unknown_cache_source_and_mutations_are_rejected(self):
  cache=batch.SnapshotCache({}, {})
  with self.assertRaises(KeyError):cache.list('/activity/sms/',{'lead_id':'lead_test'})
  with self.assertRaises(PermissionError):cache.request('PUT','/lead/lead_test/',{'status_id':policy.HOT})
 def test_write_enabled_client_cannot_supply_batch_evidence(self):
  client=Mock(write_enabled=True)
  with self.assertRaises(PermissionError):batch.BatchedEvidenceReader(client,ROSTER,set(),NOW)
 def test_held_leads_do_not_fetch_metadata_or_history(self):
  client=Mock(write_enabled=False);reader=batch.BatchedEvidenceReader(client,ROSTER,{policy.held_hash('lead_test')},NOW);reader.tasks=[]
  self.assertEqual(set(reader.read_batch(['lead_test'])),{'lead_test'});client.request.assert_not_called()
 def test_active_closers_do_not_fetch_history(self):
  client=Mock(write_enabled=False);reader=batch.BatchedEvidenceReader(client,ROSTER,set(),NOW);reader.tasks=[]
  lead={'id':'lead_test','organization_id':policy.ORG,'status_id':policy.HOT,'custom.'+policy.OWNER:'user_closer'}
  client.request.return_value={'data':[lead],'count':{'total':1},'cursor':None}
  self.assertEqual(reader.read_batch(['lead_test'])['lead_test'].owners,('user_closer',));self.assertEqual(client.request.call_count,1)
 def test_unprepared_or_oversized_batches_fail(self):
  reader=batch.BatchedEvidenceReader(Mock(write_enabled=False),ROSTER,set(),NOW)
  with self.assertRaises(RuntimeError):reader.read_batch(['lead_test'])
  reader.tasks=[]
  with self.assertRaises(ValueError):reader.read_batch(['lead_test']*51)
 def test_exact_lead_batch_requires_every_requested_identity(self):
  client=Mock();client.request.return_value={'data':[],'count':{'total':0},'cursor':None}
  with self.assertRaises(ValueError):batch.exact_leads(client,['lead_test'])
 def test_pages_fail_closed_on_duplicate_wrong_scope_or_fence(self):
  for rows in [[self.row(),self.row()],[dict(self.row(),organization_id='wrong')],[dict(self.row(),date_created=(NOW+timedelta(days=1)).isoformat())]]:
   client=Mock();client.request.return_value={'data':rows,'has_more':False}
   with self.assertRaises(ValueError):batch.fenced_rows(client,'/activity/sms/',{'_fields':'id'},NOW)
 def test_empty_continued_page_is_not_absence(self):
  client=Mock();client.request.return_value={'data':[],'has_more':True}
  with self.assertRaises(ValueError):batch.fenced_rows(client,'/task/',{'_fields':'id'},NOW)
 def test_only_inbound_message_bodies_are_hydrated(self):
  inbound=self.row(direction='inbound',activity_at=NOW.isoformat());outbound=dict(inbound,id='acti_out',direction='outbound')
  with patch.object(batch,'fenced_rows',return_value=[dict(inbound,text='STOP',remote_phone='+15551112222')]) as fetch:
   batch.hydrate_bodies(Mock(),'/activity/sms/',[inbound,outbound],NOW)
  self.assertEqual(fetch.call_count,1);self.assertEqual(inbound['text'],'STOP');self.assertNotIn('text',outbound)
 def test_missing_or_changed_hydration_never_authorizes(self):
  inbound=self.row(direction='inbound',activity_at=NOW.isoformat())
  for details in [[],[dict(inbound,direction='outbound',text='hello')]]:
   with patch.object(batch,'fenced_rows',return_value=details),self.assertRaises(ValueError):batch.hydrate_bodies(Mock(),'/activity/sms/',[inbound],NOW)
 def test_old_outbound_call_notes_are_not_needed(self):
  old=self.row(direction='outbound',activity_at=(NOW-timedelta(days=30)).isoformat())
  with patch.object(batch,'fenced_rows') as fetch:batch.hydrate_bodies(Mock(),'/activity/call/',[old],NOW)
  fetch.assert_not_called()
 def test_all_157_exclusions_are_sealed(self):
  self.assertEqual(len(policy.load_exclusions()),157);self.assertEqual(len(policy.load_held_leads()),75)
 def test_apply_requires_exact_ids_and_disallows_cached_evidence(self):
  from pathlib import Path
  import tempfile,os,io
  with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{'CLOSE_STATUS_MAINTENANCE_ENABLE_WRITES':'1'}),patch('sys.stderr',io.StringIO()):
   ids=Path(tmp)/'ids.json';ids.write_text('["lead_test"]')
   for args in [['--scan-follow-up'],['--lead-ids',str(ids),'--batched']]:
    with self.assertRaises(SystemExit):main(args+['--apply','--output',str(Path(tmp)/'out')])
 def test_apply_budget_never_writes_a_second_lead(self):
  import tempfile,os,io,json
  from pathlib import Path
  import close_status_maintenance as worker
  decision=policy.Decision('change','seven_day_inbound_expiry',policy.GENERAL)
  executor=Mock();executor.preview.return_value=decision
  executor.apply.return_value={'state':'verified','writes':[{'type':'lead'}]}
  client=Mock();client.requests=[]
  with tempfile.TemporaryDirectory() as temp,patch.dict(os.environ,{'CLOSE_STATUS_MAINTENANCE_ENABLE_WRITES':'1','CLOSE_API_KEY':'fake'}),patch.object(worker,'load_roster',return_value=ROSTER),patch.object(worker,'load_exclusions',return_value=set()),patch.object(worker,'CloseClient',return_value=client),patch.object(worker,'SnapshotReader'),patch.object(worker,'StatusExecutor',return_value=executor),patch('sys.stdout',io.StringIO()):
   ids=Path(temp)/'ids.json';ids.write_text('["lead_one","lead_two"]')
   code=worker.main(['--lead-ids',str(ids),'--apply','--max-changes','1','--output',str(Path(temp)/'out')])
   result=json.loads((Path(temp)/'out'/'results.json').read_text())
  self.assertEqual(code,1);executor.apply.assert_called_once();self.assertEqual(result[1]['execution']['reason'],'apply_lead_budget_reached')
if __name__=='__main__':unittest.main()
