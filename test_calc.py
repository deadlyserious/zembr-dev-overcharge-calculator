import unittest
from datetime import date, datetime

import calc
import rates


PERIOD = {
    "id": 10,
    "duration": 36000,  # 10 h allowance
    "sum": 100,
    "start_date": "2026-07-01",
    "end_date": "2026-07-31",
}
PSTART, PEND = calc.period_bounds(PERIOD)

ENTRY = {
    "id": 30,
    "completed_datetime": "2026-07-15 12:00:00",
    "billable_duration": 3600,
    "is_billable": True,
    "is_completed": True,
}


def _task(*entries):
    return {"id": 20, "time_entries": list(entries)}


class SelectCurrentPeriodTests(unittest.TestCase):
    def test_picks_the_period_containing_today(self):
        june = dict(PERIOD, id=11, start_date="2026-06-01", end_date="2026-06-30")

        picked = calc.select_current_period([june, PERIOD], today=date(2026, 7, 15))

        self.assertEqual(picked["id"], 10)

    def test_explicit_today_argument_decides_the_period(self):
        june = dict(PERIOD, id=11, start_date="2026-06-01", end_date="2026-06-30")
        periods = [june, PERIOD]

        self.assertEqual(
            calc.select_current_period(periods, today=date(2026, 6, 15))["id"], 11
        )
        self.assertEqual(
            calc.select_current_period(periods, today=date(2026, 7, 15))["id"], 10
        )

    def test_narrowest_window_wins_when_several_contain_today(self):
        # A quarter-long window and a monthly window both contain today: the
        # narrower monthly window is the current billing period, regardless of
        # list order.
        quarter = dict(PERIOD, id=12, start_date="2026-07-01", end_date="2026-09-30")

        for periods in ([quarter, PERIOD], [PERIOD, quarter]):
            picked = calc.select_current_period(periods, today=date(2026, 7, 15))
            self.assertEqual(picked["id"], 10)

    def test_contract_container_without_duration_or_sum_is_skipped(self):
        container = {
            "id": 1,
            "duration": None,
            "sum": None,
            "start_date": "2026-01-01",
            "end_date": "2026-12-31",
        }

        picked = calc.select_current_period(
            [container, PERIOD], today=date(2026, 7, 15)
        )

        self.assertEqual(picked["id"], 10)
        self.assertIsNone(
            calc.select_current_period([container], today=date(2026, 7, 15))
        )

    def test_period_with_only_sum_is_still_a_candidate(self):
        sum_based = dict(PERIOD, id=15, duration=None)

        picked = calc.select_current_period([sum_based], today=date(2026, 7, 15))

        self.assertEqual(picked["id"], 15)

    def test_unparseable_or_missing_dates_are_skipped(self):
        junk_start = dict(PERIOD, id=13, start_date="0000-00-00 00:00:00")
        missing_end = dict(PERIOD, id=14, end_date=None)

        self.assertIsNone(
            calc.select_current_period(
                [junk_start, missing_end], today=date(2026, 7, 15)
            )
        )

    def test_boundary_days_are_inclusive(self):
        for today in (date(2026, 7, 1), date(2026, 7, 31)):
            picked = calc.select_current_period([PERIOD], today=today)
            self.assertEqual(picked["id"], 10)

        for today in (date(2026, 6, 30), date(2026, 8, 1)):
            self.assertIsNone(calc.select_current_period([PERIOD], today=today))

    def test_returns_none_when_nothing_matches(self):
        self.assertIsNone(calc.select_current_period([], today=date(2026, 7, 15)))


class ParseDateTests(unittest.TestCase):
    def test_scoro_datetime_formats(self):
        expected = datetime(2026, 7, 15, 12, 34, 56)
        self.assertEqual(calc._parse_date("2026-07-15 12:34:56"), expected)
        self.assertEqual(calc._parse_date("2026-07-15T12:34:56"), expected)
        self.assertEqual(calc._parse_date("2026-07-15"), datetime(2026, 7, 15))

    def test_junk_sentinel_and_empty_values_are_none(self):
        self.assertIsNone(calc._parse_date("0000-00-00 00:00:00"))
        self.assertIsNone(calc._parse_date(""))
        self.assertIsNone(calc._parse_date(None))

    def test_garbage_is_none(self):
        self.assertIsNone(calc._parse_date("not a date"))


