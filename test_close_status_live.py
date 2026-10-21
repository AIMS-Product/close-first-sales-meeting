import copy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timedelta,timezone
from threading import Lock
import unittest
from unittest.mock import Mock,patch
import requests
from close_status_live import FreshSnapshotBroker,RequestLimiter,SharedTransport
from close_status_maintenance import StatusExecutor
from close_status_policy import *
from close_status_reader import SnapshotReader
NOW=datetime(2026,10,21,tzinfo=timezone.utc)
ROSTER=Roster(frozenset({'user_closer'}),NOW)
class FakeTransport:
 def __init__(self,write_enabled=False):
  self.write_enabled=write_enabled;self.leads={};self.activities={};self.calls=[];self.lock=Lock()
 def request(self,method,path,payload=None):
  with self.lock:
   self.calls.append((method,path,payload))
   if method=='GET':return copy.deepcopy(self.leads[path.split('/')[2]])
   if not self.write_enabled or method!='PUT' or set(payload)!={'status_id'}:raise PermissionError('status-only fake')
   kind,key=path.strip('/').split('/')
   if kind=='lead':self.leads[key]['status_id']=payload['status_id'];return copy.deepcopy(self.leads[key])
   for lead in self.leads.values():
    for opp in lead['opportunities']:
     if opp['id']==key:
      opp['status_id']=payload['status_id'];opp['date_updated']=NOW.isoformat()
      # Real pilot: native lead reaches the mapped target after this update.
      lead['status_id']=GENERAL if payload['status_id']==OPP_GENERAL else HOT
      return copy.deepcopy(opp)
   raise KeyError(key)
 def list(self,path,params):
  with self.lock:self.calls.append(('LIST',path,dict(params)))
  ids=params.get('lead_id','').split(',')
  return copy.deepcopy([r for r in self.activities.get(path,[]) if r['lead_id'] in ids])
 def add(self,lead_id,status=HOT,owner='user_setter',opportunity=False):
  self.leads[lead_id]={'id':lead_id,'organization_id':ORG,'status_id':status,'custom.'+OWNER:owner,'contacts':[{'emails':[],'phones':[{'phone':'+15551112222'}]}],'opportunities':[]}
  if opportunity:self.leads[lead_id]['opportunities']=[{'id':'oppo_'+lead_id,'lead_id':lead_id,'organization_id':ORG,'status_id':OPP_HOT,'date_updated':(NOW-timedelta(days=1)).isoformat(),'status_type':'active','status_label':'HOT - Follow Up','user_id':owner,'pipeline_id':SALES_PIPELINE}]
