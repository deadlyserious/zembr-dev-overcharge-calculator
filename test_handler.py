import json
import os
import threading
import unittest
from datetime import date, datetime, timedelta
from unittest.mock import patch

os.environ["SCORO_API_KEY"] = "test-key"
os.environ["SCORO_COMPANY_ACCOUNT_ID"] = "test-account"
os.environ["DRY_RUN"] = "true"

import handler
import rates
import write_overcharge
from scoro_client import ScoroError


PERIOD = {
    "id": 10,
    "duration": 3600,
    "sum": 100,
    "start_date": "2026-07-01",
    "end_date": "2026-07-31",
}
PROJECT = {
    "id": 1,
    "name": "BK | Test client",
    "retainer_id": 2,
    "status": handler.ACTIVE_STATUS,
}
IN_PERIOD_TASK = {
    "id": 20,
    "time_entries": [
        {
            "id": 30,
            "completed_datetime": "2026-07-15 12:00:00",
            "billable_duration": 7200,
            "is_billable": True,
            "is_completed": True,
        }
    ],
}


class FakeClient:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.filters = []
        self.writes = []

    def list_all(self, entity, **kwargs):
        self.filters.append(kwargs.get("filter"))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response

    def modify(self, *args):
        self.writes.append(args)


class TaskFetchTests(unittest.TestCase):
    def test_empty_filtered_fetch_is_verified_without_modified_date(self):
        client = FakeClient([[], [IN_PERIOD_TASK]])

        tasks, errors = handler.fetch_tasks_by_project(
            client, [PROJECT], {1: PERIOD}
        )

        self.assertEqual(errors, [])
        self.assertEqual(tasks[1], [IN_PERIOD_TASK])
        self.assertIn("modified_date", client.filters[0])
        self.assertEqual(client.filters[1], {"project_id": 1})

    def test_filtered_fetch_failure_is_not_treated_as_empty(self):
        client = FakeClient([ScoroError("filtered request failed")])

        tasks, errors = handler.fetch_tasks_by_project(
            client, [PROJECT], {1: PERIOD}
        )

        self.assertNotIn(1, tasks)
        self.assertEqual(errors[0]["project_id"], 1)
        self.assertIn("filtered tasks fetch failed", errors[0]["error"])
        self.assertEqual(client.writes, [])

    def test_verification_fetch_failure_is_not_treated_as_empty(self):
        client = FakeClient([[], ScoroError("verification request failed")])

        tasks, errors = handler.fetch_tasks_by_project(
            client, [PROJECT], {1: PERIOD}
        )

        self.assertNotIn(1, tasks)
        self.assertEqual(errors[0]["project_id"], 1)
        self.assertIn("verification tasks fetch failed", errors[0]["error"])
        self.assertEqual(client.writes, [])


class ResolvePeriodsTests(unittest.TestCase):
    def test_supplied_date_decides_the_current_period(self):
        # Two adjacent monthly periods: the supplied (UTC) run date picks the
        # current one — never the container's local date.
        june = dict(PERIOD, id=11, start_date="2026-06-01", end_date="2026-06-30")
        retainers = {2: {"retainer_periods": [june, PERIOD]}}

        by_pid = handler.resolve_periods(None, [PROJECT], retainers, date(2026, 6, 30))
        self.assertEqual(by_pid[1]["id"], 11)

        by_pid = handler.resolve_periods(None, [PROJECT], retainers, date(2026, 7, 1))
        self.assertEqual(by_pid[1]["id"], 10)


class ZeroWriteTests(unittest.TestCase):
    def setUp(self):
        self.previous_rates = rates._overcharge
        rates._overcharge = {"BK": 100.0}

    def tearDown(self):
        rates._overcharge = self.previous_rates

    def test_authoritative_empty_period_writes_zero(self):
        client = FakeClient([])

        with patch.object(handler, "DRY_RUN", False):
            result = handler.process_project(
                client, PROJECT, [], {1: PERIOD}
            )

        self.assertEqual(result["logged_hours"], 0)
        self.assertEqual(result["overcharge_value"], 0)
        self.assertEqual(
            client.writes,
            [("projects", 1, "c_overchargehours", 0.0)],
        )


