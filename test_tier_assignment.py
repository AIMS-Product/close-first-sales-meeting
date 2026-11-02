from dataclasses import replace
from datetime import datetime,timedelta,timezone
import json
import unittest
from close_status_policy import Evidence,Roster,Opportunity,OWNER,ORG
from rep_access import pending_access
from tier_assignment import (LeadFacts,TIER,ENTRY,BATCH,BUSINESS,OVERRIDE,VP,
    classify_for_assignment,eligible_reps,allocate_sample,view_candidates)

NOW=datetime(2026,10,23,18,tzinfo=timezone.utc)
ROSTER=Roster(frozenset({'user_closer'}),NOW)
STATUS='stat_l8ATqabgtlrL7EKL5o0EL8ufBg8UMPxMJJP1AXI9V9i'


def facts(tier='Ramp',source='Rep-Outbound',age_hours=72,owner=None):
    lead={'id':'lead_test','organization_id':ORG,'status_id':STATUS,
        'date_created':(NOW-timedelta(hours=age_hours)).isoformat(),
        'custom.'+TIER:[tier],'custom.'+ENTRY:source,'custom.'+OWNER:owner}
    return LeadFacts(lead,Evidence('lead_test',STATUS,(owner,) if owner else ()),holds=(),tier_freshness_verified=True)


def rep(name,tier='Ramp',reactivation=False,offer=VP):
    return {'rep':name,'close_user_id':'user_'+name,'offer':offer,'identity_ready':True,'certified':True,
        'crm_ready_for_leads':True,'proposed_access':{'schemaVersion':1,'state':'approved',
        'baseTier':tier,'reactivationAccess':reactivation,'approvedBy':'Dom',
        'approvedAt':(NOW-timedelta(days=1)).isoformat(),'effectiveFrom':(NOW-timedelta(days=1)).isoformat()}}


