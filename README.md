# Scoro overcharge calculator

**Lambda function name:** `zembr-dev-overcharge-calculator`

Calculates each Zembr client's monthly retainer overcharge value from Scoro and
writes it back to a project custom field. Core logic is stdlib; `email_report.py`
uses `boto3` (included in the Lambda runtime) for optional SES run reports.

## Files

| File | Role |
|------|------|
| `handler.py` | Lambda entry: fetch, per-project pipeline, write-back, optional email |
| `calc.py` | Pure calculation core (no I/O) |
| `scoro_client.py` | Scoro API v2 client |
| `rates.py` | Overcharge rate lookup from Scoro products |
| `email_report.py` | HTML report + plain-text log emails via SES |
| `write_overcharge.py` | Local CLI — same pipeline as the Lambda, always live (no dry-run) |
| `generate_example_email.py` | Render `example_email.html` from a saved Lambda JSON payload |

## Deploy

1. **Create the function**
   - Function name: `zembr-dev-overcharge-calculator`
   - Runtime: Python 3.12 (any 3.10+ works)
   - Handler: `handler.handler`
   - Timeout: 300s (5 min) — bulk retainer fetch + parallel per-project task
     fetches for ~179 eligible projects
   - Memory: 256 MB is plenty

2. **Upload the code** — in the Lambda console, *Upload from → .zip file*, choose
   `zembr-dev-overcharge-calculator.zip`.

3. **Set environment variables** (Configuration → Environment variables):

   | Variable | Required | Example | Notes |
   |----------|----------|---------|-------|
   | `SCORO_API_KEY` | yes | `abc123…` | keep secret; prefer Secrets Manager later |
   | `SCORO_COMPANY_ACCOUNT_ID` | yes | `zembrpty` | Scoro subdomain / AUD base account |
   | `OVERCHARGE_FIELD_KEY` | **confirm** | `c_overchargehours` | project custom-field key — **verify in Scoro before go-live**. Handler defaults to `overcharge_value` if unset; `write_overcharge.py` defaults to `c_overchargehours` |
   | `DRY_RUN` | yes (at first) | `true` | `true` = log only, no writes. Flip to `false` to write |
   | `EMAIL_FROM` | no | `reports@zembr.co` | SES-verified sender |
   | `EMAIL_REPORT_TO` | no | see below | HTML report recipients — **live runs only** (`DRY_RUN=false`) |
   | `EMAIL_LOG_TO` | no | `you@zembr.co` | plain-text log recipients — **every run** |
   | `EMAIL_TO` | no | — | legacy alias for `EMAIL_REPORT_TO` |
   | `SES_REGION` | no | `eu-north-1` | defaults to `AWS_REGION` |

   Report recipients (`EMAIL_REPORT_TO`):

   ```
   cassia.dalziel@zembr.co,paris.galluccio@zembr.co,olivia.wilson@zembr.co,nolita.leflaive@zembr.co,georgina.turnbull@zembr.co
   ```

   Set `EMAIL_LOG_TO` to your own address for the raw JSON log email.
   | `MAX_WORKERS` | no | `8` | thread-pool size for parallel Scoro fetches |
   | `REQS_PER_SEC` | no | `30` | global Scoro request rate cap (429 backoff built in) |
   | `TASK_FETCH_LOOKBACK_DAYS` | no | `14` | pad the task `modified_date` filter back from period start |
   | `OVERCHARGE_RATE_PRODUCT_CODES` | no | `{"BK":"BK","BD":"BD","EA":"EA","SA":"SA"}` | JSON map of service line → Scoro product code |
   | `OVERCHARGE_PRICE_LIST` | no | `1` | optional Scoro price list id when product prices differ by list |

4. **Schedule it** — EventBridge Scheduler → new schedule → cron for a Saturday,
   e.g. `cron(0 18 ? * SAT *)` (Saturday 18:00 UTC). Saturday is deliberate: no
   time is being logged then, so the snapshot is frozen and can't drift.

## First run — always dry-run

Keep `DRY_RUN=true` and trigger a test invocation (any empty `{}` event). Check
CloudWatch logs: each project logs the overcharge value it *would* write, plus a
run summary (`dry_run`, `eligible_projects`, `processed`, `written`, `skipped`,
`ineligible`, `errors`). Confirm the numbers look right, confirm
`OVERCHARGE_FIELD_KEY`, then set `DRY_RUN=false`.

Optional event fields for smoke tests:

```json
{"only_project_ids": [12345], "max_projects": 5}
```

## Local dev

Dry-run locally (requires Scoro env vars):

```bash
DRY_RUN=true SCORO_API_KEY=… SCORO_COMPANY_ACCOUNT_ID=zembrpty python3 handler.py > run_output.json
python3 generate_example_email.py run_output.json
```

`run_output.json` and `example_email.html` are gitignored — regenerate as needed.

## Notes / known follow-ups

- Writes the **overcharge portion** to the custom field (per "track overcharge
  value"): `(logged_hours - planned_hours) * overcharge_rate` when over budget,
  otherwise `0`.
- Overcharge rates are fetched once per run from Scoro products via
  `load_overcharge_rates()` (product code defaults to the service line:
  BK, BD, EA, SA). Override mapping with `OVERCHARGE_RATE_PRODUCT_CODES`.
- Scoro data is fetched in three stages:
  1. **Projects** — list all, filter to active or at-risk retainers
     (`status=additional6` or `additional8`, non-zero `retainer_id`).
  2. **Retainers** — one bulk `retainers/list` (falls back to per-id `view` if
     the list endpoint lacks nested periods).
  3. **Tasks** — `fetch_tasks_by_project` runs per-project `tasks/list` with
     `detailed_response=True` (nested `time_entries`, 25 rows/page) in a thread
     pool. Each project filters tasks by `modified_date` from period start minus
     `TASK_FETCH_LOOKBACK_DAYS` (fetch optimisation only — `calc.py` still buckets
     entries by `completed_datetime` within the period).
- The flat `timeEntries` list endpoint was tried and dropped: its rows lack
  `project_id`, so entries can't be grouped by project. Nested task fetches are
  parallelised to stay within the Lambda timeout.
- Billable duration comes from nested task time entries (`billable_duration` on
  each row). If overcharges suddenly read as 0, re-check a live nested entry
  sample — Scoro may have renamed `billable_duration` / `is_billable` /
  `completed_datetime`.
- The sandbox/Cowork can't reach `*.scoro.com`; live runs happen in AWS (or
  CloudShell). Cloudflare blocks any request without a browser User-Agent.
