"""Provider adapters for the disabled-by-default, explicitly scoped tier pilot."""
from dataclasses import replace
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from close_status_policy import COMPANY, ORG, OWNER, held_hash, load_exclusions, timestamp
from close_status_reader import CloseClient, SnapshotReader, load_roster
from lane2_roster import LANE2_ROSTER
from read_tier_assignment_preview import CaptureClient, capture_roster, search
from rep_access import apply_approved_classifications, prepare_review
from tier_assignment import LeadFacts, TIER, field, lead_owners
from tier_routing_rollout import build_plan, digest, execute, INTEGRATIONS, writes_enabled
from workflow_holds import configured_store, custom


class PilotSource:
    def __init__(self, tagger_path):
        self.client = CloseClient(os.environ['CLOSE_API_KEY'])
        self.capture = CaptureClient(self.client)
        self.history = SnapshotReader(self.capture)
        self.history.refresh_unsubscribed()
        self.exclusions = load_exclusions()
        spec = importlib.util.spec_from_file_location('existing_lead_tier_tagger', tagger_path)
        self.tagger = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.tagger)

    def read(self, scope, *, store=None, permission_source='candidate'):
        source, holds = capture_roster(lead_ids=scope.lead_ids)
        reps = prepare_review(source, LANE2_ROSTER)
        if permission_source == 'candidate':
            approval = json.loads(Path(__file__).with_name('config').joinpath('rep_access_approved_20261022.json').read_text())
            reps, _ = apply_approved_classifications(source, reps, approval)
        elif permission_source != 'stored':
            raise ValueError('Unknown rep permission source')
        reps = [rep for rep in reps if rep['close_user_id'] in scope.rep_ids]
        if permission_source == 'stored':
            for rep in reps:
                rep['proposed_access'] = rep['current_access']
        org = self.client.request('GET', f'/organization/{ORG}/')
        if org.get('id') != ORG:
            raise ValueError('Close organization mismatch')
        memberships = {row.get('user_id') or (row.get('user') or {}).get('id') for row in org['memberships']}
        closers = load_roster(datetime.now(timezone.utc))
        now = datetime.now(timezone.utc)
        items = []
        for lead_id in scope.lead_ids:
            evidence = self.history.read(lead_id, now, held=self.exclusions, roster=closers)
            lead = self.capture.leads.get(lead_id) or self.client.request('GET', f'/lead/{lead_id}/')
            if lead.get('id') != lead_id or lead.get('organization_id') != ORG:
                raise ValueError('Lead scope mismatch')
            # Reuse the contact-scoped workflow exclusion from the existing assigner.
            # Unknown or completed subscriptions are conservatively excluded too.
            if not lead_owners(lead):
                for contact in lead['contacts']:
                    rows = self.client.list('/sequence_subscription/', {'contact_id': contact['id'],
                        '_fields': 'id,contact_id,lead_id,sequence_id,status'})
                    if any(row.get('contact_id') != contact['id'] or row.get('lead_id') not in (None, lead_id) for row in rows):
                        raise ValueError('Workflow subscription scope mismatch')
                    if rows:
                        evidence = replace(evidence, review_reasons=evidence.review_reasons + ('existing_workflow_subscription',))
                        break
            calls = self.capture.activities.get((lead_id, '/activity/call/'))
            dials = tuple(timestamp(call['activity_at']) for call in calls or [] if call['direction'] == 'outbound')
            tag = self.tagger.classify_leads([lead], now.astimezone(ZoneInfo('America/Los_Angeles')).date())[0]
            value = custom(lead, TIER)
            if isinstance(value, list):
                value = value[0] if len(value) == 1 else None
            lead_holds = store.holds(lead_id) if store else None if holds is None else [row for row in holds if row['lead_id'] == lead_id]
            items.append(LeadFacts(lead, evidence, dials, None if lead_holds is None else tuple(lead_holds),
                held_hash(lead_id) in self.exclusions, calls is not None, tag['proposed_tier'] == value))
        counts = {}
        for rep in reps:
            query = {'type': 'and', 'queries': [{'type': 'object_type', 'object_type': 'lead'},
                field(OWNER, {'type': 'reference', 'reference_type': 'user_or_group', 'object_ids': [rep['close_user_id']]})]}
            count = search(self.client, query)['count']['total']
            if type(count) is not int or count < 0:
                raise ValueError('Incomplete current capacity count')
            counts[rep['close_user_id']] = count
        return {'reps': reps, 'items': items, 'closers': closers, 'memberships': memberships,
                'counts': counts, 'rep_permission_source': permission_source}

    def readback(self, lead_id):
        return self.client.request('GET', f'/lead/{lead_id}/')


class OwnerWriter:
    def __init__(self):
        self.session = requests.Session()
        self.session.auth = (os.environ['CLOSE_API_KEY'], '')

    def put_owner(self, lead_id, owner):
        if not writes_enabled():
            raise PermissionError('Tier production writes disabled')
        # No retries. The caller has already persisted the intent under the fence.
        response = self.session.put('https://api.close.com/api/v1/lead/' + lead_id + '/',
            json={'custom.' + OWNER: owner}, timeout=45)
        response.raise_for_status()


def run(args, scope):
    root = args.tier_output
    approval = None
    if args.apply:
        approval = json.loads(args.tier_approval.read_text())
        plan = approval['plan']
        # Authorization must pass before provider reads or execution artifacts.
        from tier_routing_rollout import authorize
        authorize(plan, approval, scope, datetime.now(timezone.utc))
    root.mkdir(parents=True, exist_ok=False)
    source = PilotSource(args.tier_tagger)
    if not args.apply:
        snapshot = source.read(scope, permission_source='candidate' if args.tier_use_staged_permissions else 'stored')
        plan = build_plan(scope, snapshot, datetime.now(timezone.utc))
        (root / 'plan.json').write_text(json.dumps(plan, indent=2) + '\n')
        template = {'plan': plan, 'approved_plan_sha256': digest(plan), 'approved_by': None,
                    'expires_at': None, 'hold_deployment_manifest': None,
                    'verified_integrations': {key: False for key in INTEGRATIONS}}
        (root / 'approval-template.json').write_text(json.dumps(template, indent=2) + '\n')
        print(json.dumps({'mode': 'tier_preview', 'production_writes': 0,
            'write_switch_enabled': writes_enabled(), 'scope': scope.__dict__, 'proposals': plan['proposals']}, indent=2))
    else:
        store = configured_store()
        if store is None:
            raise PermissionError('Mandatory shared hold backend unavailable')
        def record(receipt):
            with (root / 'receipts.jsonl').open('a') as handle:
                handle.write(json.dumps(receipt) + '\n')
        try:
            results = execute(plan, approval, scope, source=source, store=store, writer=OwnerWriter(),
                              clock=lambda: datetime.now(timezone.utc), record=record)
            print(json.dumps({'mode': 'tier_pilot', 'verified_assignments': len(results)}, indent=2))
        finally:
            store.close()
    (root / 'request-receipt.json').write_text(json.dumps({'close_read_search_requests': source.client.requests,
        'company_id': COMPANY, 'organization_id': ORG}, indent=2) + '\n')
