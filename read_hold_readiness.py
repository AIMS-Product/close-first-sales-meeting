"""Read-only shared hold setup inventory. Never migrates or publishes queues."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
import requests

from close_status_policy import COMPANY, ORG
from close_status_reader import CloseClient
from hold_rollout import pending_manifest
from read_tier_assignment_preview import load_environment
from workflow_holds import BATCH, no_sales_hold_query

GUARDED_FILES = ('lane2_state.py', 'assign_lane2_leads.py', 'assign_setter_leads.py', 'assign_tlg_leads.py',
    'update_lane2_reassignment.py', 'update_lost_deals.py', 'update_sales_lane.py', 'sync_lead_owner_to_opp.py',
    'sync_opp_lead_status.py', 'close_status_reader.py', 'no_show_recovery.py', 'outcome_sync.py',
    'update_field.py', 'update_followups.py', 'update_funnel_name.py', 'update_sales_cycle.py',
    'bulk_remove_from_sequence.py', 'repair_closed_lost_opportunities.py', 'backfill_outcomes.py')


def sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def has_hold_exclusion(query):
    if isinstance(query, dict):
        condition = query.get('condition') or {}
        if (query.get('type') == 'field_condition' and query.get('negate') is True
                and query.get('field',{}).get('custom_field_id') == BATCH
                and condition.get('type') == 'text' and condition.get('mode') == 'full_words'
                and condition.get('value') == 'STH1'): return True
        # A condition in an OR branch does not exclude every held lead.
        if query.get('type') == 'and' and query.get('negate') is not True:
            return any(has_hold_exclusion(child) for child in query.get('queries', []))
    return False


def database_probe():
    with psycopg.connect(host=os.environ['LAKEBASE_HOST'], dbname=os.environ['LAKEBASE_DATABASE'],
        user=os.environ['LAKEBASE_USER'], password=os.environ['LAKEBASE_PASSWORD'], sslmode='verify-full',
        sslrootcert=os.environ.get('LAKEBASE_SSLROOTCERT') or requests.certs.where(),
        connect_timeout=20, row_factory=dict_row,
        options='-c default_transaction_read_only=on -c statement_timeout=30000') as connection:
        with connection.transaction():
            connection.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            connection.execute("select set_config('app.company_id',%s,true)",(COMPANY,))
            proof = dict(connection.execute("select current_user as reader_role,current_database() as database_name,current_setting('transaction_read_only') as read_only,transaction_timestamp() as captured_at,txid_current_snapshot() as snapshot,has_database_privilege(current_user,current_database(),'CREATE') as can_create_schema").fetchone())
            if proof['read_only'] != 'on': raise ValueError('Read-only proof required')
            tables = {}
            for name in ('sales_hold', 'sms_cycle', 'workflow_event', 'workflow_write', 'workflow_meta'):
                path = 'crm_workflow_control.' + name
                row = connection.execute("select to_regclass(%s)::text as table_name", (path,)).fetchone()
                exists = bool(row['table_name'])
                privileges = dict(connection.execute("select has_table_privilege(current_user,%s,'SELECT') as can_read,has_table_privilege(current_user,%s,'INSERT,UPDATE,DELETE') as can_write", (path,path)).fetchone()) if exists else None
                tables[name] = {'exists': exists, 'privileges': privileges}
            metadata = {}
            if tables['workflow_meta']['exists']:
                metadata = {row['key']: row['value'] for row in connection.execute("select key,value from crm_workflow_control.workflow_meta where key in ('tenant','hold_contract_version')").fetchall()}
    return {'proof': proof, 'tables': tables, 'contract_metadata': metadata, 'data_writes': 0}


def collect(client):
    org = client.request('GET', '/organization/' + ORG + '/')
    if org.get('id') != ORG: raise ValueError('Close tenant mismatch')
    queues = []; changes = []
    for view in client.list('/saved_search/', {}):
        name = view.get('name') or ''
        managed = name.startswith('L2 · ') or name in [offer + ' · My Assigned Leads' for offer in ('VP','TLG','AA','PPA')]
        calling_queue = managed and 'Ops' not in name
        query = view.get('s_query')
        if isinstance(query, str):
            try: query = json.loads(query)
            except ValueError: query = None
        root = query.get('query') if isinstance(query, dict) else None
        excluded = has_hold_exclusion(root)
        queues.append({'id': view['id'], 'name': name, 'managed_by_this_package': managed,
            'sales_queue_candidate': calling_queue,
            'structured_query_available': isinstance(root, dict), 'verified_hold_exclusion': excluded})
        if calling_queue and isinstance(root, dict) and not excluded:
            after = {**query, 'query': {'type': 'and', 'queries': [root, no_sales_hold_query()]}}
            changes.append({'path': '/saved_search/' + view['id'] + '/', 'name': name,
                'expected_s_query_sha256': sha(query), 'before': {'s_query': query}, 'after': {'s_query': after},
                'production_write_authorized': False})
    sequences = []
    for sequence in client.list('/sequence/', {}):
        steps = sequence.get('steps')
        types = sorted({str(step.get('step_type')) for step in steps or []})
        sequences.append({'id': sequence['id'], 'name': sequence.get('name'), 'status': sequence.get('status'),
            'step_types': types, 'steps_sha256': sha(steps) if steps is not None else None,
            'classification': 'sales' if set(types) & {'sms','call'} else 'requires_review',
            'marketing_approved': False})
    return queues, changes, sequences


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file', type=Path, action='append', default=[])
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(); args.output.mkdir(parents=True, exist_ok=False)
    load_environment(args.env_file)
    database = database_probe()
    client = CloseClient(os.environ['CLOSE_API_KEY'])
    queues, changes, sequences = collect(client)
    now = datetime.now(timezone.utc)
    manifest = pending_manifest(now)
    root = Path(__file__).parent
    local = {name: {'candidate_sha256': hashlib.sha256((root/name).read_bytes()).hexdigest(),
                    'deployment_verified': False} for name in GUARDED_FILES}
    summary = {'captured_at': now.isoformat(), 'company_id': COMPANY, 'organization_id': ORG,
        'database': database, 'local_writer_candidates': local, 'queues': queues, 'sequences': sequences,
        'candidate_queue_changes': len(changes), 'close_read_search_requests': len(client.requests),
        'production_writes': 0, 'activation': 'off', 'deployment_certified': False}
    for name, value in [('readiness.json', summary), ('queue-change-preview.json', changes),
                        ('deployment-manifest.pending.json', manifest), ('request-receipt.json', client.requests)]:
        (args.output/name).write_text(json.dumps(value, indent=2, default=str)+'\n')
    print(json.dumps({'tables_available': all(row['exists'] for row in database['tables'].values()),
        'saved_searches_read': len(queues), 'managed_queues': sum(row['managed_by_this_package'] for row in queues),
        'managed_queues_missing_hold_exclusion': len(changes), 'sequences_read': len(sequences),
        'requests': len(client.requests), 'production_writes': 0, 'activation': 'off'}, indent=2))


if __name__ == '__main__': main()
