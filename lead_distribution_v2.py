"""Unified tier/distribution planner. No provider imports or mutation entry point."""
from collections import Counter, defaultdict
from copy import deepcopy
from dataclasses import asdict, dataclass, is_dataclass, replace
from datetime import datetime, time, timedelta
import hashlib
import json
from zoneinfo import ZoneInfo

from close_status_policy import COMPANY, ORG, OWNER, Evidence, Roster
from lead_tier_policy import (POLICY_VERSION, TIER, ENTRY, PACIFIC, classify_age,
    earned_rep_stage, new_hire_week4, trial_rep_segment, validate_trial_roster)
from rep_access import instant, parse_access
from tier_assignment import (BUSINESS, VP, LeadFacts, classify_for_assignment,
    eligible_reps, lead_owners, protect, rep_available, stored_tier)
from workflow_holds import OWNER_SINCE, BATCH, MARKER, custom, project
from sms_rest_policy import Snapshot as RestSnapshot, return_to_rep, protection as rest_protection

EXCLUDED_TRIAL_WORK = frozenset(('inbound', 'webinar_follow_up', 'qa', 'closer', 'closers'))
FRESH_TIER = {'tier': 'Top', 'minimum_age_days': 0, 'maximum_age_days': 7,
    'genuine_opt_in_required': True, 'policy_version': POLICY_VERSION}
SNAPSHOT_MAX_AGE = timedelta(hours=24)


@dataclass(frozen=True)
class DistributionItem:
    facts: LeadFacts
    active_workflow: bool | None = None
    open_task: bool | None = None
    blocked_number: bool | None = None
    primary_ready: bool | None = None
    activity_complete: bool = False
    last_activity_at: str | None = None
    ownership_verified: bool = False
    workstream: str | None = None
    trial_selected: bool = False
    call_created_times: tuple | None = None


def digest(value):
    def serializable(item):
        if is_dataclass(item):
            return asdict(item)
        if isinstance(item, datetime):
            return item.isoformat()
        if isinstance(item, (set, frozenset)):
            return sorted(item)
        raise TypeError('Unsupported distribution evidence type')
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
        default=serializable).encode()).hexdigest()


def trial_window(config, now):
    if not config or not config.get('trial_policy', {}).get('enabled'):
        return False
    validate_trial_roster(config)
    if config.get('organization_id') != ORG or config.get('external_delivery_enabled') is not False:
        raise ValueError('Trial requires the exact org and disabled outbound delivery')
    zone = ZoneInfo(config['timezone'])
    begin = datetime.combine(datetime.fromisoformat(config['launch_date']).date(), time(), zone)
    end = datetime.combine(datetime.fromisoformat(config['trial_end_exclusive']).date(), time(), zone)
    if begin >= end:
        raise ValueError('Trial end must follow launch')
    return begin <= now < end


def validate_snapshot(snapshot, now):
    if now.tzinfo is None or snapshot.get('company_id') != COMPANY or snapshot.get('organization_id') != ORG:
        raise ValueError('Distribution requires the exact tenant and timezone-aware time')
    if not timedelta(0) <= now - instant(snapshot['captured_at']) <= SNAPSHOT_MAX_AGE:
        raise ValueError('Distribution snapshot is stale or from the future')
    if snapshot.get('complete') is not True:
        raise ValueError('Incomplete distribution snapshot')
    ids = [item.facts.lead['id'] for item in snapshot['items']]
    if len(ids) != len(set(ids)):
        raise ValueError('Duplicate lead snapshots')
    rep_ids = [rep.get('close_user_id') for rep in snapshot['reps'] if rep.get('close_user_id')]
    if len(rep_ids) != len(set(rep_ids)):
        raise ValueError('Duplicate rep identities')
    for uid, count in snapshot['counts'].items():
        if type(count) is not int or count < 0:
            raise ValueError('Queue counts must be verified nonnegative integers')
    for item in snapshot['items']:
        facts, evidence = item.facts, item.facts.evidence
        if (facts.lead.get('organization_id') != ORG or evidence.lead_id != facts.lead['id'] or
            evidence.status != facts.lead['status_id'] or tuple(sorted(evidence.owners)) != tuple(sorted(lead_owners(facts.lead)))):
            raise ValueError('Lead protection evidence context mismatch')
        for at in facts.outgoing_dials:
            if at.tzinfo is None or at > now:
                raise ValueError('Call evidence must be timezone-aware and not future dated')
        for at in item.call_created_times or ():
            if at.tzinfo is None or at > now:
                raise ValueError('Call creation evidence must be timezone-aware and not future dated')
        if item.last_activity_at is not None and instant(item.last_activity_at) > now:
            raise ValueError('Activity evidence must not be future dated')


