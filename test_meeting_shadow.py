"""Test the shared discovery boundary and the two real proposal rules offline."""
from collections import Counter
from contextlib import closing, redirect_stdout
from datetime import timedelta
import io
import os
import sqlite3
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

import requests

from compare_meeting_updates import CloseReads, LeadSnapshot, WriterInputs, capture_proposals, compare, proposal_diff, run
from meeting_change_reader import ChangeSet, Checkpoint, SteelTrapMeetingReader, Window, utc

with patch.dict(os.environ, {"CLOSE_API_KEY": "offline-fixture"}):
    import update_field as first
    import update_followups as follow

ORG = "orga_fixture"
COMPANY = "88a28173-2a61-4cbb-92ea-f6b8be4592f3"
END = utc("2026-11-20T02:00:00Z")


class SteelTrapTests(unittest.TestCase):
    def test_change_feed_recovers_late_moves_and_deletions(self):
        checkpoint = Checkpoint(END - timedelta(minutes=30), 34)
        window = Window.next(END, checkpoint)
        event = {"id": "ledger1", "event_id": "event1", "meeting_id": "meeting1", "current_lead": "lead_current",
                 "previous_lead": "lead_prior", "lead_changed": "true", "change_type": "insert",
                 "source_us": str(int((window.start - timedelta(days=1)).timestamp() * 1_000_000)), "via_cdf": "true"}
        deletion = dict(event, id="ledger2", event_id="event2", meeting_id="meeting2", current_lead=None,
                        previous_lead="lead_deleted", lead_changed="false")
        sql = Mock()
        sql.read.side_effect = [[{"version": "37", "timestamp": END.isoformat()}], [event, deletion]]
        changes = SteelTrapMeetingReader(sql, COMPANY, ORG).changes(window)
        query = sql.read.call_args.args[0]
        self.assertIn("VERSION AS OF 37", query)
        self.assertIn("35, 37)", query)
        self.assertIn("provider_org_id = 'orga_fixture'", query)
        self.assertEqual(changes.leads, {"lead_current", "lead_prior", "lead_deleted"})
        self.assertEqual(changes.events["event1"], {"lead_current", "lead_prior"})
        self.assertEqual(changes.late_events, 2)
        self.assertEqual(changes.late_events_outside_overlap, 2)

    def test_mutable_ledger_and_reversed_versions_fail(self):
        window = Window.next(END, Checkpoint(END - timedelta(minutes=30), 20))
        sql = Mock()
        sql.read.side_effect = [[{"version": "21", "timestamp": END.isoformat()}],
                                [{"change_type": "update_postimage"}]]
        with self.assertRaisesRegex(RuntimeError, "changed an existing row"):
            SteelTrapMeetingReader(sql, COMPANY, ORG).changes(window)
        sql.read.side_effect = [[{"version": "19", "timestamp": END.isoformat()}]]
        with self.assertRaisesRegex(RuntimeError, "outside the supported Delta"):
            SteelTrapMeetingReader(sql, COMPANY, ORG).changes(window)
        self.assertEqual(sql.read.call_args.args[0].split()[0], "DESCRIBE")


