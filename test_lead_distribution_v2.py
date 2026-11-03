from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

from close_status_policy import OWNER, ORG, DNC
from distribution_fixture import NOW, example, item, rep
from distribution_local import main, snapshot_from_json
from lead_distribution_v2 import (FRESH_TIER, DistributionItem, build_distribution_plan,
    call_in_manifest, digest, filter_manifest_calls, simulate_plan, validate_manifest, plan_rest_return)
from lead_tier_policy import (LEGACY_DATE, TIER, ENTRY, age_band, classify_age,
    earned_rep_stage, new_hire_week4, two_goal_weeks)
from tier_assignment import BUSINESS, VP, classify_for_assignment, eligible_reps
from workflow_holds import OWNER_SINCE, BATCH, project


class RestReturnIntegrationTests(unittest.TestCase):
    def setUp(self):
        raw, _ = example()
        row = item('lead_rest_return', 100, trial=False)
        row['lead']['custom.' + BATCH] = project('Original', 'sms_rest', 'own', acquire=True)
        row['holds'] = [{'workflow': 'sms_rest', 'token': 'own'}]
        raw['leads'] = [row]
        raw['reps'] = [rep('user_ramp', 'Ramp'), rep('user_proven', 'Proven', 'Proven')]
        raw['memberships'] = ['user_ramp', 'user_proven']
        raw['counts'] = {'user_ramp': 0, 'user_proven': 1}
        self.raw = raw
        self.cycle = {'lead_id': 'lead_rest_return', 'token': 'own', 'phase': 'return_requested',
            'return_reason': 'ninety_days_elapsed', 'original_owner': 'user_ramp'}

    def plan(self):
        snapshot = snapshot_from_json(self.raw)
        return plan_rest_return(self.cycle, snapshot['items'][0], snapshot['closers'],
            snapshot['reps'], snapshot['memberships'], snapshot['counts'], NOW)

    def test_rest_duration_does_not_force_reactivation_or_unqualified_original_owner(self):
        original = deepcopy(self.raw)
        plan = self.plan()
        self.assertEqual(plan['target_owner'], 'user_proven')
        self.assertEqual(plan['lead_patch']['custom.' + TIER], ['Proven'])
        self.assertNotIn('STH1', plan['lead_patch']['custom.' + BATCH])
        self.assertTrue(plan['release_hold'])
        self.assertEqual(self.raw, original)

    def test_old_lead_needs_separate_reactivation_access(self):
        self.raw['leads'][0]['lead']['custom.' + LEGACY_DATE] = '2025-01-01'
        self.assertEqual(self.plan()['action'], 'keep')
        self.raw['reps'][0]['proposed_access']['reactivationAccess'] = True
        self.assertEqual(self.plan()['target_owner'], 'user_ramp')

    def test_other_hold_missing_hold_and_optout_keep_rest_hold(self):
        original = deepcopy(self.raw)
        for change in ('another_hold', 'unknown_holds', 'optout', 'missing_projection', 'open_task'):
            self.raw = deepcopy(original)
            row = self.raw['leads'][0]
            if change == 'another_hold': row['holds'].append({'workflow': 'qa', 'token': 'other'})
            elif change == 'unknown_holds': row['holds'] = None
            elif change == 'optout': row['evidence']['suppressions'] = ['stop']
            elif change == 'missing_projection': row['lead']['custom.' + BATCH] = 'Original'
            else: row['open_task'] = True
            with self.subTest(change=change):
                result = self.plan()
                self.assertFalse(result['release_hold'])
                self.assertEqual(result['lead_patch'], {})

    def test_capacity_and_signal_do_not_bypass_age_access(self):
        self.raw['counts']['user_proven'] = 1000
        self.cycle['return_reason'] = 'qualifying_signal'
        self.assertEqual(self.plan()['action'], 'keep')


