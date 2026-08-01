"""End-to-end test: the real Lambda handler over live HTTP against a mock Scoro API.

Unlike test_handler.py (which stubs ScoroClient), this suite runs the whole
pipeline unmodified — handler.handler() → ScoroClient._post → urllib over real
HTTP — against a local in-process mock of the Scoro v2 API. Only two seams are
faked, both at the outermost edge:

- ScoroClient.base_url is pointed at 127.0.0.1 so no request can ever reach
  live Scoro (the sandbox can't reach *.scoro.com anyway, but this makes the
  guarantee structural: even DRY_RUN=false writes land only on the mock).
- boto3.client("ses") returns a recording stub that always succeeds, i.e. the
  behaviour of a verified sending domain outside the SES sandbox.

Covered end to end: rate loading from products, project selection, bulk
retainer fetch + per-id view fallback, current-period resolution, parallel
task fetch (including the verification refetch), overcharge maths, previous
value / delta capture, the write ledger, DRY_RUN routing of both emails,
live-mode write-back payload shape, the last_n_working_days guard, and
only_project_ids targeting.
"""

import importlib
import json
import os
import threading
import unittest
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

try:
    import boto3  # noqa: F401  (handler -> scoro_api_key needs it at import)
except ImportError:  # pragma: no cover
    raise unittest.SkipTest("boto3 not installed; e2e suite needs it")

import scoro_client

# One frozen clock for the whole suite so period selection, the month-end
# guard, and cancelled-sub bounds are deterministic regardless of when the
# tests actually run. 2026-08-14 is a mid-month Friday.
FIXED_TODAY = datetime(2026, 8, 14, 10, 0, 0)
# Last 3 working days of August 2026 are Thu 27, Fri 28, Mon 31.
IN_WINDOW_DAY = datetime(2026, 8, 27, 10, 0, 0)

FIELD_KEY = "c_overchargehours"

BASE_ENV = {
    "SCORO_API_KEY": "e2e-test-key",
    "SCORO_COMPANY_ACCOUNT_ID": "e2e-test-account",
    "OVERCHARGE_FIELD_KEY": FIELD_KEY,
    # Mirrors the mapping deployed on the real functions.
    "OVERCHARGE_RATE_PRODUCT_CODES": json.dumps(
        {"BK": "SCORO_61", "BD": "SCORO_65", "EA": "SCORO_66", "SA": "SCORO_67"}
    ),
    "EMAIL_FROM": "reports@zembr.co",
    "EMAIL_REPORT_TO": "report-a@zembr.co,report-b@zembr.co",
    "EMAIL_LOG_TO": "ops@zembr.co",
    "EMAIL_TESTING_TO": "testing@zembr.co",
    "SES_REGION": "eu-north-1",
    "REQS_PER_SEC": "1000",  # local mock; no need to pace
}


# ---- fixtures ---------------------------------------------------------------

PRODUCTS = [
    {"product_id": 61, "code": "SCORO_61", "price": 30.0, "is_active": 1},
    {"product_id": 65, "code": "SCORO_65", "price": 40.0, "is_active": 1},
    {"product_id": 66, "code": "SCORO_66", "price": 50.0, "is_active": 1},
    {"product_id": 67, "code": "SCORO_67", "price": 60.0, "is_active": 1},
    # Deleted product with a colliding code must be ignored by rates.py.
    {"product_id": 99, "code": "SCORO_61", "price": 999.0, "is_deleted": 1},
]

AUG_PERIOD = {
    "id": 9001,
    "start_date": "2026-08-01",
    "end_date": "2026-08-31",
    "duration": 36000,  # 10h allowance
    "sum": 1500,
}
JUL_PERIOD = {
    "id": 9000,
    "start_date": "2026-07-01",
    "end_date": "2026-07-31",
    "duration": 36000,
    "sum": 1500,
}
# Contract container row: no duration/sum — select_current_period must skip it.
CONTRACT_ROW = {"id": 8999, "start_date": "2026-01-01", "end_date": "2026-12-31"}

