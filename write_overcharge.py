"""Write overcharge values for all eligible projects (local CLI, always live).

Mirrors the handler.py pipeline end-to-end but with no dry-run gate — every
eligible project gets written back. Requires the same env vars as the Lambda.

Usage:
    SCORO_API_KEY=… SCORO_COMPANY_ACCOUNT_ID=zembrsandbox python write_overcharge.py
"""

import json
import logging
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import calc
import rates
from scoro_client import ScoroClient, ScoroError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("write_overcharge")

try:
    API_KEY = os.environ["SCORO_API_KEY"]
    COMPANY_ACCOUNT_ID = os.environ["SCORO_COMPANY_ACCOUNT_ID"]
except KeyError as e:
    raise SystemExit(f"Missing required environment variable: {e}") from e

FIELD = os.environ.get("OVERCHARGE_FIELD_KEY", "c_overchargehours")
ACTIVE_STATUS = "additional6"
MAX_WORKERS = 8
REQS_PER_SEC = 20
TASK_FETCH_LOOKBACK_DAYS = 14

VALID_PREFIXES = {"BK", "EA UK", "EA NA", "EA S", "SA", "BD"}
_VALID_PREFIXES_UPPER = {p.upper() for p in VALID_PREFIXES}


# ---- helpers -----------------------------------------------------------------

def _project_id(p):
    return p.get("project_id") or p.get("id")

def _project_name(p):
    return p.get("project_name") or p.get("name") or ""

def _service_line(project_name):
    prefix = project_name.split("|")[0].strip().upper()
    if prefix.startswith("EA"):
        return "EA"
    if prefix not in _VALID_PREFIXES_UPPER:
        return None
    return prefix

def _retainer_periods(retainer):
    return (
        retainer.get("retainer_periods")
        or retainer.get("data", {}).get("retainer_periods")
        or []
    )

def _task_fetch_from(period_start):
    if not period_start:
        return ""
    try:
        d = datetime.strptime(period_start, "%Y-%m-%d").date()
        return (d - timedelta(days=TASK_FETCH_LOOKBACK_DAYS)).isoformat()
    except ValueError:
        return ""


# ---- fetch -------------------------------------------------------------------

def select_projects(client):
    projects = client.list_all("projects")
    eligible, skipped = [], []
    for p in projects:
        pid = _project_id(p)
        name = _project_name(p)
        if not p.get("retainer_id"):
            skipped.append({"project_id": pid, "name": name, "reason": "no retainer_id"})
            continue
        eligible.append(p)
    return eligible, skipped


def fetch_retainers(client):
    try:
        retainers = client.list_all_parallel("retainers", detailed_response=True, per_page=25, window=MAX_WORKERS)
    except ScoroError as e:
        log.warning("retainers/list failed (%s); falling back to per-id view", e)
        return {}
    by_id = {}
    for r in retainers:
        rid = r.get("id") or r.get("retainer_id")
        if rid is not None:
            by_id[rid] = r
    log.info("fetched %d retainers", len(by_id))
    return by_id


def resolve_periods(client, projects, retainers_by_id):
    period_by_pid = {}
    for p in projects:
        pid = _project_id(p)
        retainer_id = p.get("retainer_id")
        retainer = retainers_by_id.get(retainer_id)
        periods = _retainer_periods(retainer) if retainer else []
        if not periods and retainer_id is not None:
            retainer = client.view("retainers", retainer_id)
            periods = _retainer_periods(retainer)
        period = calc.select_current_period(periods)
        if period:
            period_by_pid[pid] = period
    log.info("resolved periods for %d/%d projects", len(period_by_pid), len(projects))
    return period_by_pid


