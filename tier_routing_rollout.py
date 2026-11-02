"""Explicit, bounded tier pilot switch. Imports no provider clients when off."""
from dataclasses import dataclass
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import re

from close_status_policy import COMPANY, ORG, OWNER, timestamp

MAX_PILOT_LEADS = 10
MAX_PILOT_REPS = 5
INTEGRATIONS = ('shared_store_and_all_writers', 'sales_dispatch_and_enrollment',
                'all_dial_queues', 'schema_and_tenant', 'ownership_tracking')


def add_arguments(parser):
    parser.add_argument('--tier-routing', action='store_true', help='use bounded tier pilot instead of legacy assignment; default off')
    parser.add_argument('--tier-rep', action='append', default=[], help='explicit Close user ID; repeat for up to five pilot reps')
    parser.add_argument('--tier-lead', action='append', default=[], help='explicit unowned Close lead ID; repeat for up to ten leads')
    parser.add_argument('--tier-max-assignments', type=int, default=None, help='required pilot cap, 1–10')
    parser.add_argument('--tier-output', type=Path, help='new local directory for preview or execution receipts')
    parser.add_argument('--tier-approval', type=Path, help='exact approved preview and integration receipts; required for writes')
    parser.add_argument('--tier-tagger', type=Path, help='existing lead-age updater, reused read-only')
    parser.add_argument('--tier-use-staged-permissions', action='store_true', help='preview approved local roster candidates; these previews cannot authorize writes')


@dataclass(frozen=True)
class PilotScope:
    rep_ids: tuple
    lead_ids: tuple
    max_assignments: int
    max_queue: int

    def __post_init__(self):
        if not 1 <= len(self.rep_ids) <= MAX_PILOT_REPS or len(set(self.rep_ids)) != len(self.rep_ids):
            raise ValueError('Select one to five distinct pilot reps')
        if not 1 <= len(self.lead_ids) <= MAX_PILOT_LEADS or len(set(self.lead_ids)) != len(self.lead_ids):
            raise ValueError('Select one to ten distinct pilot leads')
        if any(not re.fullmatch(r'user_[A-Za-z0-9]+', value) for value in self.rep_ids):
            raise ValueError('Pilot reps require exact Close user IDs')
        if any(not re.fullmatch(r'lead_[A-Za-z0-9]+', value) for value in self.lead_ids):
            raise ValueError('Pilot leads require exact Close lead IDs')
        if type(self.max_assignments) is not int or not 1 <= self.max_assignments <= MAX_PILOT_LEADS:
            raise ValueError('Pilot assignment cap must be explicitly set to 1–10')
        if type(self.max_queue) is not int or not 1 <= self.max_queue <= 1000:
            raise ValueError('Tier pilot queue target must be 1–1000; unlimited is disabled')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def writes_enabled():
    return os.environ.get('LANE2_TIER_ROUTING_ENABLED', '0') == '1'


def dispatch(args, runner=None):
    """Return False only for an untouched legacy invocation. Tier errors propagate."""
    options_used = bool(args.tier_rep or args.tier_lead or args.tier_output or args.tier_tagger or args.tier_use_staged_permissions
                        or args.tier_approval or args.tier_max_assignments is not None)
    if not args.tier_routing:
        if options_used:
            raise ValueError('Tier pilot options require --tier-routing; legacy assignment was not run')
        return False
    if args.reclaim or args.actionable_queue or args.actionable_owner or args.no_census:
        raise ValueError('Tier pilot cannot combine with legacy reclaim or queue modes')
    scope = PilotScope(tuple(args.tier_rep), tuple(args.tier_lead), args.tier_max_assignments, args.max_queue)
    if not args.tier_output:
        raise ValueError('Tier pilot requires a new --tier-output directory')
    if not args.tier_tagger:
        raise ValueError('Tier pilot requires the existing --tier-tagger path')
    if args.apply:
        if args.tier_use_staged_permissions:
            raise PermissionError('Staged rep permissions are preview-only')
        if not writes_enabled():
            raise PermissionError('Tier production writes disabled; LANE2_TIER_ROUTING_ENABLED defaults to 0')
        if not args.tier_approval:
            raise PermissionError('An exact approved tier preview is required for writes')
        if os.environ.get('WORKFLOW_HOLD_BACKEND') != 'postgres' or os.environ.get('WORKFLOW_HOLDS_REQUIRED') != '1':
            raise PermissionError('Tier writes require the mandatory shared PostgreSQL hold backend')
    if runner is None:
        from tier_routing_pilot import run
        runner = run
    runner(args, scope)
    return True


def build_plan(scope, snapshot, now):
    """Snapshot supplies fresh facts; allocate only inside the explicit pilot scope."""
    from tier_assignment import allocate_sample
    reps = [rep for rep in snapshot['reps'] if rep['close_user_id'] in scope.rep_ids]
    items = snapshot['items']
    if len(reps) != len(scope.rep_ids) or {item.lead['id'] for item in items} != set(scope.lead_ids) or len(items) != len(scope.lead_ids):
        raise ValueError('Incomplete or duplicate pilot identities/leads; refuse allocation')
    proposals = allocate_sample(items, reps, snapshot['closers'], now,
                                snapshot['memberships'], snapshot['counts'], scope.max_queue)
    assigned = 0
    for proposal in proposals:
        if not proposal['target_owner']:
            continue
        if assigned >= scope.max_assignments:
            proposal.update(target_owner=None, field_changes={}, pilot_cap_excluded=True)
        else:
            assigned += 1
    return {'schema_version': 1, 'company_id': COMPANY, 'organization_id': ORG,
            'proposed_at': now.isoformat(), 'scope': scope.__dict__,
            'rep_permission_source': snapshot['rep_permission_source'], 'proposals': proposals}


