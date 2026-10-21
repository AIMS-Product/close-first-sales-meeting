import copy
from contextlib import redirect_stdout
from datetime import datetime,timezone
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch
import close_status_cycle as cycle
from close_status_policy import HOT,ORG

class WorkerTests(unittest.TestCase):
 def setUp(self):
  self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name).resolve()
  self.env=patch.dict(os.environ,{'CLOSE_API_KEY':'fake','CLOSE_STATUS_DATA_DIR':str(self.root)},clear=True);self.env.start();self.addCleanup(self.env.stop)
  self.stdout=redirect_stdout(io.StringIO());self.stdout.__enter__();self.addCleanup(self.stdout.__exit__,None,None,None)
 def preview(self,rows,code=0):
  def run(argv):
   directory=Path(argv[-1]);directory.mkdir();(directory/'results.json').write_text(json.dumps(rows));return code
  return run
 def row(self,index):return {'lead_id':'lead_'+str(index),'kind':'change','reason':'expired_hot'}
 def test_disabled_worker_never_loads_close_or_roster(self):
  with patch.object(cycle,'run_cycle') as run,patch.object(cycle,'startup_check') as check:
   self.assertEqual(cycle.main(),0);run.assert_not_called();check.assert_not_called()
 def test_enabled_worker_requires_write_gate_before_cycle(self):
  os.environ['CLOSE_STATUS_MAINTENANCE_ENABLED']='1'
  with patch.object(cycle,'run_cycle') as run,self.assertRaises(PermissionError):cycle.main()
  run.assert_not_called()
 def test_startup_only_does_not_run_cycle_even_with_write_env(self):
  os.environ.update(CLOSE_STATUS_STARTUP_CHECK_ONLY='1',CLOSE_STATUS_MAINTENANCE_ENABLED='1',CLOSE_STATUS_MAINTENANCE_ENABLE_WRITES='1')
  with patch.object(cycle,'startup_check') as check,patch.object(cycle,'run_cycle') as run:
   cycle.main();check.assert_called_once_with(self.root);run.assert_not_called()
 def test_overlap_lock_returns_before_any_read_or_write(self):
  with patch.object(cycle.fcntl,'flock',side_effect=BlockingIOError),patch.object(cycle,'run_cycle') as run:
   self.assertEqual(cycle.main(),0);run.assert_not_called()
 def test_startup_check_uses_only_native_get_and_validates_tenant(self):
  os.environ['CLOSE_STATUS_PROOF_LEAD_ID']='lead_proof'
  client=Mock();client.request.return_value={'id':'lead_proof','organization_id':ORG}
  roster=SimpleNamespace(closer_ids={'user_one'},source_receipt={'read_only':'on'})
  with patch.object(cycle,'load_roster',return_value=roster),patch.object(cycle,'load_exclusions',return_value=set()),patch.object(cycle,'CloseClient',return_value=client):
   receipt=cycle.startup_check(self.root)
   self.assertEqual(receipt['close_writes'],0);client.request.assert_called_once_with('GET','/lead/lead_proof/?_fields=id,organization_id')
   client.request.return_value['organization_id']='wrong'
   with self.assertRaises(ValueError):cycle.startup_check(self.root)
 def marker(self,with_intent=False,nested=False):
  run=self.root/'runs'/'interrupted';run.mkdir(parents=True)
  if with_intent:
   directory=run/'batch-0001'
   if nested:directory=directory/'hot-followup'
   directory.mkdir(parents=True);(directory/'write-journal.ndjson').write_text(json.dumps({'state':'intent'})+'\n')
  cycle.save(self.root/'active-run.json',{'run_directory':str(run)})
 def test_interrupted_writes_block_even_nested_followup(self):
  self.marker(True,True)
  with patch.object(cycle,'preview_main') as preview,self.assertRaises(RuntimeError):cycle.run_cycle(self.root)
  preview.assert_not_called();self.assertTrue((self.root/'active-run.json').exists())
 def test_interrupted_read_only_run_can_restart(self):
  self.marker();self.assertFalse(cycle.previous_run_requires_reconciliation(self.root));self.assertFalse((self.root/'active-run.json').exists())
 def test_interrupted_marker_cannot_escape_data_root(self):
  cycle.save(self.root/'active-run.json',{'run_directory':'/private/tmp/not-this-worker'})
  with self.assertRaises(ValueError):cycle.previous_run_requires_reconciliation(self.root)
 def test_invalid_budget_never_reads(self):
  with patch.object(cycle,'preview_main') as preview,self.assertRaises(ValueError):cycle.run_cycle(self.root,max_changes=1001)
  preview.assert_not_called()
 def test_failed_preview_never_writes(self):
  with patch.object(cycle,'preview_main',side_effect=self.preview([],1)),patch.object(cycle,'apply_and_verify') as apply,self.assertRaises(RuntimeError):cycle.run_cycle(self.root)
  apply.assert_not_called();self.assertFalse((self.root/'active-run.json').exists())
 def test_success_is_bounded_to_budget_and_batches_100(self):
  rows=[self.row(i) for i in range(215)]+[{'lead_id':'lead_held','kind':'hold'}]
  with patch.object(cycle,'preview_main',side_effect=self.preview(rows)),patch.object(cycle,'apply_and_verify',return_value={'verification':{'complete':True}}) as apply:
   result=cycle.run_cycle(self.root,max_changes=200)
  self.assertEqual([len(call.args[0]) for call in apply.call_args_list],[100,100]);self.assertEqual(result['remaining_for_next_run'],15)
  self.assertFalse((self.root/'active-run.json').exists());self.assertTrue((self.root/'last-success.json').exists())
 def test_first_failed_batch_leaves_marker_and_stops_next_batch(self):
  with patch.object(cycle,'preview_main',side_effect=self.preview([self.row(i) for i in range(200)])),patch.object(cycle,'apply_and_verify',side_effect=RuntimeError('unknown')) as apply,self.assertRaises(RuntimeError):cycle.run_cycle(self.root)
  self.assertEqual(apply.call_count,1);self.assertTrue((self.root/'active-run.json').exists());self.assertFalse((self.root/'last-success.json').exists())