def fetch_tasks(client, projects, period_by_pid):
    def _fetch(p):
        pid = _project_id(p)
        period = period_by_pid.get(pid)
        if not period:
            return pid, []
        period_start = period.get("start_date", "")[:10]
        filt = {"project_id": pid}
        fetch_from = _task_fetch_from(period_start)
        if fetch_from:
            filt["modified_date"] = {"from": fetch_from}
        try:
            tasks = client.list_all("tasks", filter=filt, detailed_response=True, per_page=25)
            return pid, tasks
        except ScoroError as e:
            log.warning("tasks fetch failed for project %s: %s", pid, e)
            return pid, []

    workers = max(1, min(MAX_WORKERS, len(projects)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        pairs = list(pool.map(_fetch, projects))

    tasks_by_pid = {pid: tasks for pid, tasks in pairs if tasks}
    log.info("fetched tasks for %d/%d projects", len(tasks_by_pid), len(projects))
    return tasks_by_pid


# ---- process -----------------------------------------------------------------

def process_project(client, project, tasks, period_by_pid):
    pid = _project_id(project)
    name = _project_name(project)

    period = period_by_pid.get(pid)
    if not period:
        return {"project_id": pid, "name": name, "skipped": "no current period"}

    if int(period.get("duration") or 0) <= 0:
        return {"project_id": pid, "name": name, "skipped": "zero allowance"}

    pstart, pend = calc.period_bounds(period)
    if not calc.list_period_entries(tasks, pstart, pend):
        return {"project_id": pid, "name": name, "skipped": "zero time entries in period"}

    service_line = _service_line(name)
    if service_line not in rates.known_service_lines():
        return {"project_id": pid, "name": name, "skipped": f"unrecognised prefix ({service_line!r})"}

    result = calc.compute_project(period, tasks, service_line)
    if result["logged_hours"] <= 0:
        return {"project_id": pid, "name": name, "skipped": "zero billable time"}

    overcharge = result["overcharge_value"]
    client.modify("projects", pid, FIELD, overcharge)

    log.info(
        "project %s %r | planned=%.4fh logged=%.4fh overcharge=%.2f -> written",
        pid, name, result["planned_hours"], result["logged_hours"], overcharge,
    )
    return {
        "project_id": pid,
        "name": name,
        "service_line": service_line,
        "planned_hours": result["planned_hours"],
        "logged_hours": result["logged_hours"],
        "overcharge_value": overcharge,
        "written": True,
    }


def main():
    client = ScoroClient(API_KEY, COMPANY_ACCOUNT_ID, reqs_per_sec=REQS_PER_SEC)
    rates.load_overcharge_rates(client)

    projects, pre_filtered = select_projects(client)
    log.info("%d eligible projects after pre-filter (%d filtered out)", len(projects), len(pre_filtered))

    retainers_by_id = fetch_retainers(client)
    period_by_pid = resolve_periods(client, projects, retainers_by_id)
    tasks_by_pid = fetch_tasks(client, projects, period_by_pid)

    results, errors = [], []
    lock = threading.Lock()

    def _run(project):
        pid = _project_id(project)
        try:
            project_tasks = tasks_by_pid.get(pid, [])
            r = process_project(client, project, project_tasks, period_by_pid)
            with lock:
                results.append(r)
        except Exception as e:
            log.exception("project %s failed: %s", pid, e)
            with lock:
                errors.append({"project_id": pid, "error": str(e)})

    workers = max(1, min(MAX_WORKERS, len(projects))) if projects else 1
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(_run, projects))

    written = [r for r in results if r.get("written")]
    skipped = [r for r in results if "skipped" in r]

    summary = {
        "eligible": len(projects),
        "pre_filtered": len(pre_filtered),
        "written": len(written),
        "skipped": len(skipped),
        "errors": len(errors),
    }
    log.info("done: %s", json.dumps(summary))

    print("\n=== written ===")
    for r in written:
        print(f"  [{r['project_id']}] {r['name']!r:50s} {r['overcharge_value']:.2f}")

    if skipped:
        print("\n=== skipped ===")
        for r in skipped:
            print(f"  [{r['project_id']}] {r['name']!r:50s} {r['skipped']}")

    if pre_filtered:
        print("\n=== pre-filtered (no retainer) ===")
        for r in pre_filtered:
            print(f"  [{r['project_id']}] {r['name']!r:50s} {r['reason']}")

    if errors:
        print("\n=== errors ===")
        for e in errors:
            print(f"  [{e['project_id']}] {e['error']}")


if __name__ == "__main__":
    main()