class DurationToSecondsTests(unittest.TestCase):
    def test_numeric_and_hms_values(self):
        self.assertEqual(calc._duration_to_seconds(3600), 3600)
        self.assertEqual(calc._duration_to_seconds(3600.9), 3600)  # truncates
        self.assertEqual(calc._duration_to_seconds("01:30:15"), 5415)
        self.assertEqual(calc._duration_to_seconds("5400"), 5400)
        self.assertEqual(calc._duration_to_seconds("5400.5"), 5400)

    def test_garbage_and_missing_values_are_zero(self):
        self.assertEqual(calc._duration_to_seconds("garbage"), 0)
        self.assertEqual(calc._duration_to_seconds("1:30"), 0)
        self.assertEqual(calc._duration_to_seconds(None), 0)


class EntryDatetimeTests(unittest.TestCase):
    def test_completed_datetime_preferred_over_start(self):
        entry = {
            "completed_datetime": "2026-07-15 12:00:00",
            "start_datetime": "2026-07-10 09:00:00",
        }
        self.assertEqual(
            calc._entry_datetime(entry), datetime(2026, 7, 15, 12, 0, 0)
        )

    def test_junk_completed_falls_back_to_start(self):
        entry = {
            "completed_datetime": "0000-00-00 00:00:00",
            "start_datetime": "2026-07-10 09:00:00",
        }
        self.assertEqual(
            calc._entry_datetime(entry), datetime(2026, 7, 10, 9, 0, 0)
        )

    def test_neither_datetime_is_none(self):
        self.assertIsNone(calc._entry_datetime({}))


class SumBillableSecondsTests(unittest.TestCase):
    def test_deleted_non_billable_and_non_completed_are_excluded(self):
        tasks = [
            _task(
                ENTRY,
                dict(ENTRY, id=31, is_deleted="1"),
                dict(ENTRY, id=32, is_billable=False),
                dict(ENTRY, id=33, is_completed=False),
            )
        ]

        self.assertEqual(calc.sum_billable_seconds(tasks, PSTART, PEND), 3600)

    def test_truthy_flag_variants(self):
        for truthy in ("1", "true", True):
            tasks = [_task(dict(ENTRY, is_billable=truthy, is_completed=truthy))]
            self.assertEqual(
                calc.sum_billable_seconds(tasks, PSTART, PEND), 3600, truthy
            )

        for falsy in (0, "0", False, None):
            tasks = [_task(dict(ENTRY, is_billable=falsy))]
            self.assertEqual(
                calc.sum_billable_seconds(tasks, PSTART, PEND), 0, falsy
            )

    def test_period_boundary_days_are_inclusive(self):
        tasks = [
            _task(
                dict(ENTRY, id=31, completed_datetime="2026-07-01 00:00:00"),
                dict(ENTRY, id=32, completed_datetime="2026-07-31 23:59:59"),
                dict(ENTRY, id=33, completed_datetime="2026-06-30 23:59:59"),
                dict(ENTRY, id=34, completed_datetime="2026-08-01 00:00:00"),
            )
        ]

        self.assertEqual(calc.sum_billable_seconds(tasks, PSTART, PEND), 7200)

    def test_entry_date_decides_not_task_dates(self):
        # The task's own dates are ignored: an in-period entry on a task dated
        # outside the period counts, an out-of-period entry on a task dated
        # inside the period does not.
        outside_task = dict(
            _task(ENTRY),
            start_datetime="2025-01-01 00:00:00",
            datetime_due="2025-12-31 00:00:00",
        )
        inside_task = dict(
            _task(dict(ENTRY, completed_datetime="2026-08-05 10:00:00")),
            start_datetime="2026-07-01 00:00:00",
            datetime_due="2026-07-20 00:00:00",
        )

        self.assertEqual(calc.sum_billable_seconds([outside_task], PSTART, PEND), 3600)
        self.assertEqual(calc.sum_billable_seconds([inside_task], PSTART, PEND), 0)

    def test_entry_without_a_date_is_excluded(self):
        tasks = [_task(dict(ENTRY, completed_datetime=None))]

        self.assertEqual(calc.sum_billable_seconds(tasks, PSTART, PEND), 0)


class ListPeriodEntriesTests(unittest.TestCase):
    def test_rows_carry_ids_datetime_and_hours(self):
        tasks = [_task(dict(ENTRY, billable_duration=5400))]

        rows = calc.list_period_entries(tasks, PSTART, PEND)

        self.assertEqual(
            rows,
            [
                {
                    "task_id": 20,
                    "time_entry_id": 30,
                    "datetime": "2026-07-15T12:00:00",
                    "duration_hours": 1.5,
                }
            ],
        )

    def test_task_id_and_time_entry_id_take_precedence_over_id(self):
        tasks = [
            {
                "task_id": 99,
                "time_entries": [dict(ENTRY, time_entry_id=88)],
            }
        ]

        rows = calc.list_period_entries(tasks, PSTART, PEND)

        self.assertEqual(rows[0]["task_id"], 99)
        self.assertEqual(rows[0]["time_entry_id"], 88)


