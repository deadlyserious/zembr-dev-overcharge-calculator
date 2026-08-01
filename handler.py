"""Compute Scoro project overcharges and write them back via AWS Lambda.

Runs on a weekly EventBridge cron (scheduled for Sunday to ensure no tasks
are still running, freezing the snapshot and preventing data drift).

Flow:
1. Select active projects with a retainer_id, non-zero allowance,
   and non-zero billable time.
2. Fetch the current retainer period (allowance) and billable time entries.
3. Compute overcharge: (Billable Time - Allowance) * Overcharge Rate.
4. If >0, write overcharge back to the project's custom field (c_overchargehours).

Guards:
- In DRY_RUN mode, logs results without writing to Scoro.

Environment Variables:
    SCORO_API_KEY (str): Authenticates Scoro API requests.
    SCORO_COMPANY_ACCOUNT_ID (str): Scoro account subdomain the API requests target.
    DRY_RUN (bool/str): If 'true', logs calculations without writing to Scoro.

Overcharge rates come from Scoro products (rates.load_overcharge_rates), not
from an environment variable.
"""

import calendar
import json
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta

import calc
import email_report
import rates
from scoro_client import ScoroClient, ScoroError
from service_lines import overcharge_rate_line, service_line_from_project
from scoro_api_key import get_scoro_api_key


# ---- logging ----------------------------------------------------------------

log = logging.getLogger("overcharge_calculator")
log.setLevel(logging.INFO)


# ---- configuration ----------------------------------------------------------

def _parse_bool_env(name, default):
    raw = os.environ.get(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value == "true":
        return True
    if value == "false":
        return False
    raise RuntimeError(
        f"{name} must be 'true' or 'false', got {raw!r}"
    )


try:
    API_KEY = get_scoro_api_key()
    COMPANY_ACCOUNT_ID = os.environ["SCORO_COMPANY_ACCOUNT_ID"]
except KeyError as e:
    raise RuntimeError(f"Missing required environment variable: {e}") from e

DRY_RUN = _parse_bool_env("DRY_RUN", True)
OVERCHARGE_FIELD_KEY = os.environ.get("OVERCHARGE_FIELD_KEY", "c_overchargehours")
ACTIVE_STATUS = "additional6"
AT_RISK_STATUS = "additional8"
HANDOVER_IN_PROGRESS_STATUS = "pending"
ON_HOLD_STATUS = "future"
ELIGIBLE_STATUSES = frozenset(
    {ACTIVE_STATUS, AT_RISK_STATUS, HANDOVER_IN_PROGRESS_STATUS, ON_HOLD_STATUS}
)
# Scoro status "completed" displays as "Subscription cancelled" in the report.
CANCELLED_SUB_STATUS = "completed"
# Concurrency for the per-project pipeline. I/O-bound (urllib releases the GIL on
# network waits), so threads cut wall time. Keep modest to respect Scoro's rate
# limit (the client backs off on 429). Override via MAX_WORKERS.
MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "8"))
# Global request rate cap (req/sec) shared across worker threads. Scoro 429s
# above ~25-80 req/2s depending on plan. The account is on Ultimate (80/2s = 40/s),
# so 30/s drains the now-small page count fast while keeping margin under the ceiling.
REQS_PER_SEC = float(os.environ.get("REQS_PER_SEC", "30"))
# Look-back pad (days) applied to the task-fetch modified_date filter. That filter
# is only a fetch optimisation — calc still buckets each entry by completed_datetime
# within the period. Padding the window backwards catches "retrospective" entries on
# tasks whose modified_date predates the period start (bulk edits, imports, status-
# only changes), which would otherwise be dropped at the fetch stage and undercounted.
# Override via TASK_FETCH_LOOKBACK_DAYS.
TASK_FETCH_LOOKBACK_DAYS = int(os.environ.get("TASK_FETCH_LOOKBACK_DAYS", "14"))
# Email via SES. Report (HTML) goes to EMAIL_REPORT_TO; log (HTML + text) goes to
# EMAIL_LOG_TO. In DRY_RUN both go to EMAIL_TESTING_TO instead.
# EMAIL_TO is a legacy alias for EMAIL_REPORT_TO.
def _parse_email_list(raw):
    return [a.strip() for a in raw.split(",") if a.strip()]


