"""Close status maintenance. Default: preview; no scheduler is enabled here."""
import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from close_status_policy import FOLLOW_UP, HOT, NONRESPONSIVE, ORG, decide, load_exclusions
from close_status_reader import CloseClient, SnapshotReader, load_roster


class StatusExecutor:
    def __init__(self, reader, roster, held, clock=lambda: datetime.now(timezone.utc), journal=None):
        self.reader, self.roster, self.held, self.clock = reader, roster, held, clock
        self.journal = journal

    def preview(self, lead_id):
        return decide(self.reader.read(lead_id, self.clock(), held=self.held, roster=self.roster),
                      self.roster, self.held, self.clock())

    def apply(self, lead_id, expected):
        """Replan before either native status-only PUT; never compensate across a race."""
        if not self.reader.client.write_enabled or self.journal is None:
            raise PermissionError('Close writes disabled')
        if not expected.changes:
            return {'state': 'skipped', 'reason': expected.reason, 'writes': []}
        writes = []
        self.reader.refresh_unsubscribed()
        if self.preview(lead_id) != expected:
            return {'state': 'held', 'reason': 'changed_before_write', 'writes': writes}
        if expected.opportunity_id:
            result = self.write_status(f'/opportunity/{expected.opportunity_id}/', expected.opportunity_status)
            if result.get('id') != expected.opportunity_id or result.get('lead_id') != lead_id or result.get('status_id') != expected.opportunity_status:
                raise RuntimeError('Opportunity write unverified; stop and reconcile')
            writes.append({'type': 'opportunity', 'id': expected.opportunity_id, 'status_id': expected.opportunity_status})
        if expected.lead_status:
            self.reader.refresh_unsubscribed()
            now = self.clock()
            current = self.reader.read(lead_id, now, held=self.held, roster=self.roster)
            continuing = decide(current, self.roster, self.held, now)
            opportunity_applied = not expected.opportunity_id or any(
                opp.id == expected.opportunity_id and opp.status == expected.opportunity_status
                for opp in current.opportunities)
            if (continuing.kind == 'keep' and current.status == expected.lead_status
                    and current.owners == expected.owner_ids and opportunity_applied):
                return {'state': 'verified', 'reason': 'target_already_reconciled', 'writes': writes}
            if not continuing.changes or continuing.lead_status != expected.lead_status or continuing.opportunity_id or continuing.owner_ids != expected.owner_ids:
                return {'state': 'partial' if writes else 'held', 'reason': 'changed_before_lead_write', 'writes': writes}
            result = self.write_status(f'/lead/{lead_id}/', expected.lead_status)
            if result.get('id') != lead_id or result.get('status_id') != expected.lead_status:
                raise RuntimeError('Lead write unverified; stop and reconcile')
            writes.append({'type': 'lead', 'id': lead_id, 'status_id': expected.lead_status})
        now = self.clock()
        current = self.reader.read(lead_id, now, held=self.held, roster=self.roster)
        verification = decide(current, self.roster, self.held, now)
        normalization_pending_hot = (expected.reason == 'consolidate_legacy_status'
            and current.status == NONRESPONSIVE and current.owners == expected.owner_ids
            and verification.reason == 'recent_inbound' and verification.lead_status == HOT)
        complete = verification.kind == 'keep' or normalization_pending_hot
        reason = 'legacy_normalized_pending_hot_refresh' if normalization_pending_hot else verification.reason
        return {'state': 'verified' if complete else 'partial', 'reason': reason, 'writes': writes}

    def write_status(self, path, status_id):
        intent = {'path': path, 'status_id': status_id, 'at': self.clock().isoformat()}
        self.journal(dict(intent, state='intent'))
        try:
            result = self.reader.client.request('PUT', path, {'status_id': status_id})
        except Exception:
            self.journal(dict(intent, state='unknown_outcome_stop_and_reconcile'))
            raise
        self.journal(dict(intent, state='response_received'))
        return result


