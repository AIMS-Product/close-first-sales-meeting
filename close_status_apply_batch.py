"""Bounded live batches using unchanged StatusExecutor guards and fresh read waves."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from threading import Event, Lock

from close_status_live import FreshSnapshotBroker, RequestLimiter, SharedTransport
from close_status_maintenance import StatusExecutor
from close_status_policy import load_exclusions
from close_status_reader import load_roster


def run_batch(ids, output, *, apply=False, expected=None, concurrency=50):
    if not ids or len(ids) > 100 or len(set(ids)) != len(ids):
        raise ValueError('A batch requires 1–100 unique exact lead IDs')
    if not all(isinstance(x, str) and x.startswith('lead_') for x in ids):
        raise ValueError('Invalid native lead ID')
    if apply and os.environ.get('CLOSE_STATUS_MAINTENANCE_ENABLE_WRITES') != '1':
        raise PermissionError('Close maintenance writes disabled')
    if not 1 <= concurrency <= 50:
        raise ValueError('Concurrency must be 1–50')
    output = Path(output)
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    started = datetime.now(timezone.utc)
    roster = load_roster(started)
    held = load_exclusions()
    output.joinpath('roster.json').write_text(json.dumps(asdict(roster), default=str, indent=2))
    limiter, fence, receipt_lock = RequestLimiter(), Event(), Lock()
    def receipt(filename, entry, durable=False):
        with receipt_lock:
            with output.joinpath(filename).open('a') as stream:
                stream.write(json.dumps(entry, default=str) + '\n')
                stream.flush()
                if durable:
                    os.fsync(stream.fileno())
    trace = lambda entry: receipt('request-trace.ndjson', entry)
    reads = SharedTransport(os.environ['CLOSE_API_KEY'], limiter, trace=trace)
    writes = SharedTransport(os.environ['CLOSE_API_KEY'], limiter, write_enabled=apply, trace=trace, write_fence=fence)
    results = []
    def journal(entry):
        receipt('write-journal.ndjson', entry, durable=True)
    with FreshSnapshotBroker(reads, writes, capture_first=lambda lead_id, lead:
            receipt('before-leads.ndjson', {'lead_id':lead_id,'lead':lead}, durable=apply)) as reader:
        def process(lead_id):
            executor = StatusExecutor(reader, roster, held, journal=journal)
            try:
                decision = executor.preview(lead_id)
                row = {'lead_id': lead_id, **asdict(decision)}
                previous = (expected or {}).get(lead_id)
                if previous and (decision.lead_status != previous.get('lead_status')
                    or decision.opportunity_id != previous.get('opportunity_id')
                    or decision.opportunity_status != previous.get('opportunity_status')
                    or tuple(decision.owner_ids) != tuple(previous.get('owner_ids', ()))):
                    row['execution'] = {'state':'held','reason':'preview_plan_changed','writes':[]}
                elif apply and decision.changes:
                    row['execution'] = executor.apply(lead_id, decision)
                    if row['execution']['state'] == 'partial':
                        fence.set()
                receipt('results.ndjson', row, durable=apply)
                return row
            except Exception as error:
                fence.set()
                row = {'lead_id':lead_id, 'kind':'error', 'reason':type(error).__name__, 'detail':str(error)}
                receipt('results.ndjson', row, durable=apply)
                return row
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            for future in as_completed([pool.submit(process, lead_id) for lead_id in ids]):
                results.append(future.result())
                print(json.dumps({'completed':len(results),'total':len(ids),
                    'verified':sum(r.get('execution',{}).get('state')=='verified' for r in results),
                    'errors':sum(r['kind']=='error' for r in results)}), flush=True)
        output.joinpath('before-leads.json').write_text(json.dumps(reader.raw_first, indent=2))
        output.joinpath('snapshot-receipts.json').write_text(json.dumps(reader.capture_receipts, indent=2))
    output.joinpath('results.json').write_text(json.dumps(results, indent=2))
    summary = {'mode':'apply' if apply else 'preview', 'scanned':len(results),
        'changes_proposed':sum(r['kind']=='change' for r in results),
        'verified':sum(r.get('execution',{}).get('state')=='verified' for r in results),
        'held':sum(r['kind']=='hold' or r.get('execution',{}).get('state')=='held' for r in results),
        'partial':sum(r.get('execution',{}).get('state')=='partial' for r in results),
        'errors':sum(r['kind']=='error' for r in results),
        'close_put_requests':sum(r['method']=='PUT' for r in writes.requests),
        'http_requests':len(reads.requests)+len(writes.requests),
        'explicit_exclusions':len(held),'active_closers':len(roster.closer_ids),
        'started_at':started.isoformat(),'finished_at':datetime.now(timezone.utc).isoformat()}
    output.joinpath('summary.json').write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary), flush=True)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--lead-ids', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--expected', type=Path)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--concurrency', type=int, default=50)
    args = parser.parse_args()
    os.umask(0o077)
    expected = {r['lead_id']:r for r in json.loads(args.expected.read_text())} if args.expected else None
    summary = run_batch(json.loads(args.lead_ids.read_text()), args.output,
                        apply=args.apply, expected=expected, concurrency=args.concurrency)
    return 1 if summary['errors'] or summary['partial'] else 0

if __name__ == '__main__':
    raise SystemExit(main())