EMAIL_FROM = os.environ.get("EMAIL_FROM", "")
EMAIL_REPORT_TO = _parse_email_list(
    os.environ.get("EMAIL_REPORT_TO", "") or os.environ.get("EMAIL_TO", "")
)
EMAIL_LOG_TO = _parse_email_list(os.environ.get("EMAIL_LOG_TO", ""))
EMAIL_TESTING_TO = _parse_email_list(os.environ.get("EMAIL_TESTING_TO", ""))
SES_REGION = os.environ.get("SES_REGION") or os.environ.get("AWS_REGION", "eu-north-1")


# ---- helpers ----------------------------------------------------------------

# skip payload for a project that was not calculated
def _skip(project_id, reason):  
    return {"project_id": project_id, "skipped": reason}

# audit row for a project excluded as ineligible
def _ineligible_record(project, reason):
    name = _project_name(project)
    record = {
        "project_id": _project_id(project),
        "name": name,
        "reason": reason,
    }
    service_line = service_line_from_project(name)
    if service_line:
        record["service_line"] = service_line
    status = project.get("status")
    if status is not None:
        record["status"] = status
    return record


def _skipped_record(project, reason, period_by_pid=None):
    """Audit row for an eligible project that could not be calculated."""
    pid = _project_id(project)
    name = _project_name(project)
    record = {
        "project_id": pid,
        "name": name,
        "reason": reason,
    }
    retainer_id = project.get("retainer_id")
    if retainer_id is not None:
        record["retainer_id"] = retainer_id

    period = (period_by_pid or {}).get(pid)
    if period:
        record["period_start"] = (period.get("start_date") or "")[:10]
        record["period_end"] = (period.get("end_date") or "")[:10]
        duration_secs = int(period.get("duration") or 0)
        record["period_duration_secs"] = duration_secs
        if duration_secs:
            record["period_allowance_hours"] = round(duration_secs / 3600, 2)
        period_sum = period.get("sum")
        if period_sum is not None:
            record["period_sum"] = period_sum

    if name and "|" in name:
        record["name_prefix"] = name.split("|", 1)[0].strip()

    service_line = service_line_from_project(name)
    if service_line:
        record["service_line"] = service_line

    status = project.get("status")
    if status is not None:
        record["status"] = status

    return record

# normalise Scoro project id field
def _project_id(project):
    return project.get("project_id") or project.get("id")

# normalise Scoro project name field
def _project_name(project):
    return project.get("project_name") or project.get("name") or ""


def _custom_field(project, field_id):
    """Return a Scoro custom field value from project.custom_fields, or None."""
    for field in project.get("custom_fields") or []:
        if field.get("id") == field_id:
            return field.get("value")
    return None


def _previous_overcharge(project):
    """Return the project's current OVERCHARGE_FIELD_KEY value as float, or None.

    Last run's value is whatever the write-back left in the custom field, so it
    doubles as the previous reading. Parse defensively: None, "" or non-numeric
    text mean "unknown"; ints, floats and numeric strings become floats.
    """
    raw = _custom_field(project, OVERCHARGE_FIELD_KEY)
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


# ---- fetch ------------------------------------------------------------------

def select_projects(projects):
    """Return (eligible, ineligible) — active projects with a retainer."""
    eligible = []
    ineligible = []
    for p in projects:
        # Exclude projects without a retainer_id
        if not p.get("retainer_id"):
            ineligible.append(_ineligible_record(p, "no retainer_id"))
            continue
        # Exclude inactive projects (active and at-risk are eligible)
        status = p.get("status")
        if status and status not in ELIGIBLE_STATUSES:
            ineligible.append(
                _ineligible_record(p, f"not active (status={status})")
            )
            continue
        # Add eligible projects to the list
        eligible.append(p)
    return eligible, ineligible


def _previous_calendar_month_bounds(run_date):
    """Return (first_day, last_day) for the calendar month before run_date."""
    try:
        d = datetime.strptime(run_date, "%Y-%m-%d").date()
    except ValueError:
        d = datetime.utcnow().date()
    first_this_month = d.replace(day=1)
    last_prev = first_this_month - timedelta(days=1)
    first_prev = last_prev.replace(day=1)
    return first_prev, last_prev


