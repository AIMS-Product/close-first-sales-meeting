"""Serialized scheduled maintenance with a durable unresolved-write interlock."""
import fcntl
import json
import os
from pathlib import Path
from datetime import datetime,timezone
from uuid import uuid4

from close_status_apply_batch import run_batch
from close_status_maintenance import main as preview_main
from close_status_policy import HOT, ORG, load_exclusions
from close_status_reader import CloseClient, load_roster
from close_status_verify import verify_batch


def save(path, value):
    temporary=path.with_suffix('.tmp')
    with temporary.open('w') as stream:
        json.dump(value,stream,indent=2,default=str);stream.flush();os.fsync(stream.fileno())
    temporary.replace(path)


def startup_check(root):
    roster=load_roster(datetime.now(timezone.utc));held=load_exclusions()
    client=CloseClient(os.environ['CLOSE_API_KEY'])
    proof_id=os.environ['CLOSE_STATUS_PROOF_LEAD_ID']
    lead=client.request('GET',f'/lead/{proof_id}/?_fields=id,organization_id')
    if lead['id']!=proof_id or lead['organization_id']!=ORG:
        raise ValueError('Close tenant proof mismatch')
    receipt={'mode':'startup_check','at':datetime.now(timezone.utc).isoformat(),
             'organization_id':ORG,'active_closers':len(roster.closer_ids),
             'explicit_exclusions':len(held),'roster_source':roster.source_receipt,'close_writes':0}
    save(root/('startup-'+uuid4().hex+'.json'),receipt)
    print(json.dumps(receipt),flush=True)
    return receipt


def previous_run_requires_reconciliation(root):
    marker=root/'active-run.json'
    if not marker.exists():
        return False
    previous=json.loads(marker.read_text());run=Path(previous['run_directory']).resolve()
    if not run.is_relative_to(root/'runs'):
        raise ValueError('Invalid interrupted-run path')
    journals=list(run.glob('batch-*/**/write-journal.ndjson'))
    has_writes=any(any(json.loads(line)['state']=='intent' for line in p.read_text().splitlines()) for p in journals)
    if has_writes:
        return True
    marker.unlink()
    return False


def verification_allows_hot_followup(verification, results):
    summary=verification['summary']
    if (summary['native_targets_verified']!=summary['written_leads']
            or summary['unexpected_business_change_records']
            or not summary['all_receipts_match'] or summary['policy_replay_holds']):
        return False
    pending=[r for r in verification['replay'] if r['kind']!='keep']
    by_id={r['lead_id']:r for r in results}
    return bool(pending) and all(
        row['kind']=='change' and row['reason']=='recent_inbound' and row.get('lead_status')==HOT
        and by_id[row['lead_id']].get('execution',{}).get('reason')=='legacy_normalized_pending_hot_refresh'
        for row in pending)


def apply_and_verify(ids, expected, directory, *, apply=True):
    directory=Path(directory)
    summary=run_batch(ids,directory,apply=apply,expected=expected)
    verification=verify_batch(directory,os.environ['CLOSE_API_KEY']) if apply else None
    if summary['errors'] or summary['partial']:
        raise RuntimeError('Batch stopped; reconcile durable receipts before continuing')
    result={'execution':summary,'verification':verification}
    if verification and not verification['complete']:
        evidence=json.loads((directory/'independent-verification.json').read_text())
        rows=json.loads((directory/'results.json').read_text())
        if not verification_allows_hot_followup(evidence,rows):
            raise RuntimeError('Independent verification failed; no next batch authorized by this run')
        pending={row['lead_id']:row for row in evidence['replay'] if row['kind']=='change'}
        followup=directory/'hot-followup'
        followup_execution=run_batch(list(pending),followup,apply=True,expected=pending)
        followup_verification=verify_batch(followup,os.environ['CLOSE_API_KEY'])
        if followup_execution['errors'] or followup_execution['partial'] or not followup_verification['complete']:
            raise RuntimeError('HOT follow-up verification failed; stop and reconcile')
        result['hot_followup']={'execution':followup_execution,'verification':followup_verification}
        save(directory/'reconciliation.json',{'complete':True,**result})
    return result


def run_cycle(root, *, max_changes=200):
    if not 1<=max_changes<=1000:
        raise ValueError('Cycle lead budget must be 1–1000')
    root=Path(root).resolve();root.mkdir(parents=True,exist_ok=True,mode=0o700)
    if previous_run_requires_reconciliation(root):
        raise RuntimeError('Interrupted write run requires read-only reconciliation; no writes attempted')
    run=root/'runs'/(datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid4().hex[:8])
    run.mkdir(parents=True,mode=0o700)
    code=preview_main(['--scan-follow-up','--batched','--output',str(run/'preview')])
    if code:
        raise RuntimeError('Incomplete eligibility preview; no writes attempted')
    rows=json.loads((run/'preview'/'results.json').read_text())
    candidates=[row for row in rows if row['kind']=='change']
    chosen=candidates[:max_changes]
    summary={'mode':'scheduled_maintenance','source_revision':os.environ.get('CLOSE_STATUS_SOURCE_REVISION'),
             'started_at':run.name,'eligible':len(candidates),'selected':len(chosen),
             'remaining_for_next_run':max(0,len(candidates)-len(chosen)),'batches':[]}
    if chosen:
        save(root/'active-run.json',{'run_directory':str(run),'created_at':datetime.now(timezone.utc).isoformat()})
        for index in range(0,len(chosen),100):
            batch=chosen[index:index+100];expected={r['lead_id']:r for r in batch}
            result=apply_and_verify(list(expected),expected,run/f'batch-{index//100+1:04}')
            summary['batches'].append(result)
            save(run/'summary.json',summary)
    summary['complete']=True;summary['finished_at']=datetime.now(timezone.utc).isoformat()
    save(run/'summary.json',summary);save(root/'last-success.json',summary)
    root.joinpath('active-run.json').unlink(missing_ok=True)
    print(json.dumps(summary),flush=True)
    return summary


def main():
    os.umask(0o077)
    root=Path(os.environ.get('CLOSE_STATUS_DATA_DIR','/data')).resolve()
    root.mkdir(parents=True,exist_ok=True,mode=0o700)
    with root.joinpath('worker.lock').open('a') as lock:
        try:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({'state':'skipped','reason':'another_worker_owns_lock'}),flush=True)
            return 0
        if os.environ.get('CLOSE_STATUS_STARTUP_CHECK_ONLY')=='1':
            startup_check(root)
            return 0
        if os.environ.get('CLOSE_STATUS_MAINTENANCE_ENABLED')!='1':
            print(json.dumps({'state':'disabled','close_writes':0}),flush=True)
            return 0
        if os.environ.get('CLOSE_STATUS_MAINTENANCE_ENABLE_WRITES')!='1':
            raise PermissionError('Scheduled writes require the explicit write gate')
        run_cycle(root,max_changes=int(os.environ.get('CLOSE_STATUS_MAX_CHANGES','200')))
    return 0

if __name__=='__main__':
    raise SystemExit(main())
