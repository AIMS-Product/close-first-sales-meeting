import contextlib
import io
import json
import os
import unittest
from unittest.mock import call, patch

import requests
import workflow_holds

with patch.dict(os.environ, {"CLOSE_API_KEY": "test-key"}):
    import update_followups as followups


def response(status=200, payload=None, headers=None):
    result = requests.Response()
    result.status_code = status
    result.url = f"{followups.BASE_URL}/activity/meeting/"
    result._content = json.dumps(payload if payload is not None else {}).encode()
    result._content_consumed = True
    result.headers.update(headers or {})
    return result


class FollowupRateLimitTests(unittest.TestCase):
    def setUp(self):
        self.request = self.enterContext(patch.object(followups.session, "request"))
        self.sleep = self.enterContext(patch.object(followups.time, "sleep"))
        self.enterContext(patch.object(workflow_holds, "configured_store", return_value=None))
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))

    def test_meeting_scan_retries_offset_17700_without_losing_or_duplicating_rows(self):
        offsets = []

        def fetch_page(method, url, *, params, timeout):
            offset = params["_skip"]
            offsets.append(offset)
            if offset == 17700 and offsets.count(offset) == 1:
                return response(429, headers={"Retry-After": "2"})
            count = 100 if offset < 17800 else 1
            return response(payload={
                "data": [{"id": f"meeting_{index}"} for index in range(offset, offset + count)],
                "has_more": offset < 17800,
            })

        self.request.side_effect = fetch_page
        meetings = followups.fetch_all_meetings()

        self.assertEqual([meeting["id"] for meeting in meetings], [
            f"meeting_{index}" for index in range(17801)
        ])
        self.assertEqual(offsets, list(range(0, 17701, 100)) + [17700, 17800])
        self.sleep.assert_called_once_with(2.5)

    def test_server_delay_is_not_capped_by_fallback_backoff(self):
        self.request.side_effect = [
            response(429, headers={"Retry-After": "120.25"}),
            response(payload={"data": []}),
        ]
        self.assertEqual(followups.fetch_users(), {})
        self.sleep.assert_called_once_with(120.75)

    def test_reset_header_used_when_retry_after_is_missing_or_invalid(self):
        for retry_after in (None, "bad", "nan", "inf", "-1"):
            with self.subTest(retry_after=retry_after):
                headers = {"RateLimit": "limit=100, remaining=0, reset=2.25"}
                if retry_after is not None:
                    headers["Retry-After"] = retry_after
                self.request.side_effect = [response(429, headers=headers), response()]
                followups.request_close("GET", "/user/")
                self.assertEqual(self.sleep.call_args, call(2.75))

    def test_legacy_reset_header_is_supported(self):
        self.request.side_effect = [
            response(429, headers={"x-rate-limit-reset": "3.5"}), response(),
        ]
        followups.request_close("GET", "/user/")
        self.sleep.assert_called_once_with(4.0)

    def test_invalid_headers_fall_back_to_exponential_backoff(self):
        for headers in ({}, {"Retry-After": "bad", "RateLimit": "reset=nan"}):
            with self.subTest(headers=headers):
                self.sleep.reset_mock()
                self.request.side_effect = [response(429, headers=headers) for _ in range(3)] + [response()]
                followups.request_close("GET", "/user/")
                self.assertEqual(self.sleep.call_args_list, [call(1.5), call(2.5), call(4.5)])

    def test_persistent_throttling_raises_after_bounded_retries(self):
        self.request.return_value = response(429)
        with self.assertRaises(requests.HTTPError):
            followups.request_close("GET", "/user/")
        self.assertEqual(self.request.call_count, followups.MAX_RATE_LIMIT_RETRIES + 1)
        self.assertEqual(self.sleep.call_count, followups.MAX_RATE_LIMIT_RETRIES)
        self.assertEqual(self.sleep.call_args, call(60.5))

    def test_other_http_errors_are_not_retried(self):
        for status in (400, 401, 403, 404, 500):
            with self.subTest(status=status):
                self.request.reset_mock()
                self.request.return_value = response(status)
                with self.assertRaises(requests.HTTPError):
                    followups.request_close("GET", "/user/")
                self.request.assert_called_once()
        self.sleep.assert_not_called()

    def test_lead_reads_retry_and_preserve_custom_field_values(self):
        field = followups.FIELD_FOLLOWUP_1
        self.request.side_effect = [response(429), response(payload={f"custom.{field}": "2026-11-12"})]
        actual = followups.get_lead_followup_fields("lead_test")
        self.assertEqual(actual, {key: "2026-11-12" if key == field else None for key in followups.FOLLOWUP_FIELDS})
        expected = call("GET", f"{followups.BASE_URL}/lead/lead_test/", timeout=followups.REQUEST_TIMEOUT)
        self.assertEqual(self.request.call_args_list, [expected, expected])

    def test_lead_updates_retry_identical_minimal_payload_including_clears(self):
        updates = {followups.FIELD_FOLLOWUP_1: "2026-11-12", followups.FIELD_FOLLOWUP_2: None}
        payload = {f"custom.{key}": value for key, value in updates.items()}
        self.request.side_effect = [
            response(payload={"id": "lead_test"}),
            response(429), response(), response(payload=payload),
        ]
        followups.patch_lead("lead_test", updates)
        expected = call(
            "PUT", f"{followups.BASE_URL}/lead/lead_test/",
            json={f"custom.{key}": value for key, value in updates.items()},
            timeout=followups.REQUEST_TIMEOUT,
        )
        self.assertEqual(self.request.call_args_list[1:3], [expected, expected])
        self.assertEqual([args.args[0] for args in self.request.call_args_list], ["GET", "PUT", "PUT", "GET"])

    def test_throttled_write_verification_does_not_repeat_successful_put(self):
        field = followups.FIELD_FOLLOWUP_1
        self.request.side_effect = [
            response(payload={"id": "lead_test"}), response(),
            response(429), response(payload={f"custom.{field}": "2026-11-12"}),
        ]
        followups.patch_lead("lead_test", {field: "2026-11-12"})
        self.assertEqual([args.args[0] for args in self.request.call_args_list], ["GET", "PUT", "GET", "GET"])

    def test_sales_hold_still_prevents_date_updates(self):
        self.request.return_value = response(payload={
            "id": "lead_test", f"custom.{workflow_holds.BATCH}": "[STH1 qa test]",
        })
        with self.assertRaisesRegex(RuntimeError, "Sales hold"):
            followups.patch_lead("lead_test", {followups.FIELD_FOLLOWUP_1: "2026-11-12"})
        self.request.assert_called_once()
        self.assertEqual(self.request.call_args.args[0], "GET")

    def test_incomplete_scan_never_updates_leads_or_state_cache(self):
        self.request.side_effect = [
            response(payload={"data": []}),  # Users
            response(payload={"data": [{"id": f"meeting_{index}"} for index in range(100)], "has_more": True}),
        ] + [response(429) for _ in range(followups.MAX_RATE_LIMIT_RETRIES + 1)]
        with patch.object(followups, "save_state_cache") as save, patch.object(followups, "patch_lead") as update:
            with self.assertRaises(requests.HTTPError):
                followups.main()
        save.assert_not_called()
        update.assert_not_called()


if __name__ == "__main__":
    unittest.main()