class LiveReadTests(unittest.TestCase):
 def setUp(self):
  self.transport=FakeTransport();self.refresh=patch.object(SnapshotReader,'refresh_unsubscribed',return_value=None);self.refresh.start();self.addCleanup(self.refresh.stop)
 def read(self,broker,lead_id,held=frozenset()):return broker.read(lead_id,NOW,held=held,roster=ROSTER)
 def test_concurrent_reads_share_activity_fetch_but_not_native_lead_reads(self):
  ids=['lead_'+str(i) for i in range(8)]
  for lead_id in ids:self.transport.add(lead_id)
  with FreshSnapshotBroker(self.transport,self.transport,collect_seconds=.05) as broker,ThreadPoolExecutor(max_workers=8) as pool:
   results=list(pool.map(lambda x:self.read(broker,x),ids))
  self.assertEqual(len(results),8)
  self.assertEqual(sum(c[0]=='GET' for c in self.transport.calls),8)
  self.assertEqual(sum(c[0]=='LIST' and c[1]=='/activity/sms/' for c in self.transport.calls),1)
 def test_later_read_loads_new_stop_and_native_status(self):
  self.transport.add('lead_test')
  with FreshSnapshotBroker(self.transport,self.transport,collect_seconds=.001) as broker:
   self.assertFalse(self.read(broker,'lead_test').suppressions)
   self.transport.leads['lead_test']['status_id']=GENERAL
   self.transport.activities['/activity/sms/']=[{'id':'acti_stop','lead_id':'lead_test','organization_id':ORG,'direction':'inbound','text':'STOP','remote_phone':'+15551112222','activity_at':NOW.isoformat()}]
   later=self.read(broker,'lead_test')
  self.assertEqual(later.status,GENERAL);self.assertIn('historical_stop',later.suppressions)
 def test_held_ids_and_active_closers_remain_excluded(self):
  self.transport.add('lead_closer',owner='user_closer')
  held={held_hash('lead_held')}
  with FreshSnapshotBroker(self.transport,self.transport,collect_seconds=.001) as broker:
   a=self.read(broker,'lead_held',held);b=self.read(broker,'lead_closer',held)
  self.assertEqual(decide(a,ROSTER,held,NOW).kind,'hold');self.assertEqual(decide(b,ROSTER,held,NOW).reason,'active_closer_owner')
  self.assertEqual(len(self.transport.calls),1)
 def test_scope_failure_never_returns_usable_evidence(self):
  self.transport.add('lead_test');self.transport.activities['/activity/call/']=[{'id':'acti_bad','lead_id':'lead_other','organization_id':ORG}]
  # An endpoint ignoring the lead filter must fail the complete wave.
  self.transport.list=lambda path,params:[{'id':'acti_bad','lead_id':'lead_other','organization_id':ORG}] if path=='/activity/call/' else []
  with FreshSnapshotBroker(self.transport,self.transport,collect_seconds=.001) as broker,self.assertRaises(ValueError):self.read(broker,'lead_test')
 def test_existing_executor_uses_fresh_waves_and_handles_native_side_effect(self):
  self.transport.write_enabled=True
  for i in range(4):self.transport.add('lead_'+str(i),opportunity=i<2)
  readonly=Mock(wraps=self.transport);readonly.write_enabled=False
  receipts=[]
  with FreshSnapshotBroker(readonly,self.transport,collect_seconds=.03) as broker:
   def apply(lead_id):
    executor=StatusExecutor(broker,ROSTER,set(),clock=lambda:NOW,journal=receipts.append)
    return executor.apply(lead_id,executor.preview(lead_id))
   with ThreadPoolExecutor(max_workers=4) as pool:results=list(pool.map(apply,list(self.transport.leads)))
  self.assertTrue(all(r['state']=='verified' for r in results))
  self.assertEqual(sum(c[0]=='PUT' for c in self.transport.calls),4)
  self.assertTrue(all(l['status_id']==GENERAL for l in self.transport.leads.values()))
  self.assertEqual(sum(r['state']=='intent' for r in receipts),4)
 def test_durable_before_snapshot_must_succeed_before_any_put(self):
  self.transport.add('lead_test')
  capture=Mock(side_effect=OSError('disk unavailable'))
  with FreshSnapshotBroker(self.transport,self.transport,collect_seconds=.001,capture_first=capture) as broker,self.assertRaises(OSError):self.read(broker,'lead_test')
  self.assertFalse(any(call[0]=='PUT' for call in self.transport.calls));capture.assert_called_once()
 def test_open_callback_tasks_are_read_per_lead_and_refreshed(self):
  self.transport.add('lead_a');self.transport.add('lead_b')
  self.transport.activities['/task/']=[{'id':'task_a','lead_id':'lead_a','organization_id':ORG,'is_complete':False,'text':'call me','date_created':NOW.isoformat()}]
  with FreshSnapshotBroker(self.transport,self.transport,collect_seconds=.02) as broker,ThreadPoolExecutor(max_workers=2) as pool:
   evidence=list(pool.map(lambda x:self.read(broker,x),['lead_a','lead_b']))
   self.assertIn('open_callback_task',evidence[0].review_reasons);self.assertFalse(evidence[1].review_reasons)
   self.transport.activities['/task/']=[{'id':'task_b','lead_id':'lead_b','organization_id':ORG,'is_complete':False,'text':'call back','date_created':NOW.isoformat()}]
   self.assertIn('open_callback_task',self.read(broker,'lead_b').review_reasons)
  calls=[call for call in self.transport.calls if call[0]=='LIST' and call[1]=='/task/']
  self.assertTrue(all(call[2].get('lead_id') in {'lead_a','lead_b'} for call in calls))
 def test_ignored_task_filter_or_invalid_completion_fails_closed(self):
  self.transport.add('lead_a')
  for task in [{'id':'task_bad','lead_id':'lead_other','organization_id':ORG,'is_complete':False},
               {'id':'task_bad','lead_id':'lead_a','organization_id':ORG,'is_complete':None}]:
   self.transport.list=lambda path,params:[task] if path=='/task/' else []
   with FreshSnapshotBroker(self.transport,self.transport,collect_seconds=.001) as broker,self.assertRaises(ValueError):self.read(broker,'lead_a')
 def test_write_capable_snapshot_transport_rejected(self):
  self.transport.write_enabled=True
  with self.assertRaises(PermissionError):FreshSnapshotBroker(self.transport,self.transport)
