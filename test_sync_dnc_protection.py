import contextlib
import io
import os
import unittest
from unittest.mock import Mock, patch

import sync_opp_lead_status as sync
import close_status_policy as policy


class DncProtectionTests(unittest.TestCase):
    def test_dnc_is_preserved_for_lost_and_won_opportunities(self):
        for opportunity_status in ('stat_bBWcww9IflskaleadKuK2E4SGFF4qy3IuBucrqo7H4u',
                                   'stat_WnFc0uhjcjV0cc3bVzdFVqDz7av6rbsOmOvHUsO6s03'):
            client = Mock()
            client.paginate_leads_with_opportunities.return_value = [{'id': 'lead',
                'status_id': sync.DNC_LEAD_STATUS_ID, 'opportunities': [{'id': 'opp',
                'status_id': opportunity_status, 'date_updated': '2026-11-09T00:00:00Z'}]}]
            with self.subTest(status=opportunity_status), patch.dict(os.environ, {'CLOSE_API_KEY': 'fake'}), \
                    patch.object(sync, 'CloseClient', return_value=client), \
                    patch.object(policy, 'load_exclusions', return_value=set()), \
                    contextlib.redirect_stdout(io.StringIO()):
                sync.run(False, 1, 0)
            client.set_lead_status.assert_not_called()

    def test_optout_arriving_after_scan_prevents_put(self):
        client = sync.CloseClient('fake')
        with patch.object(client, '_request', return_value={'id': 'lead', 'status_id': sync.DNC_LEAD_STATUS_ID}) as request:
            result = client.set_lead_status('lead', 'stat_lost')
        self.assertTrue(result['_dnc_guarded'])
        request.assert_called_once()
        self.assertEqual(request.call_args.args[0], 'GET')

    def test_unsuppressed_status_still_syncs_and_setting_dnc_is_allowed(self):
        client = sync.CloseClient('fake')
        with patch.object(client, '_request', side_effect=[{'id': 'lead', 'status_id': 'old'}, {'status_id': 'new'}]) as request:
            self.assertEqual(client.set_lead_status('lead', 'new')['status_id'], 'new')
            self.assertEqual([call.args[0] for call in request.call_args_list], ['GET', 'PUT'])
        with patch.object(client, '_request', return_value={'status_id': sync.DNC_LEAD_STATUS_ID}) as request:
            client.set_lead_status('lead', sync.DNC_LEAD_STATUS_ID)
            self.assertEqual(request.call_args.args[0], 'PUT')

    def test_invalid_fresh_identity_never_writes(self):
        client = sync.CloseClient('fake')
        with patch.object(client, '_request', return_value={'id': 'other'}) as request:
            with self.assertRaises(RuntimeError):
                client.set_lead_status('lead', 'new')
        request.assert_called_once()


if __name__ == '__main__':
    unittest.main()