PROJECTS = [
    {
        "project_id": 101,
        "project_name": "BK | Acme Pty | Zoe",
        "retainer_id": 501,
        "status": "additional6",  # active
        "custom_fields": [{"id": FIELD_KEY, "value": "12.5"}],
    },
    {
        "project_id": 102,
        "project_name": "EA North | Beta Ltd | Sam",
        "retainer_id": 502,
        "status": "additional8",  # at risk
        "custom_fields": [],
    },
    {
        # No retainer_id -> ineligible.
        "project_id": 103,
        "project_name": "SA | Gamma Co | Ash",
        "retainer_id": 0,
        "status": "additional6",
    },
    {
        # Unrecognised prefix -> eligible but skipped at service-line stage.
        "project_id": 104,
        "project_name": "Consulting | Delta Inc | Kim",
        "retainer_id": 504,
        "status": "additional6",
    },
    {
        # Cancelled last month -> ineligible for calc, listed as cancelled sub.
        "project_id": 105,
        "project_name": "BD | Epsilon | Lee",
        "retainer_id": 505,
        "status": "completed",
        "custom_fields": [{"id": "c_cancellationmonth", "value": "2026-07-01"}],
    },
    {
        # Retainer exists but bulk record has no nested periods and the per-id
        # view only returns an expired one -> "no current period" skip, via the
        # view-fallback path.
        "project_id": 106,
        "project_name": "BK | Zeta | Ola",
        "retainer_id": 506,
        "status": "additional6",
    },
]

RETAINERS = [
    {"id": 501, "retainer_periods": [CONTRACT_ROW, JUL_PERIOD, AUG_PERIOD]},
    {"id": 502, "retainer_periods": [AUG_PERIOD]},
    {"id": 504, "retainer_periods": [AUG_PERIOD]},
    {"id": 505, "retainer_periods": [JUL_PERIOD]},
    {"id": 506},  # no nested periods -> forces retainers/view fallback
]

RETAINER_VIEWS = {
    "506": {"id": 506, "retainer_periods": [JUL_PERIOD]},
}


def _entry(entry_id, day, seconds, billable=True, completed=True):
    return {
        "time_entry_id": entry_id,
        "completed_datetime": f"2026-08-{day:02d} 09:00:00",
        "billable_duration": seconds,
        "is_billable": 1 if billable else 0,
        "is_completed": 1 if completed else 0,
        "is_deleted": 0,
    }


TASKS_BY_PID = {
    # 12h billable in period against a 10h allowance -> 2h * 30/h = 60.00.
    # The non-billable and out-of-period entries must not count.
    101: [
        {
            "task_id": 2001,
            "time_entries": [
                _entry(30001, 5, 5 * 3600),
                _entry(30002, 12, 7 * 3600),
                _entry(30003, 13, 3600, billable=False),
                {
                    "time_entry_id": 30004,
                    "completed_datetime": "2026-07-20 09:00:00",  # out of period
                    "billable_duration": 4 * 3600,
                    "is_billable": 1,
                    "is_completed": 1,
                },
            ],
        }
    ],
    # 5h of 10h -> under budget -> overcharge 0.00.
    102: [{"task_id": 2002, "time_entries": [_entry(30010, 6, 5 * 3600)]}],
}


# ---- mock Scoro server ------------------------------------------------------

