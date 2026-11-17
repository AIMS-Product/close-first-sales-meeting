"""Reactivation booking attribution must preserve acquisition source."""
import unittest
from unittest.mock import patch
from test_update_field_latest import updater

class OriginalSourceTests(unittest.TestCase):
    def test_scraper_preserves_known_funnel_and_captures_origin(self):
        with patch.object(updater,'api_put') as write:
            updater.write_lead('lead_fixture','Fixture',{'funnel_name':'YouTube'},{'funnel_name':'Reactivation Scrapers'}, {})
        self.assertEqual(write.call_args.args[1],{updater.FIELD_ORIGINAL_SOURCE_KEY:'YouTube'})

    def test_scraper_cannot_invent_original_source_on_blank_lead(self):
        with patch.object(updater,'api_put') as write:
            updater.write_lead('lead_fixture','Fixture',{}, {'funnel_name':'Reactivation Scrapers'}, {})
        write.assert_not_called()

    def test_explicit_origin_is_never_overwritten(self):
        for funnel in ('YouTube','Reactivation Scrapers',None):
            with self.subTest(funnel=funnel),patch.object(updater,'api_put') as write:
                updater.write_lead('lead_fixture','Fixture',{'funnel_name':funnel,'original_source':'Website'}, {'funnel_name':'Reactivation Scrapers'}, {})
                write.assert_not_called()

    def test_vsl_only_fills_a_blank_origin_and_legacy_funnel(self):
        with patch.object(updater,'api_put') as write:
            updater.write_lead('lead_fixture','Fixture',{}, {'funnel_name':'VSL'}, {})
        self.assertEqual(write.call_args.args[1],{updater.FIELD_FUNNEL_KEY:'VSL',updater.FIELD_ORIGINAL_SOURCE_KEY:'VSL'})

    def test_existing_scraper_value_is_not_copied_to_original_source(self):
        with patch.object(updater,'api_put') as write:
            updater.write_lead('lead_fixture','Fixture',{'funnel_name':'Reactivation Scrapers'}, {'funnel_name':'Reactivation Scrapers'}, {})
        write.assert_not_called()

if __name__=='__main__':unittest.main()
