from contextlib import redirect_stdout
from io import StringIO
import os
import sys
import unittest
from unittest.mock import Mock, patch

with patch.dict(os.environ, {"CLOSE_API_KEY": "test"}):
    import update_lane2_reassignment as release


class ApprovedReleaseExclusionTests(unittest.TestCase):
    def test_release_skips_excluded_leads_and_rechecks_before_writing(self):
        owner_key = f"custom.{release.LEAD_OWNER_FIELD}"
        exclusion_key = f"custom.{release.APPROVED_RELEASE_EXCLUSION_FIELD}"
        base = {owner_key: "user_closer", "status_id": "stat_follow_up"}
        after_release = {**base, owner_key: None}
        search_results = {"data": [{"id": key} for key in (
            "lead_excluded", "lead_changed", "lead_eligible",
        )]}
        responses = [
            search_results,
            {**base, "custom": {"Approved_Release_Exclusion": "Yes"}},
            base, {**base, exclusion_key: ["Yes"]},
            base, base, after_release, after_release,
        ]
        output = StringIO()
        with (patch.object(sys, "argv", ["update_lane2_reassignment.py"]),
              patch.dict(os.environ, {"DRY_RUN": "false"}),
              patch.object(release, "save_state"),
              patch("workflow_holds.configured_store", return_value=None),
              patch.object(release.requests, "request", side_effect=[
                  Mock(status_code=200, json=lambda data=data: data) for data in responses
              ]) as request,
              redirect_stdout(output)):
            release.main()

        search = request.call_args_list[0].kwargs["json"]["query"]
        self.assertEqual(search, {"type": "and", "negate": False, "queries": [
            release.load_view_filters()["bucket2"]["query"], {
                "type": "field_condition", "negate": True,
                "field": {"type": "custom_field", "custom_field_id": release.APPROVED_RELEASE_EXCLUSION_FIELD},
                "condition": {"type": "term", "values": ["Yes"]},
            },
        ]})
        writes = [call for call in request.call_args_list if call.args[0] == "PUT"]
        self.assertEqual([call.args[1] for call in writes], [f"{release.BASE}/lead/lead_eligible/"])
        self.assertEqual(writes[0].kwargs["json"], {owner_key: None})
        self.assertIn("exclusion-skipped 2", output.getvalue())