class ChangeTrackingTests(unittest.TestCase):
    def setUp(self):
        self.previous_rates = rates._overcharge
        rates._overcharge = {"BK": 100.0}

    def tearDown(self):
        rates._overcharge = self.previous_rates

    def _project_with_field(self, value):
        return dict(
            PROJECT,
            custom_fields=[{"id": handler.CURRENT_MONTH_OVERCHARGE_FIELD_KEY, "value": value}],
        )

    def test_numeric_previous_value_yields_signed_delta(self):
        result = handler.process_project(
            None, self._project_with_field("980"), [IN_PERIOD_TASK], {1: PERIOD}
        )

        self.assertEqual(result["overcharge_value"], 100.0)
        self.assertEqual(result["previous_overcharge_value"], 980.0)
        self.assertEqual(result["overcharge_delta"], -880.0)

    def test_missing_or_garbage_previous_value_is_unknown(self):
        projects = [
            PROJECT,  # detailed payload without custom_fields at all
            dict(PROJECT, custom_fields=[]),
            self._project_with_field(None),
            self._project_with_field(""),
            self._project_with_field("abc"),
        ]
        for project in projects:
            with self.subTest(custom_fields=project.get("custom_fields")):
                result = handler.process_project(
                    None, project, [IN_PERIOD_TASK], {1: PERIOD}
                )

                self.assertIsNone(result["previous_overcharge_value"])
                self.assertIsNone(result["overcharge_delta"])

    def test_write_back_still_writes_the_new_absolute_value(self):
        client = FakeClient([])

        with patch.object(handler, "DRY_RUN", False):
            result = handler.process_project(
                client, self._project_with_field(980), [IN_PERIOD_TASK], {1: PERIOD}
            )

        self.assertEqual(result["previous_overcharge_value"], 980.0)
        self.assertEqual(result["overcharge_delta"], -880.0)
        self.assertEqual(
            client.writes,
            [("projects", 1, "c_overchargehours", 100.0)],
        )


class WriteLedgerTests(unittest.TestCase):
    def setUp(self):
        self.previous_rates = rates._overcharge
        rates._overcharge = {"BK": 100.0}

    def tearDown(self):
        rates._overcharge = self.previous_rates

    def _writeback_events(self, logs):
        return [
            json.loads(record.getMessage())
            for record in logs.records
            if record.getMessage().startswith('{"event": "writeback"')
        ]

    def test_live_write_emits_event_and_ledger_row(self):
        client = FakeClient([])
        ledger = handler._WriteLedger(total=3)

        with (
            patch.object(handler, "DRY_RUN", False),
            self.assertLogs("overcharge_calculator", level="INFO") as logs,
        ):
            handler.process_project(
                client, PROJECT, [IN_PERIOD_TASK], {1: PERIOD}, ledger
            )

        self.assertEqual(
            client.writes,
            [("projects", 1, "c_overchargehours", 100.0)],
        )
        self.assertEqual(
            self._writeback_events(logs),
            [{
                "event": "writeback",
                "project_id": 1,
                "value": 100.0,
                "field_key": "c_overchargehours",
                "dry_run": False,
                "seq": 1,
                "total": 3,
            }],
        )
        self.assertEqual(
            ledger.rows(),
            [{
                "project_id": 1,
                "project_name": "BK | Test client",
                "value": 100.0,
                "written": True,
            }],
        )

    def test_dry_run_emits_event_without_writing(self):
        client = FakeClient([])
        ledger = handler._WriteLedger(total=1)

        with self.assertLogs("overcharge_calculator", level="INFO") as logs:
            handler.process_project(
                client, PROJECT, [IN_PERIOD_TASK], {1: PERIOD}, ledger
            )

        self.assertEqual(client.writes, [])
        events = self._writeback_events(logs)
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0]["dry_run"])
        self.assertEqual(events[0]["value"], 100.0)
        self.assertEqual(
            ledger.rows(),
            [{
                "project_id": 1,
                "project_name": "BK | Test client",
                "value": 100.0,
                "written": False,
            }],
        )