def authorize(plan, approval, scope, now):
    if not writes_enabled():
        raise PermissionError('Tier production writes disabled')
    age = now - timestamp(plan['proposed_at'])
    if (plan.get('company_id') != COMPANY or plan.get('organization_id') != ORG
            or plan.get('schema_version') != 1 or not timedelta(0) <= age <= timedelta(minutes=10)
            or plan.get('rep_permission_source') != 'stored'
            or digest(plan.get('scope')) != digest(scope.__dict__)):
        raise PermissionError('Fresh stored-permission pilot preview required')
    if (not isinstance(approval, dict) or approval.get('approved_plan_sha256') != digest(plan)
            or not isinstance(approval.get('approved_by'), str) or not approval['approved_by'].strip()
            or not approval.get('expires_at') or timestamp(approval['expires_at']) <= now
            or not all(approval.get('verified_integrations', {}).get(key) is True for key in INTEGRATIONS)):
        raise PermissionError('Exact unexpired pilot approval and integration receipts required')
    from hold_rollout import validate_manifest
    validate_manifest(approval.get('hold_deployment_manifest'),now)
    targets = [proposal for proposal in plan['proposals'] if proposal.get('target_owner')]
    ids = [proposal['lead_id'] for proposal in plan['proposals']]
    if (len(ids) != len(set(ids)) or set(ids) != set(scope.lead_ids)
            or len(targets) > scope.max_assignments
            or any(proposal['target_owner'] not in scope.rep_ids for proposal in targets)
            or any(proposal['field_changes'] != {'custom.' + OWNER: {'before': None, 'after': proposal['target_owner']}}
                   for proposal in targets)):
        raise PermissionError('Proposed writes exceed the approved pilot scope')
    return targets


def execute(plan, approval, scope, *, source, store, writer, clock, record):
    """Write once under the shared fence, with fresh eligibility and durable intent."""
    from tier_assignment import classify_for_assignment, eligible_reps, lead_owners
    from workflow_holds import custom
    targets = authorize(plan, approval, scope, clock())
    results = []
    for proposal in targets:
        # Recheck the kill switch and exact authorization before every lead.
        authorize(plan, approval, scope, clock())
        lead_id, owner = proposal['lead_id'], proposal['target_owner']
        patch = {'custom.' + OWNER: owner}
        if proposal['field_changes'] != {'custom.' + OWNER: {'before': None, 'after': owner}}:
            raise PermissionError('Pilot permits only an empty-to-rep Lead Owner patch')
        with store.serialized():
            if store.uncertain(lead_id):
                raise RuntimeError('Uncertain write: reconcile before retry')
            fresh_scope = PilotScope((owner,), (lead_id,), 1, scope.max_queue)
            fresh = source.read(fresh_scope, store=store, permission_source='stored')
            now = clock()
            item = next(item for item in fresh['items'] if item.lead['id'] == lead_id)
            decision = classify_for_assignment(item, fresh['closers'], now)
            reps = [rep for rep in fresh['reps'] if rep['close_user_id'] in scope.rep_ids]
            allowed = eligible_reps(item, decision, reps, now, fresh['memberships'])
            if lead_owners(item.lead) or owner not in {rep['close_user_id'] for rep in allowed}:
                raise RuntimeError('Lead or rep no longer eligible; preview again')
            count = fresh['counts'].get(owner)
            if type(count) is not int or count < 0 or count >= scope.max_queue:
                raise RuntimeError('Fresh capacity unavailable or full')
            authorize(plan, approval, scope, clock())
            latest = source.readback(lead_id)
            from tier_assignment import TIER, ENTRY, BUSINESS
            from workflow_holds import BATCH
            if (latest.get('id') != lead_id or latest.get('organization_id') != ORG
                    or latest['status_id'] != item.lead['status_id']
                    or any(custom(latest, key) != custom(item.lead, key) for key in (OWNER, TIER, ENTRY, BUSINESS, BATCH))
                    or store.holds(lead_id)):
                raise RuntimeError('Lead changed immediately before write; preview again')
            if not writes_enabled():
                raise PermissionError('Tier production writes disabled')
            intent = store.intent(lead_id, 'tier_assignment', {'path': '/lead/' + lead_id + '/', 'patch': patch})
            writer.put_owner(lead_id, owner)
            after = source.readback(lead_id)
            if (after.get('id') != lead_id or after.get('organization_id') != ORG
                    or custom(after, OWNER) != owner or after['status_id'] != item.lead['status_id']):
                raise RuntimeError('Status/owner readback differs; reconcile before retry')
            store.receipt(intent)
            result = {'lead_id': lead_id, 'target_owner': owner, 'state': 'verified'}
            record(result)
            results.append(result)
    return results
