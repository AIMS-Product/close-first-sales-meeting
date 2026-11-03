"""No-network local CLI and snapshot adapters for unified V2 distribution."""
import argparse
from dataclasses import asdict
from datetime import datetime
import json
import os
from pathlib import Path
import sys
import tempfile

from close_status_policy import COMPANY, ORG, Evidence, Opportunity, Roster
from lead_distribution_v2 import DistributionItem, build_distribution_plan, simulate_plan, plan_rest_return
from rep_access import instant
from tier_assignment import LeadFacts, view_candidates


def evidence_from_json(raw):
    values = dict(raw)
    values['complete'] = raw.get('complete') is True
    for name in ('latest_inbound',):
        if values.get(name):
            values[name] = instant(values[name])
    values['owners'] = tuple(values['owners'])
    for name in ('suppressions', 'review_reasons'):
        values[name] = tuple(values.get(name, []))
    opportunities = []
    for row in values.get('opportunities', []):
        row = dict(row)
        row['updated_at'] = instant(row['updated_at'])
        opportunities.append(Opportunity(**row))
    values['opportunities'] = tuple(opportunities)
    return Evidence(**values)


def snapshot_from_json(raw):
    if raw.get('schema_version') != 1:
        raise ValueError('Expected local snapshot schema_version 1')
    items = []
    for row in raw['leads']:
        for name in ('holds', 'outgoing_dials', 'call_created_times'):
            value = row.get(name)
            if value is not None and not isinstance(value, list):
                raise ValueError(name + ' must be an explicit list or unknown')
        for hold in row.get('holds') or []:
            if not isinstance(hold, dict) or not all(isinstance(hold.get(key), str) and hold[key] for key in ('workflow', 'token')):
                raise ValueError('Malformed hold evidence')
        for name in ('active_workflow', 'open_task', 'blocked_number', 'primary_ready',
                     'activity_complete', 'ownership_verified', 'trial_selected', 'calls_complete'):
            if row.get(name) is not None and type(row[name]) is not bool:
                raise ValueError(name + ' must be a boolean or unknown')
        if row.get('calls_complete') is True and row.get('outgoing_dials') is None:
            raise ValueError('Complete call evidence requires explicit outgoing_dials')
        facts = LeadFacts(row['lead'], evidence_from_json(row['evidence']),
            tuple(instant(value) for value in row.get('outgoing_dials', [])),
            tuple(row['holds']) if row.get('holds') is not None else None,
            row.get('explicitly_excluded', False), row.get('calls_complete') is True,
            row.get('tier_freshness_verified') is True)
        values = {name: row[name] for name in (
            'active_workflow', 'open_task', 'blocked_number', 'primary_ready', 'activity_complete',
            'last_activity_at', 'ownership_verified', 'workstream', 'trial_selected') if name in row}
        values['call_created_times'] = tuple(instant(at) for at in row['call_created_times']) if row.get('call_created_times') is not None else None
        items.append(DistributionItem(facts, **values))
    return {'company_id': raw['company_id'], 'organization_id': raw['organization_id'],
        'captured_at': raw['captured_at'], 'complete': raw.get('complete') is True,
        'items': items, 'reps': raw['reps'], 'counts': raw['counts'],
        'memberships': set(raw['memberships']),
        'closers': Roster(frozenset(raw['closer_ids']), instant(raw['closer_roster_captured_at'])),
        'rest_returns': raw.get('rest_returns', [])}


def captured_preview_snapshot(preview, source_leads):
    """Replay earlier evidence without claiming unknown holds/tasks are clear."""
    rows = []
    for row in source_leads:
        rows.append({**row, 'active_workflow': None, 'open_task': None, 'blocked_number': None,
            'primary_ready': None, 'activity_complete': False, 'ownership_verified': False,
            'trial_selected': False})
    return {'schema_version': 1, 'company_id': COMPANY, 'organization_id': ORG,
        'captured_at': preview['captured_at'], 'complete': True, 'leads': rows,
        'reps': preview['reps'], 'counts': preview['queue_counts'],
        'memberships': preview['active_close_membership_ids'],
        'closer_ids': preview['active_closer_ids'],
        'closer_roster_captured_at': preview['closer_roster_captured_at']}


def private_write(path, value):
    raw = value if isinstance(value, str) else json.dumps(value, sort_keys=True, indent=2, default=str) + '\n'
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.distribution-')
    try:
        with os.fdopen(fd, 'w') as handle:
            handle.write(raw)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def run(snapshot, at, output, trial_config=None):
    plan = build_distribution_plan(snapshot, at, trial_config=trial_config)
    simulation = simulate_plan(plan, snapshot)
    by_id = {item.facts.lead['id']: item for item in snapshot['items']}
    cycles = snapshot.get('rest_returns', [])
    if len({cycle['lead_id'] for cycle in cycles}) != len(cycles):
        raise ValueError('Duplicate rest-return cycles')
    return_plans = []
    counts = dict(plan['queue_counts_after_preview'])
    for cycle in sorted(cycles, key=lambda row: row['lead_id']):
        result = plan_rest_return(cycle, by_id[cycle['lead_id']], snapshot['closers'],
            snapshot['reps'], snapshot['memberships'], counts, at)
        return_plans.append({'lead_id': cycle['lead_id'], **result, 'apply_ready': False})
        if result['action'] == 'returned':
            counts[result['target_owner']] += 1
    definitions = view_candidates({rep['offer'] for rep in snapshot['reps'] if rep.get('offer')}, snapshot['closers'].closer_ids)
    summary = {'mode': 'local_dry_run', **plan['summary'], 'provider_requests': 0,
        'outbound_messages': 0, 'unpaired_trial_offers': plan['unpaired_trial_offers'],
        'rest_return_proposals': sum(row['action'] == 'returned' for row in return_plans),
        'original_snapshot_unchanged': simulation['original_snapshot_unchanged']}
    output.mkdir(parents=True, exist_ok=False)
    for name, value in (('plan.json', plan), ('dialer-manifest.json', plan['dialer_manifest']),
        ('simulation.json', simulation), ('summary.json', summary), ('owned-queue-definitions.json', definitions),
        ('rest-return-plans.json', return_plans)):
        private_write(output / name, value)
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument('--snapshot', type=Path)
    inputs.add_argument('--captured-preview', type=Path, help='Earlier preview folder with preview.json and source-leads.json')
    parser.add_argument('--trial-config', type=Path)
    parser.add_argument('--at', required=True, help='Explicit timezone-aware replay/evaluation time')
    parser.add_argument('--output', type=Path, required=True, help='New private local output directory')
    args = parser.parse_args(argv)
    try:
        raw = json.loads(args.snapshot.read_text()) if args.snapshot else captured_preview_snapshot(
            json.loads((args.captured_preview / 'preview.json').read_text()),
            json.loads((args.captured_preview / 'source-leads.json').read_text()))
        cfg = json.loads(args.trial_config.read_text()) if args.trial_config else None
        summary = run(snapshot_from_json(raw), instant(args.at), args.output, cfg)
    except (KeyError, TypeError, ValueError, OSError) as error:
        print('Local distribution dry run did not complete: ' + str(error), file=sys.stderr)
        return 1
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