class MockScoro:
    """In-process Scoro v2 mock. Records every request; refuses nothing."""

    def __init__(self):
        self.lock = threading.Lock()
        self.requests = []       # (path, body) in arrival order
        self.modify_calls = []   # (entity, entity_id, body)
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                parts = self.path.strip("/").split("/")  # api/v2/<entity>/<action>[/<id>]
                entity, action = parts[2], parts[3]
                with outer.lock:
                    outer.requests.append((self.path, body))
                if action == "modify":
                    with outer.lock:
                        outer.modify_calls.append((entity, parts[4], body))
                    payload = {"status": "OK", "data": {}}
                elif action == "view":
                    payload = {"status": "OK", "data": RETAINER_VIEWS.get(parts[4], {})}
                elif entity == "products":
                    payload = {"status": "OK", "data": self._page(PRODUCTS, body)}
                elif entity == "projects":
                    payload = {"status": "OK", "data": self._page(PROJECTS, body)}
                elif entity == "retainers":
                    payload = {"status": "OK", "data": self._page(RETAINERS, body)}
                elif entity == "tasks":
                    pid = (body.get("filter") or {}).get("project_id")
                    payload = {
                        "status": "OK",
                        "data": self._page(TASKS_BY_PID.get(pid, []), body),
                    }
                else:
                    payload = {"status": "ERROR", "messages": f"unmocked {self.path}"}
                raw = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            @staticmethod
            def _page(rows, body):
                page = int(body.get("page") or 1)
                per_page = int(body.get("per_page") or 25)
                return rows[(page - 1) * per_page:page * per_page]

            def log_message(self, *args):  # keep test output clean
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def reset(self):
        with self.lock:
            self.requests.clear()
            self.modify_calls.clear()

    def request_count(self):
        with self.lock:
            return len(self.requests)

    def shutdown(self):
        self.server.shutdown()
        self.server.server_close()


# ---- SES stub ---------------------------------------------------------------

class FakeSES:
    """Records send_email calls and always succeeds — a verified domain with
    production (out-of-sandbox) SES access."""

    def __init__(self, calls):
        self.calls = calls

    def send_email(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "MessageId": f"fake-message-{len(self.calls)}",
            "ResponseMetadata": {"HTTPStatusCode": 200},
        }


# ---- harness ----------------------------------------------------------------

MOCK = None
SES_CALLS = []
_PATCHERS = []


def _fake_boto3_client(service, *args, **kwargs):
    if service == "ses":
        return FakeSES(SES_CALLS)
    raise RuntimeError(f"e2e test blocked boto3 client for {service!r}")


def _load_handler(dry_run):
    """(Re)load rates + handler with the e2e env. Returns the handler module."""
    os.environ.update(BASE_ENV)
    os.environ["DRY_RUN"] = "true" if dry_run else "false"
    import rates
    importlib.reload(rates)
    import handler
    handler = importlib.reload(handler)
    return handler


class _FrozenDatetime(datetime):
    _now = FIXED_TODAY

    @classmethod
    def utcnow(cls):
        return cls._now


def setUpModule():
    global MOCK
    MOCK = MockScoro()

    orig_init = scoro_client.ScoroClient.__init__

    def routed_init(self, *args, **kwargs):
        orig_init(self, *args, **kwargs)
        # Structural guarantee that no request leaves the box, DRY_RUN or not.
        self.base_url = f"http://127.0.0.1:{MOCK.port}/api/v2"

    _PATCHERS.append(patch.object(scoro_client.ScoroClient, "__init__", routed_init))
    _PATCHERS.append(patch("boto3.client", side_effect=_fake_boto3_client))
    for p in _PATCHERS:
        p.start()


def tearDownModule():
    for p in _PATCHERS:
        p.stop()
    _PATCHERS.clear()
    if MOCK:
        MOCK.shutdown()
    # Leave module state the way test_handler.py expects if it loads after us.
    os.environ["DRY_RUN"] = "true"
    import rates, handler  # noqa: E401
    importlib.reload(rates)
    importlib.reload(handler)


class _E2EBase(unittest.TestCase):
    dry_run = True
    frozen_now = FIXED_TODAY
    event = {}

    @classmethod
    def setUpClass(cls):
        MOCK.reset()
        SES_CALLS.clear()
        cls.handler = _load_handler(cls.dry_run)
        frozen = type("Frozen", (_FrozenDatetime,), {"_now": cls.frozen_now})
        cls._dt_patch = patch.object(cls.handler, "datetime", frozen)
        cls._dt_patch.start()
        cls.result = cls.handler.handler(dict(cls.event))
        cls.ses_calls = list(SES_CALLS)
        cls.modify_calls = list(MOCK.modify_calls)
        cls.requests = list(MOCK.requests)

    @classmethod
    def tearDownClass(cls):
        cls._dt_patch.stop()

    def result_for(self, pid):
        return next(r for r in self.result["results"] if r.get("project_id") == pid)


# ---- scenario: dev configuration (DRY_RUN=true), full pipeline --------------