def build_cancelled_subs(projects, run_date):
    """Return subscription-cancelled retainer projects from the previous calendar month."""
    period_start, _period_end = _previous_calendar_month_bounds(run_date)

    records = []
    for p in projects:
        if p.get("status") != CANCELLED_SUB_STATUS:
            continue
        if not p.get("retainer_id"):
            continue
        name = _project_name(p)
        service_line = service_line_from_project(name)
        if not service_line:
            continue
        cancel = calc._parse_date(_custom_field(p, "c_cancellationmonth"))
        if not cancel:
            continue
        cancel_date = cancel.date()
        if (cancel_date.year, cancel_date.month) != (
            period_start.year,
            period_start.month,
        ):
            continue
        records.append({
            "project_id": _project_id(p),
            "name": name,
            "status": CANCELLED_SUB_STATUS,
            "cancellation_month": cancel_date.isoformat(),
            "retainer_id": p.get("retainer_id"),
            "service_line": service_line,
        })

    records.sort(key=lambda r: r["cancellation_month"], reverse=True)
    return records


def _task_fetch_from(period_start, lookback_days=TASK_FETCH_LOOKBACK_DAYS):
    """Return period_start minus lookback_days as 'YYYY-MM-DD' (or '' if unparseable).

    The task fetch filters on modified_date only to avoid pulling lifetime task
    history; calc still buckets each entry by completed_datetime within the period.
    Padding the fetch window backwards means a retrospective entry on a task last
    modified shortly before the period opened is still fetched (then correctly
    bucketed in-period), instead of being silently dropped at the fetch stage.
    """
    if not period_start:
        return ""
    try:
        d = datetime.strptime(period_start, "%Y-%m-%d").date()
    except ValueError:
        return ""
    return (d - timedelta(days=lookback_days)).isoformat()


