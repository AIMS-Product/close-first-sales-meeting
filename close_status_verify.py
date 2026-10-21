"""Independent native readbacks and business-field preservation checks."""
from collections import Counter
from dataclasses import asdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
from close_status_live import FreshSnapshotBroker, RequestLimiter, SharedTransport
from close_status_maintenance import StatusExecutor
from close_status_policy import OWNER, load_exclusions
from close_status_reader import load_roster

STATUS_METADATA = {'status_id','status_label','status_display_name','status_type','date_updated','updated_by','updated_by_name'}
# Close can alternate these equivalent IANA names on successive reads of an
# unchanged, phone-inferred contact. Keep the exception narrow: manual values,
# other zones, source changes and every other contact field remain protected.
INFERRED_TIMEZONE_ALIASES = {'Arctic/Longyearbyen': 'Europe/Oslo'}


def comparable_contacts(contacts):
    return [dict(contact, timezone=INFERRED_TIMEZONE_ALIASES.get(contact['timezone'],contact['timezone']))
            if contact.get('timezone_source')=='inferred_from_phone_number' and contact.get('timezone')
            else contact for contact in contacts]


def business_differences(before, after, target_opportunity=None):
    differences = [key for key in set(before) | set(after)
        if key not in STATUS_METADATA | {'opportunities','contacts'} and before.get(key) != after.get(key)]
    if ('contacts' in before) != ('contacts' in after) or comparable_contacts(before.get('contacts',[])) != comparable_contacts(after.get('contacts',[])):
        differences.append('contacts')
    old = {row['id']:row for row in before['opportunities']}
    new = {row['id']:row for row in after['opportunities']}
    if set(old) != set(new):
        differences.append('opportunity_membership')
    for key in set(old) & set(new):
        ignored = STATUS_METADATA if key == target_opportunity else {'date_updated','updated_by','updated_by_name'}
        differences.extend('opportunity.' + field for field in set(old[key]) | set(new[key])
            if field not in ignored and old[key].get(field) != new[key].get(field))
    return sorted(set(differences))


def verify_batch(path, api_key, *, policy_replay=True):
    path = Path(path)
    results = json.loads(path.joinpath('results.json').read_text())
    before = json.loads(path.joinpath('before-leads.json').read_text())
    written = [r for r in results if r.get('execution',{}).get('writes')]
    client = SharedTransport(api_key, RequestLimiter())
    with ThreadPoolExecutor(max_workers=8) as pool:
        rows = list(pool.map(lambda r: client.request('GET',f'/lead/{r["lead_id"]}/'), written))
    records = []
    after = {row['id']:row for row in rows}
    for result in written:
        lead_id=result['lead_id'];current=after[lead_id];old=before[lead_id]
        expected_status=result.get('lead_status') or old['status_id']
        matched=current['status_id']==expected_status
        opp_id=result.get('opportunity_id')
        if opp_id:
            embedded=next((o for o in current['opportunities'] if o['id']==opp_id),None)
            direct=client.request('GET',f'/opportunity/{opp_id}/')
            matched=matched and embedded is not None and embedded['status_id']==result['opportunity_status'] and direct['lead_id']==lead_id and direct['status_id']==result['opportunity_status']
        records.append({'lead_id':lead_id,'targets_match':matched,
            'unexpected_business_changes':business_differences(old,current,opp_id)})
    replay=[]
    if written and policy_replay:
        roster=load_roster(datetime.now(timezone.utc));held=load_exclusions()
        with FreshSnapshotBroker(client,client) as reader:
            def plan(result):
                decision=StatusExecutor(reader,roster,held).preview(result['lead_id'])
                return {'lead_id':result['lead_id'],**asdict(decision)}
            with ThreadPoolExecutor(max_workers=50) as pool:
                replay=list(pool.map(plan,written))
    journal=path/'write-journal.ndjson'
    entries=[json.loads(row) for row in journal.read_text().splitlines()] if journal.exists() else []
    intents=[r for r in entries if r['state']=='intent'];responses=[r for r in entries if r['state']=='response_received']
    receipt_key=lambda r:(r['path'],r['status_id'],r['at'])
    allowed_statuses={(f'/lead/{r["lead_id"]}/',r.get('lead_status')) for r in results}|{(f'/opportunity/{r["opportunity_id"]}/',r.get('opportunity_status')) for r in results if r.get('opportunity_id')}
    matched_receipts=Counter(map(receipt_key,intents))==Counter(map(receipt_key,responses)) and all((r['path'],r['status_id']) in allowed_statuses for r in intents) and not any(r['state']=='unknown_outcome_stop_and_reconcile' for r in entries)
    pending_changes=[r for r in replay if r['kind']=='change']
    unexpected_replays=[r for r in replay if r['kind']!='keep']
    summary={'written_leads':len(written),'native_targets_verified':sum(r['targets_match'] for r in records),
        'unexpected_business_change_records':sum(bool(r['unexpected_business_changes']) for r in records),
        'intent_count':len(intents),'response_count':len(responses),'all_receipts_match':matched_receipts,
        'policy_replay_changes':len(pending_changes),'policy_replay_holds':sum(r['kind']=='hold' for r in replay),
        'verification_close_puts':sum(r['method']=='PUT' for r in client.requests),
        'finished_at':datetime.now(timezone.utc).isoformat()}
    summary['complete']=all(r['targets_match'] and not r['unexpected_business_changes'] for r in records) and matched_receipts and not unexpected_replays
    path.joinpath('independent-verification.json').write_text(json.dumps({'summary':summary,'records':records,'replay':replay},indent=2))
    path.joinpath('after-leads.json').write_text(json.dumps(after,indent=2))
    path.joinpath('verification-read-trace.json').write_text(json.dumps(client.requests,indent=2))
    return summary