class ListAllPeriodEntriesTests(unittest.TestCase):
    def test_reason_strings_for_each_flag_combination(self):
        tasks = [
            _task(
                dict(ENTRY, id=31),
                dict(ENTRY, id=32, is_billable=False),
                dict(ENTRY, id=33, is_completed=False),
                dict(ENTRY, id=34, is_billable=False, is_completed=False),
            )
        ]

        rows = calc.list_all_period_entries(tasks, PSTART, PEND)
        by_id = {r["time_entry_id"]: r for r in rows}

        self.assertTrue(by_id[31]["billable"])
        self.assertEqual(by_id[31]["reason"], "")
        self.assertFalse(by_id[32]["billable"])
        self.assertEqual(by_id[32]["reason"], "not billable")
        self.assertEqual(by_id[33]["reason"], "not completed")
        self.assertEqual(by_id[34]["reason"], "not billable, not completed")

    def test_non_billable_rows_fall_back_to_duration(self):
        # Non-billable entries usually carry billable_duration 0; the report
        # shows their tracked duration instead, falling back to
        # billable_duration only when duration is missing.
        tasks = [
            _task(
                dict(ENTRY, id=31, is_billable=False, duration="01:00:00", billable_duration=0),
                dict(ENTRY, id=32, is_billable=False, billable_duration=1800),
            )
        ]

        rows = calc.list_all_period_entries(tasks, PSTART, PEND)
        by_id = {r["time_entry_id"]: r for r in rows}

        self.assertEqual(by_id[31]["duration_hours"], 1.0)
        self.assertEqual(by_id[32]["duration_hours"], 0.5)

    def test_sorted_by_datetime_and_deleted_excluded(self):
        tasks = [
            _task(
                dict(ENTRY, id=31, completed_datetime="2026-07-20 09:00:00"),
                dict(ENTRY, id=32, completed_datetime="2026-07-05 09:00:00"),
                dict(ENTRY, id=33, is_deleted=True),
            )
        ]

        rows = calc.list_all_period_entries(tasks, PSTART, PEND)

        self.assertEqual([r["time_entry_id"] for r in rows], [32, 31])


class OverchargeValueTests(unittest.TestCase):
    def test_within_budget_is_zero(self):
        self.assertEqual(calc.overcharge_value(5.0, 10.0, 120.0), 0.0)

    def test_exact_budget_is_zero(self):
        self.assertEqual(calc.overcharge_value(10.0, 10.0, 120.0), 0.0)

    def test_over_budget_charges_the_excess_only(self):
        self.assertEqual(calc.overcharge_value(12.5, 10.0, 120.0), 300.0)


class ComputeProjectTests(unittest.TestCase):
    def setUp(self):
        self.previous_rates = rates._overcharge
        rates._overcharge = {"BK": 100.0}

    def tearDown(self):
        rates._overcharge = self.previous_rates

    def test_over_budget_project_end_to_end(self):
        tasks = [
            _task(
                dict(ENTRY, billable_duration=3600 * 12),
                dict(ENTRY, id=31, billable_duration=1800),
            )
        ]

        result = calc.compute_project(PERIOD, tasks, "BK")

        self.assertEqual(
            result,
            {
                "service_line": "BK",
                "planned_hours": 10.0,
                "logged_hours": 12.5,
                "remaining_hours": -2.5,
                "overcharge_rate": 100.0,
                "overcharge_value": 250.0,
            },
        )

    def test_within_budget_project_writes_zero(self):
        tasks = [_task(ENTRY)]

        result = calc.compute_project(PERIOD, tasks, "BK")

        self.assertEqual(result["logged_hours"], 1.0)
        self.assertEqual(result["remaining_hours"], 9.0)
        self.assertEqual(result["overcharge_value"], 0.0)

    def test_hours_rounded_to_four_places(self):
        tasks = [_task(dict(ENTRY, billable_duration=1000))]  # 0.27777... h

        result = calc.compute_project(PERIOD, tasks, "BK")

        self.assertEqual(result["logged_hours"], 0.2778)
        self.assertEqual(result["remaining_hours"], 9.7222)

    def test_value_rounded_to_two_places(self):
        tasks = [_task(dict(ENTRY, billable_duration=36000 + 1000))]  # 27.777... over

        result = calc.compute_project(PERIOD, tasks, "BK")

        self.assertEqual(result["overcharge_value"], 27.78)


if __name__ == "__main__":
    unittest.main()