def follow_up_ids(client):
    """Partition broad searches by creation time to stay below Close's 10k ceiling."""
    def query(start, end, status):
        return {'type': 'and', 'queries': [
            {'type': 'object_type', 'object_type': 'lead'},
            {'type': 'field_condition', 'field': {'type': 'regular_field', 'object_type': 'lead', 'field_name': 'status_id'},
             'condition': {'type': 'reference', 'reference_type': 'status.lead', 'object_ids': [status]}},
            {'type': 'field_condition', 'field': {'type': 'regular_field', 'object_type': 'lead', 'field_name': 'date_created'},
             'condition': {'type': 'moment_range', 'on_or_after': {'type': 'fixed_utc', 'value': start.isoformat()},
                           'before': {'type': 'fixed_utc', 'value': end.isoformat()}}}]}
    seen = set()
    def partition(start, end, status):
        body = {'query': query(start, end, status), '_fields': {'lead': ['id']}, '_limit': 100, 'include_counts': True,
                'sort': [{'direction': 'asc', 'field': {'type': 'regular_field', 'object_type': 'lead',
                                                     'field_name': 'date_created'}}]}
        page = client.request('POST', '/data/search/', body)
        total = page['count']['total']
        if total > 9000:
            midpoint = start + (end - start) / 2
            if (end - start).total_seconds() < 1:
                raise RuntimeError('Unpartitionable Close inventory; no writes permitted')
            yield from partition(start, midpoint, status)
            yield from partition(midpoint, end, status)
            return
        cursors, returned = set(), 0
        while True:
            for lead in page['data']:
                if lead['id'] in seen:
                    raise RuntimeError('Inventory changed during pagination; rerun preview')
                seen.add(lead['id'])
                returned += 1
                yield lead['id']
            cursor = page.get('cursor')
            if cursor is None:
                if returned != total:
                    raise RuntimeError('Inventory count changed during pagination; rerun preview')
                return
            if not cursor or cursor in cursors:
                raise ValueError('Invalid search cursor')
            cursors.add(cursor)
            page = client.request('POST', '/data/search/', dict(body, cursor=cursor))
    for status in sorted(FOLLOW_UP):
        yield from partition(datetime(1970, 1, 1, tzinfo=timezone.utc), datetime.now(timezone.utc), status)