class AgePolicyTests(unittest.TestCase):
    def test_all_band_boundaries_and_import_source_guard(self):
        for days, expected in ((0,'Top'),(7,'Top'),(8,'Proven'),(29,'Proven'),(30,'Ramp'),
            (60,'Ramp'),(61,'Proven'),(179,'Proven'),(180,'Ramp'),(364,'Ramp'),(365,'Reactivation')):
            with self.subTest(days=days):
                self.assertEqual(age_band(days), expected)
                row = item('lead_boundary', days)['lead']
                self.assertEqual(classify_age(row, NOW).tier, expected)
        for source in ('List-Swap-Ingest', None, ''):
            self.assertEqual(classify_age(item('lead_import', 2, source=source)['lead'], NOW).tier, 'Ramp')

    def test_legacy_age_prevents_fresh_import_reclassification(self):
        row = item('lead_legacy', 1)['lead']
        row['custom.' + LEGACY_DATE] = '2025-01-01'
        classification = classify_age(row, NOW)
        self.assertEqual(classification.tier, 'Reactivation')
        self.assertEqual(classification.date_source, 'Legacy Create Date')

    def test_legacy_date_forms_match_existing_tagger(self):
        row = item('lead_legacy_forms', 1)['lead']
        values = ['2025-01-01', '2025-01-01T00:00:00', '2025-01-01T08:00:00Z',
            datetime(2025,1,1,tzinfo=timezone.utc).timestamp()]
        for value in values:
            row['custom.' + LEGACY_DATE] = value
            with self.subTest(value=value):
                self.assertEqual(classify_age(row,NOW).effective_date,'2025-01-01')
                self.assertEqual(classify_age(row,NOW).tier,'Reactivation')
        row['custom.' + LEGACY_DATE] = 'invalid'
        self.assertIn('invalid legacy fallback',classify_age(row,NOW).date_source)

    def test_future_or_missing_creation_is_not_guessed(self):
        row = item('lead_bad')['lead']
        row['date_created'] = (NOW + timedelta(minutes=1)).isoformat()
        self.assertIsNone(classify_age(row, NOW).tier)
        row.pop('date_created')
        self.assertIsNone(classify_age(row, NOW).tier)

    def test_calendar_age_uses_pacific_date_across_dst(self):
        row = item('lead_dst')['lead']
        row['date_created'] = '2026-10-31T23:30:00-07:00'
        now = datetime.fromisoformat('2026-11-01T00:30:00-07:00')
        self.assertEqual(classify_age(row, now).age_days, 1)

    def test_original_24_hour_overflow_is_preserved(self):
        raw, _ = example()
        snapshot = snapshot_from_json(raw)
        facts = snapshot['items'][0].facts
        fresh_lead = dict(facts.lead, **{'custom.' + TIER: ['Top']})
        facts = replace(facts, lead=fresh_lead, tier_freshness_verified=True)
        decision = classify_for_assignment(facts, snapshot['closers'], NOW)
        self.assertTrue(decision.overflow)
        people = [rep('user_proven', 'Proven Example', 'Proven'), rep('user_ramp', 'Ramp Example')]
        eligible = eligible_reps(facts, decision, people, NOW, {'user_proven', 'user_ramp'})
        self.assertEqual([person['close_user_id'] for person in eligible], ['user_proven'])


