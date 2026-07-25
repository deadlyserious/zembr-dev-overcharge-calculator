import os
import unittest
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