class FakeRunClient:
    """Just enough Scoro surface for a full handler() run."""

    def __init__(self, projects, retainers, tasks_by_pid):
        self.projects = projects
        self.retainers = retainers
        self.tasks_by_pid = tasks_by_pid
        self.writes = []
        self._lock = threading.Lock()

    def list_all_parallel(self, entity, **kwargs):
        return {"projects": self.projects, "retainers": self.retainers}[entity]

    def list_all(self, entity, **kwargs):
        pid = (kwargs.get("filter") or {}).get("project_id")
        return self.tasks_by_pid.get(pid, []) if entity == "tasks" else []

    def modify(self, *args):
        with self._lock:
            self.writes.append(args)


class HandlerRunLedgerTests(unittest.TestCase):
    def setUp(self):
        self.previous_rates = rates._overcharge
        rates._overcharge = {"BK": 100.0}

    def tearDown(self):
        rates._overcharge = self.previous_rates

    def test_ledger_preserves_write_order_in_return_payload(self):
        # Periods and entries built around the real UTC run date, since
        # handler() selects the current period with utcnow.
        today = datetime.utcnow().date()
        window = {
            "start_date": (today - timedelta(days=1)).isoformat(),
            "end_date": (today + timedelta(days=1)).isoformat(),
        }
        task = {
            "id": 20,
            "time_entries": [
                {
                    "id": 30,
                    "completed_datetime": f"{today.isoformat()} 12:00:00",
                    "billable_duration": 7200,
                    "is_billable": True,
                    "is_completed": True,
                }
            ],
        }
        client = FakeRunClient(
            projects=[
                dict(PROJECT, id=1, name="BK | First client", retainer_id=2),
                dict(PROJECT, id=5, name="BK | Second client", retainer_id=6),
            ],
            retainers=[
                {"id": 2, "retainer_periods": [dict(PERIOD, id=10, **window)]},
                {"id": 6, "retainer_periods": [dict(PERIOD, id=11, **window)]},
            ],
            tasks_by_pid={1: [task], 5: [task]},
        )

        with (
            patch.object(handler, "ScoroClient", return_value=client),
            patch.object(handler.rates, "load_overcharge_rates"),
            patch.object(handler, "DRY_RUN", False),
            # Serial pool so write order deterministically follows project order.
            patch.object(handler, "MAX_WORKERS", 1),
        ):
            payload = handler.handler()

        self.assertEqual(
            payload["write_ledger"],
            [
                {
                    "project_id": 1,
                    "project_name": "BK | First client",
                    "value": 100.0,
                    "written": True,
                },
                {
                    "project_id": 5,
                    "project_name": "BK | Second client",
                    "value": 100.0,
                    "written": True,
                },
            ],
        )
        # Summary "written" is counted off the ledger and matches actual writes.
        self.assertEqual(payload["summary"]["written"], 2)
        self.assertEqual(len(client.writes), 2)


class WriteLedgerRenderingTests(unittest.TestCase):
    LEDGER = [
        {
            "project_id": 8812,
            "project_name": "BK | Client A",
            "value": 1240.0,
            "written": True,
        },
        {
            "project_id": 9001,
            "project_name": "SM | Client B",
            "value": 350.5,
            "written": True,
        },
    ]

    def test_log_bodies_list_ledger_in_write_order(self):
        common = dict(
            run_date="2026-07-25",
            dry_run=False,
            summary={"dry_run": False, "eligible_projects": 2, "written": 2},
            results=[],
            ineligible=[],
            skipped=[],
            errors=[],
            projects_by_pid={},
            period_by_pid={},
            tasks_by_project={},
            write_ledger=self.LEDGER,
        )

        html = handler.email_report.build_log_html_body(**common)
        text = handler.email_report.build_log_text_body(**common)
        for body in (html, text):
            self.assertIn("#8812", body)
            self.assertIn("#9001", body)
            self.assertLess(body.index("#8812"), body.index("#9001"))
            self.assertIn("1,240.00", body)