def stage_reps(reps, now):
    staged, recommendations = [], []
    for original in reps:
        rep = deepcopy(original)
        earned = earned_rep_stage(rep, now)
        recommendations.append({'user_id': rep.get('close_user_id'), 'name': rep['rep'], **earned})
        # Explicit stored approvals always win; automatic staging is opt-in and
        # cannot overwrite a manually approved lower tier or Reactivation grant.
        existing = parse_access(rep.get('proposed_access'))
        if existing is None and rep.get('use_performance_ladder') is True and earned['tier']:
            rep['proposed_access'] = {'schemaVersion': 1, 'state': 'approved',
                'baseTier': earned['tier'], 'reactivationAccess': False,
                'approvedBy': 'LOCAL POLICY PREVIEW ONLY', 'approvedAt': now.isoformat(),
                'effectiveFrom': now.isoformat(), 'policyVersion': POLICY_VERSION}
            rep['permission_source'] = 'local_earned_policy_proposal'
        staged.append(rep)
    return staged, recommendations


def input_protection(item, roster, now):
    reason = protect(item.facts, roster, now)
    if reason:
        return reason
    if item.facts.calls_complete is not True or item.call_created_times is None:
        return 'call_history_incomplete'
    if item.activity_complete is not True:
        return 'activity_history_incomplete'
    for value, unknown, blocked in (
        (item.active_workflow, 'workflow_evidence_unavailable', 'active_workflow'),
        (item.open_task, 'task_evidence_unavailable', 'open_task'),
        (item.blocked_number, 'blocked_number_evidence_unavailable', 'blocked_number')):
        if type(value) is not bool:
            return unknown
        if value:
            return blocked
    return None


def staged_facts(item, classification):
    lead = deepcopy(item.facts.lead)
    lead['custom.' + TIER] = [classification.tier]
    return replace(item.facts, lead=lead, tier_freshness_verified=True)


def release_reason(item, roster, lane2_ids, now):
    reason = input_protection(item, roster, now)
    if reason:
        return reason
    owners = lead_owners(item.facts.lead)
    if len(owners) != 1 or owners[0] not in lane2_ids:
        return 'not_single_lane2_owner'
    if item.ownership_verified is not True or item.activity_complete is not True:
        return 'ownership_or_activity_evidence_unavailable'
    try:
        since = instant(custom(item.facts.lead, OWNER_SINCE))
        activity = instant(item.last_activity_at)
        created = instant(item.facts.lead['date_created'])
    except (KeyError, TypeError, ValueError):
        return 'activity_or_owner_since_unavailable'
    dates = [since, activity, created, *item.facts.outgoing_dials]
    if any(at > now for at in dates):
        return 'future_activity_evidence'
    if max(dates) > now - timedelta(days=7):
        return 'recent_activity_or_ownership'
    return None


def trial_role(rep, now, config):
    uid = rep.get('close_user_id')
    policy = config['trial_policy']
    for entry in policy['core_reps']:
        if uid and entry.get('user_id') == uid:
            return {'arm': entry['arm'], 'segment': 'core'}
    for entry in policy['new_hires']:
        if uid and entry.get('user_id') == uid:
            eligible = new_hire_week4(entry)
            day = now.astimezone(ZoneInfo(config['timezone'])).date().isoformat()
            if not eligible or day < entry['live_start_date']:
                return None
            return {'arm': 'predictive' if day >= eligible else 'power',
                'segment': 'new_hire_week4' if day >= eligible else 'new_hire_training'}
    return None


