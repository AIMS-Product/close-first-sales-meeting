"""Tests for replacing two scheduled scans with one shared update."""
from collections import Counter
from datetime import timedelta
import unittest
from unittest.mock import Mock, patch

from compare_meeting_updates import CloseChanges, WriterInputs
from meeting_change_reader import utc
from run_meeting_updates import Progress, execute, merge_cache

END = utc("2026-11-20T02:00:00Z")
EMPTY = {"first_sales_meeting": {}, "follow_up_dates": {}}


class CombinedMeetingTests(unittest.TestCase):
    def setUp(self):
        self.close = Mock()
        self.close.calls = Counter()
        self.first = Mock(FIELD_DATE_KEY="custom.first")
        self.follow = Mock()
        self.snapshots = Mock()
        self.snapshots.read.return_value = {}
        self.row = {"id": "meeting_fixture", "lead_id": "lead_fixture"}
        self.close.event_changes.return_value = CloseChanges({"lead_fixture"}, {"meeting_fixture"}, {}, frozenset())
        self.close.meetings.return_value = [self.row]
        inputs = (WriterInputs(self.first, {"lead_outside": {"date": "2026-01-01"}}, {}),
                  WriterInputs(self.follow, {"lead_outside": {"field": "2026-01-02"}}, {}))
        self.enterContext(patch("run_meeting_updates.load_writer_inputs", return_value=inputs))
        self.enterContext(patch("run_meeting_updates.lead_snapshot", return_value=self.snapshots))
        self.full_cohort = self.enterContext(patch("run_meeting_updates.latest_full_cohort", return_value={}))
        self.evaluate = self.enterContext(patch("run_meeting_updates.evaluate_writers",
                                                return_value=(EMPTY, {}, {})))
        self.progress = Progress(END - timedelta(minutes=30), END - timedelta(hours=4))

    def run_combined(self, progress=None, *, verify=False, apply=False):
        return execute(self.close, END, progress, self.first, self.follow, verify=verify, apply=apply)

    def test_unverified_first_live_run_is_blocked(self):
        with self.assertRaisesRegex(RuntimeError, "requires an exact"):
            self.run_combined(apply=True)
        self.close.meetings.assert_not_called()

    def test_different_clear_blocks_live_cutover(self):
        baseline = {**EMPTY, "follow_up_dates": {"lead_fixture": {"custom.date": None}}}
        self.evaluate.side_effect = [(baseline, {}, {}), (EMPTY, {}, {})]
        summary, state = self.run_combined(verify=True, apply=True)
        self.assertFalse(summary["comparison_matches"])
        self.assertIsNone(state)
        self.assertEqual(summary["proposal_differences"]["follow_up_dates"], 1)
        self.assertFalse(any(call.kwargs.get("apply") for call in self.evaluate.call_args_list))

    def test_matching_first_run_applies_one_complete_shared_scan(self):
        summary, state = self.run_combined(verify=True, apply=True)
        self.assertTrue(summary["comparison_matches"])
        self.assertEqual(summary["mode"], "full")
        self.assertEqual(state[0], Progress(END, END))
        self.assertEqual([call.args for call in self.close.meetings.call_args_list], [("lead_fixture",), ()])
        self.assertEqual([call.kwargs.get("apply", False) for call in self.evaluate.call_args_list],
                         [False, False, True])

    def test_incremental_reads_one_history_and_keeps_unrelated_caches(self):
        summary, state = self.run_combined(self.progress, apply=True)
        self.assertEqual(summary["mode"], "incremental")
        self.assertEqual(state[0], Progress(END, self.progress.last_full))
        self.assertEqual(state[1], {"lead_outside": {"date": "2026-01-01"}})
        self.assertEqual(state[2], {"lead_outside": {"field": "2026-01-02"}})
        self.close.meetings.assert_called_once_with("lead_fixture")
        self.full_cohort.assert_not_called()
        self.assertEqual(self.evaluate.call_args.args[-1], {"lead_fixture"})

    def test_daily_reconciliation_and_incomplete_discovery_use_full_scan(self):
        for delay, discovery_error in ((timedelta(days=1), None), (timedelta(hours=4), RuntimeError("incomplete"))):
            with self.subTest(delay=delay):
                self.close.reset_mock()
                self.close.event_changes.side_effect = discovery_error
                progress = Progress(self.progress.cutoff, END - delay)
                summary, state = self.run_combined(progress, apply=True)
                self.assertEqual(summary["mode"], "full")
                self.assertEqual(state[0].last_full, END)
                self.close.meetings.assert_called_once_with()

    def test_empty_targeted_result_prunes_only_its_scoped_cache(self):
        raw = {"lead_a": {"value": 1}, "lead_b": {"value": 2}, "lead_c": {"value": 3}}
        result = merge_cache(raw, {"lead_b": {"value": 4}}, {"lead_a", "lead_b"})
        self.assertEqual(result, {"lead_b": {"value": 4}, "lead_c": {"value": 3}})
        self.assertEqual(len(raw), 3)