def fetch_tasks_by_project(client, projects, period_by_pid):
    """Return (tasks_by_project, errors), fetching each project in parallel.

    The flat ``timeEntries`` list endpoint does not include ``project_id`` in
    its response rows (only ``event_id`` / task id), so we cannot use it to
    group entries by project. Instead we fall back to the original per-project
    ``tasks/list`` approach — but with a thread pool so all projects run
    concurrently rather than serially (which was the original timeout cause).

    Each project only fetches tasks if it has a resolved current period (from
    ``period_by_pid``). ``detailed_response=True`` makes Scoro nest the
    ``time_entries`` list inside each task row, which is the shape
    ``calc._iter_period_entries`` already consumes.

    The modified-date filter is only an optimisation. If it finds no in-period
    entries, retry without that filter before treating the period as empty.
    Fetch failures are returned separately and must never be interpreted as
    authoritative zero usage.
    """
    def _fetch(project):
        pid = _project_id(project)
        period = period_by_pid.get(pid)
        if not period:
            return pid, [], None
        # Filter by modified_date to skip historical tasks with no recent activity,
        # keeping each project to 1-2 pages instead of its full lifetime history.
        # Pad the window back by TASK_FETCH_LOOKBACK_DAYS so retrospective entries on
        # tasks modified just before the period opened are still fetched (calc then
        # buckets them by completed_datetime within the period).
        period_start = period.get("start_date", "")[:10]  # YYYY-MM-DD
        filt = {"project_id": pid}
        fetch_from = _task_fetch_from(period_start)
        if fetch_from:
            filt["modified_date"] = {"from": fetch_from}
        fetch_stage = "filtered"
        try:
            tasks = client.list_all(
                "tasks",
                filter=filt,
                detailed_response=True,
                per_page=25,
            )
            pstart, pend = calc.period_bounds(period)
            if fetch_from and not calc.list_period_entries(tasks, pstart, pend):
                fetch_stage = "verification"
                log.info(
                    "filtered task fetch found no in-period entries for project %s; "
                    "verifying without modified_date",
                    pid,
                )
                tasks = client.list_all(
                    "tasks",
                    filter={"project_id": pid},
                    detailed_response=True,
                    per_page=25,
                )
            return pid, tasks, None
        except ScoroError as e:
            log.warning(
                "%s tasks fetch failed for project %s: %s",
                fetch_stage,
                pid,
                e,
            )
            return pid, None, {
                "project_id": pid,
                "error": f"{fetch_stage} tasks fetch failed: {e}",
            }

    workers = max(1, min(MAX_WORKERS, len(projects)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        pairs = list(pool.map(_fetch, projects))

    result = {}
    errors = []
    total_tasks = 0
    for pid, tasks, error in pairs:
        if error:
            errors.append(error)
            continue
        result[pid] = tasks
        total_tasks += len(tasks)

    log.info(
        "fetched %d tasks across %d/%d eligible projects (%d failed)",
        total_tasks, len(result), len(projects), len(errors),
    )
    return result, errors


def _retainer_periods(retainer):
    """Pull the nested period list out of a retainer record (handles both shapes)."""
    return (
        retainer.get("retainer_periods")
        or retainer.get("data", {}).get("retainer_periods")
        or []
    )


def fetch_retainers_by_id(client):
    """Bulk-fetch every retainer once and index by id.

    Replaces the per-project ``retainers/view`` N+1 (one serial round-trip per
    eligible project — the main cause of the timeout) with a single paginated
    list. ``detailed_response`` is requested so periods come back nested; if the
    list endpoint doesn't nest them, ``_load_retainer_period`` falls back to a
    per-id ``view`` for that retainer only.

    If ``retainers/list`` is not supported by this Scoro account, returns an
    empty dict so ``resolve_periods`` falls back to per-id ``view`` calls.
    """
    try:
        retainers = client.list_all_parallel(
            "retainers", detailed_response=True, per_page=25, window=MAX_WORKERS
        )
    except ScoroError as e:
        log.warning("retainers/list unsupported (%s); falling back to per-id view", e)
        return {}
    by_id = {}
    for r in retainers:
        rid = r.get("id") or r.get("retainer_id")
        if rid is not None:
            by_id[rid] = r
    with_periods = sum(1 for r in by_id.values() if _retainer_periods(r))
    log.info(
        "fetched %d retainers (%d with nested periods)", len(by_id), with_periods
    )
    return by_id


def resolve_periods(client, projects, retainers_by_id, today):
    """Select each eligible project's current retainer period, up front.

    Run before the time-entry fetch so the global date window can be derived from
    the selected periods (see ``fetch_time_entries_by_project``). Uses the prefetched
    ``retainers_by_id`` map when it carries nested periods; otherwise falls back to a
    per-id ``retainers/view`` for that retainer only. ``today`` (a datetime.date —
    the UTC run date) decides which period is current, so period selection can
    never disagree with the reported run date. Returns ``period_by_pid``
    (pid -> selected raw period dict); projects with no current period are absent.
    """
    period_by_pid = {}
    for project in projects:
        pid = _project_id(project)
        retainer_id = project.get("retainer_id")
        retainer = (retainers_by_id or {}).get(retainer_id)
        periods = _retainer_periods(retainer) if retainer else []
        # Fall back to a single view() if the bulk record lacked nested periods.
        if not periods and retainer_id is not None:
            retainer = client.view("retainers", retainer_id)
            periods = _retainer_periods(retainer)
        period = calc.select_current_period(periods, today=today)
        if period:
            period_by_pid[pid] = period
    log.info(
        "resolved current periods for %d/%d eligible projects",
        len(period_by_pid), len(projects),
    )
    return period_by_pid


# ---- project pipeline -------------------------------------------------------

def _load_retainer_period(pid, period_by_pid):
    """Return (period, None) or (None, skip_result).

    Pure lookup against the prefetched ``period_by_pid`` map (built by
    ``resolve_periods``) plus allowance/rate validation — no I/O in the hot loop.
    """
    period = (period_by_pid or {}).get(pid)
    if not period:
        return None, _skip(pid, "no current period")

    # Validate the period
    period_seconds = int(period.get("duration") or 0)
    period_sum = float(period.get("sum") or 0)
    if period_seconds <= 0:
        # Skip the project if the period duration is zero
        return None, _skip(pid, "zero allowance (period duration 0)")
    if period_sum <= 0:
        # Skip the project if the period sum is zero
        return None, _skip(pid, "no rate (period sum 0)")
    return period, None


def _resolve_service_line(project, pid):
    """Return (service_line, None) or (None, skip_result).

    Service line comes from the project name prefix before the first "|".
    """
    display_line = service_line_from_project(_project_name(project))
    if display_line is None:
        return None, _skip(pid, "unrecognised project prefix")
    rate_line = overcharge_rate_line(display_line)
    if rate_line not in rates.known_service_lines():
        return None, _skip(pid, f"unknown service line {display_line!r}")
    return display_line, None


class _WriteLedger:
    """Thread-safe, ordered record of per-project write-backs.

    Write-backs happen as each project finishes, before any email is sent, so a
    run that dies part-way leaves a mix of old and new values in Scoro. The
    ledger (plus the per-write "writeback" log event) makes that interim state
    observable: rows are appended in write order, so a partial run stops
    partway down the list.
    """

    def __init__(self, total=0):
        self._lock = threading.Lock()
        self._rows = []
        # Number of projects being processed this run (the progress-counter
        # total). Skipped projects never write, so seq can finish below total.
        self.total = total

    def record(self, project_id, project_name, value, written):
        """Append a row in write order; return its 1-based sequence number."""
        with self._lock:
            self._rows.append({
                "project_id": project_id,
                "project_name": project_name,
                "value": value,
                "written": written,
            })
            return len(self._rows)

    def rows(self):
        with self._lock:
            return list(self._rows)


def _write_overcharge(client, pid, overcharge, project_name="", ledger=None):
    """Persist overcharge to Scoro (no-op write in DRY_RUN) and log the event.

    Emits a single-line JSON "writeback" event via the logger AT write time so
    CloudWatch shows exactly how far a dead run got. In DRY_RUN the event still
    fires with dry_run=true (value is what WOULD be written) and the ledger row
    carries written=false. A failed modify raises before the event and the
    ledger row, so the trail only ever counts completed writes.
    """
    written = not DRY_RUN
    if written:
        client.modify("projects", pid, OVERCHARGE_FIELD_KEY, overcharge)
    seq = total = None
    if ledger is not None:
        seq = ledger.record(pid, project_name, overcharge, written)
        total = ledger.total
    log.info(json.dumps({
        "event": "writeback",
        "project_id": pid,
        "value": overcharge,
        "dry_run": DRY_RUN,
        "seq": seq,
        "total": total,
    }))


def _log_excluded_projects(ineligible, skipped):
    log.info(
        "ineligible projects (%d): %s",
        len(ineligible),
        json.dumps(ineligible, default=str),
    )
    log.info(
        "skipped projects (%d): %s",
        len(skipped),
        json.dumps(skipped, default=str),
    )


def _log_project_detail(project, period, tasks, result):
    """Log project metadata, nested time entries, allowance, and overcharge math."""
    pid = _project_id(project)
    name = _project_name(project) or "(unnamed)"
    pstart, pend = calc.period_bounds(period)
    entries = calc.list_period_entries(tasks, pstart, pend)

    lines = [
        (
            f"project {pid} \"{name}\" | retainer={project.get('retainer_id')} | "
            f"{result['service_line']}"
        ),
        (
            f"  period: {period.get('start_date')} → {period.get('end_date')} | "
            f"allowance={result['planned_hours']:.4f}h | sum={period.get('sum')}"
        ),
        f"  time entries ({len(entries)} billable):",
    ]
    for e in entries:
        lines.append(
            f"    task {e['task_id']} / entry {e['time_entry_id']} | "
            f"{e['datetime'][:10]} | {e['duration_hours']:.4f}h"
        )
    lines.extend(
        [
            (
                f"  totals: logged={result['logged_hours']:.4f}h | "
                f"remaining={result['remaining_hours']:.4f}h"
            ),
            (
                f"  overcharge_rate={result['overcharge_rate']}/h | "
                f"overcharge_value={result['overcharge_value']:.2f}"
            ),
        ]
    )
    if DRY_RUN:
        lines.append(
            f"  [DRY_RUN] would write overcharge_value={result['overcharge_value']:.2f} "
            f"to {OVERCHARGE_FIELD_KEY}"
        )
    else:
        lines.append(
            f"  wrote overcharge_value={result['overcharge_value']:.2f} "
            f"to {OVERCHARGE_FIELD_KEY}"
        )
    log.info("\n".join(lines))


def process_project(client, project, tasks, period_by_pid=None, ledger=None):
    """Load retainer period, resolve service line, compute overcharge, write back."""
    pid = _project_id(project)

    period, skip = _load_retainer_period(pid, period_by_pid)
    if skip:
        return skip

    display_line, skip = _resolve_service_line(project, pid)
    if skip:
        return skip

    result = calc.compute_project(period, tasks, overcharge_rate_line(display_line))
    result["service_line"] = display_line

    result["project_id"] = pid
    result["project_name"] = _project_name(project)
    result["period_id"] = period.get("id")
    status = project.get("status")
    if status is not None:
        result["status"] = status

    # Capture last run's value before the write-back overwrites it, so the
    # report can show week-on-week movement without any extra storage.
    previous = _previous_overcharge(project)
    result["previous_overcharge_value"] = previous
    result["overcharge_delta"] = (
        round(result["overcharge_value"] - previous, 2)
        if previous is not None
        else None
    )

    _log_project_detail(project, period, tasks, result)
    _write_overcharge(
        client, pid, result["overcharge_value"], result["project_name"], ledger
    )
    return result


def _send_error_alert(run_date, summary, errors):
    """Alert testing recipients about partial failures without failing the run."""
    if not errors:
        return
    if not EMAIL_TESTING_TO:
        log.warning(
            "run has %d errors but EMAIL_TESTING_TO is not set; "
            "skipping error alert",
            len(errors),
        )
        return
    email_report.send_error_alert(
        run_date=run_date,
        dry_run=DRY_RUN,
        summary=summary,
        errors=errors,
        from_addr=EMAIL_FROM,
        to_addrs=EMAIL_TESTING_TO,
        ses_region=SES_REGION,
    )


# ---- lambda entry -----------------------------------------------------------

def last_n_working_days(year, month, n):
    """Last n weekdays (Mon-Fri) of the month, ascending order. Bank holidays ignored."""
    last_day_num = calendar.monthrange(year, month)[1]
    d = date(year, month, last_day_num)
    days = []
    while len(days) < n:
        if d.weekday() < 5:  # 0=Mon ... 4=Fri
            days.append(d)
        d -= timedelta(days=1)
    return list(reversed(days))


def is_in_last_n_working_days(today, n):
    """True if ``today`` is one of the last ``n`` working days of its month."""
    return today in last_n_working_days(today.year, today.month, n)


def handler(event=None, context=None):
    """Run the overcharge calculation for all eligible Scoro projects.

    ``event`` and ``context`` are unused (EventBridge/cron invocation).

    Returns:
        dict with keys ``summary`` (counts), ``results`` (per-project outcomes),
        and ``errors`` (failures).

    Raises:
        RuntimeError: If ``SCORO_API_KEY`` is not set.
    """
    if not API_KEY:
        raise RuntimeError("SCORO_API_KEY is not set")
    # One UTC clock for the whole run: the reported run date and retainer-period
    # selection must agree even when the container's local date differs from UTC
    # at a month boundary.
    run_day = datetime.utcnow().date()
    run_date = run_day.isoformat()
    # Gated trigger support: EventBridge fires this on a wide day-of-month window
    # (cron can't express "last N working days"); this guard turns off-window
    # firings into cheap no-ops before any Scoro call is made.
    trigger_mode = (event or {}).get("trigger_mode")
    send_email = True
    if trigger_mode == "last_n_working_days":
        n = int((event or {}).get("days", 1))
        if not is_in_last_n_working_days(run_day, n):
            log.info(
                "last_n_working_days(n=%d) fired on %s, not in window — skipping",
                n, run_date,
            )
            return {
                "skipped": True,
                "reason": "not_in_last_n_working_days",
                "days": n,
                "run_date": run_date,
            }
        send_email = bool((event or {}).get("send_email", True))
    client = ScoroClient(API_KEY, COMPANY_ACCOUNT_ID, reqs_per_sec=REQS_PER_SEC)
    rates.load_overcharge_rates(client)

    # detailed_response required so custom_fields (e.g. c_cancellationmonth) are present
    all_projects = client.list_all_parallel(
        "projects", detailed_response=True, per_page=25, window=MAX_WORKERS
    )
    projects, ineligible = select_projects(all_projects)
    cancelled_subs = build_cancelled_subs(all_projects, run_date)
    # only_project_ids: restrict the run to specific project ids (for a targeted
    # writeback smoke test — write to one known project, verify in the Scoro UI,
    # then widen). max_projects: cap to the first N eligible.
    only = (event or {}).get("only_project_ids")
    if only:
        only_set = {int(x) for x in only}
        projects = [p for p in projects if _project_id(p) in only_set]
    max_projects = (event or {}).get("max_projects")
    if max_projects:
        projects = projects[:int(max_projects)]
    log.info("starting run: %d eligible projects, dry_run=%s", len(projects), DRY_RUN)
    # Resolve current periods first, then fetch tasks (with nested time entries)
    # per project in parallel.
    retainers_by_id = fetch_retainers_by_id(client)
    period_by_pid = resolve_periods(client, projects, retainers_by_id, run_day)
    tasks_by_project, task_fetch_errors = fetch_tasks_by_project(
        client, projects, period_by_pid
    )

    results = []
    skipped = []
    errors = list(task_fetch_errors)
    failed_task_pids = {error["project_id"] for error in task_fetch_errors}
    projects_to_process = [
        project
        for project in projects
        if _project_id(project) not in failed_task_pids
    ]
    total = len(projects_to_process)
    lock = threading.Lock()
    done = {"n": 0}
    write_ledger = _WriteLedger(total=total)

    def _run(project):
        pid = _project_id(project)
        try:
            project_tasks = tasks_by_project.get(pid, [])
            result = process_project(
                client, project, project_tasks, period_by_pid, write_ledger
            )
            with lock:
                results.append(result)
                if "skipped" in result:
                    skipped.append(
                        _skipped_record(
                            project, result["skipped"], period_by_pid
                        )
                    )
        except Exception as e:  # noqa: BLE001
            log.exception("project %s failed: %s", pid, e)
            with lock:
                errors.append({"project_id": pid, "error": str(e)})
        finally:
            with lock:
                done["n"] += 1
                if done["n"] % 25 == 0:
                    log.info("progress: %d/%d projects", done["n"], total)

    # Per-project work is the write-back I/O (calc itself is pure/CPU-light and
    # thread-safe), so fan out across a thread pool. In DRY_RUN it's effectively
    # CPU-only, but the pool is harmless there.
    workers = max(1, min(MAX_WORKERS, total)) if total else 1
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(_run, projects_to_process))

    _log_excluded_projects(ineligible, skipped)

    write_ledger_rows = write_ledger.rows()
    summary = {
        "dry_run": DRY_RUN,
        "eligible_projects": len(projects),
        "computed": sum(1 for r in results if "skipped" not in r),
        "ineligible": len(ineligible),
        "processed": len(results),
        # Counted off the ledger so the summary can never disagree with the
        # write trail (rows are only recorded after a successful modify).
        "written": sum(1 for row in write_ledger_rows if row["written"]),
        "skipped": len(skipped),
        "errors": len(errors),
    }
    log.info("run summary: %s", json.dumps(summary))
    _send_error_alert(run_date, summary, errors)

    projects_by_pid = {_project_id(p): p for p in projects}

    report_to = (EMAIL_TESTING_TO if DRY_RUN else EMAIL_REPORT_TO) if send_email else []
    log_to = (EMAIL_TESTING_TO if DRY_RUN else EMAIL_LOG_TO) if send_email else []

    if report_to:
        email_report.send_run_email(
            run_date=run_date,
            dry_run=DRY_RUN,
            summary=summary,
            results=results,
            ineligible=ineligible,
            skipped=skipped,
            cancelled_subs=cancelled_subs,
            projects_by_pid=projects_by_pid,
            period_by_pid=period_by_pid,
            tasks_by_project=tasks_by_project,
            from_addr=EMAIL_FROM,
            to_addrs=report_to,
            ses_region=SES_REGION,
        )
    elif DRY_RUN:
        log.debug("EMAIL_TESTING_TO not set — skipping report email")
    else:
        log.debug("EMAIL_REPORT_TO not set — skipping report email")

    if log_to:
        email_report.send_log_email(
            run_date=run_date,
            dry_run=DRY_RUN,
            summary=summary,
            results=results,
            ineligible=ineligible,
            skipped=skipped,
            errors=errors,
            projects_by_pid=projects_by_pid,
            period_by_pid=period_by_pid,
            tasks_by_project=tasks_by_project,
            from_addr=EMAIL_FROM,
            to_addrs=log_to,
            ses_region=SES_REGION,
            field_key=OVERCHARGE_FIELD_KEY,
            lookback_days=TASK_FETCH_LOOKBACK_DAYS,
            write_ledger=write_ledger_rows,
        )
    elif DRY_RUN:
        log.debug("EMAIL_TESTING_TO not set — skipping log email")
    else:
        log.debug("EMAIL_LOG_TO not set — skipping log email")

    return {
        "summary": summary,
        "ineligible": ineligible,
        "skipped": skipped,
        "cancelled_subs": cancelled_subs,
        "results": results,
        "write_ledger": write_ledger_rows,
        "errors": errors,
    }


if __name__ == "__main__":
    print(json.dumps(handler(), indent=2, default=str))