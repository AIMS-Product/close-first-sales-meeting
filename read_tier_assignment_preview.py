"""Bounded read-only Close/Lakebase capture for the tier assignment preview.

Only GET and read-only /data/search/ POST are available. No export, mutation,
provider enrollment, ledger operation, migration, or activation exists here.
"""
import argparse
from dataclasses import asdict,replace
from datetime import datetime,timezone
import importlib.util
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo
from collections import Counter

import psycopg
import requests
from psycopg.rows import dict_row
from close_status_reader import CloseClient,SnapshotReader,load_roster
from close_status_policy import ORG,COMPANY,OWNER,load_exclusions,held_hash,timestamp
from lane2_roster import LANE2_ROSTER
from rep_access import prepare_review,apply_approved_classifications
from tier_assignment import (TIER,BUSINESS,LeadFacts,allocate_sample,field,regular,
    view_candidates,lead_owners,rep_available,classify_for_assignment,VP)


def load_environment(paths):
    for path in paths:
        for line in path.read_text().splitlines():
            line=line.strip()
            if not line or line.startswith('#') or '=' not in line:continue
            key,value=line.split('=',1)
            os.environ.setdefault(key.strip(),value.strip().strip('\"').strip("'"))


def capture_roster(lead_ids=None):
    with psycopg.connect(host=os.environ['LAKEBASE_HOST'],dbname=os.environ['LAKEBASE_DATABASE'],
        user=os.environ['LAKEBASE_USER'],password=os.environ['LAKEBASE_PASSWORD'],sslmode='verify-full',
        sslrootcert=os.environ.get('LAKEBASE_SSLROOTCERT') or requests.certs.where(),connect_timeout=20,
        row_factory=dict_row,options='-c default_transaction_read_only=on -c statement_timeout=30000') as connection:
        with connection.transaction():
            connection.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            connection.execute("select set_config('app.company_id',%s,true)",(COMPANY,))
            proof=connection.execute("select current_user as reader_role,current_database() as database_name,current_setting('transaction_read_only') as read_only,transaction_timestamp() as captured_at,txid_current_snapshot() as snapshot").fetchone()
            if proof['read_only']!='on':raise ValueError('Read-only snapshot required')
            rows=connection.execute('''select u.id::text as company_user_id,u.display_name,u.status,u.sales_team_lane,
                u.metadata_json->'cohortTracker' as cohort_settings,u.metadata_json->>'lane2StartDate' as legacy_start_date,
                u.metadata_json->>'offer' as legacy_offer,u.metadata_json->'lane2ConfigurationDraft' as configuration,
                (select coalesce(jsonb_object_agg(k,v),'{}'::jsonb) from jsonb_each(u.metadata_json) t(k,v)
                 where k ~* 'tier|access|assignment') as possible_access_metadata,
                (select coalesce(json_agg(json_build_object('provider_user_id',a.provider_user_id,'status',a.status)),'[]'::json)
                 from app.company_user_provider_accounts a where a.company_id=u.company_id and a.company_user_id=u.id
                   and a.source_domain='crm' and a.provider='close') as close_accounts
                from app.company_users u where u.company_id=%s and
                (u.sales_team_lane=2 or u.metadata_json->>'lane2Roster'='true' or u.metadata_json ? 'lane2StartDate'
                 or u.metadata_json ? 'lane2ManualIdentity') order by u.status,u.display_name''',(COMPANY,)).fetchall()
            table=connection.execute("select to_regclass('crm_workflow_control.sales_hold')::text as table_name").fetchone()['table_name']
            holds=None
            if table:
                tenant=connection.execute("select value from crm_workflow_control.workflow_meta where key='tenant'").fetchone()
                if not tenant or tenant['value']!=COMPANY+'/'+ORG:raise ValueError('Shared hold tenant mismatch')
                query='select lead_id,workflow,token,expires_at from crm_workflow_control.sales_hold'
                holds=[dict(row) for row in (connection.execute(query+' where lead_id = ANY(%s)',(list(lead_ids),))
                    if lead_ids is not None else connection.execute(query)).fetchall()]
    return {'company_id':COMPANY,'proof':proof,'rows':rows,'production_writes':0,'close_requests':0},holds


class CaptureClient:
    def __init__(self,client):self.raw=client;self.activities={};self.leads={}
    def request(self,method,path,payload=None):
        result=self.raw.request(method,path,payload)
        if method=='GET' and path.startswith('/lead/') and '?' not in path:self.leads[result['id']]=result
        return result
    def list(self,path,params):
        result=self.raw.list(path,params)
        if path.startswith('/activity/') and params.get('lead_id'):
            self.activities[(params['lead_id'],path)]=result
        return result


