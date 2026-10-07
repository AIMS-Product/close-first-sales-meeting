import json
import os
import tempfile
import unittest
from unittest import mock

import requests

import outcome_sync
import repair_zoom_false_noshows


ORG_EMAILS = {"rep@vendingpreneurs.com"}


def meeting(contact_id="contact_1", attendee_name=None):
    return {
        "attendees": [{
            "email": "prospect@example.com",
            "name": attendee_name,
            "contact_id": contact_id,
        }]
    }


def lead(display_name, contact_name):
    return {
        "display_name": display_name,
        "contacts": [{
            "id": "contact_1",
            "name": contact_name,
            "emails": [{"email": "prospect@example.com"}],
        }],
    }


class ProductionNameMatchingTests(unittest.TestCase):
    def test_contact_name_fills_missing_attendee_name(self):
        names = outcome_sync.prospect_names_for(
            meeting(), lead("Debbie Barca", "Debbie Barca"), ORG_EMAILS
        )

        self.assertEqual(["Debbie Barca"], names)

    def test_different_contact_surname_is_rejected(self):
        names = outcome_sync.prospect_names_for(
            meeting(), lead("Ashley Trybus", "Ashley Hoeger"), ORG_EMAILS
        )

        self.assertEqual([], names)

    def test_different_zoom_surname_is_rejected(self):
        self.assertFalse(outcome_sync._name_match("Lowell Gilliland", "Lowell Gill"))

    def test_zoom_age_metadata_is_ignored(self):
        self.assertTrue(
            outcome_sync._name_match("Chelsea Streeter 37 YO", "Chelsea Streeter")
        )

    def test_ambiguous_one_word_name_is_rejected(self):
        self.assertFalse(outcome_sync._name_match("Mark D", "Mark"))


class RepairAllowlistTests(unittest.TestCase):
    def test_known_exceptions_are_not_allowlisted(self):
        targets = repair_zoom_false_noshows.REPAIR_TARGETS

        self.assertNotIn("acti_wWzkSGghaFoOxblzin8uqLJiJHQRIS2RxE44EzprIwY", targets)
        self.assertNotIn("acti_X6rjlXSdCSM9IXO17h22ctwzVErQhVhP5ZtP3J3FRuf", targets)

    def test_only_reviewed_nine_are_allowlisted(self):
        self.assertEqual(9, len(repair_zoom_false_noshows.REPAIR_TARGETS))


class ZoomAuthenticationTests(unittest.TestCase):
    def test_oauth_error_reports_reason_without_exposing_credentials(self):
        response = requests.Response()
        response.status_code = 400
        response._content = (b'{"error":"invalid_client",'
                             b'"reason":"Invalid client id or client secret: secret-value\\n"}')
        credentials = {"ZOOM_ACCOUNT_ID": "account-value", "ZOOM_CLIENT_ID": "client-value",
                       "ZOOM_CLIENT_SECRET": "secret-value"}
        with mock.patch.dict(os.environ, credentials), mock.patch.object(
            outcome_sync.requests, "post", return_value=response
        ) as post:
            with self.assertRaisesRegex(RuntimeError, "Zoom OAuth HTTP 400.*invalid_client") as caught:
                outcome_sync.Zoom().token()

        self.assertIn("Invalid client id or client secret", str(caught.exception))
        self.assertNotIn("account-value", str(caught.exception))
        self.assertNotIn("secret-value", str(caught.exception))
        self.assertIn("[redacted]", str(caught.exception))
        self.assertNotIn("\n", str(caught.exception))
        self.assertEqual(1, post.call_count)

    def test_required_zoom_auth_failure_stops_before_close_access(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            os.environ, {"ZOOM_REQUIRED": "1", "ZOOM_ACCOUNT_ID": "account-value",
                         "ZOOM_CLIENT_ID": "client-value", "ZOOM_CLIENT_SECRET": "secret-value"}
        ), mock.patch.object(outcome_sync.Zoom, "token", side_effect=RuntimeError("Zoom OAuth HTTP 400")), \
                mock.patch.object(outcome_sync, "close_session") as close_session:
            previous = os.getcwd()
            try:
                os.chdir(directory)
                self.assertEqual(1, outcome_sync.run())
                with open("outcome_sync_report.json") as fh:
                    report = json.load(fh)
            finally:
                os.chdir(previous)

        close_session.assert_not_called()
        self.assertEqual("Zoom authentication failed: Zoom OAuth HTTP 400",
                         report["errors"][0]["error"])

    def test_required_zoom_missing_secret_stops_before_close_access(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            os.environ, {"ZOOM_REQUIRED": "1", "ZOOM_ACCOUNT_ID": "account-value",
                         "ZOOM_CLIENT_ID": "client-value", "ZOOM_CLIENT_SECRET": ""}
        ), mock.patch.object(outcome_sync.requests, "post") as post, \
                mock.patch.object(outcome_sync, "close_session") as close_session:
            previous = os.getcwd()
            try:
                os.chdir(directory)
                self.assertEqual(1, outcome_sync.run())
                with open("outcome_sync_report.json") as fh:
                    report = json.load(fh)
            finally:
                os.chdir(previous)

        post.assert_not_called()
        close_session.assert_not_called()
        self.assertIn("ZOOM_CLIENT_SECRET", report["errors"][0]["error"])


if __name__ == "__main__":
    unittest.main()
