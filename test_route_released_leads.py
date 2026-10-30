import copy
import os
import sys
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import route_released_leads as routing

NOW = datetime(2026, 10, 30, 15, tzinfo=timezone.utc)
TERMINAL = {"no_show", "canceled"}
PROTECTED = {"won", "dnc", "disqualified"}


def snapshot(outcome=None, status="completed", starts_at="2026-09-01T15:00:00Z"):
    return {"lead": {"id": "lead_a", "organization_id": routing.ORGANIZATION_ID,
                     "status_id": "lost", "num_upcoming_meetings": 0,
                     "custom." + routing.OWNER_FIELD: routing.DOM_ID,
                     "custom." + routing.BATCH_FIELD: "Released - Original Closer",
                     "custom." + routing.STATE_FIELD: "Deep-Nurture"},
            "meetings": [{"id": "meeting_a", "lead_id": "lead_a", "starts_at": starts_at,
                          "status": status, "outcome_id": outcome}],
            "opportunities": [], "lane2_state": "Deep-Nurture", "complete": True}


def plan(value):
    return routing.handoff_plan(value, now=NOW, terminal_statuses=TERMINAL, suppressed_statuses=PROTECTED)


class AttendanceRoutingTests(unittest.TestCase):
    def test_native_completed_routes_to_naria_without_recency_limit(self):
        result = plan(snapshot(routing.COMPLETED_OUTCOME, starts_at="2025-01-01T15:00:00Z"))
        self.assertEqual(result["destination"], "qa")
        self.assertEqual(result["changes"], {"custom." + routing.OWNER_FIELD: routing.NARIA_ID,
                                             "custom." + routing.STATE_FIELD: "QA Triage"})

    def test_calendar_completed_alone_is_not_attendance(self):
        self.assertEqual(plan(snapshot())["destination"], "lane2")

    def test_all_non_completed_outcomes_return_to_lane2(self):
        for outcome in (None, "", "scheduled", "no_show", "cancelled", "rescheduled"):
            with self.subTest(outcome=outcome):
                result = plan(snapshot(outcome))
                self.assertEqual(result["destination"], "lane2")
                self.assertIsNone(result["changes"]["custom." + routing.OWNER_FIELD])

    def test_no_meetings_returns_to_lane2(self):
        value = snapshot(); value["meetings"] = []
        self.assertEqual(plan(value)["destination"], "lane2")

    def test_completed_followup_after_missed_first_booking_can_enter_qa(self):
        value = snapshot("no_show")
        value["meetings"].append({"id": "meeting_b", "starts_at": "2026-10-01T15:00:00Z",
                                  "status": "completed", "outcome_id": routing.COMPLETED_OUTCOME})
        self.assertEqual(plan(value)["destination"], "qa")

    def test_current_terminal_status_wins_over_old_attendance(self):
        for status in TERMINAL:
            value = snapshot(routing.COMPLETED_OUTCOME)
            value["lead"]["status_id"] = status
            value["lane2_state"] = "Booked"
            result = plan(value)
            self.assertEqual(result["destination"], "lane2")
            self.assertEqual(result["changes"]["custom." + routing.STATE_FIELD], "Active-Nurture")

    def test_upcoming_or_in_progress_booking_is_held(self):
        for status, starts in (("upcoming", "2026-11-01T15:00:00Z"), ("in-progress", "2026-10-30T14:59:00Z")):
            self.assertEqual(plan(snapshot(None, status, starts))["destination"], "held")

    def test_future_completed_outcome_cannot_prove_attendance(self):
        result = plan(snapshot(routing.COMPLETED_OUTCOME, starts_at="2026-11-01T15:00:00Z"))
        self.assertNotEqual(result["destination"], "qa")

    def test_canceled_future_booking_does_not_count_as_completion(self):
        self.assertEqual(plan(snapshot("cancelled", "canceled", "2026-11-01T15:00:00Z"))["destination"], "lane2")

    def test_protected_lead_statuses_and_won_deals_hold(self):
        for status in PROTECTED:
            value = snapshot(routing.COMPLETED_OUTCOME);value["lead"]["status_id"] = status
            self.assertEqual(plan(value)["destination"], "held")
        for opportunity in ({"status_type": "won"}, {"status_id": next(iter(routing.WON_OPPORTUNITY_STATUSES))}):
            value = snapshot(); value["opportunities"] = [opportunity]
            self.assertEqual(plan(value)["destination"], "held")

    def test_existing_naria_leads_other_owners_and_qa_holds_untouched(self):
        for owner in (routing.NARIA_ID, "closer", None):
            value = snapshot();value["lead"]["custom." + routing.OWNER_FIELD] = owner
            self.assertEqual(plan(value)["changes"], {})
        value = snapshot();value["lead"]["custom." + routing.STATE_FIELD] = "QA Triage"
        self.assertEqual(plan(value)["changes"], {})

    def test_missing_release_tag_wrong_org_and_workflow_markers_hold(self):
        for batch in ("", "manual", "Released - Closer | [STH1 sms_rest token]", "Released - Closer | [STL1 lane1_release token]"):
            value = snapshot();value["lead"]["custom." + routing.BATCH_FIELD] = batch
            self.assertEqual(plan(value)["changes"], {})
        value = snapshot();value["lead"]["organization_id"] = "other"
        self.assertEqual(plan(value)["changes"], {})

    def test_overrides_incomplete_reads_invalid_dates_and_missing_counts_hold(self):
        for override in ("Yes", "unexpected"):
            value = snapshot();value["lead"]["custom." + routing.OVERRIDE_FIELD] = override
            self.assertEqual(plan(value)["changes"], {})
        value = snapshot();value["complete"] = False
        self.assertEqual(plan(value)["changes"], {})
        for starts in (None, "invalid", "2026-09-01T15:00:00"):
            self.assertEqual(plan(snapshot(starts_at=starts))["changes"], {})
        value = snapshot();del value["lead"]["num_upcoming_meetings"]
        self.assertEqual(plan(value)["changes"], {})

    def test_booked_active_deal_is_held_for_nonterminal_lead(self):
        for outcome in (None, routing.COMPLETED_OUTCOME):
            value = snapshot(outcome);value["lane2_state"] = "Booked"
            self.assertEqual(plan(value)["changes"], {})

    def test_planner_does_not_mutate_source_or_write_other_fields(self):
        value = snapshot(routing.COMPLETED_OUTCOME); before = copy.deepcopy(value)
        self.assertEqual(set(plan(value)["changes"]), {"custom." + routing.OWNER_FIELD, "custom." + routing.STATE_FIELD})
        self.assertEqual(value, before)