class TierAssignmentTests(unittest.TestCase):
    def setUp(self):
        self.reps=[rep('ramp'),rep('proven','Proven'),rep('top','Top'),rep('vince','Top',True)]
        self.members={r['close_user_id'] for r in self.reps}
    def eligible(self,item):
        decision=classify_for_assignment(item,ROSTER,NOW)
        return [r['rep'] for r in eligible_reps(item,decision,self.reps,NOW,self.members)]
    def test_ramp_can_go_to_all_base_tiers(self):
        self.assertEqual(self.eligible(facts()),['ramp','proven','top','vince'])
    def test_proven_requires_proven_or_top(self):
        self.assertEqual(self.eligible(facts('Proven')),['proven','top','vince'])
    def test_fresh_optin_requires_top(self):
        self.assertEqual(self.eligible(facts('Top','Website-Apply',12)),['top','vince'])
    def test_365_plus_requires_separate_permission(self):
        self.assertEqual(self.eligible(facts('Reactivation')),['vince'])
    def test_imported_fake_top_routes_to_ramp(self):
        item=facts('Top','List-Swap-Ingest',12)
        decision=classify_for_assignment(item,ROSTER,NOW)
        self.assertEqual(decision.assignment_tier,'Ramp')
        self.assertEqual(self.eligible(item),['ramp','proven','top','vince'])
        self.assertEqual(item.lead['custom.'+TIER],['Top'])
    def test_missing_source_never_grants_top_exclusivity(self):
        self.assertEqual(classify_for_assignment(facts('Top',None,12),ROSTER,NOW).assignment_tier,'Ramp')
    def test_undialed_opens_to_proven_at_exactly_24h(self):
        self.assertEqual(self.eligible(facts('Top','Typeform',24)),['proven','top','vince'])
        self.assertEqual(self.eligible(facts('Top','Typeform',23.99)),['top','vince'])
    def test_outbound_dial_stops_overflow_even_if_not_connected(self):
        item=replace(facts('Top','Webinar',48),outgoing_dials=(NOW-timedelta(hours=47),))
        self.assertEqual(self.eligible(item),['top','vince'])
    def test_call_before_lead_creation_does_not_stop_overflow(self):
        item=replace(facts('Top','Webinar',48),outgoing_dials=(NOW-timedelta(hours=100),))
        self.assertEqual(self.eligible(item),['proven','top','vince'])
    def test_missing_call_evidence_blocks_top_routing(self):
        item=replace(facts('Top','Webinar'),calls_complete=False)
        self.assertEqual(classify_for_assignment(item,ROSTER,NOW).reason,'dial_history_incomplete')
    def test_existing_owner_keeps_lead_across_aging(self):
        item=facts('Reactivation',owner='user_ramp')
        result=allocate_sample([item],self.reps,ROSTER,NOW,self.members,{})[0]
        self.assertEqual(result['decision']['state'],'retain_owner')
        self.assertIsNone(result['target_owner']);self.assertEqual(result['field_changes'],{})
    def test_owned_fresh_lead_is_not_reassigned_after_24_hours(self):
        item=facts('Top','Website-Apply',48,owner='user_top')
        self.assertEqual(classify_for_assignment(item,ROSTER,NOW).state,'retain_owner')
    def test_stale_tag_blocks_new_assignment(self):
        self.assertEqual(classify_for_assignment(replace(facts(),tier_freshness_verified=False),ROSTER,NOW).reason,'lead_tier_stale_or_unverified')
    def test_stale_tag_does_not_transfer_existing_owner(self):
        item=replace(facts(owner='user_ramp'),tier_freshness_verified=False)
        self.assertEqual(classify_for_assignment(item,ROSTER,NOW).state,'retain_owner')
    def test_missing_hold_coverage_blocks(self):
        self.assertEqual(classify_for_assignment(replace(facts(),holds=None),ROSTER,NOW).reason,'durable_hold_verification_unavailable')
    def test_qa_sms_education_recovery_holds_block(self):
        for workflow in ['qa','sms_rest','education','recovery']:
            with self.subTest(workflow=workflow):
                item=replace(facts(),holds=({'workflow':workflow,'expires_at':'2000-01-01'},))
                self.assertEqual(classify_for_assignment(item,ROSTER,NOW).reason,'shared_durable_hold')
    def test_marker_without_durable_row_blocks(self):
        item=facts();item.lead['custom.'+BATCH]='batch [STH1 sms_rest abc]'
        self.assertEqual(classify_for_assignment(item,ROSTER,NOW).reason,'shared_hold_marker')
    def test_permanent_release_marker_does_not_block(self):
        item=facts();item.lead['custom.'+BATCH]='batch [STL1 lane1_release abc]'
        self.assertEqual(classify_for_assignment(item,ROSTER,NOW).state,'candidate')
    def test_stop_dnc_history_survives_later_status_reset(self):
        item=facts();item=replace(item,evidence=replace(item.evidence,suppressions=('historical_stop',)))
        self.assertEqual(self.eligible(item),[])
    def test_current_dnc_blocks(self):
        item=facts();item.lead['status_id']='stat_U9MI7pqsvIjceTv3pCU7b1EghO8Q83h1HUcL6fGVyi6'
        self.assertEqual(self.eligible(item),[])
    def test_active_closer_owner_blocks(self):
        self.assertEqual(classify_for_assignment(facts(owner='user_closer'),ROSTER,NOW).reason,'active_closer_owner')
    def test_active_closer_opportunity_blocks(self):
        item=facts();opportunity=Opportunity('opp','s',NOW,'active','Follow up','user_closer','pipe')
        item=replace(item,evidence=replace(item.evidence,opportunities=(opportunity,)))
        self.assertEqual(self.eligible(item),[])
    def test_old_lost_opportunity_owner_does_not_block(self):
        item=facts();opportunity=Opportunity('opp','s',NOW,'lost','Lost','user_closer','pipe')
        item=replace(item,evidence=replace(item.evidence,opportunities=(opportunity,)))
        self.assertEqual(len(self.eligible(item)),4)
    def test_legacy_won_status_is_protected(self):
        item=facts();opportunity=Opportunity('opp','s',NOW,'active','OLD Deal Won','user_rep','pipe')
        item=replace(item,evidence=replace(item.evidence,opportunities=(opportunity,)))
        self.assertEqual(self.eligible(item),[])
    def test_booked_lead_blocks(self):
        item=facts();item=replace(item,evidence=replace(item.evidence,future_meeting=True))
        self.assertEqual(self.eligible(item),[])
    def test_incomplete_evidence_blocks(self):
        item=facts();item=replace(item,evidence=replace(item.evidence,complete=False))
        self.assertEqual(self.eligible(item),[])
    def test_explicit_exclusions_block(self):
        self.assertEqual(self.eligible(replace(facts(),explicitly_excluded=True)),[])
    def test_stale_closer_roster_blocks(self):
        self.assertEqual(classify_for_assignment(facts(),replace(ROSTER,captured_at=NOW-timedelta(days=2)),NOW).state,'excluded')
    def test_wrong_tenant_blocks(self):
        item=facts();item.lead['organization_id']='other'
        self.assertEqual(self.eligible(item),[])
    def test_no_unapproved_reps(self):
        self.reps[0]['proposed_access']=pending_access()
        self.assertNotIn('ramp',self.eligible(facts()))
    def test_uncertified_reps_do_not_receive_leads(self):
        self.reps[0]['certified']=False;self.reps[1]['crm_ready_for_leads']=False
        self.assertEqual(self.eligible(facts()),['proven','top','vince'])
    def test_legacy_false_flag_does_not_block_certified_rep(self):
        self.reps[0]['crm_ready_for_leads']=False
        self.assertIn('ramp',self.eligible(facts()))
    def test_certification_is_not_inferred_from_legacy_true_flag(self):
        self.reps[0]['certified']=None
        self.assertNotIn('ramp',self.eligible(facts()))
    def test_missing_capacity_is_not_assumed_to_be_zero(self):
        result=allocate_sample([facts()],self.reps,ROSTER,NOW,self.members,{})[0]
        self.assertIsNone(result['target_owner'])
        self.assertEqual(result['capacity_unverified_for'],['ramp','proven','top','vince'])
    def test_revoked_membership_blocks(self):
        self.members.remove('user_ramp')
        self.assertNotIn('ramp',self.eligible(facts()))
    def test_other_offer_never_gets_vp_leads(self):
        self.reps[0]['offer']='The Land Geek (TLG)'
        self.assertNotIn('ramp',self.eligible(facts()))
    def test_multi_tag_ambiguity_blocks(self):
        item=facts();item.lead['custom.'+TIER]=['Ramp','Proven']
        self.assertEqual(self.eligible(item),[])
    def test_at_capacity_does_not_get_more(self):
        result=allocate_sample([facts()],self.reps,ROSTER,NOW,self.members,
            {r['close_user_id']:1000 for r in self.reps})[0]
        self.assertIsNone(result['target_owner']);self.assertTrue(result['capacity_blocked'])
    def test_furthest_behind_goes_first(self):
        result=allocate_sample([facts()],self.reps,ROSTER,NOW,self.members,
            {'user_ramp':1000,'user_proven':500,'user_top':999,'user_vince':900})[0]
        self.assertEqual(result['target_owner'],'user_proven')
        self.assertEqual(result['field_changes'],{'custom.'+OWNER:{'before':None,'after':'user_proven'}})
    def test_queue_does_not_filter_existing_owner_by_lead_tier(self):
        queue=view_candidates([VP],ROSTER.closer_ids)[0]
        encoded=json.dumps(queue)
        self.assertNotIn(TIER,encoded);self.assertIn('STH1',encoded);self.assertIn('CURRENT_USER',encoded)
        self.assertNotIn('Recapture State',encoded);self.assertEqual(queue['activation'],'off')


if __name__=='__main__':unittest.main()
