"""Regression cases from the Lane 2 unresolved attendance audit."""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
import outcome_sync as sync

START = datetime(2026, 10, 21, 15, 30, tzinfo=timezone.utc)
MEETING = {"id": "meeting", "lead_id": "lead", "user_id": "rep", "title": "Vending Strategy Call",
           "starts_at": START.isoformat(), "ends_at": (START+timedelta(hours=1)).isoformat(), "status": "completed"}


class CoverageTests(unittest.TestCase):
    def test_unordered_close_page_does_not_hide_later_booking(self):
        with patch.object(sync, "read_outcome_backlog", return_value=[MEETING]):
            self.assertEqual([MEETING], sync.fetch_meetings_window(None, START-timedelta(days=1), START+timedelta(days=1)))

    def test_explicit_calendar_declines_close_as_cancelled(self):
        for status in ("declined-by-lead", "declined-by-org"):
            result = sync.decide({**MEETING, "status": status}, [MEETING], "skip", START+timedelta(days=1))
            self.assertEqual("cancelled", result[0])


    def test_bot_named_for_prospect_never_counts_as_attendance(self):
        self.assertEqual(None, sync.zoom_signal([{"name": "Jane Doe's AI Agent", "email": "jane@example.com", "seconds": 1200}], {"jane@example.com"}, set(), ["Jane Doe"])[0])
        self.assertTrue(sync.is_automation("Calendly Nоtеtаkеr"))

    def test_unmatched_human_and_short_join_block_no_show(self):
        host = {"name": "Rep", "email": "rep@example.com", "seconds": 1000}
        for human in [{"name": "iPhone", "email": "", "seconds": 20},
                      {"name": "Jane Doe", "email": "jane@example.com", "seconds": 299}]:
            with patch.object(sync, "ZOOM_AUTO_NOSHOW", True):
                self.assertIsNone(sync.zoom_signal([host, human], {"jane@example.com"}, {"rep@example.com"}, ["Jane Doe"])[0])

    def test_union_deduplicates_overlapping_devices_and_preappointment_time(self):
        rows = [{"name": "Rep", "join_time": (START-timedelta(minutes=8)).isoformat(),
                 "leave_time": (START+timedelta(minutes=4)).isoformat(), "duration": 720},
                {"name": "Rep", "join_time": START.isoformat(),
                 "leave_time": (START+timedelta(minutes=4)).isoformat(), "duration": 240}]
        parts = sync.aggregate_participants(rows, START)
        self.assertEqual(240, parts[0]["seconds"])
        self.assertIsNone(sync.zoom_signal(parts, set(), set(), [], ["Rep"])[0])

    def test_phone_switch_overrides_host_only_zoom(self):
        zoom = ([{"name": "Rep", "email": "rep@example.com", "seconds": 1200}], {"p@example.com"}, {"rep@example.com"}, [])
        call = {"at": START+timedelta(minutes=5), "user_id": "rep", "duration": 600, "disposition": "answered"}
        with patch.object(sync, "ZOOM_AUTO_NOSHOW", True):
            self.assertEqual("completed", sync.decide(MEETING, [MEETING], zoom, START+timedelta(days=1), calls=[call])[0])
        call["at"] = START+timedelta(hours=4)
        self.assertIsNone(sync.phone_evidence(MEETING, [call])[0])
        call["at"], call["user_id"] = START, "different_rep"
        self.assertIsNone(sync.phone_evidence(MEETING, [call])[0])

    def test_current_status_does_not_prove_historical_attendance(self):
        self.assertIsNone(sync.decide(MEETING, [MEETING], "skip", START+timedelta(days=1), lead_status="No Show", ext_attendee_statuses=["noreply"])[0])

    def test_zoom_never_falls_back_to_latest_numeric_meeting(self):
        zoom = sync.Zoom()
        with patch.object(zoom, "_get", return_value={"meetings": []}) as get:
            self.assertIsNone(zoom.participants_for("123", START))
            self.assertEqual(1, get.call_count)

    def test_zoom_paginates_and_encodes_uuid(self):
        zoom = sync.Zoom()
        pages = [{"meetings": [{"uuid": "a+b/c==", "start_time": START.isoformat()}]},
                 {"participants": [], "next_page_token": "page2"},
                 {"participants": [], "next_page_token": ""}]
        with patch.object(zoom, "_get", side_effect=pages) as get:
            self.assertEqual([], zoom.participants_for("123", START))
            self.assertIn("a%2Bb%2Fc%3D%3D", get.call_args_list[1].args[0])
            self.assertEqual("page2", get.call_args_list[2].args[1]["next_page_token"])

    def test_inbound_change_request_is_exposed_but_automation_is_ignored(self):
        sms = {"id": "prospect-change", "direction": "inbound", "text": "I need to reschedule"}
        email = {"id": "calendar", "direction": "incoming", "sender": "notifications@calendly.com", "body_text": "Cancel or reschedule"}
        with patch.object(sync, "close_get", side_effect=[{"data": [sms]}, {"data": [email]}]):
            self.assertEqual([{"activity_id": "prospect-change", "type": "sms"}], sync.meeting_change_messages(None, MEETING))


    def test_meeting_day_host_room_link_is_collected(self):
        activity = {"id": "room", "direction": "outbound", "activity_at": START.isoformat(),
                    "text": "Try this room https://zoom.us/j/12345678901"}
        rooms = set()
        with patch.object(sync, "close_get", side_effect=[{"data": [activity]}, {"data": []}]):
            self.assertEqual([], sync.meeting_change_messages(None, MEETING, zoom_ids=rooms))
        self.assertEqual({"12345678901"}, rooms)


    def test_reviewed_no_show_upgrade_remains_separate_from_generic_writes(self):
        old = {**MEETING, "outcome_id": sync.OUTCOMES["no_show"]}
        new = {**MEETING, "outcome_id": sync.OUTCOMES["completed"]}
        with patch.object(sync, "close_get", side_effect=[old, new]), patch.object(sync, "close_put") as put:
            self.assertEqual(new["outcome_id"], sync.set_meeting_outcome(None, old, new["outcome_id"], reviewed_from=old["outcome_id"]))
            put.assert_called_once()
        canceled = {**old, "outcome_id": sync.OUTCOMES["cancelled"]}
        with patch.object(sync, "close_get", return_value=canceled), patch.object(sync, "close_put") as put:
            self.assertEqual(canceled["outcome_id"], sync.set_meeting_outcome(None, canceled, new["outcome_id"], reviewed_from=old["outcome_id"]))
            put.assert_not_called()


    def test_preserve_terminal_during_write_and_verify_readback(self):
        fresh = {**MEETING, "outcome_id": sync.OUTCOMES["cancelled"]}
        with patch.object(sync, "close_get", return_value=fresh), patch.object(sync, "close_put") as put:
            self.assertEqual(fresh["outcome_id"], sync.set_meeting_outcome(None, MEETING, sync.OUTCOMES["completed"]))
            put.assert_not_called()
        with patch.object(sync, "close_get", side_effect=[MEETING, {**MEETING, "outcome_id": None}]), patch.object(sync, "close_put"):
            with self.assertRaisesRegex(RuntimeError, "readback"):
                sync.set_meeting_outcome(None, MEETING, sync.OUTCOMES["completed"])


if __name__ == "__main__":
    unittest.main()