class ChangeRenderingTests(unittest.TestCase):
    RESULT = {
        "project_id": 1,
        "project_name": "BK | Test client",
        "service_line": "BK",
        "status": handler.ACTIVE_STATUS,
        "planned_hours": 1.0,
        "logged_hours": 2.0,
        "remaining_hours": -1.0,
        "overcharge_rate": 100.0,
        "overcharge_value": 1240.0,
        "previous_overcharge_value": 980.0,
        "overcharge_delta": 260.0,
    }

    def test_known_previous_value_renders_signed_change(self):
        tile = handler.email_report._project_tile(self.RESULT)
        self.assertIn("was 980.00, +260.00", tile)

        text = handler.email_report._project_detail_text(
            self.RESULT, None, None, [], True, "c_overchargehours"
        )
        self.assertIn("was 980.00, +260.00", text)

    def test_unknown_previous_value_renders_neutrally(self):
        # Older payloads (e.g. saved run JSON) lack the change keys entirely.
        result = {
            k: v
            for k, v in self.RESULT.items()
            if k not in ("previous_overcharge_value", "overcharge_delta")
        }

        tile = handler.email_report._project_tile(result)
        self.assertIn("(new)", tile)
        self.assertNotIn("None", tile)

        text = handler.email_report._project_detail_text(
            result, None, None, [], True, "c_overchargehours"
        )
        self.assertIn("| new", text)
        self.assertNotIn("None", text)


class ErrorAlertTests(unittest.TestCase):
    def test_clean_run_does_not_send_alert(self):
        with patch.object(
            handler.email_report, "send_error_alert"
        ) as send_alert:
            handler._send_error_alert(
                "2026-07-25",
                {"errors": 0},
                [],
            )

        send_alert.assert_not_called()

    def test_failed_run_alerts_testing_recipients(self):
        summary = {
            "eligible_projects": 2,
            "processed": 1,
            "written": 1,
            "errors": 1,
        }
        errors = [{"project_id": 123, "error": "sensitive detail"}]

        with (
            patch.object(handler, "EMAIL_TESTING_TO", ["ops@example.com"]),
            patch.object(handler, "EMAIL_FROM", "reports@example.com"),
            patch.object(handler, "SES_REGION", "eu-north-1"),
            patch.object(handler, "DRY_RUN", False),
            patch.object(
                handler.email_report, "send_error_alert"
            ) as send_alert,
        ):
            handler._send_error_alert("2026-07-25", summary, errors)

        send_alert.assert_called_once_with(
            run_date="2026-07-25",
            dry_run=False,
            summary=summary,
            errors=errors,
            from_addr="reports@example.com",
            to_addrs=["ops@example.com"],
            ses_region="eu-north-1",
        )

    def test_alert_body_contains_project_ids_not_error_details(self):
        with patch.object(
            handler.email_report, "send_ses_email"
        ) as send_email:
            handler.email_report.send_error_alert(
                run_date="2026-07-25",
                dry_run=False,
                summary={
                    "eligible_projects": 2,
                    "processed": 1,
                    "written": 1,
                },
                errors=[
                    {"project_id": 123, "error": "sensitive detail"},
                ],
                from_addr="reports@example.com",
                to_addrs=["ops@example.com"],
                ses_region="eu-north-1",
            )

        text = send_email.call_args.kwargs["text_body"]
        self.assertIn("- 123", text)
        self.assertNotIn("sensitive detail", text)


