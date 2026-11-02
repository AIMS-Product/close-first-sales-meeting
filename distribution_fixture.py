"""Synthetic local distribution fixture; no credentials or real lead data."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

from close_status_policy import COMPANY, ORG, OWNER, DNC
from lead_distribution_v2 import FRESH_TIER
from lead_tier_policy import TIER, ENTRY
from tier_assignment import BUSINESS, VP
from workflow_holds import OWNER_SINCE

NOW = datetime(2026, 11, 9, 17, tzinfo=timezone.utc)
STATUS = 'stat_synthetic'


def rep(uid, name, tier='Ramp', offer=VP):
    return {'rep': name, 'close_user_id': uid, 'offer': offer, 'identity_ready': True,
        'certified': True, 'live_start_date': '2026-09-01',
        'proposed_access': {'schemaVersion': 1, 'state': 'approved', 'baseTier': tier,
            'reactivationAccess': False, 'approvedBy': 'SYNTHETIC FIXTURE',
            'approvedAt': '2026-10-01T00:00:00Z', 'effectiveFrom': '2026-10-01T00:00:00Z'}}


def item(lead_id, days=2, *, source='Website-Apply', owner=None, trial=True):
    lead = {'id': lead_id, 'organization_id': ORG, 'status_id': STATUS,
        'date_created': (NOW - timedelta(days=days)).isoformat(),
        'custom.' + TIER: ['Ramp'], 'custom.' + ENTRY: source, 'custom.' + OWNER: owner,
        'custom.' + BUSINESS: VP,
        'contacts': [{'id': 'cont_' + lead_id, 'phones': [{'phone': '+12025550999'}]}]}
    return {'lead': lead, 'evidence': {'lead_id': lead_id, 'status': STATUS,
        'owners': [owner] if owner else [], 'complete': True}, 'holds': [],
        'outgoing_dials': [], 'call_created_times': [], 'calls_complete': True, 'active_workflow': False,
        'open_task': False, 'blocked_number': False, 'primary_ready': True,
        'activity_complete': True, 'last_activity_at': (NOW - timedelta(days=days)).isoformat(),
        'ownership_verified': False, 'workstream': 'lane2_outbound', 'trial_selected': trial}


def example():
    reps = [rep('user_predictive', 'Example Predictive Rep'), rep('user_power', 'Example Control Rep'),
        rep('user_hire', 'Example New Hire')]
    rows = [item('lead_fresh_' + str(i), days=(i % 4) + 1) for i in range(8)]
    rows.append(item('lead_training', days=40))
    imported = item('lead_imported', source='List-Swap-Ingest')
    rows.append(imported)
    suppressed = item('lead_suppressed')
    suppressed['lead']['status_id'] = DNC
    suppressed['evidence']['status'] = DNC
    rows.append(suppressed)
    held = item('lead_held')
    held['holds'] = [{'workflow': 'qa', 'token': 'synthetic'}]
    rows.append(held)
    unknown = item('lead_unknown_holds')
    unknown['holds'] = None
    rows.append(unknown)
    owned = item('lead_owned', days=400, owner='user_power', trial=False)
    owned['lead']['custom.' + TIER] = ['Top']
    owned['last_activity_at'] = (NOW - timedelta(days=1)).isoformat()
    owned['lead']['custom.' + OWNER_SINCE] = (NOW - timedelta(days=30)).isoformat()
    owned['ownership_verified'] = True
    rows.append(owned)
    stale = item('lead_release', days=100, owner='user_power', trial=False)
    stale['lead']['custom.' + OWNER_SINCE] = (NOW - timedelta(days=30)).isoformat()
    stale['last_activity_at'] = (NOW - timedelta(days=10)).isoformat()
    stale['ownership_verified'] = True
    rows.append(stale)
    snapshot = {'schema_version': 1, 'company_id': COMPANY, 'organization_id': ORG,
        'captured_at': NOW.isoformat(), 'complete': True, 'leads': rows, 'reps': reps,
        'counts': {row['close_user_id']: 0 for row in reps},
        'memberships': [row['close_user_id'] for row in reps],
        'closer_ids': ['user_synthetic_closer'], 'closer_roster_captured_at': NOW.isoformat()}
    root = Path(__file__).resolve().parents[2]
    config = json.loads((root / 'predictive-dialer-trial/config.example.json').read_text())
    config.update(organization_id=ORG, brand='Example Brand', require_distribution_manifest=True,
        last_rep_field_id='cf_lastrep', dnc_field_id='cf_dnc', sms_sent_field_id='cf_sent',
        fallback_sms_user_id='user_predictive', sms_text='Synthetic follow-up preview only.',
        number_brands={'+12025550101': 'Example Brand', '+12025550102': 'Example Brand'},
        primary_number_by_user={'user_predictive': '+12025550101', 'user_power': '+12025550102'},
        saved_search_ids={'predictive': ['save_predictive'], 'power': ['save_power']})
    config['trial_policy'].update(enabled=True, fresh_lead_tier=dict(FRESH_TIER),
        core_reps=[{'name': 'Example Predictive Rep', 'user_id': 'user_predictive', 'arm': 'predictive'},
            {'name': 'Example Control Rep', 'user_id': 'user_power', 'arm': 'power'}],
        new_hires=[{'name': 'Example New Hire', 'user_id': 'user_hire', 'live_start_date': '2026-11-02'}],
        new_hire_saved_search_ids={'predictive': ['save_hire_predictive'], 'power': ['save_hire_power']})
    return snapshot, config


def calls_from_manifest(manifest):
    rows = []
    for i, entry in enumerate(manifest['entries']):
        new = entry['segment'] != 'core'
        rows.append({'id': f'acti_fixture_{i}', 'organization_id': ORG,
            'lead_id': entry['lead_id'], 'user_id': entry['owner_user_id'],
            'activity_at': (datetime.fromisoformat(entry['valid_from']) + timedelta(minutes=1)).isoformat(),
            'direction': 'outbound', 'status': 'completed', 'call_method': entry['arm'],
            'dialer_saved_search_id': ('save_hire_' if new else 'save_') + entry['arm'],
            'duration': 0, 'disposition': 'no-answer',
            'local_phone': '+12025550101' if entry['arm'] == 'predictive' else '+12025550102'})
    return rows


def main():
    import argparse
    from distribution_local import private_write
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    snapshot, config = example()
    private_write(args.output / 'snapshot.json', snapshot)
    private_write(args.output / 'trial-config.json', config)
    print('Synthetic local inputs written; no provider access.')


if __name__ == '__main__':
    main()