def trial_reps(item, facts, classification, decision, reps, memberships, now, config):
    if decision.state != 'candidate':
        return [], decision.reason
    if config['trial_policy'].get('fresh_lead_tier') != FRESH_TIER:
        return [], 'fresh_tier_policy_not_bound'
    if item.workstream is None:
        return [], 'workstream_evidence_unavailable'
    if item.workstream.casefold() in EXCLUDED_TRIAL_WORK:
        return [], 'excluded_trial_workstream'
    if item.primary_ready is not True:
        return [], 'primary_contact_number_unverified'
    today = now.astimezone(PACIFIC).date()
    if any(at.astimezone(PACIFIC).date() == today for at in item.call_created_times):
        return [], 'call_created_today'
    ordinary = {rep['close_user_id'] for rep in eligible_reps(facts, decision, reps, now, memberships)}
    result = []
    for rep in reps:
        if not rep_available(rep, now, memberships) or (custom(facts.lead, BUSINESS) or VP) != rep.get('offer'):
            continue
        role = trial_role(rep, now, config)
        if not role:
            continue
        if role['segment'] == 'core':
            # Exact trial membership grants temporary fresh access. Certification,
            # existing approved base access, offer and protections still apply.
            if classification.tier != 'Top' or not classification.genuine_opt_in:
                continue
        elif rep['close_user_id'] not in ordinary:
            continue
        elif role['segment'] == 'new_hire_training' and (classification.tier != 'Ramp' or classification.age_days < 30):
            continue
        result.append((rep, role))
    return result, 'trial_eligible' if result else 'no_eligible_trial_rep'


def sort_key(item, now):
    age = classify_age(item.facts.lead, now)
    priorities = {'Top': 0, 'Proven': 1, 'Ramp': 2, 'Reactivation': 3}
    return (priorities.get(age.tier, 9), age.age_days if age.age_days is not None else 99999,
        digest(item.facts.lead['id']))


def plan_rest_return(cycle, item, roster, reps, memberships, counts, now, *, max_queue=1000):
    """Local rest handoff using normal tier access, never temporary trial access.

    Only the requesting cycle's own hold is removed in the copied evidence. The
    durable workflow still owns any eventual write and hold release.
    """
    if type(max_queue) is not int or not 1 <= max_queue <= 1000:
        raise ValueError('Working queue target must be 1–1000')
    if cycle.get('lead_id') != item.facts.lead['id'] or cycle.get('phase') != 'return_requested':
        raise ValueError('Rest return requires the matching cycle and request phase')
    own = ('sms_rest', cycle['token'])
    snapshot = RestSnapshot(item.facts.lead, item.facts.evidence, holds=item.facts.holds or ())
    reason = rest_protection(snapshot, roster, now, cycle)
    if item.facts.holds is None:
        reason = 'durable_hold_verification_unavailable'
    elif own not in {(row['workflow'], row['token']) for row in item.facts.holds} or own not in MARKER.findall(str(custom(item.facts.lead, BATCH) or '')):
        reason = 'rest_projection_missing'
    if reason:
        return {'action': 'excluded', 'reason': reason, 'lead_patch': {}, 'release_hold': False}
    lead = deepcopy(item.facts.lead)
    lead['custom.' + BATCH] = project(custom(lead, BATCH), *own, acquire=False)
    cleared = replace(item, facts=replace(item.facts, lead=lead, holds=()))
    reason = input_protection(cleared, roster, now)
    age = classify_age(lead, now)
    if reason or age.tier is None:
        return {'action': 'excluded', 'reason': reason or age.reason, 'lead_patch': {}, 'release_hold': False}
    facts = staged_facts(cleared, age)
    decision = classify_for_assignment(facts, roster, now)
    staged, _ = stage_reps(reps, now)
    eligible = eligible_reps(facts, decision, staged, now, memberships)
    available = []
    for rep in eligible:
        uid = rep['close_user_id']
        count = counts.get(uid)
        if type(count) is int and 0 <= count < max_queue:
            available.append(uid)
    available.sort(key=lambda uid: (counts[uid], uid))
    preferred = cycle.get('original_owner')
    target = preferred if preferred in available else next(iter(available), None)
    if target is None:
        return {'action': 'keep', 'reason': 'tier_eligible_capacity_unavailable', 'lead_patch': {}, 'release_hold': False}
    result = asdict(return_to_rep(cycle, snapshot, roster, now, target, available))
    return {**result, 'mode': 'local_dry_run', 'target_owner': target,
        'eligible_reps': available, 'age': asdict(age), 'provider_requests': 0}