class BrokerQueueTests(unittest.TestCase):
 def test_deferred_same_kind_requests_coalesce_before_reading_queue(self):
  from collections import deque
  from concurrent.futures import Future
  from queue import Queue
  broker=FreshSnapshotBroker.__new__(FreshSnapshotBroker)
  reads=[('read',(name,NOW,set(),ROSTER),Future()) for name in ['lead_a','lead_b','lead_c']]
  refresh=('refresh',None,Future())
  broker.pending=deque([reads[0],refresh,reads[1],reads[2]])
  broker.queue=Queue();broker.queue.put(None);broker.group_size=50;broker.collect_seconds=.001
  broker._capture=Mock(side_effect=lambda rows:[row[0] for row in rows]);broker.read_client=Mock()
  with patch.object(SnapshotReader,'refresh_unsubscribed',return_value=None) as refreshed:broker._serve()
  self.assertEqual(broker._capture.call_count,1);self.assertEqual(len(broker._capture.call_args.args[0]),3)
  self.assertEqual([read[2].result() for read in reads],['lead_a','lead_b','lead_c']);refreshed.assert_called_once()

class LimiterTests(unittest.TestCase):
 def test_rate_is_bounded_and_defaults_to_five(self):
  import os
  with patch.dict(os.environ,{},clear=True):self.assertEqual(RequestLimiter().interval,.2)
  with patch.dict(os.environ,{'CLOSE_STATUS_REQUESTS_PER_SECOND':'10'}):self.assertEqual(RequestLimiter().interval,.1)
  for rate in [0,11,float('nan'),float('inf')]:
   with self.assertRaises(ValueError):RequestLimiter(rate)

class TransportFenceTests(unittest.TestCase):
 def test_unknown_put_outcome_closes_shared_write_fence(self):
  fake=Mock();fake.requests=[];fake.request.side_effect=requests.ConnectionError('unknown')
  with patch('close_status_live.CloseClient',return_value=fake):
   transport=SharedTransport('fake',RequestLimiter(),write_enabled=True)
   with self.assertRaises(requests.ConnectionError):transport.request('PUT','/lead/lead_test/',{'status_id':GENERAL})
   with self.assertRaises(PermissionError):transport.request('PUT','/lead/lead_other/',{'status_id':GENERAL})
  self.assertEqual(fake.request.call_count,1)
class FieldAuditTests(unittest.TestCase):
 def test_status_display_changes_are_derived_but_custom_changes_are_not(self):
  from close_status_verify import business_differences
  before={'status_id':HOT,'custom.owner':'same','contacts':[],'opportunities':[{'id':'oppo_one','status_id':OPP_HOT,'status_display_name':'HOT'}]}
  after=copy.deepcopy(before);after['status_id']=GENERAL;after['opportunities'][0].update(status_id=OPP_GENERAL,status_display_name='General')
  self.assertEqual(business_differences(before,after,'oppo_one'),[])
  after['custom.owner']='changed'
  self.assertIn('custom.owner',business_differences(before,after,'oppo_one'))
 def test_unrelated_opportunity_status_changes_are_detected(self):
  from close_status_verify import business_differences
  before={'opportunities':[{'id':'oppo_unrelated','status_id':OPP_HOT}]}
  after={'opportunities':[{'id':'oppo_unrelated','status_id':OPP_GENERAL}]}
  self.assertIn('opportunity.status_id',business_differences(before,after,'oppo_target'))
 def test_batch_budget_and_write_switch_are_checked_before_any_reads(self):
  from close_status_apply_batch import run_batch
  import os
  with self.assertRaises(ValueError):run_batch(['lead_test']*101,'never-created')
  with patch.dict(os.environ,{},clear=True),self.assertRaises(PermissionError):run_batch(['lead_test'],'never-created',apply=True)
if __name__=='__main__':unittest.main()