class ReadAndWriteTests(unittest.TestCase):
    def reader(self, values):
        reader = SimpleNamespace(lane2=SimpleNamespace(QA_TRIAGE_RELEASE_STATUSES=TERMINAL, SUPPRESS_STATUSES=PROTECTED),
                                 preflight=Mock(), candidates=Mock(return_value=[{"id": "lead_a"}]),
                                 snapshot=Mock(side_effect=values), write_once=Mock())
        return reader

    def test_dry_run_has_no_write(self):
        reader = self.reader([snapshot(routing.COMPLETED_OUTCOME)])
        result = routing.run(reader)
        self.assertEqual(result["verified_writes"], 0);reader.write_once.assert_not_called()

    def test_apply_rechecks_and_writes_once(self):
        value = snapshot(routing.COMPLETED_OUTCOME);reader = self.reader([value, copy.deepcopy(value)])
        self.assertEqual(routing.run(reader, apply=True)["verified_writes"], 1)
        self.assertEqual(reader.snapshot.call_count, 2);reader.write_once.assert_called_once()

    def test_changed_outcome_owner_or_meeting_before_write_is_skipped(self):
        for change in ("outcome", "owner", "meeting"):
            value = snapshot(routing.COMPLETED_OUTCOME);fresh = copy.deepcopy(value)
            if change == "outcome":fresh["meetings"][0]["outcome_id"] = None
            if change == "owner":fresh["lead"]["custom." + routing.OWNER_FIELD] = "closer"
            if change == "meeting":fresh["lead"]["num_upcoming_meetings"] = 1
            reader = self.reader([value, fresh]);result = routing.run(reader, apply=True)
            reader.write_once.assert_not_called();self.assertEqual(result["destinations"]["changed_before_write"], 1)

    def test_zero_write_budget_and_preflight_failure_do_not_write(self):
        reader = self.reader([snapshot()]);routing.run(reader, apply=True, max_writes=0)
        reader.write_once.assert_not_called()
        reader = self.reader([]);reader.preflight.side_effect = RuntimeError("preflight")
        with self.assertRaisesRegex(RuntimeError, "preflight"):routing.run(reader, apply=True)
        reader.snapshot.assert_not_called();reader.write_once.assert_not_called()

    def test_empty_staging_queue_does_not_add_dependency_to_normal_reconciler(self):
        reader = self.reader([]);reader.candidates.return_value = []
        self.assertEqual(routing.run(reader, apply=True)["verified_writes"], 0)
        reader.preflight.assert_not_called();reader.snapshot.assert_not_called()

    def test_uncertain_write_stops_without_retry(self):
        value = snapshot();reader = self.reader([value, copy.deepcopy(value)])
        reader.write_once.side_effect = TimeoutError("uncertain")
        with self.assertRaises(TimeoutError):routing.run(reader, apply=True)
        reader.write_once.assert_called_once()

    def test_related_reader_pages_every_meeting_and_rejects_other_leads(self):
        reader = routing.HandoffReader(SimpleNamespace(SKIP_CAP=10000))
        reader.get = Mock(side_effect=[{"data": [{"id": "one", "lead_id": "lead_a"}], "has_more": True},
                                       {"data": [{"id": "two", "lead_id": "lead_a"}], "has_more": False}])
        self.assertEqual(len(reader.related("/activity/meeting/", "lead_a", "id")), 2)
        self.assertEqual(reader.get.call_args_list[1].args[1]["_skip"], 1)
        reader.get = Mock(return_value={"data": [{"id": "one", "lead_id": "lead_other"}], "has_more": False})
        with self.assertRaisesRegex(RuntimeError, "scope"):reader.related("/activity/meeting/", "lead_a", "id")

    def test_duplicate_missing_and_empty_pagination_fail_closed(self):
        reader = routing.HandoffReader(SimpleNamespace(SKIP_CAP=10000))
        for page in ({"data": [], "has_more": True}, {"data": []},
                     {"data": [{"id": "same", "lead_id": "lead_a"}] * 2, "has_more": False}):
            reader.get = Mock(return_value=page)
            with self.assertRaises(RuntimeError):reader.related("/activity/meeting/", "lead_a", "id")

    def test_snapshot_rejects_lead_changes_during_evidence_read(self):
        original = snapshot()["lead"]
        fresh = copy.deepcopy(original);fresh["custom." + routing.OWNER_FIELD] = "other"
        reader = routing.HandoffReader(SimpleNamespace(FALLTHROUGH="Deep-Nurture", BUCKETS=[]))
        reader.get = Mock(side_effect=[original, fresh]);reader.related = Mock(return_value=[])
        with self.assertRaisesRegex(RuntimeError, "changed_during"):reader.snapshot("lead_a")


