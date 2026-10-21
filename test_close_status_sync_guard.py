import contextlib
import io
import os
import unittest
from unittest.mock import Mock, patch
from datetime import datetime, timezone
import close_status_policy as policy
import close_status_maintenance as maintenance
import sync_opp_lead_status as sync


class ExistingSyncTests(unittest.TestCase):
    def run_sync(self, *, allowed=True, error=None, held=False, status=None):
        lead={'id':'lead_test','status_id':policy.GENERAL,'opportunities':[{'id':'oppo_test','status_id':status or policy.OPP_HOT,'date_updated':'2026-10-20T00:00:00Z'}]}
        client=Mock();client.paginate_leads_with_opportunities.return_value=[lead]
        client.set_lead_status.return_value={'status_id':policy.HOT}
        guard=Mock(return_value=allowed)
        factory=Mock(side_effect=error)if error else Mock(return_value=guard)
        with patch.dict(os.environ,{'CLOSE_API_KEY':'fake'}),patch.object(sync,'CloseClient',return_value=client),patch.object(policy,'load_exclusions',return_value={policy.held_hash(lead['id'])}if held else set()),patch.object(maintenance,'hot_sync_guard',factory),contextlib.redirect_stdout(io.StringIO()):
            sync.run(dry_run=True,limit=1,lookback_days=0)
        return client,guard,factory

    def test_reversion_is_blocked_when_hot_policy_rejects(self):
        client,guard,_=self.run_sync(allowed=False)
        guard.assert_called_once_with('lead_test');client.set_lead_status.assert_not_called()

    def test_guard_unavailable_never_restores_hot(self):
        client,_,_=self.run_sync(error=RuntimeError('unavailable'))
        client.set_lead_status.assert_not_called()

    def test_held_excluded_before_guard_or_any_write(self):
        client,_,factory=self.run_sync(held=True)
        factory.assert_not_called();client.set_lead_status.assert_not_called()

    def test_dry_run_stays_read_only_after_allowed_hot(self):
        client,guard,_=self.run_sync(allowed=True)
        guard.assert_called_once();client.set_lead_status.assert_not_called()

    def test_other_sync_targets_do_not_require_hot_evidence(self):
        client,_,factory=self.run_sync(status=policy.OPP_NONRESPONSIVE)
        factory.assert_not_called();client.set_lead_status.assert_not_called()

    def test_hot_sync_never_uses_a_write_enabled_client(self):
        roster=policy.Roster(frozenset({'user_closer'}),datetime.now(timezone.utc))
        reader=Mock();reader.read.return_value=policy.Evidence('lead_test',policy.HOT,(),(),latest_inbound=datetime.now(timezone.utc))
        with patch.object(maintenance,'load_roster',return_value=roster),patch.object(maintenance,'load_exclusions',return_value=set()),patch.object(maintenance,'SnapshotReader',return_value=reader),patch.object(maintenance,'CloseClient')as client:
            allowed=maintenance.hot_sync_guard('fake')
            self.assertTrue(allowed('lead_test'));client.assert_called_once_with('fake')


if __name__=='__main__':unittest.main()