class CloseReadTests(unittest.TestCase):
    def test_event_paging_retains_previous_lead_and_rechecks_cutoff(self):
        client = Mock()
        read = CloseReads(client, ORG)
        window = Window.next(END)
        kind = {"organization_id": ORG, "action": "updated", "object_type": "activity.meeting"}
        move = dict(kind, id="event1", object_id="meeting1", lead_id="lead_after",
                    date_updated="2026-11-20T01:50:00", changed_fields=["lead_id"],
                    previous_data={"lead_id": "lead_before"})
        older = dict(kind, id="event2", object_id="meeting2", lead_id="lead_older",
                     date_updated="2026-11-19T23:59:59Z")

        def request(method, path):
            self.assertEqual(method, "GET")
            params = parse_qs(urlsplit(path).query)
            if params["action"] != ["updated"]:
                return {"data": []}
            if "_cursor" in params:
                self.assertNotIn("date_updated__gte", params)
                return {"data": [older], "cursor_next": "past-window"}
            return {"data": [move], "cursor_next": "next"}

        client.request.side_effect = request
        leads, meetings, revisions = read.event_leads(window)
        self.assertEqual((leads, meetings, revisions), ({"lead_after", "lead_before"}, {"meeting1"},
                                                       {"event1": {"lead_after", "lead_before"}}))

        changes = read.event_changes(window)
        self.assertEqual(len(changes.revisions), 1)
        self.assertFalse(read.event_changes(window, changes.revisions).leads)
        # Close can amend an event while retaining its ID and timestamp.
        move["data"] = {"lead_id": "lead_after", "title": "Changed meeting"}
        self.assertEqual(read.event_changes(window, changes.revisions).leads, {"lead_after", "lead_before"})

    def test_malformed_or_unscoped_meeting_page_fails(self):
        client = Mock()
        client.request.return_value = {
            "data": [{"id": "meeting1", "organization_id": ORG, "lead_id": "lead_other"}], "has_more": False,
        }
        with self.assertRaisesRegex(RuntimeError, "different lead"):
            CloseReads(client, ORG).meetings("lead_requested")


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(redirect_stdout(io.StringIO()))
        self.enterContext(patch.object(first, "api_put", side_effect=AssertionError("Production write")))
        self.enterContext(patch.object(follow, "patch_lead", side_effect=AssertionError("Production write")))
        self.enterContext(patch.object(first, "save_state_cache", side_effect=AssertionError("Production cache")))
        self.enterContext(patch.object(follow, "save_state_cache", side_effect=AssertionError("Production cache")))

    def test_both_real_writers_capture_date_and_clear_and_share_field_read(self):
        close = Mock(organization=ORG)
        close.get.return_value = {"organization_id": ORG, "id": "lead_fixture",
                                  f"custom.{follow.FIELD_FOLLOWUP_1}": "2026-11-08"}
        inputs = WriterInputs(first, {}, {})
        follow_inputs = WriterInputs(follow, {"lead_fixture": {follow.FIELD_FOLLOWUP_1: "2026-11-08"}}, {})
        meetings = [{"lead_id": "lead_fixture", "title": "Vendingpreneur Next Steps",
                     "starts_at": "2026-11-02T18:00:00Z", "status": "completed"}]
        snapshots = LeadSnapshot(close, (*first.FIELDS_PARAM.split(","), f"custom.{follow.FIELD_FOLLOWUP_1}"))
        baseline = capture_proposals(inputs, follow_inputs, snapshots, meetings, {})
        incremental = capture_proposals(inputs, follow_inputs, snapshots, meetings, {}, {"lead_fixture"})
        close.get.assert_called_once()
        self.assertEqual(baseline, incremental)
        payload = baseline["first_sales_meeting"]["lead_fixture"]
        self.assertEqual(payload[first.FIELD_DATE_KEY], "2026-11-02")
        self.assertEqual(baseline["follow_up_dates"]["lead_fixture"], {f"custom.{follow.FIELD_FOLLOWUP_1}": None})
        empty = {writer: {} for writer in baseline}
        self.assertFalse(proposal_diff(baseline, empty, set())["follow_up_dates"]["lead_fixture"]["in_incremental_scope"])

    def test_only_a_404_is_skippable(self):
        close = Mock(organization=ORG)
        response = requests.Response()
        response.status_code = 403
        close.get.side_effect = requests.HTTPError(response=response)
        inputs = WriterInputs(first, {"lead_missing": {"date": "2026-11-01"}}, {})
        follow_inputs = WriterInputs(follow, {}, {})
        with self.assertRaisesRegex(RuntimeError, "comparison was incomplete"):
            capture_proposals(inputs, follow_inputs, LeadSnapshot(close, ()), [], {})
        response.status_code = 404
        snapshot = LeadSnapshot(close, ())
        self.assertEqual(capture_proposals(inputs, follow_inputs, snapshot, [], {})["first_sales_meeting"], {})
        self.assertEqual(snapshot.absent, {"lead_missing"})

    def test_comparison_reads_union_once_and_reports_full_scan_changes_outside_it(self):
        close = Mock(organization=ORG)
        close.calls = Counter()
        close.event_leads.return_value = ({"lead_shared", "lead_close"}, {"meeting1"}, {"event1": {"lead_shared"}})
        meetings = [{"id": f"meeting{index}", "organization_id": ORG, "lead_id": identity,
                     "title": "Vendingpreneur Next Steps", "starts_at": "2026-11-02T18:00:00Z",
                     "status": "completed"} for index, identity in enumerate(
                         ("lead_shared", "lead_close", "lead_steel", "lead_outside"), 1)]
        close.meetings.side_effect = lambda identity=None: [row for row in meetings if identity is None or row["lead_id"] == identity]
        close.get.side_effect = lambda path, _=None, **kwargs: (
            {"data": [{"id": "user_fixture", "display_name": "Fixture"}], "has_more": False} if path == "/user/"
            else {"choices": ["Fixture"]} if path.startswith("/custom_field/")
            else {"organization_id": ORG, "id": path.split("/")[2]}
        )
        close.search.return_value = {"data": [{"id": "lead_elsewhere", first.FIELD_DATE_KEY: "2026-11-01",
                                                first.FIELD_LATEST_DATE_KEY: "2026-11-01"}], "cursor": None}
        steel = Mock()
        steel.changes.return_value = ChangeSet(frozenset({"lead_shared", "lead_steel"}), frozenset({"meeting1"}),
                                               {"event1": {"lead_shared"}}, 10, END.isoformat(), 0, 0, 0)
        with patch.object(first, "load_state_cache", return_value={"lead_fixture": {}}), patch.object(
                follow, "load_state_cache", return_value={"lead_fixture": {}}):
            report = compare(close, steel, Window.next(END), first, follow)
        self.assertEqual([c.args for c in close.meetings.call_args_list],
                         [(), ("lead_close",), ("lead_shared",), ("lead_steel",)])
        self.assertEqual(report["proposals"]["steeltrap"]["first_sales_meeting"].keys(), {"lead_shared", "lead_steel"})
        self.assertIn("lead_outside", report["differences"]["steeltrap"]["first_sales_meeting"])
        self.assertEqual(report["discovery"]["close_only_leads"], ["lead_close"])


class CheckpointTests(unittest.TestCase):
    def test_failed_run_and_historical_replay_leave_live_cursor_alone(self):
        with closing(sqlite3.connect(":memory:")) as db:
            def success(window):
                return {"cutoff": window.end.isoformat(), "steeltrap_version": 87}

            run(db, END, success)
            later = END + timedelta(minutes=30)
            with self.assertRaisesRegex(RuntimeError, "unavailable"):
                run(db, later, Mock(side_effect=RuntimeError("unavailable")))
            self.assertEqual(db.execute("SELECT count(*) FROM runs").fetchone(), (1,))
            run(db, END - timedelta(days=1), success, replay=True)
            self.assertEqual(db.execute("SELECT cutoff FROM checkpoint").fetchone(), (END.isoformat(),))
            compare_run = Mock(side_effect=success)
            run(db, later, compare_run)
            self.assertEqual(compare_run.call_args.args[0], Window(END - timedelta(hours=2), later, 87, END))