class RepLadderTests(unittest.TestCase):
    def setUp(self):
        self.rep = rep('user_example', 'Example')
        self.rep['live_start_date'] = '2026-10-19'
        self.rep['performance'] = {'source': 'synthetic complete weeks', 'complete': True,
            'weeks': [{'week_start': '2026-10-25', 'booked': 10, 'goal': 10, 'complete': True, 'paused': False},
                      {'week_start': '2026-11-01', 'booked': 10, 'goal': 10, 'complete': True, 'paused': False}]}

    def test_day22_and_two_consecutive_goal_weeks(self):
        self.assertEqual(earned_rep_stage(self.rep, NOW)['tier'], 'Proven')
        self.rep['performance']['weeks'][0]['booked'] = 9
        self.assertEqual(earned_rep_stage(self.rep, NOW)['tier'], 'Ramp')

    def test_tenure_alone_does_not_promote(self):
        self.rep['performance'] = None
        self.assertEqual(earned_rep_stage(self.rep, NOW)['tier'], 'Ramp')

    def test_paused_or_missing_week_does_not_count_twice(self):
        self.rep['performance']['weeks'][0]['paused'] = True
        self.assertFalse(two_goal_weeks(self.rep['performance'], NOW))
        self.rep['performance']['weeks'] = self.rep['performance']['weeks'][1:]
        self.assertFalse(two_goal_weeks(self.rep['performance'], NOW))

    def test_duplicate_week_rejected(self):
        self.rep['performance']['weeks'].append(self.rep['performance']['weeks'][0])
        with self.assertRaises(ValueError):
            earned_rep_stage(self.rep, NOW)

    def test_top_after60_requires_own_verified_conversion(self):
        self.rep['live_start_date'] = '2026-09-01'
        self.rep['performance']['conversion'] = {'complete': True, 'user_id': 'user_example',
            'start': '2026-10-26T00:00:00Z', 'end_exclusive': '2026-11-09T00:00:00Z', 'booked': 5, 'dials': 200}
        self.assertEqual(earned_rep_stage(self.rep, NOW)['tier'], 'Top')
        self.rep['performance']['conversion']['user_id'] = 'user_someone_else'
        self.assertEqual(earned_rep_stage(self.rep, NOW)['tier'], 'Proven')

    def test_week4_and_top_are_distinct(self):
        self.assertEqual(new_hire_week4({'live_start_date': '2026-11-02'}), '2026-11-23')
        self.rep['live_start_date'] = '2026-11-02'
        self.rep['performance'] = None
        self.assertEqual(earned_rep_stage(self.rep, datetime(2026,11,23,17,tzinfo=timezone.utc))['tier'], 'Ramp')