class DryRunFullPipeline(_E2EBase):
    dry_run = True

    def test_summary_counts(self):
        self.assertEqual(
            self.result["summary"],
            {
                "dry_run": True,
                "eligible_projects": 4,   # 101, 102, 104, 106
                "computed": 2,            # 101, 102
                "ineligible": 2,          # 103 (no retainer), 105 (cancelled)
                "processed": 4,
                "written": 0,             # DRY_RUN writes nothing
                "skipped": 2,             # 104 (prefix), 106 (no period)
                "errors": 0,
            },
        )

    def test_no_writes_reach_scoro(self):
        self.assertEqual(self.modify_calls, [])
        self.assertFalse(any("/modify/" in path for path, _ in self.requests))

    def test_overcharge_maths_and_delta(self):
        over = self.result_for(101)
        self.assertEqual(over["planned_hours"], 10.0)
        self.assertEqual(over["logged_hours"], 12.0)   # 5h + 7h in period only
        self.assertEqual(over["overcharge_rate"], 30.0)
        self.assertEqual(over["overcharge_value"], 60.0)  # 2h over * 30/h
        self.assertEqual(over["previous_overcharge_value"], 12.5)
        self.assertEqual(over["overcharge_delta"], 47.5)

        under = self.result_for(102)
        self.assertEqual(under["service_line"], "EA North")
        self.assertEqual(under["logged_hours"], 5.0)
        self.assertEqual(under["overcharge_value"], 0.0)
        self.assertIsNone(under["previous_overcharge_value"])

    def test_skips_and_ineligible(self):
        skips = {r["project_id"]: r["reason"] for r in self.result["skipped"]}
        self.assertEqual(skips[104], "unrecognised project prefix")
        self.assertEqual(skips[106], "no current period")
        inel = {r["project_id"]: r["reason"] for r in self.result["ineligible"]}
        self.assertEqual(inel[103], "no retainer_id")
        self.assertIn("not active", inel[105])

    def test_cancelled_subscription_reported(self):
        self.assertEqual(len(self.result["cancelled_subs"]), 1)
        row = self.result["cancelled_subs"][0]
        self.assertEqual(row["project_id"], 105)
        self.assertEqual(row["cancellation_month"], "2026-07-01")
        self.assertEqual(row["service_line"], "BD")

    def test_write_ledger_dry(self):
        rows = self.result["write_ledger"]
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["written"] is False for row in rows))
        self.assertEqual(
            {row["project_id"]: row["value"] for row in rows},
            {101: 60.0, 102: 0.0},
        )

    def test_retainer_view_fallback_used(self):
        self.assertTrue(
            any(path.endswith("/retainers/view/506") for path, _ in self.requests)
        )

    def test_task_fetch_window(self):
        task_filters = [
            body["filter"]
            for path, body in self.requests
            if path.endswith("/tasks/list") and body["filter"]["project_id"] == 101
        ]
        # period start 2026-08-01 minus the 14-day lookback pad
        self.assertEqual(
            task_filters[0]["modified_date"], {"from": "2026-07-18"}
        )

    def test_every_request_carries_auth_envelope(self):
        # scoro_api_key caches the key at first import, so compare against what
        # the handler actually resolved rather than the raw env var.
        for _path, body in self.requests:
            self.assertEqual(body["apiKey"], self.handler.API_KEY)
            self.assertEqual(
                body["company_account_id"], self.handler.COMPANY_ACCOUNT_ID
            )

    def test_emails_route_to_testing_recipients(self):
        self.assertEqual(len(self.ses_calls), 2)  # report + log, no error alert
        for call in self.ses_calls:
            self.assertEqual(call["Source"], "reports@zembr.co")
            self.assertEqual(
                call["Destination"]["ToAddresses"], ["testing@zembr.co"]
            )
            self.assertIn("DRY RUN", call["Message"]["Subject"]["Data"])
        subjects = [c["Message"]["Subject"]["Data"] for c in self.ses_calls]
        self.assertTrue(any(s.startswith("Overcharge Run —") for s in subjects))
        self.assertTrue(any(s.startswith("Overcharge Run Log —") for s in subjects))

    def test_report_email_content(self):
        report = next(
            c for c in self.ses_calls
            if c["Message"]["Subject"]["Data"].startswith("Overcharge Run —")
        )
        html = report["Message"]["Body"]["Html"]["Data"]
        self.assertIn("BK | Acme Pty | Zoe", html)
        log_email = next(
            c for c in self.ses_calls
            if "Run Log" in c["Message"]["Subject"]["Data"]
        )
        self.assertIn("Text", log_email["Message"]["Body"])  # multipart log