def hot_sync_guard(api_key):
    """Existing sync can only enter HOT if it passes the same full policy."""
    now = datetime.now(timezone.utc)
    roster = load_roster(now, os.environ.get('CLOSE_STATUS_ROSTER_FILE'))
    reader = SnapshotReader(CloseClient(api_key))
    reader.refresh_unsubscribed()
    held = load_exclusions()
    executor = StatusExecutor(reader, roster, held)
    def allowed(lead_id):
        reader.refresh_unsubscribed()
        result = executor.preview(lead_id)
        return result.kind == 'keep' and result.reason == 'recent_inbound' or (
            result.changes and result.lead_status == HOT and result.opportunity_id is None)
    return allowed


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--lead-ids', type=Path, help='JSON list of exact lead IDs for a bounded preview')
    group.add_argument('--scan-follow-up', action='store_true')
    parser.add_argument('--roster', type=Path, help='Fresh authoritative roster export, otherwise use existing Lakebase credentials')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--limit', type=int)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--max-changes', type=int, default=10, help='Maximum leads permitted to change in an apply run')
    parser.add_argument('--batched', action='store_true', help='Use bounded batched evidence reads for a preview')
    args = parser.parse_args(argv)
    if args.max_changes <= 0:
        parser.error('--max-changes must be positive')
    if args.apply and (not args.lead_ids or args.batched):
        parser.error('Apply requires exact --lead-ids and fresh per-lead evidence; batched evidence is preview-only')
    if args.limit is not None and args.limit <= 0:
        parser.error('--limit must be positive')
    if args.apply and os.environ.get('CLOSE_STATUS_MAINTENANCE_ENABLE_WRITES') != '1':
        parser.error('--apply requires CLOSE_STATUS_MAINTENANCE_ENABLE_WRITES=1; default is zero-write preview')
    os.umask(0o077)
    now = datetime.now(timezone.utc)
    roster, held = load_roster(now, args.roster), load_exclusions()
    client = CloseClient(os.environ['CLOSE_API_KEY'], write_enabled=args.apply)
    reader = SnapshotReader(client)
    reader.refresh_unsubscribed()
    executor = StatusExecutor(reader, roster, held)
    if args.lead_ids:
        ids = json.loads(args.lead_ids.read_text())
        if not isinstance(ids, list) or not all(isinstance(x, str) for x in ids) or len(set(ids)) != len(ids):
            raise ValueError('Exact lead-ID input must be a unique JSON list')
    else:
        # Complete inventory before any potential write; partial inventory never authorizes a run.
        ids = list(follow_up_ids(client))
    if args.limit:
        ids = ids[:args.limit]
    args.output.mkdir(parents=True, exist_ok=False, mode=0o700)
    def journal(entry):
        with (args.output / 'write-journal.ndjson').open('a') as receipt:
            receipt.write(json.dumps(entry) + '\n')
            receipt.flush()
            os.fsync(receipt.fileno())
    executor.journal = journal
    (args.output / 'roster-source.json').write_text(json.dumps({'company_id': '88a28173-2a61-4cbb-92ea-f6b8be4592f3', 'organization_id': ORG, 'captured_at': roster.captured_at.isoformat(), 'active_closer_ids': sorted(roster.closer_ids), 'source_receipt': roster.source_receipt}, indent=2) + '\n')
    results, failed, changed = [], False, 0
    batch_reader = None
    if args.batched and not args.apply:
        from close_status_batch import BatchedEvidenceReader
        batch_reader = BatchedEvidenceReader(client, roster, held, datetime.now(timezone.utc))
        batch_reader.prepare()
    evidence_batch = {}
    try:
        for index, lead_id in enumerate(ids):
            try:
                if batch_reader:
                    if index % 50 == 0:
                        evidence_batch = batch_reader.read_batch(ids[index:index+50])
                    decision = decide(evidence_batch[lead_id], roster, held, batch_reader.cutoff)
                else:
                    decision = executor.preview(lead_id)
                row = {'lead_id': lead_id, **asdict(decision)}
                if args.apply and decision.changes:
                    if changed >= args.max_changes:
                        row['execution'] = {'state':'skipped', 'reason':'apply_lead_budget_reached', 'writes':[]}
                    else:
                        row['execution'] = executor.apply(lead_id, decision)
                        if row['execution']['writes']:
                            changed += 1
                    if row['execution']['state'] != 'verified':
                        failed = True
                results.append(row)
                # Persist after each lead so an interruption retains evidence of every submitted write.
                with (args.output / 'results.ndjson').open('a') as receipt:
                    receipt.write(json.dumps(row) + '\n')
                    receipt.flush()
                    if args.apply:
                        os.fsync(receipt.fileno())
                print(json.dumps({'scanned': len(results), 'decision': decision.kind, 'reason': decision.reason}), flush=True)
            except Exception as error:
                results.append({'lead_id': lead_id, 'kind': 'error', 'reason': type(error).__name__})
                failed = True
                if args.apply:
                    raise
                print(json.dumps({'scanned': len(results), 'decision': 'error', 'reason': type(error).__name__}), flush=True)
    finally:
        (args.output / 'results.json').write_text(json.dumps(results, indent=2) + '\n')
        (args.output / 'read-trace.json').write_text(json.dumps(client.requests, indent=2) + '\n')
        summary = {'mode': 'apply' if args.apply else 'preview', 'scanned': len(results),
                   'changes_proposed': sum(r.get('kind') == 'change' for r in results),
                   'errors': sum(r.get('kind') == 'error' for r in results),
                   'close_put_requests': sum(r['method'] == 'PUT' for r in client.requests),
                   'active_closers': len(roster.closer_ids), 'explicit_held_leads': len(held),
                   'started_at': now.isoformat(), 'finished_at': datetime.now(timezone.utc).isoformat()}
        (args.output / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
        print(json.dumps(summary))
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