class FirstNWorkingDaysTests(unittest.TestCase):
    def test_month_starting_midweek(self):
        # August 2026 starts on Saturday; first 3 working days are Mon–Wed 3–5.
        days = handler.first_n_working_days(2026, 8, 3)
        self.assertEqual(
            days,
            [date(2026, 8, 3), date(2026, 8, 4), date(2026, 8, 5)],
        )

    def test_month_starting_on_monday(self):
        # June 2026 starts on Monday.
        days = handler.first_n_working_days(2026, 6, 3)
        self.assertEqual(
            days,
            [date(2026, 6, 1), date(2026, 6, 2), date(2026, 6, 3)],
        )

    def test_n_equals_one(self):
        # February 2026 starts on Sunday → first working day is Mon 2nd.
        self.assertEqual(
            handler.first_n_working_days(2026, 2, 1),
            [date(2026, 2, 2)],
        )

    def test_is_in_window(self):
        self.assertTrue(
            handler.is_in_first_n_working_days(date(2026, 8, 3), 3)
        )
        self.assertTrue(
            handler.is_in_first_n_working_days(date(2026, 8, 5), 3)
        )
        self.assertFalse(
            handler.is_in_first_n_working_days(date(2026, 8, 1), 3)
        )  # Saturday
        self.assertFalse(
            handler.is_in_first_n_working_days(date(2026, 8, 6), 3)
        )  # past window


class AlternateFieldKeyTests(unittest.TestCase):
    def setUp(self):
        self.previous_rates = rates._overcharge
        rates._overcharge = {"BK": 100.0}

    def tearDown(self):
        rates._overcharge = self.previous_rates

    def test_writes_and_reads_the_alternate_field(self):
        field_key = handler.LAST_MONTH_OVERCHARGE_FIELD_KEY
        project = dict(
            PROJECT,
            custom_fields=[{"id": field_key, "value": "50"}],
        )
        client = FakeClient([])

        with patch.object(handler, "DRY_RUN", False):
            result = handler.process_project(
                client,
                project,
                [IN_PERIOD_TASK],
                {1: PERIOD},
                field_key=field_key,
            )

        self.assertEqual(result["previous_overcharge_value"], 50.0)
        self.assertEqual(result["overcharge_delta"], 50.0)
        self.assertEqual(
            client.writes,
            [("projects", 1, field_key, 100.0)],
        )


class FirstNWorkingDaysGuardTests(unittest.TestCase):
    def test_off_window_returns_skip_without_scoro_calls(self):
        # Pick a date known to be outside the first-3 working-day window.
        off_window = date(2026, 8, 10)  # Monday, well past Aug 3–5

        class Boom:
            def __init__(self, *args, **kwargs):
                raise AssertionError("ScoroClient must not be constructed")

        class FixedDateTime(datetime):
            @classmethod
            def utcnow(cls):
                return datetime(
                    off_window.year, off_window.month, off_window.day, 12, 0, 0
                )

        with (
            patch.object(handler, "ScoroClient", Boom),
            patch.object(handler, "datetime", FixedDateTime),
        ):
            payload = handler.handler({
                "trigger_mode": "first_n_working_days",
                "days": 3,
                "send_email": False,
            })

        self.assertEqual(
            payload,
            {
                "skipped": True,
                "reason": "not_in_first_n_working_days",
                "days": 3,
                "run_date": off_window.isoformat(),
            },
        )


class LocalCliTests(unittest.TestCase):
    def test_cli_delegates_to_guarded_handler(self):
        expected = {"summary": {"dry_run": True}}

        with (
            patch.object(
                write_overcharge, "run_handler", return_value=expected
            ) as run_handler,
            patch("builtins.print") as print_result,
        ):
            result = write_overcharge.main()

        self.assertEqual(result, expected)
        run_handler.assert_called_once_with()
        print_result.assert_called_once()


if __name__ == "__main__":
    unittest.main()
