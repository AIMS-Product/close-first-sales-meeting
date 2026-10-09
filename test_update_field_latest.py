"""Offline tests for the scheduled updater's latest sales-call date."""

import importlib.util
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


class FakeSession:
    auth = None


with patch.dict(os.environ, {"CLOSE_API_KEY": "test-key"}), patch.dict(
    sys.modules, {"requests": types.SimpleNamespace(Session=FakeSession)}
):
    spec = importlib.util.spec_from_file_location(
        "update_field_under_test", Path(__file__).with_name("update_field.py")
    )
    updater = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(updater)


def meeting(lead_id, title, starts_at, status="completed"):
    return {
        "lead_id": lead_id,
        "title": title,
        "starts_at": starts_at,
        "status": status,
        "user_id": "user_qualifying",
    }


class LatestSalesCallUpdaterTests(unittest.TestCase):
    def test_rebooked_meetings_keep_first_date_and_move_latest_to_newest_active(self):
        desired = updater.calculate_desired_state([
            meeting("lead_a", "Vending Consult Call", "2026-10-08T18:00:00Z"),
            meeting("lead_a", "Vending Consult Call", "2026-10-11T18:00:00Z", "completed"),
            meeting("lead_a", "Vending Consult Call", "2026-10-14T18:00:00Z", "upcoming"),
            meeting("lead_a", "Vending Consult Call", "2026-10-18T18:00:00Z", "canceled-by-lead"),
        ])

        self.assertEqual(desired["lead_a"]["date"], "2026-10-08")
        self.assertEqual(desired["lead_a"]["latest_meeting_date"], "2026-10-14")

    def test_scraper_next_steps_is_qualifying_for_latest_date(self):
        desired = updater.calculate_desired_state([
            meeting("lead_a", "Vendingpreneur Next Steps", "2026-10-08T18:00:00Z"),
            meeting("lead_a", "Vendingpreneurs Pinnacle - Next Steps", "2026-10-17T18:00:00Z"),
        ])

        self.assertEqual(desired["lead_a"]["date"], "2026-10-08")
        self.assertEqual(desired["lead_a"]["latest_meeting_date"], "2026-10-17")

    def test_all_canceled_qualifying_meetings_fall_back_to_first_date(self):
        live = {"lead_a": {"first": "2026-10-08", "latest": "2026-10-20"}}
        writes = []
        with patch.object(
            updater, "api_get",
            return_value={updater.FIELD_DATE_KEY: "2026-10-08", updater.FIELD_LATEST_DATE_KEY: "2026-10-20"},
        ), patch.object(
            updater, "api_put", side_effect=lambda path, payload: writes.append((path, payload))
        ):
            updated, errors = updater.reconcile_latest_only({}, live, set())

        self.assertEqual((updated, errors), (1, 0))
        self.assertEqual(writes, [
            ("/lead/lead_a/", {updater.FIELD_LATEST_DATE_KEY: "2026-10-08"})
        ])

    def test_latest_only_reconciliation_rereads_and_never_writes_first_date(self):
        live = {"lead_a": {"first": "2026-10-08", "latest": "2026-10-08"}}
        desired = {"lead_a": {"latest_meeting_date": "2026-10-15"}}
        writes = []
        with patch.object(
            updater, "api_get",
            return_value={updater.FIELD_DATE_KEY: "2026-10-08", updater.FIELD_LATEST_DATE_KEY: "2026-10-08"},
        ) as get, patch.object(
            updater, "api_put", side_effect=lambda path, payload: writes.append((path, payload))
        ):
            updated, errors = updater.reconcile_latest_only(desired, live, set())

        self.assertEqual((updated, errors), (1, 0))
        get.assert_called_once_with(
            "/lead/lead_a/",
            params={"_fields": f"id,{updater.FIELD_DATE_KEY},{updater.FIELD_LATEST_DATE_KEY}"},
        )
        self.assertEqual(writes, [
            ("/lead/lead_a/", {updater.FIELD_LATEST_DATE_KEY: "2026-10-15"})
        ])
        self.assertNotIn(updater.FIELD_DATE_KEY, writes[0][1])

    def test_latest_only_reconciliation_skips_when_first_date_was_removed(self):
        live = {"lead_a": {"first": "2026-10-08", "latest": None}}
        with patch.object(
            updater, "api_get",
            return_value={updater.FIELD_DATE_KEY: None, updater.FIELD_LATEST_DATE_KEY: None},
        ), patch.object(updater, "api_put") as put:
            updated, errors = updater.reconcile_latest_only(
                {"lead_a": {"latest_meeting_date": "2026-10-15"}}, live, set()
            )

        self.assertEqual((updated, errors), (0, 0))
        put.assert_not_called()

    def test_cacheable_state_excludes_dynamic_latest_date(self):
        desired = {
            "date": "2026-10-08",
            "latest_meeting_date": "2026-10-15",
            "call_type": "Closer",
        }

        self.assertEqual(
            updater.cacheable_state(desired),
            {"date": "2026-10-08", "call_type": "Closer"},
        )

    def test_same_title_rules_pacific_dates_and_canceled_activity_filter(self):
        meetings = [
            meeting("lead_a", "Vending Consult Call", "2026-10-09T01:00:00Z"),
            meeting("lead_a", "Generic Next Steps Follow-Up", "2026-11-01T18:00:00Z"),
            meeting("lead_a", "VendHub Next Steps Call", "2026-10-20T18:00:00Z", "declined-by-org"),
            meeting("lead_a", "Vendingpreneurs Pinnacle - Next Steps", "2026-10-18T18:00:00Z", "upcoming"),
            meeting("lead_a", "Canceled: Vendingpreneurs Pinnacle - Next Steps", "2026-10-21T18:00:00Z"),
            meeting("lead_a", "VendHub Next Steps Call", "2026-10-25T18:00:00Z", "canceled-by-lead"),
            meeting("lead_b", "VendHub Next Steps Call", "2026-10-22T18:00:00Z", "cancelled-by-lead"),
        ]
        desired = updater.calculate_desired_state(meetings)

        self.assertEqual(desired["lead_a"]["date"], "2026-10-08")
        self.assertEqual(desired["lead_a"]["latest_meeting_date"], "2026-10-21")
        self.assertEqual(desired["lead_b"]["date"], "2026-10-22")
        self.assertIsNone(desired["lead_b"]["latest_meeting_date"])
        self.assertEqual(updater.classify_meeting(meetings[1])[0], None)
        self.assertEqual(updater.classify_meeting(meetings[2])[0], "vendhub_nextsteps")
        self.assertEqual(updater.classify_meeting(meetings[4])[0], "scraper")

    def test_live_first_date_search_paginates_and_rejects_incomplete_page(self):
        responses = [
            {"data": [{"lead": {"id": "lead_a", updater.FIELD_DATE_KEY: "2026-10-08"}}], "cursor": "next"},
            {"data": [{"lead": {"id": "lead_b", updater.FIELD_DATE_KEY: "2026-10-09"}}]},
        ]
        with patch.object(updater, "api_post", side_effect=responses) as post:
            leads = updater.fetch_leads_with_first_date()
        self.assertEqual(set(leads), {"lead_a", "lead_b"})
        self.assertEqual(post.call_count, 2)
        self.assertEqual(post.call_args_list[1].args[1]["cursor"], "next")

        with patch.object(updater, "api_post", return_value={"cursor": "next"}):
            with self.assertRaisesRegex(RuntimeError, "incomplete page"):
                updater.fetch_leads_with_first_date()

    def test_existing_lead_gets_only_latest_field_and_cache_does_not_churn(self):
        desired = updater.calculate_desired_state([
            meeting("lead_a", "Vending Consult Call", "2026-10-08T18:00:00Z"),
            meeting("lead_a", "Vending Consult Call", "2026-10-15T18:00:00Z"),
        ])
        cached = {"lead_a": updater.cacheable_state(desired["lead_a"])}
        live = {"lead_a": {"first": "2026-10-08", "latest": "2026-10-08"}}
        writes = []

        def get_lead(path, params=None):
            self.assertEqual(path, "/lead/lead_a/")
            return {
                updater.FIELD_DATE_KEY: "2026-10-08",
                updater.FIELD_LATEST_DATE_KEY: "2026-10-08",
            }

        with patch.object(updater, "api_get", side_effect=get_lead), patch.object(
            updater, "api_put", side_effect=lambda path, payload: writes.append((path, payload))
        ):
            new_cache = updater.routine_update(desired, cached, {}, live)

        self.assertEqual(new_cache, cached)
        self.assertEqual(writes, [
            ("/lead/lead_a/", {updater.FIELD_LATEST_DATE_KEY: "2026-10-15"})
        ])
        self.assertNotIn(updater.FIELD_DATE_KEY, writes[0][1])

    def test_live_first_date_fallback_when_no_qualifying_meeting(self):
        live = {"lead_a": {"first": "2026-09-03", "latest": None}}
        writes = []
        with patch.object(
            updater, "api_get",
            return_value={updater.FIELD_DATE_KEY: "2026-09-04", updater.FIELD_LATEST_DATE_KEY: None},
        ), patch.object(
            updater, "api_put", side_effect=lambda path, payload: writes.append(payload)
        ):
            updater.routine_update({}, {}, {}, live)

        self.assertEqual(writes, [{updater.FIELD_LATEST_DATE_KEY: "2026-09-04"}])

    def test_canceled_rebooking_moves_latest_back_without_writing_first(self):
        desired = updater.calculate_desired_state([
            meeting("lead_a", "Vending Consult Call", "2026-10-08T18:00:00Z"),
            meeting("lead_a", "Vending Consult Call", "2026-10-20T18:00:00Z", "canceled-by-lead"),
        ])
        cached = {"lead_a": updater.cacheable_state(desired["lead_a"])}
        live = {"lead_a": {"first": "2026-10-08", "latest": "2026-10-20"}}
        writes = []
        with patch.object(
            updater, "api_get",
            return_value={
                updater.FIELD_DATE_KEY: "2026-10-08",
                updater.FIELD_LATEST_DATE_KEY: "2026-10-20",
            },
        ), patch.object(updater, "api_put", side_effect=lambda path, payload: writes.append(payload)):
            updater.routine_update(desired, cached, {}, live)

        self.assertEqual(desired["lead_a"]["date"], "2026-10-08")
        self.assertEqual(writes, [{updater.FIELD_LATEST_DATE_KEY: "2026-10-08"}])

    def test_fresh_read_with_missing_first_date_skips_latest_write(self):
        live = {"lead_a": {"first": "2026-09-03", "latest": None}}
        with patch.object(
            updater, "api_get",
            return_value={updater.FIELD_DATE_KEY: None, updater.FIELD_LATEST_DATE_KEY: None},
        ), patch.object(updater, "api_put") as put:
            updater.routine_update({}, {}, {}, live)
        put.assert_not_called()

    def test_changed_meeting_lead_writes_first_and_latest_together(self):
        desired = updater.calculate_desired_state([
            meeting("lead_a", "Vending Consult Call", "2026-10-08T18:00:00Z"),
            meeting("lead_a", "Vending Consult Call", "2026-10-15T18:00:00Z"),
        ])["lead_a"]
        writes = []
        with patch.object(
            updater, "api_put", side_effect=lambda path, payload: writes.append(payload)
        ):
            updater.write_lead("lead_a", "Lead A", {}, desired, {})

        self.assertEqual(writes[0][updater.FIELD_DATE_KEY], "2026-10-08")
        self.assertEqual(writes[0][updater.FIELD_LATEST_DATE_KEY], "2026-10-15")


if __name__ == "__main__":
    unittest.main()
