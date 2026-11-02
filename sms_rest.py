"""SMS-rest command line: read-only preview by default, bounded approved apply."""
import argparse
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path

from close_status_reader import CloseClient,load_roster
from close_status_policy import timestamp
from sms_rest_close import Reader,Writer,event_pages
from sms_rest_policy import Calendar
from sms_rest_workflow import Workflow,digest
from workflow_holds import Store,open_store


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode',choices=['preview','observe','due','apply','reconcile'])
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--state',type=Path)
    parser.add_argument('--tasks',type=Path,help='Explicit task/lead sample JSON; no full task scan')
    parser.add_argument('--proposal',type=Path)
    parser.add_argument('--approval',type=Path)
    parser.add_argument('--lead-id')
    parser.add_argument('--launch-at',type=timestamp)
    parser.add_argument('--roster-file')
    parser.add_argument('--holiday',action='append',default=[])
    parser.add_argument('--limit',type=int,default=5)
    args=parser.parse_args()
    if not 1<=args.limit<=10: parser.error('Pilot limit must be 1–10')
    if args.mode=='apply' and (os.environ.get('SMS_REST_ENABLE_WRITES')!='1' or not args.approval or not args.proposal):
        parser.error('Apply requires explicit write gate, exact proposal and approval')
    if args.mode=='observe' and os.environ.get('SMS_REST_ENABLE_OBSERVATION')!='1':
        parser.error('Durable observation disabled; set the separate observation gate after approval')
    if args.mode in ('apply','observe') and (os.environ.get('WORKFLOW_HOLD_BACKEND')!='postgres'
            or os.environ.get('WORKFLOW_HOLDS_REQUIRED')!='1' or args.state):
        parser.error('Live workflow state requires the mandatory shared PostgreSQL backend; local state is preview-only')
    args.output.mkdir(parents=True,exist_ok=False)
    now=datetime.now(timezone.utc)
    client=CloseClient(os.environ['CLOSE_API_KEY'])
    roster=load_roster(now,args.roster_file)
    # Local preview state only. Production store installation is independently gated.
    store=open_store(local_path=args.state or args.output/'preview.sqlite')
    sequences=client.list('/sequence/',{})
    reader=Reader(client,roster,store,sequences=sequences)
    reader.refresh_suppression()
    writer=Writer(client,enabled=args.mode=='apply')
    workflow=Workflow(reader,writer,store,roster,calendar=Calendar(holidays=frozenset(args.holiday)),launch_at=args.launch_at)
    try:
        if args.mode=='preview':
            if not args.tasks: parser.error('Preview requires an explicit bounded --tasks sample')
            tasks=json.loads(args.tasks.read_text())
            if len(tasks)>args.limit: parser.error('Sample exceeds pilot limit')
            result=[workflow.propose(row['lead_id'],task_id=row['task_id']) for row in tasks]
        elif args.mode=='due':
            result=[workflow.propose(c['lead_id']) for c in store.due(now.isoformat(),args.limit)]
        elif args.mode=='observe':
            row=store.connection.execute("SELECT value FROM workflow_meta WHERE key='event_through'").fetchone()
            if not row and not args.launch_at: parser.error('First observation requires explicit launch cutoff')
            since=timestamp(row['value'])-timedelta(minutes=5) if row else args.launch_at
            events=list(event_pages(client,since,now))
            workflow.observe(events,now)
            result={'events_read':len(events),'through':now.isoformat()}
        elif args.mode=='apply':
            if os.environ.get('WORKFLOW_HOLD_BACKEND')!='postgres':
                parser.error('Production apply requires shared PostgreSQL adapter')
            result=workflow.apply(json.loads(args.proposal.read_text()),authorization=json.loads(args.approval.read_text()))
        else:
            if not args.lead_id: parser.error('Reconciliation requires --lead-id')
            result=workflow.reconcile(args.lead_id)
        (args.output/'result.json').write_text(json.dumps(result,indent=2,default=str)+'\n')
        (args.output/'receipt.json').write_text(json.dumps({'at':now.isoformat(),'mode':args.mode,
            'close_puts':len(writer.requests),'close_requests':client.requests,
            'result_sha256':digest(result),'production_activated':False},indent=2)+'\n')
        print(json.dumps({'mode':args.mode,'close_puts':len(writer.requests),'output':str(args.output)}))
    finally:
        store.close()


if __name__=='__main__': main()