class ReconcilerIntegrationTests(unittest.TestCase):
    def setUp(self):
        with patch.dict(os.environ, {"CLOSE_API_KEY": "offline-test-key"}):
            import lane2_state
        self.reconciler = lane2_state

    def test_budget_is_shared_with_terminal_cleanup_and_handoff(self):
        with patch.object(sys, "argv", ["lane2_state.py", "--apply", "--max-writes", "3"]), \
             patch.object(self.reconciler, "reconcile_qa_triage_terminal", return_value=1) as terminal, \
             patch.object(routing, "run", return_value={"verified_writes": 2}) as handoff, \
             patch.object(self.reconciler, "search") as search:
            self.reconciler.main()
        terminal.assert_called_once_with(apply=True, max_writes=3)
        self.assertEqual(handoff.call_args.kwargs, {"apply": True, "max_writes": 2})
        search.assert_not_called()

    def test_terminal_only_does_not_call_new_handoff(self):
        with patch.object(sys, "argv", ["lane2_state.py", "--qa-triage-terminal-only"]), \
             patch.object(self.reconciler, "reconcile_qa_triage_terminal") as terminal, \
             patch.object(routing, "run") as handoff:
            self.reconciler.main()
        terminal.assert_called_once();handoff.assert_not_called()

    def test_zero_budget_prevents_all_writes(self):
        with patch.object(sys, "argv", ["lane2_state.py", "--apply", "--max-writes", "0"]), \
             patch.object(self.reconciler, "reconcile_qa_triage_terminal") as terminal, \
             patch.object(routing, "run") as handoff:
            self.reconciler.main()
        terminal.assert_not_called();handoff.assert_not_called()


if __name__ == "__main__":
    unittest.main()