def build_distribution_plan(snapshot, now, *, trial_config=None, max_queue=1000):
    validate_snapshot(snapshot, now)
    if type(max_queue) is not int or not 1 <= max_queue <= 1000:
        raise ValueError('Working queue target must be 1–1000')
    roster, memberships = snapshot['closers'], set(snapshot['memberships'])
    reps, recommendations = stage_reps(snapshot['reps'], now)
    lane2_ids = {rep['close_user_id'] for rep in reps if rep.get('close_user_id')}
    trial_active = trial_window(trial_config, now)
    counts = dict(snapshot['counts'])
    decisions, assignments, releases, receipt_entries = [], [], [], []
    arm_allocations = Counter()
    offer_arms = defaultdict(set)
    for item in sorted(snapshot['items'], key=lambda row: sort_key(row, now)):
        age = classify_age(item.facts.lead, now)
        lead = item.facts.lead
        owners = lead_owners(lead)
        row = {'lead_id': lead['id'], 'age': asdict(age), 'stored_tier': stored_tier(lead),
            'tier_patch': {}, 'target_owner': None, 'owner_patch': {}, 'state': 'excluded', 'reason': None}
        reason = input_protection(item, roster, now)
        if reason:
            row['reason'] = reason
        elif owners:
            row.update(state='retain_owner', reason='existing_owner_keeps_lead')
            if age.tier and age.tier != stored_tier(lead):
                row['tier_patch'] = {'custom.' + TIER: [age.tier]}
            release_block = release_reason(item, roster, lane2_ids, now)
            row['release_block'] = release_block
            if release_block is None:
                releases.append({'lead_id': lead['id'], 'before_owner': owners[0],
                    'owner_generation': custom(lead, OWNER_SINCE), 'reason': 'seven_day_inactivity',
                    'owner_patch': {'custom.' + OWNER: None}, 'apply_ready': False})
            elif item.trial_selected and trial_active and age.tier and len(owners) == 1:
                facts = staged_facts(item, age)
                unowned = deepcopy(facts.lead)
                unowned['custom.' + OWNER] = None
                hypothetical = replace(facts, lead=unowned)
                eligible, why = trial_reps(item, hypothetical, age,
                    classify_for_assignment(hypothetical, roster, now), reps, memberships, now, trial_config)
                owned = [(rep, role) for rep, role in eligible if rep['close_user_id'] == owners[0]]
                if owned:
                    receipt_entries.append(receipt_entry(item, age, owned[0][0], owned[0][1], now, trial_config))
                else:
                    row['trial_exclusion'] = why if not eligible else 'owner_not_eligible_for_trial_inventory'
        elif not age.tier:
            row['reason'] = age.reason
        elif item.primary_ready is not True:
            row['reason'] = 'primary_contact_number_unverified'
        elif any(now - timedelta(days=7) <= at <= now for at in item.call_created_times):
            row['reason'] = 'recent_call_assignment_cooldown'
        else:
            facts = staged_facts(item, age)
            decision = classify_for_assignment(facts, roster, now)
            row.update(state=decision.state, reason=decision.reason)
            if age.tier != stored_tier(lead):
                row['tier_patch'] = {'custom.' + TIER: [age.tier]}
            if item.trial_selected:
                eligible, why = trial_reps(item, facts, age, decision, reps, memberships, now, trial_config) if trial_active else ([], 'trial_inactive')
            else:
                eligible = [(rep, None) for rep in eligible_reps(facts, decision, reps, now, memberships)]
                why = 'normal_tier_access'
            eligible_before_capacity = list(eligible)
            eligible = [(rep, role) for rep, role in eligible_before_capacity if rep['close_user_id'] in counts and counts[rep['close_user_id']] < max_queue]
            if not eligible:
                row['reason'] = 'capacity_unavailable_or_full' if eligible_before_capacity else 'no_eligible_normal_rep' if why == 'normal_tier_access' else why
            else:
                offer = custom(lead, BUSINESS) or VP
                stratum = (offer, str(custom(lead, ENTRY)), min(age.age_days // 2, 3))
                if item.trial_selected:
                    sizes = Counter(role['arm'] for rep, role in eligible if role['segment'] == 'core')
                    for arm in sizes:
                        offer_arms[offer].add(arm)
                    def rank(pair):
                        rep, role = pair
                        normalized = arm_allocations[(stratum, role['arm'])] / sizes[role['arm']] if role['segment'] == 'core' else sum(arm_allocations.values()) + 1
                        return normalized, counts[rep['close_user_id']], rep['close_user_id']
                    chosen, role = min(eligible, key=rank)
                    arm_allocations[(stratum, role['arm'])] += 1
                else:
                    chosen, role = min(eligible, key=lambda pair: (counts[pair[0]['close_user_id']], pair[0]['close_user_id']))
                uid = chosen['close_user_id']
                counts[uid] += 1
                row.update(state='proposed_assignment', reason=why, target_owner=uid,
                    owner_patch={'custom.' + OWNER: uid}, trial_role=role)
                assignments.append({'lead_id': lead['id'], 'user_id': uid, 'offer': offer,
                    'tier': age.tier, 'trial_role': role, 'apply_ready': False})
                if role:
                    receipt_entries.append(receipt_entry(item, age, chosen, role, now, trial_config))
        decisions.append(row)
    manifest = {'schema_version': 1, 'mode': 'local_dry_run', 'authority': 'lead_distribution_v2',
        'policy_version': POLICY_VERSION, 'organization_id': ORG, 'evaluated_at': now.isoformat(),
        'valid_before': (now + timedelta(hours=24)).isoformat(), 'entries': receipt_entries}
    manifest['sha256'] = digest(manifest)
    result = {'schema_version': 1, 'mode': 'local_dry_run', 'policy_version': POLICY_VERSION,
        'organization_id': ORG, 'company_id': COMPANY, 'captured_at': snapshot['captured_at'],
        'evaluated_at': now.isoformat(), 'snapshot_sha256': digest(snapshot), 'external_writes': 0, 'outbound_messages': 0,
        'decisions': decisions, 'assignments': assignments, 'releases': releases,
        'rep_stage_recommendations': recommendations, 'queue_counts_after_preview': counts,
        'unpaired_trial_offers': sorted(offer for offer, arms in offer_arms.items() if len(arms) < 2),
        'dialer_manifest': manifest,
        'summary': {'states': dict(Counter(row['state'] for row in decisions)),
            'reasons': dict(Counter(row['reason'] for row in decisions)),
            'proposed_assignments': len(assignments), 'proposed_releases': len(releases),
            'tier_updates': sum(bool(row['tier_patch']) for row in decisions)}}
    result['plan_sha256'] = digest(result)
    return result


def receipt_entry(item, classification, rep, role, now, config):
    tomorrow = now.astimezone(PACIFIC).date() + timedelta(days=1)
    return {'lead_id': item.facts.lead['id'], 'owner_user_id': rep['close_user_id'],
        'offer': rep['offer'], 'arm': role['arm'], 'segment': role['segment'],
        'tier': classification.tier, 'age_days': classification.age_days,
        'genuine_opt_in': classification.genuine_opt_in,
        'temporary_trial_access': role['segment'] == 'core',
        'valid_from': now.isoformat(), 'valid_before': min(now + timedelta(hours=24),
            datetime.combine(tomorrow, time(), PACIFIC),
            datetime.combine(datetime.fromisoformat(config['trial_end_exclusive']).date(), time(), ZoneInfo(config['timezone']))).isoformat()}


def validate_manifest(manifest):
    expected = manifest.get('sha256')
    body = {key: value for key, value in manifest.items() if key != 'sha256'}
    if (manifest.get('schema_version') != 1 or manifest.get('mode') != 'local_dry_run' or manifest.get('authority') != 'lead_distribution_v2' or
        manifest.get('policy_version') != POLICY_VERSION or manifest.get('organization_id') != ORG or
        expected != digest(body)):
        raise ValueError('Invalid local distribution receipt')
    ids = [row['lead_id'] for row in manifest['entries']]
    if len(ids) != len(set(ids)):
        raise ValueError('Duplicate receipt lead')
    for row in manifest['entries']:
        if row['arm'] not in ('power', 'predictive') or row['segment'] not in ('core', 'new_hire_week4', 'new_hire_training'):
            raise ValueError('Invalid receipt arm or segment')
        if not instant(row['valid_from']) < instant(row['valid_before']):
            raise ValueError('Invalid receipt interval')
        if row['segment'] == 'core' and (row['tier'] != 'Top' or not row['genuine_opt_in'] or not 0 <= row['age_days'] <= 7):
            raise ValueError('Core trial requires genuine first-week opt-ins')
    return {row['lead_id']: row for row in manifest['entries']}


def call_in_manifest(call, manifest):
    entries = validate_manifest(manifest)
    return call_matches_entry(call, entries.get(call.get('lead_id')))


def call_matches_entry(call, row):
    if not row or call.get('organization_id') != ORG or call.get('call_method') != row['arm']:
        return False
    at = instant(call['activity_at'])
    return instant(row['valid_from']) <= at < instant(row['valid_before'])


def filter_manifest_calls(calls, manifests):
    if not manifests:
        raise ValueError('At least one local distribution receipt is required')
    indexed = defaultdict(list)
    for manifest in manifests:
        for lead_id, row in validate_manifest(manifest).items():
            indexed[lead_id].append(row)
    return [call for call in calls if any(call_matches_entry(call, row)
        for row in indexed.get(call.get('lead_id'), []))]


def simulate_plan(plan, snapshot):
    """Execute only against a deep-copied in-memory snapshot; no provider exists."""
    if plan.get('mode') != 'local_dry_run' or plan.get('snapshot_sha256') != digest(snapshot):
        raise ValueError('Local simulation requires the exact original snapshot')
    expected = plan.get('plan_sha256')
    if expected != digest({key: value for key, value in plan.items() if key != 'plan_sha256'}):
        raise ValueError('Distribution plan changed after preview')
    leads = {item.facts.lead['id']: deepcopy(item.facts.lead) for item in snapshot['items']}
    for row in plan['decisions']:
        lead = leads[row['lead_id']]
        for key, value in row['tier_patch'].items():
            lead[key] = deepcopy(value)
        if row['owner_patch']:
            if lead_owners(lead):
                raise ValueError('Local assignment cannot replace an existing owner')
            lead.update(row['owner_patch'])
    for release in plan['releases']:
        lead = leads[release['lead_id']]
        if lead_owners(lead) != (release['before_owner'],) or custom(lead, OWNER_SINCE) != release['owner_generation']:
            raise ValueError('Local release owner generation changed')
        lead.update(release['owner_patch'])
    if {row['lead_id'] for row in plan['assignments']} & {row['lead_id'] for row in plan['releases']}:
        raise ValueError('A released lead cannot be reassigned in the same cycle')
    return {'mode': 'in_memory_simulation', 'provider_requests': 0, 'outbound_messages': 0,
        'owner_counts': dict(Counter(owner for lead in leads.values() for owner in lead_owners(lead))),
        'lead_count': len(leads), 'snapshot_sha256': plan['snapshot_sha256'],
        'original_snapshot_unchanged': plan['snapshot_sha256'] == digest(snapshot)}