def search(client,query,limit=1,direction="asc"):
    return client.request('POST','/data/search/',{'query':query,'_limit':limit,'include_counts':True,
        '_fields':{'lead':['id','date_created']},'sort':[{'direction':direction,'field':{'type':'regular_field','object_type':'lead','field_name':'date_created'}}]})


def sample_queries():
    common=[{'type':'object_type','object_type':'lead'},
        {'type':'or','queries':[field(BUSINESS,{'type':'term','values':[VP]}),field(BUSINESS,{'type':'exists'},True)]}]
    for tier in ['Top','Proven','Ramp','Reactivation']:
        yield tier,{'type':'and','queries':common+[field(TIER,{'type':'term','values':[tier]}),field(OWNER,{'type':'exists'},True)]}
    yield 'Owned age-band example',{'type':'and','queries':common+[field(TIER,{'type':'term','values':['Proven','Reactivation']}),field(OWNER,{'type':'exists'})]}


def reconcile_identities(source):
    proposals=[]
    for old in LANE2_ROSTER:
        if old['user_id']:continue
        matches=[row for row in source['rows'] if ' '.join(row['display_name'].split()).casefold()==' '.join(old['rep'].split()).casefold()]
        if len(matches)!=1:continue
        accounts=[a['provider_user_id'] for a in matches[0]['close_accounts'] if a['status']=='active']
        if len(accounts)==1:
            proposals.append({'rep':old['rep'],'before':'','after':accounts[0],
                              'ready_for_leads_preserved':old['ready_for_leads']})
    return proposals


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file',type=Path,action='append',default=[])
    parser.add_argument('--approval',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--per-tier',type=int,default=2)
    parser.add_argument('--tagger',type=Path,required=True,help='Existing age updater, reused for read-only freshness checks')
    args=parser.parse_args()
    if not 1<=args.per_tier<=3:parser.error('Bounded preview uses 1–3 leads per sample category')
    args.output.mkdir(parents=True,exist_ok=False)
    load_environment(args.env_file)
    source,holds=capture_roster()
    approval=json.loads(args.approval.read_text())
    reps,excluded=apply_approved_classifications(source,prepare_review(source,LANE2_ROSTER),approval)
    source_out=args.output/'roster-source.json';source_out.write_text(json.dumps(source,indent=2,default=str)+'\n')
    identities=reconcile_identities(source)
    raw=CloseClient(os.environ['CLOSE_API_KEY'])
    captured=CaptureClient(raw)
    org=raw.request('GET',f'/organization/{ORG}/')
    if org.get('id')!=ORG:raise ValueError('Close organization mismatch')
    memberships={m.get('user_id') or (m.get('user') or {}).get('id') for m in org['memberships']}
    if not memberships:raise ValueError('Empty organization membership')
    closers=load_roster(datetime.now(timezone.utc))
    now=datetime.now(timezone.utc)
    spec=importlib.util.spec_from_file_location("existing_lead_tier_tagger",args.tagger)
    tagger=importlib.util.module_from_spec(spec);spec.loader.exec_module(tagger)
    history=SnapshotReader(captured);history.refresh_unsubscribed()
    exclusions=load_exclusions()
    queries=[];sample={}
    for category,query in sample_queries():
        result=search(raw,query,1)
        sample_rows=list(result['data'])
        if args.per_tier>1: sample_rows.extend(search(raw,query,args.per_tier-1,direction='desc')['data'])
        queries.append({'category':category,'query':query,'total':result['count']['total'],'sample_returned':len(sample_rows)})
        for row in sample_rows:sample.setdefault(row['id'],category)
    items=[];details=[]
    for index,(lead_id,category) in enumerate(sample.items(),1):
        evidence=history.read(lead_id,now,held=exclusions,roster=closers)
        lead=captured.leads.get(lead_id) or captured.request('GET',f'/lead/{lead_id}/')
        calls=captured.activities.get((lead_id,'/activity/call/'))
        dials=tuple(timestamp(c['activity_at']) for c in calls or [] if c['direction']=='outbound')
        lead_holds=tuple(h for h in holds if h['lead_id']==lead_id) if holds is not None else None
        tag_check=tagger.classify_leads([lead],now.astimezone(ZoneInfo('America/Los_Angeles')).date())[0]
        tier_value=lead.get('custom.'+TIER)
        if isinstance(tier_value,list): tier_value=tier_value[0] if len(tier_value)==1 else None
        fresh=tag_check['proposed_tier']==tier_value
        facts=LeadFacts(lead,evidence,dials,lead_holds,held_hash(lead_id) in exclusions,calls is not None,fresh)
        items.append(facts)
        details.append({'lead_id':lead_id,'category':category,'lead':lead,'evidence':asdict(evidence),
                        'outgoing_dials':[at.isoformat() for at in dials],
                        'holds':lead_holds,'explicitly_excluded':facts.explicitly_excluded,'calls_complete':facts.calls_complete,
                        'tag_check':tag_check,'tier_freshness_verified':fresh})
        print(f'Read-only sample {index}/{len(sample)} complete',flush=True)
    counts={}
    for rep in reps:
        if not rep_available(rep,now,memberships):continue
        query={'type':'and','queries':[{'type':'object_type','object_type':'lead'},
            field(OWNER,{'type':'reference','reference_type':'user_or_group','object_ids':[rep['close_user_id']]})]}
        counts[rep['close_user_id']]=search(raw,query)['count']['total']
    proposals=allocate_sample(items,reps,closers,now,memberships,counts)
    # Show the tier-policy result separately when a shared-store proof is unavailable.
    conditional=[]
    if holds is None:
        conditional=allocate_sample([replace(item,holds=()) for item in items],reps,closers,now,memberships,counts)
    views=view_candidates({rep['offer'] for rep in reps if rep['offer']},closers.closer_ids)
    for view in views:
        try:
            result=search(raw,view['query'])
            view['api_validation']={'valid':True,'current_api_user_count':result['count']['total']}
        except requests.HTTPError as error:
            view['api_validation']={'valid':False,'status':error.response.status_code,'detail':error.response.text[:1200]}
    metadata=[{'company_user_id':rep['company_user_id'],'rep':rep['rep'],
        'metadata_key':'leadAssignmentAccess','before':rep['current_access'],'after':rep['proposed_access']}
        for rep in reps if rep['proposed_access'].get('state')=='approved' and rep['current_access']!=rep['proposed_access']]
    output={'captured_at':now.isoformat(),'scope':'bounded VP lead sample; company-wide approved rep roster',
        'production_writes':0,'activation':'off','reps':reps,'excluded_reps':excluded,
        'identity_reconciliation':identities,'sample_queries':queries,'queue_counts':counts,
        'lead_proposals':proposals,'conditional_policy_examples':conditional,
        'durable_holds_available':holds is not None,'view_candidates':views,'metadata_changes':metadata,
        'active_close_membership_ids':sorted(uid for uid in memberships if uid),
        'active_closer_ids':sorted(closers.closer_ids),'closer_roster_captured_at':closers.captured_at.isoformat(),
        'lead_tag_freshness':{'checked':len(details),'stale':sum(not row['tier_freshness_verified'] for row in details),
            'existing_updater':str(args.tagger.resolve())},
        'deployment_gates':['Rep permission metadata not saved','Six reconciled identities are local only; deployment is pending',
            'Production assigners still require the gated tier-routing integration',
            'Certification opens assignment under the approved tier; uncertified reps remain Before Roadmap',
            'Verify shared hold registry availability; missing state grants no assignment permission',
            'Shared hold, STOP/DNC, outbound dispatch and all writer integrations must be verified',
            '24-hour overflow is modeled for unowned leads; current owners are retained',
            'This is a bounded sample, not a full-cohort assignment plan']}
    (args.output/'preview.json').write_text(json.dumps(output,indent=2,default=str)+'\n')
    (args.output/'source-leads.json').write_text(json.dumps(details,indent=2,default=str)+'\n')
    (args.output/'request-receipt.json').write_text(json.dumps({'requests':raw.requests,'close_mutations':0,
        'database_data_writes':0,'read_only_roster_proof':source['proof']},indent=2,default=str)+'\n')
    print(json.dumps({'sample_leads':len(items),'approved_reps':len(reps),'durable_holds_available':holds is not None,
        'proposed_assignments':sum(bool(p['target_owner']) for p in proposals),'conditional_assignments':sum(bool(p['target_owner']) for p in conditional),
        'view_validation':[v['api_validation'] for v in views],'requests':len(raw.requests),'production_writes':0,'output':str(args.output)},indent=2))


if __name__=='__main__':main()
