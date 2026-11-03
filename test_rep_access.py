from datetime import datetime,timedelta,timezone
import unittest
from rep_access import parse_access,pending_access,prepare_review,apply_approved_classifications,METADATA_KEY

NOW=datetime(2026,10,22,18,tzinfo=timezone.utc)


def approved(tier='Ramp',reactivation=False):
    return {'schemaVersion':1,'state':'approved','baseTier':tier,'reactivationAccess':reactivation,
        'approvedBy':'Dom Ellis','approvedAt':(NOW-timedelta(hours=1)).isoformat(),
        'effectiveFrom':NOW.isoformat()}


class RepAccessTests(unittest.TestCase):
    def test_ramp_access(self):
        self.assertEqual(parse_access(approved()).lead_tiers(NOW),('Ramp',))
    def test_proven_is_cumulative(self):
        self.assertEqual(parse_access(approved('Proven')).lead_tiers(NOW),('Ramp','Proven'))
    def test_top_excludes_reactivation(self):
        self.assertEqual(parse_access(approved('Top')).lead_tiers(NOW),('Ramp','Proven','Top'))
    def test_reactivation_is_independent_of_base(self):
        self.assertEqual(parse_access(approved('Ramp',True)).lead_tiers(NOW),('Ramp','Reactivation'))
        self.assertEqual(parse_access(approved('Top',True)).lead_tiers(NOW),('Ramp','Proven','Top','Reactivation'))
    def test_unclassified_is_not_defaulted(self):
        self.assertIsNone(parse_access(None));self.assertIsNone(parse_access(pending_access()))
    def test_missing_reactivation_decision_is_invalid(self):
        data=approved();data['reactivationAccess']=None
        with self.assertRaises(ValueError):parse_access(data)
    def test_boolean_is_not_coerced(self):
        for value in ['false','true',1,0]:
            with self.subTest(value=value),self.assertRaises(ValueError):parse_access(approved(reactivation=value))
    def test_approval_is_required(self):
        data=approved();data['approvedBy']=''
        with self.assertRaises(ValueError):parse_access(data)
    def test_bad_tier_is_rejected(self):
        with self.assertRaises(ValueError):parse_access(approved('Certified'))
    def test_future_access_does_not_apply_early(self):
        data=approved();data['effectiveFrom']=(NOW+timedelta(days=1)).isoformat()
        self.assertEqual(parse_access(data).lead_tiers(NOW),())
    def test_future_approval_does_not_apply_early(self):
        data=approved();data['approvedAt']=(NOW+timedelta(days=1)).isoformat()
        self.assertEqual(parse_access(data).lead_tiers(NOW),())
    def test_timestamp_needs_timezone(self):
        data=approved();data['effectiveFrom']='2026-10-22T18:00:00'
        with self.assertRaises(ValueError):parse_access(data)
    def source(self):
        return {'rows':[{'company_user_id':'id_1','display_name':'Example','status':'active','sales_team_lane':2,
             'close_accounts':[{'provider_user_id':'user_1','status':'active'}],
             'cohort_settings':{'startDate':'2026-01-01','certified':True},'configuration':{'offer':'VP'},
             'legacy_start_date':None,'legacy_offer':None,'possible_access_metadata':{}}]}
    def test_certification_and_tenure_do_not_grant_tier(self):
        rows=prepare_review(self.source(),[])
        self.assertEqual(rows[0]['proposed_access'],pending_access())
        self.assertIsNone(rows[0]['crm_ready_for_leads'])
    def test_other_metadata_does_not_grant_access(self):
        source=self.source();source['rows'][0]['possible_access_metadata']={'otherAccess':{'tier':'Top'}}
        self.assertFalse(prepare_review(source,[])[0]['access_record_valid'])
    def test_existing_approved_access_is_preserved(self):
        source=self.source();source['rows'][0]['possible_access_metadata'][METADATA_KEY]=approved('Proven')
        row=prepare_review(source,[])[0]
        self.assertTrue(row['access_record_valid']);self.assertEqual(row['proposed_access']['baseTier'],'Proven')
    def test_inactive_and_closers_are_not_classification_targets(self):
        source=self.source();source['rows'][0]['status']='inactive'
        self.assertEqual(prepare_review(source,[]),[])
        source['rows'][0]['status']='active';source['rows'][0]['sales_team_lane']=1
        self.assertEqual(prepare_review(source,[]),[])
    def test_ambiguous_close_identity_is_explicit(self):
        source=self.source();source['rows'][0]['close_accounts'].append({'provider_user_id':'user_2','status':'active'})
        row=prepare_review(source,[])[0]
        self.assertFalse(row['identity_ready']);self.assertIsNone(row['close_user_id'])
    def test_existing_readiness_remains_independent(self):
        config=[{'user_id':'user_1','ready_for_leads':False}]
        row=prepare_review(self.source(),config)[0]
        self.assertFalse(row['crm_ready_for_leads']);self.assertTrue(row['certified'])

    def approval(self):
        return {'companyId':'company','recordedAt':NOW.isoformat(),'approvedBy':'Anthony Kolodziej',
                'approvalReportedBy':'Dom Ellis','approvalEvidence':{'kind':'attachment'},
                'reps':[{'rep':'Example','baseTier':'Top','reactivationAccess':False}]}

    def test_approved_list_stages_classification_without_source_mutation(self):
        source=self.source();source['company_id']='company'
        review=prepare_review(source,[])
        result,excluded=apply_approved_classifications(source,review,self.approval())
        self.assertEqual(result[0]['proposed_access']['baseTier'],'Top')
        self.assertEqual(review[0]['proposed_access'],pending_access())
        self.assertEqual(source['rows'][0]['possible_access_metadata'],{})
        self.assertEqual(excluded,[])

    def test_inactive_approved_rep_is_not_reactivated(self):
        source=self.source();source['company_id']='company';source['rows'][0]['status']='inactive'
        result,excluded=apply_approved_classifications(source,[],self.approval())
        self.assertEqual(result,[]);self.assertEqual(excluded[0]['reason'],'inactive_or_outside_lane2')

    def test_names_are_matched_case_insensitively_without_fuzzy_guessing(self):
        source=self.source();source['company_id']='company';approval=self.approval()
        approval['reps'][0]['rep']='  EXAMPLE  '
        result,excluded=apply_approved_classifications(source,prepare_review(source,[]),approval)
        self.assertEqual(result[0]['proposed_access']['baseTier'],'Top');self.assertEqual(excluded,[])
        approval['reps'][0]['rep']='Examp'
        result,excluded=apply_approved_classifications(source,prepare_review(source,[]),approval)
        self.assertEqual(result[0]['proposed_access'],pending_access())
        self.assertEqual(excluded[0]['reason'],'roster_identity_missing_or_ambiguous')

    def test_duplicate_approval_rejected(self):
        source=self.source();source['company_id']='company';approval=self.approval()
        approval['reps'].append(dict(approval['reps'][0]))
        with self.assertRaises(ValueError):apply_approved_classifications(source,prepare_review(source,[]),approval)

    def test_cross_company_approval_rejected(self):
        source=self.source();source['company_id']='other'
        with self.assertRaises(ValueError):apply_approved_classifications(source,prepare_review(source,[]),self.approval())

    def test_ambiguous_provider_identity_is_excluded(self):
        source=self.source();source['company_id']='company'
        source['rows'][0]['close_accounts'].append({'provider_user_id':'user_other','status':'active'})
        result,excluded=apply_approved_classifications(source,prepare_review(source,[]),self.approval())
        self.assertEqual(result[0]['proposed_access'],pending_access())
        self.assertEqual(excluded[0]['reason'],'close_identity_not_unique')

    def test_actual_approved_roster_matches_active_snapshot(self):
        import json
        from pathlib import Path
        approval=json.loads(Path('config/rep_access_approved_20261022.json').read_text())
        self.assertEqual(len(approval['reps']),24)
        self.assertEqual(next(row for row in approval['reps'] if row['rep']=='Cassie Caraballo')['baseTier'],'Proven')
        self.assertEqual([row['rep'] for row in approval['reps'] if row['reactivationAccess']],['Vince Bartolini'])
        self.assertEqual(sum(row['baseTier']=='Top' for row in approval['reps']),4)
        self.assertEqual(sum(row['baseTier']=='Proven' for row in approval['reps']),3)


if __name__=='__main__':unittest.main()