class DistributionTests(unittest.TestCase):
    def setUp(self):
        self.raw, self.config = example()

    def plan(self, raw=None, config=None, at=NOW):
        return build_distribution_plan(snapshot_from_json(raw or self.raw), at, trial_config=config or self.config)

    def test_balanced_fresh_cohorts_and_separate_training(self):
        plan = self.plan()
        entries = plan['dialer_manifest']['entries']
        main = [row for row in entries if row['segment'] == 'core']
        self.assertEqual(len(main), 8)
        self.assertEqual(sum(row['arm'] == 'predictive' for row in main), 4)
        self.assertEqual(sum(row['arm'] == 'power' for row in main), 4)
        self.assertTrue(all(row['tier'] == 'Top' and row['genuine_opt_in'] for row in main))
        training = [row for row in entries if row['segment'] == 'new_hire_training']
        self.assertEqual([row['lead_id'] for row in training], ['lead_training'])
        self.assertTrue(all(row['age_days'] >= 30 for row in training))

    def test_trial_does_not_promote_permanent_rep_tiers_or_mutate_inputs(self):
        before = deepcopy(self.raw)
        self.plan()
        self.assertEqual(self.raw, before)
        self.assertTrue(all(row['proposed_access']['baseTier'] == 'Ramp' for row in self.raw['reps']))

    def test_existing_owner_is_preserved_even_when_tier_changes(self):
        plan = self.plan()
        decision = next(row for row in plan['decisions'] if row['lead_id'] == 'lead_owned')
        self.assertEqual(decision['state'], 'retain_owner')
        self.assertEqual(decision['tier_patch']['custom.' + TIER], ['Reactivation'])
        self.assertEqual(decision['owner_patch'], {})
        self.assertNotIn('lead_owned', [row['lead_id'] for row in plan['releases']])

    def test_seven_day_release_is_separate_from_assignment(self):
        plan = self.plan()
        self.assertEqual([row['lead_id'] for row in plan['releases']], ['lead_release'])
        self.assertNotIn('lead_release', [row['lead_id'] for row in plan['assignments']])
        snapshot = snapshot_from_json(self.raw)
        self.assertTrue(simulate_plan(plan, snapshot)['original_snapshot_unchanged'])

    def test_all_hold_and_suppression_paths_block(self):
        for field, value in (('active_workflow',True),('open_task',True),('blocked_number',True),
            ('active_workflow',None),('open_task',None),('primary_ready',None),('activity_complete',False),
            ('call_created_times',None),('calls_complete',False)):
            raw = deepcopy(self.raw)
            raw['leads'] = [item('lead_gate')]
            raw['leads'][0][field] = value
            with self.subTest(field=field,value=value):
                plan = self.plan(raw)
                self.assertEqual(plan['assignments'], [])
        plan = self.plan()
        excluded = {row['lead_id']: row for row in plan['decisions']}
        for lead_id in ('lead_suppressed', 'lead_held', 'lead_unknown_holds', 'lead_imported'):
            self.assertIsNone(excluded[lead_id]['target_owner'])

    def test_recent_calls_do_not_refill_an_unowned_queue(self):
        raw = deepcopy(self.raw)
        row = item('lead_recent', days=40, trial=False)
        row['call_created_times'] = [(NOW - timedelta(days=2)).isoformat()]
        raw['leads'] = [row]
        plan = self.plan(raw)
        self.assertEqual(plan['decisions'][0]['reason'], 'recent_call_assignment_cooldown')

    def test_owned_lead_called_today_does_not_enter_trial_receipt(self):
        raw = deepcopy(self.raw)
        row = item('lead_owned_today', owner='user_predictive')
        row['call_created_times'] = [(NOW - timedelta(minutes=10)).isoformat()]
        raw['leads'] = [row]
        plan = self.plan(raw)
        self.assertEqual(plan['decisions'][0]['state'], 'retain_owner')
        self.assertEqual(plan['dialer_manifest']['entries'], [])

    def test_inactivity_release_needs_current_owner_generation(self):
        for change in ({'ownership_verified':False}, {'open_task':True}, {'active_workflow':True}):
            raw = deepcopy(self.raw)
            row = next(row for row in raw['leads'] if row['lead']['id'] == 'lead_release')
            row.update(change)
            self.assertEqual(self.plan(raw)['releases'], [])

    def test_all_excluded_workstreams_stay_out_of_trial(self):
        for stream in ('inbound','webinar_follow_up','QA','closers'):
            raw = deepcopy(self.raw)
            raw['leads'] = [item('lead_workstream')]
            raw['leads'][0]['workstream'] = stream
            self.assertEqual(self.plan(raw)['assignments'], [])

    def test_certification_and_membership_still_gate_trial_exception(self):
        raw = deepcopy(self.raw)
        raw['reps'][0]['certified'] = False
        raw['memberships'].remove('user_power')
        self.assertFalse(any(row['trial_role']['segment'] == 'core' for row in self.plan(raw)['assignments']))

    def test_missing_counts_never_assume_empty_queues(self):
        raw = deepcopy(self.raw)
        raw['counts'] = {}
        self.assertEqual(self.plan(raw)['assignments'], [])

    def test_offer_is_never_crossed_and_unpaired_offer_is_visible(self):
        raw = deepcopy(self.raw)
        other = 'Publishing Profits Academy (PPA)'
        raw['leads'] = [item('lead_ppa')]
        raw['leads'][0]['lead']['custom.' + BUSINESS] = other
        self.assertEqual(self.plan(raw)['assignments'], [])
        raw['reps'][0]['offer'] = other
        plan = self.plan(raw)
        self.assertEqual(plan['assignments'][0]['user_id'], 'user_predictive')
        self.assertEqual(plan['unpaired_trial_offers'], [other])

    def test_trial_expiry_no_fallback_to_normal_distribution(self):
        cfg = deepcopy(self.config)
        cfg['trial_end_exclusive'] = '2026-11-09'
        with self.assertRaises(ValueError):
            self.plan(config=cfg)
        cfg['launch_date'],cfg['trial_end_exclusive'] = '2026-11-01','2026-11-08'
        plan = self.plan(config=cfg)
        self.assertEqual(plan['assignments'], [])

    def test_new_hire_week4_has_no_automatic_top_access(self):
        raw = deepcopy(self.raw)
        now = datetime(2026,11,23,17,tzinfo=timezone.utc)
        raw['captured_at'] = raw['closer_roster_captured_at'] = now.isoformat()
        raw['leads'] = [item('lead_hire_older',40)]
        plan = self.plan(raw, at=now)
        entry = plan['dialer_manifest']['entries'][0]
        self.assertEqual(entry['segment'], 'new_hire_week4')
        self.assertEqual(entry['arm'], 'predictive')
        self.assertEqual(entry['tier'], 'Ramp')

    def test_duplicate_inputs_wrong_tenant_and_stale_snapshot_rejected(self):
        raw = deepcopy(self.raw)
        raw['leads'].append(raw['leads'][0])
        with self.assertRaises(ValueError): self.plan(raw)
        raw = deepcopy(self.raw)
        raw['organization_id'] = 'orga_other'
        with self.assertRaises(ValueError): self.plan(raw)
        with self.assertRaises(ValueError): self.plan(at=NOW + timedelta(days=2))

    def test_evidence_owner_mismatch_rejected(self):
        raw = deepcopy(self.raw)
        raw['leads'][0]['evidence']['owners'] = ['user_other']
        with self.assertRaises(ValueError): self.plan(raw)

    def test_malformed_evidence_cannot_be_converted_into_clear_state(self):
        for field, value in (('holds',''),('holds',[{}]),('call_created_times',''),
                             ('active_workflow','false'),('outgoing_dials',None)):
            raw = deepcopy(self.raw)
            raw['leads'][0][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                self.plan(raw)
        raw = deepcopy(self.raw)
        raw['leads'][0]['evidence'].pop('complete')
        plan = self.plan(raw)
        row = next(row for row in plan['decisions'] if row['lead_id'] == raw['leads'][0]['lead']['id'])
        self.assertEqual(row['reason'], 'incomplete_protection_evidence')

    def test_deterministic_plan_independent_of_input_order(self):
        plan = self.plan()
        raw = deepcopy(self.raw)
        raw['leads'].reverse()
        other = self.plan(raw)
        self.assertEqual(plan['assignments'], other['assignments'])
        self.assertEqual(plan['dialer_manifest'], other['dialer_manifest'])

    def test_simulation_rejects_changed_snapshot_or_plan(self):
        snapshot = snapshot_from_json(self.raw)
        plan = build_distribution_plan(snapshot, NOW, trial_config=self.config)
        plan['assignments'][0]['user_id'] = 'user_other'
        with self.assertRaises(ValueError): simulate_plan(plan, snapshot)
        plan = build_distribution_plan(snapshot, NOW, trial_config=self.config)
        snapshot['counts']['user_power'] += 1
        with self.assertRaises(ValueError): simulate_plan(plan, snapshot)

    def test_receipt_limits_lead_method_and_calendar_day(self):
        manifest = self.plan()['dialer_manifest']
        entry = next(row for row in manifest['entries'] if row['segment']=='core')
        call = {'lead_id': entry['lead_id'], 'organization_id': ORG, 'call_method': entry['arm'],
            'activity_at': (NOW + timedelta(minutes=1)).isoformat()}
        self.assertTrue(call_in_manifest(call, manifest))
        self.assertFalse(call_in_manifest(dict(call, call_method='regular'), manifest))
        self.assertFalse(call_in_manifest(dict(call, activity_at='2026-11-10T00:00:00-08:00'), manifest))
        self.assertEqual(filter_manifest_calls([call], [manifest]), [call])
        manifest['entries'][0]['tier'] = 'Ramp'
        with self.assertRaises(ValueError): validate_manifest(manifest)

    def test_local_cli_runs_without_network_even_if_live_switch_is_on(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder/'snapshot.json').write_text(json.dumps(self.raw))
            (folder/'trial.json').write_text(json.dumps(self.config))
            with (patch.object(socket.socket, 'connect', side_effect=AssertionError('Network forbidden')),
                  patch.dict('os.environ', {'LANE2_TIER_ROUTING_ENABLED':'1'})):
                result = main(['--snapshot',str(folder/'snapshot.json'),'--trial-config',str(folder/'trial.json'),
                    '--at',NOW.isoformat(),'--output',str(folder/'out')])
            self.assertEqual(result,0)
            summary=json.loads((folder/'out/summary.json').read_text())
            self.assertEqual(summary['provider_requests'],0)
            self.assertEqual(summary['outbound_messages'],0)
            self.assertEqual((folder/'out/plan.json').stat().st_mode & 0o777,0o600)


if __name__ == '__main__':
    unittest.main()
