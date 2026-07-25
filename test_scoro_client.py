"""Unit tests for ScoroClient.modify() — the low-level HTTP call that writes
the overcharge value to a project's custom field (e.g. c_overchargehours).

These sit below test_handler.py's WriteBackTests: those confirm *when*
process_project decides to write; these confirm *what actually goes over
the wire* is shaped the way Scoro's API requires. Scoro silently ignores a
malformed write (no error, field just never updates), so the request shape
is worth pinning down with a test rather than only trusting the docstring.
"""

import json
import unittest
from unittest.mock import MagicMock, patch

from scoro_client import ScoroClient, ScoroError


def _stub_response(mock_urlopen, payload):
    """Make `with urllib.request.urlopen(...) as resp:` yield a fake response
    whose .read() returns the given payload as JSON bytes."""
    resp = MagicMock()
    resp.read.return_value = json.dumps(payload).encode("utf-8")
    mock_urlopen.return_value.__enter__.return_value = resp
    return resp


class ModifyRequestShapeTests(unittest.TestCase):
    def setUp(self):
        # reqs_per_sec=0 disables the rate limiter's sleep so tests run instantly.
        self.client = ScoroClient(
            "test-key", "zembrpty", reqs_per_sec=0
        )

    @patch("scoro_client.urllib.request.urlopen")
    def test_custom_field_sent_as_nested_array_not_flat_key(self, mock_urlopen):
        _stub_response(mock_urlopen, {"status": "OK"})

        self.client.modify("projects", 492, "c_overchargehours", 123.45)

        sent_request = mock_urlopen.call_args[0][0]
        body = json.loads(sent_request.data.decode("utf-8"))

        # Scoro requires: request.custom_fields = [{id, value}].
        # A flat top-level key (e.g. body["c_overchargehours"] = ...) is
        # silently ignored by the API — this is the exact bug the contract
        # guards against.
        self.assertEqual(
            body.get("request"),
            {"custom_fields": [{"id": "c_overchargehours", "value": 123.45}]},
        )
        self.assertNotIn("c_overchargehours", body)

    @patch("scoro_client.urllib.request.urlopen")
    def test_posts_to_the_correct_entity_modify_endpoint(self, mock_urlopen):
        _stub_response(mock_urlopen, {"status": "OK"})

        self.client.modify("projects", 492, "c_overchargehours", 0.0)

        sent_request = mock_urlopen.call_args[0][0]
        self.assertEqual(
            sent_request.full_url,
            "https://zembrpty.scoro.com/api/v2/projects/modify/492",
        )

    @patch("scoro_client.urllib.request.urlopen")
    def test_zero_value_is_written_not_dropped_as_falsy(self, mock_urlopen):
        # 0 is a legitimate overcharge value (within-budget projects still
        # refresh the field to 0) — make sure nothing along the way treats
        # `value=0.0` as "no value" and omits it from the payload.
        _stub_response(mock_urlopen, {"status": "OK"})

        self.client.modify("projects", 492, "c_overchargehours", 0.0)

        sent_request = mock_urlopen.call_args[0][0]
        body = json.loads(sent_request.data.decode("utf-8"))
        field = body["request"]["custom_fields"][0]
        self.assertEqual(field, {"id": "c_overchargehours", "value": 0.0})

    @patch("scoro_client.urllib.request.urlopen")
    def test_sends_auth_envelope_and_browser_user_agent(self, mock_urlopen):
        # Scoro is behind Cloudflare: no browser User-Agent -> 403 (error
        # code 1010), so the write would fail outright rather than just
        # writing the wrong thing.
        _stub_response(mock_urlopen, {"status": "OK"})

        self.client.modify("projects", 492, "c_overchargehours", 0.0)

        sent_request = mock_urlopen.call_args[0][0]
        body = json.loads(sent_request.data.decode("utf-8"))

        self.assertEqual(body["apiKey"], "test-key")
        self.assertEqual(body["company_account_id"], "zembrpty")
        self.assertEqual(body["lang"], "eng")
        self.assertIn("Chrome", sent_request.get_header("User-agent"))

    @patch("scoro_client.urllib.request.urlopen")
    def test_scoro_error_status_raises_scoro_error(self, mock_urlopen):
        _stub_response(
            mock_urlopen, {"status": "ERROR", "messages": ["bad custom field id"]}
        )

        with self.assertRaises(ScoroError):
            self.client.modify("projects", 492, "c_overchargehours", 0.0)

    @patch("scoro_client.urllib.request.urlopen")
    def test_modify_return_value_is_the_parsed_response(self, mock_urlopen):
        _stub_response(mock_urlopen, {"status": "OK", "data": {"id": 492}})

        result = self.client.modify("projects", 492, "c_overchargehours", 0.0)

        self.assertEqual(result, {"status": "OK", "data": {"id": 492}})


if __name__ == "__main__":
    unittest.main()
