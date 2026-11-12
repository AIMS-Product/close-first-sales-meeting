from datetime import date, datetime, timezone, timedelta
from unittest import TestCase, mock
import unittest
import sync_lead_tier_tags as refresh
from lane2_policy import age_tier, require_dry_run


class TierRefreshTests(TestCase):
    def test_all_boundaries_and_real_opt_ins(self):
        cases={0:'Top',7:'Top',8:'Proven',29:'Proven',30:'Ramp',60:'Ramp',61:'Proven',179:'Proven',180:'Ramp',364:'Ramp',365:'Reactivation'}
        for age,target in cases.items():
            self.assertEqual(age_tier(age,'Typeform')[0],target)
            self.assertEqual(age_tier(age,'Typeform',True)[0],'Ramp')
        self.assertEqual(age_tier(7,'Rep-Outbound')[0],'Ramp')
        self.assertIsNone(age_tier(-1,'Typeform')[0])

    def test_close_creation_date_and_pacific_boundary(self):
        lead={'id':'lead_one','date_created':'2026-11-05T07:30:00Z','custom':{refresh.LEGACY_DATE_FIELD:'2020-01-01',refresh.ENTRY_SOURCE_FIELD:'Typeform'}}
        row=refresh.classify_leads([lead],date(2026,11,12),set())[0]
        self.assertEqual(row['effective_date'],'2026-11-04')
        self.assertEqual(row['proposed_tier'],'Proven')
        self.assertEqual(refresh.classify_leads([lead],date(2026,11,12),{'lead_one'})[0]['proposed_tier'],'Ramp')
        self.assertEqual(refresh.classify_leads([lead],date(2026,11,12))[0]['proposed_tier'],'REVIEW')

    def test_only_allowed_field_is_written_with_old_value_and_id_guard(self):
        with mock.patch.object(refresh,'request_json',return_value={'id':'bulkedit_test'}) as request:
            refresh.create_bulk_edit('secret','Proven','Ramp',['lead_one'])
        _,method,path,payload=request.call_args.args
        self.assertEqual((method,path),('POST','/bulk_action/edit/'))
        self.assertEqual(payload['custom_field_id'],refresh.LEAD_TIER_FIELD)
        self.assertEqual(payload['custom_field_operation'],'replace')
        self.assertFalse(payload['send_done_email'])
        self.assertEqual(payload['s_query'],refresh.make_guarded_query(['lead_one'],'Ramp'))
        self.assertEqual(set(payload),{'type','custom_field_id','custom_field_value','custom_field_operation','send_done_email','results_limit','s_query'})

    def test_import_census_requires_complete_scoped_results(self):
        with mock.patch.object(refresh,'request_json',return_value={'data':[{'id':'lead_outside'}]}):
            with self.assertRaises(RuntimeError):refresh.imported_lead_ids('secret',[{'id':'lead_one'}])
        with mock.patch.object(refresh,'request_json',return_value={'data':[],'count':1}):
            with self.assertRaises(RuntimeError):refresh.imported_lead_ids('secret',[{'id':'lead_one'}])

    def test_stale_apply_is_rejected_before_network(self):
        for args in [['--apply','--run-date','2020-01-01']]:
            with mock.patch.object(refresh,'request_json') as request:
                with self.assertRaises(SystemExit):refresh.main(args)
                request.assert_not_called()

    def test_resumed_export_must_be_recent_same_day_and_timezone_aware(self):
        now=datetime(2026,11,12,20,tzinfo=timezone.utc)
        refresh.require_fresh_export({'date_created':(now-timedelta(minutes=10)).isoformat()},now)
        for created in [None,'2026-11-12T19:00:00Z','2026-11-12T21:00:00Z','2026-11-12T19:59:00']:
            with self.assertRaises((RuntimeError,ValueError)):
                refresh.require_fresh_export({'date_created':created},now)

    def test_unrelated_policy_writes_stay_disabled(self):
        with self.assertRaises(RuntimeError):require_dry_run(True)

    def test_published_evidence_excludes_prospect_records(self):
        from summarize_lead_tier_refresh import aggregate_summary
        source={'mode':'apply','changes_proposed':1,'csv':'sensitive.csv','actions':[
            {'action_id':'bulkedit_example','new_tier':'Ramp','lead_ids':['lead_private'],'name':'Private Prospect'}]}
        result=aggregate_summary(source)
        self.assertEqual(result['changes_proposed'],1)
        self.assertEqual(result['actions'],[{'action_id':'bulkedit_example','new_tier':'Ramp'}])
        self.assertNotIn('csv',result)

if __name__=='__main__':unittest.main()