class FollowupTests(unittest.TestCase):
 def setUp(self):
  self.verification={'summary':{'written_leads':1,'native_targets_verified':1,'unexpected_business_change_records':0,'all_receipts_match':True,'policy_replay_holds':0},'replay':[{'lead_id':'lead_one','kind':'change','reason':'recent_inbound','lead_status':HOT}]}
  self.rows=[{'lead_id':'lead_one','execution':{'reason':'legacy_normalized_pending_hot_refresh'}}]
 def test_only_verified_legacy_normalization_allows_followup(self):
  self.assertTrue(cycle.verification_allows_hot_followup(self.verification,self.rows))
  self.rows[0]['execution']['reason']='different'
  self.assertFalse(cycle.verification_allows_hot_followup(self.verification,self.rows))
 def test_business_changes_unknown_receipts_wrong_targets_or_hold_prevent_followup(self):
  for field,value in [('unexpected_business_change_records',1),('all_receipts_match',False),('native_targets_verified',0),('policy_replay_holds',1)]:
   current=copy.deepcopy(self.verification);current['summary'][field]=value
   self.assertFalse(cycle.verification_allows_hot_followup(current,self.rows),field)
 def test_non_hot_or_non_recent_pending_decision_cannot_followup(self):
  for field,value in [('lead_status','different'),('reason','different'),('kind','hold')]:
   current=copy.deepcopy(self.verification);current['replay'][0][field]=value
   self.assertFalse(cycle.verification_allows_hot_followup(current,self.rows),field)
 def test_failed_followup_cannot_be_recorded_as_complete(self):
  with TemporaryDirectory() as temp,patch.dict(os.environ,{'CLOSE_API_KEY':'fake'}):
   directory=Path(temp)/'batch';directory.mkdir();(directory/'independent-verification.json').write_text(json.dumps(self.verification));(directory/'results.json').write_text(json.dumps(self.rows))
   execution={'errors':0,'partial':0}
   with patch.object(cycle,'run_batch',return_value=execution) as run,patch.object(cycle,'verify_batch',return_value={'complete':False}),self.assertRaises(RuntimeError):cycle.apply_and_verify(['lead_one'],{},directory)
   self.assertEqual(run.call_count,2);self.assertFalse((directory/'reconciliation.json').exists())
 def test_successful_followup_retains_initial_receipts(self):
  with TemporaryDirectory() as temp,patch.dict(os.environ,{'CLOSE_API_KEY':'fake'}):
   directory=Path(temp)/'batch';directory.mkdir();(directory/'independent-verification.json').write_text(json.dumps(self.verification));(directory/'results.json').write_text(json.dumps(self.rows))
   with patch.object(cycle,'run_batch',return_value={'errors':0,'partial':0}),patch.object(cycle,'verify_batch',side_effect=[{'complete':False},{'complete':True}]):result=cycle.apply_and_verify(['lead_one'],{},directory)
   self.assertTrue(result['hot_followup']['verification']['complete']);self.assertFalse(result['verification']['complete']);self.assertTrue(json.loads((directory/'reconciliation.json').read_text())['complete'])

if __name__=='__main__':unittest.main()