# ---- scenario: live mode against the mock only ------------------------------

class LiveWriteAgainstMock(_E2EBase):
    dry_run = False

    def test_writes_land_with_correct_payload(self):
        self.assertEqual(len(self.modify_calls), 2)
        by_pid = {int(pid): body for _entity, pid, body in self.modify_calls}
        self.assertEqual(
            by_pid[101]["request"],
            {"custom_fields": [{"id": FIELD_KEY, "value": 60.0}]},
        )
        self.assertEqual(
            by_pid[102]["request"],
            {"custom_fields": [{"id": FIELD_KEY, "value": 0.0}]},
        )

    def test_summary_and_ledger_agree(self):
        self.assertEqual(self.result["summary"]["written"], 2)
        self.assertTrue(
            all(row["written"] is True for row in self.result["write_ledger"])
        )

    def test_emails_route_to_live_recipients(self):
        report = next(
            c for c in self.ses_calls
            if c["Message"]["Subject"]["Data"].startswith("Overcharge Run —")
        )
        self.assertEqual(
            report["Destination"]["ToAddresses"],
            ["report-a@zembr.co", "report-b@zembr.co"],
        )
        self.assertIn("LIVE", report["Message"]["Subject"]["Data"])
        log_email = next(
            c for c in self.ses_calls
            if "Run Log" in c["Message"]["Subject"]["Data"]
        )
        self.assertEqual(log_email["Destination"]["ToAddresses"], ["ops@zembr.co"])

    def test_all_traffic_stayed_local(self):
        self.assertTrue(self.requests)
        # The routed client cannot form a scoro.com URL; belt and braces:
        for path, _body in self.requests:
            self.assertTrue(path.startswith("/api/v2/"))


# ---- scenario: month-end guard, off window ----------------------------------

class GuardOffWindow(_E2EBase):
    dry_run = True
    frozen_now = FIXED_TODAY  # Aug 14 — not in the last 3 working days
    event = {"trigger_mode": "last_n_working_days", "days": 3}

    def test_skips_before_any_scoro_call(self):
        self.assertEqual(
            self.result,
            {
                "skipped": True,
                "reason": "not_in_last_n_working_days",
                "days": 3,
                "run_date": "2026-08-14",
            },
        )
        self.assertEqual(self.requests, [])
        self.assertEqual(self.ses_calls, [])


# ---- scenario: month-end guard, in window, emails suppressed ----------------

class GuardInWindowNoEmail(_E2EBase):
    dry_run = True
    frozen_now = IN_WINDOW_DAY  # Aug 27 — in the last 3 working days
    event = {"trigger_mode": "last_n_working_days", "days": 3, "send_email": False}

    def test_full_run_without_emails(self):
        self.assertEqual(self.result["summary"]["computed"], 2)
        self.assertEqual(self.result["summary"]["written"], 0)
        self.assertEqual(self.ses_calls, [])
        self.assertEqual(self.modify_calls, [])


# ---- scenario: targeted smoke test ------------------------------------------

class TargetedRun(_E2EBase):
    dry_run = True
    event = {"only_project_ids": [101]}

    def test_only_targeted_project_processed(self):
        self.assertEqual(self.result["summary"]["eligible_projects"], 1)
        self.assertEqual(self.result["summary"]["processed"], 1)
        self.assertEqual(self.result_for(101)["overcharge_value"], 60.0)
        task_pids = {
            body["filter"]["project_id"]
            for path, body in self.requests
            if path.endswith("/tasks/list")
        }
        self.assertEqual(task_pids, {101})


if __name__ == "__main__":
    unittest.main()
