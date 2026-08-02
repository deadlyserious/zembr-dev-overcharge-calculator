"""Calculation engine for retainer overcharge.

Selects the current billing period, sums nested time entries, and computes
overcharge value. Called by handler.py.
"""

from datetime import date, datetime
from rates import get_overcharge_rate


# ---- helpers ----------------------------------------------------------------

def _truthy(v):
    """Normalises truthyness.
    Return True if the value is truthy (1, true, True, TRUE)."""
    return str(v) in ("1", "true", "True", "TRUE")

def _parse_date(s):
    """Parse a date string into a datetime object."""
    if not s:
        return None
    s = str(s).strip()
    if s.startswith("0000"):  # Scoro junk sentinel
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[: len(fmt) + 2], fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None

def _entry_datetime(entry):
    """Bucketing key: completed_datetime, fallback start_datetime."""
    return _parse_date(entry.get("completed_datetime")) or _parse_date(
        entry.get("start_datetime")
    )

def _duration_to_seconds(value):
    """Accept seconds (int) or 'HH:MM:SS' strings. Nested entries are populated;
    standalone endpoint returns '00:00:00' (we never read that path here)."""
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    parts = str(value).split(":")
    try:
        if len(parts) == 3:
            h, m, s = (int(x) for x in parts)
            return h * 3600 + m * 60 + s
        return int(float(value))
    except ValueError:
        return 0


# ---- period selection -------------------------------------------------------

def select_current_period(periods, today=None):
    """Return the period whose [start_date, end_date] window contains today."""
    today = today or date.today()
    candidates = []
    for p in periods:
        if p.get("duration") is None and p.get("sum") is None:
            continue  # not a billing period (e.g. the contract container)
        start = _parse_date(p.get("start_date"))
        end = _parse_date(p.get("end_date"))
        if not start or not end:
            continue
        if start.date() <= today <= end.date():
            span = (end - start).days
            candidates.append((span, p))
    if not candidates:
        return None
    candidates.sort(key=lambda c: c[0])  # narrowest first
    return candidates[0][1]


# ---- billable summing -------------------------------------------------------

def period_bounds(period):
    """Return (start, end) datetimes for a retainer period."""
    return _parse_date(period.get("start_date")), _parse_date(period.get("end_date"))

def _iter_period_entries(tasks, period_start, period_end):
    """Yield (task, entry, when, duration_seconds) for billable entries in period."""
    ps = period_start.date() if isinstance(period_start, datetime) else period_start
    pe = period_end.date() if isinstance(period_end, datetime) else period_end

    for task in tasks:
        for e in task.get("time_entries") or []:
            if _truthy(e.get("is_deleted")):
                continue
            if not (_truthy(e.get("is_billable")) and _truthy(e.get("is_completed"))):
                continue
            when = _entry_datetime(e)
            if not when or not (ps <= when.date() <= pe):
                continue
            dur = _duration_to_seconds(e.get("billable_duration"))
            yield task, e, when, dur

def sum_billable_seconds(tasks, period_start, period_end):
    """Sum billable_duration (seconds) for billable entries in [period_start, period_end]."""
    return sum(
        dur
        for _task, _e, _when, dur in _iter_period_entries(
            tasks, period_start, period_end
        )
    )

def list_period_entries(tasks, period_start, period_end):
    """List billable time entries in the current period (for logging/reporting)."""
    entries = []
    for task, e, when, dur in _iter_period_entries(
        tasks, period_start, period_end
    ):
        entries.append(
            {
                "task_id": task.get("task_id") or task.get("id"),
                "time_entry_id": e.get("time_entry_id") or e.get("id"),
                "datetime": when.isoformat(),
                "duration_hours": round(dur / 3600.0, 4),
            }
        )
    return entries


def list_all_period_entries(tasks, period_start, period_end):
    """All non-deleted time entries in the period, with billable flag and reason.

    Unlike list_period_entries, this includes non-billable and non-completed
    entries so the email report can show why each entry was or wasn't counted.
    """
    ps = period_start.date() if isinstance(period_start, datetime) else period_start
    pe = period_end.date() if isinstance(period_end, datetime) else period_end
    entries = []
    for task in tasks:
        for e in task.get("time_entries") or []:
            if _truthy(e.get("is_deleted")):
                continue
            when = _entry_datetime(e)
            if not when or not (ps <= when.date() <= pe):
                continue
            is_b = _truthy(e.get("is_billable"))
            is_c = _truthy(e.get("is_completed"))
            if is_b and is_c:
                billable, reason = True, ""
            elif not is_b and not is_c:
                billable, reason = False, "not billable, not completed"
            elif not is_b:
                billable, reason = False, "not billable"
            else:
                billable, reason = False, "not completed"
            dur_raw = e.get("billable_duration") if billable else (e.get("duration") or e.get("billable_duration"))
            dur = _duration_to_seconds(dur_raw)
            entries.append({
                "task_id": task.get("task_id") or task.get("id"),
                "time_entry_id": e.get("time_entry_id") or e.get("id"),
                "datetime": when.isoformat(),
                "duration_hours": round(dur / 3600.0, 4),
                "billable": billable,
                "reason": reason,
            })
    return sorted(entries, key=lambda x: x["datetime"])


# ---- pricing ----------------------------------------------------------------

def overcharge_value(logged_hours, planned_hours, over_rate):
    """The number written to the custom field: the overcharge portion only.

    Within budget -> 0 overcharge (still tracked so dashboards show 0).
    Over budget   -> (logged - planned) * overcharge_rate.
    """
    if logged_hours <= planned_hours:
        return 0.0
    return (logged_hours - planned_hours) * over_rate


# ---- project compute --------------------------------------------------------

def compute_project(period, tasks, service_line):
    """Combine allowance, billable time, and rates into a result dict.

    `period` is the selected current retainer period.
    `tasks` is the project's task list with nested time_entries.
    """
    period_seconds = int(period.get("duration") or 0)
    planned_hours = period_seconds / 3600.0

    pstart, pend = period_bounds(period)
    billable_sec = sum_billable_seconds(tasks, pstart, pend)
    logged_hours = billable_sec / 3600.0

    over = get_overcharge_rate(service_line)
    oc_value = overcharge_value(logged_hours, planned_hours, over)

    return {
        "service_line": service_line,
        "planned_hours": round(planned_hours, 4),
        "logged_hours": round(logged_hours, 4),
        "remaining_hours": round(planned_hours - logged_hours, 4),
        "overcharge_rate": over,
        "overcharge_value": round(oc_value, 2),
    }
