# Scoro overcharge calculator

**Lambda functions:** `zembr-dev-overcharge-calculator` (dev, `DRY_RUN=true`) and
`zembr-prod-overcharge-calculator` (prod, `DRY_RUN=false`) — same code, both in
`eu-north-1`. This repo is the dev copy; deploy the same zip to both.

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
| `service_lines.py` | Shared service-line codes and project-name prefix mapping |
| `email_report.py` | HTML report + multipart log emails via SES |
| `write_overcharge.py` | Thin local CLI wrapper around the guarded Lambda pipeline |
| `generate_example_email.py` | Render `example_email.html` from a saved Lambda JSON payload |
| `test_calc.py`, `test_handler.py`, `test_service_lines.py` | Regression suite (stdlib `unittest`) |

Run the tests with `python3 -m unittest discover` — no third-party test deps.

## Deploy

1. **Create the function**
   - Function name: `zembr-dev-overcharge-calculator`
   - Runtime: Python 3.14 (currently deployed; any 3.10+ works)
   - Handler: `handler.handler`
   - Timeout: 300s (5 min) — bulk retainer fetch + parallel per-project task
     fetches for ~179 eligible projects
   - Memory: 256 MB (dev) / 512 MB (prod)

2. **Upload the code** — in the Lambda console, *Upload from → .zip file*, choose
   `zembr-dev-overcharge-calculator.zip`.

3. **Set environment variables** (Configuration → Environment variables):

   | Variable | Required | Example | Notes |
   |----------|----------|---------|-------|
   | `SCORO_API_KEY` | yes | `ScoroAPI_…` | keep secret; prefer Secrets Manager later |
   | `SCORO_COMPANY_ACCOUNT_ID` | yes | `zembrpty` | Scoro subdomain / AUD base account |
   | `OVERCHARGE_FIELD_KEY` | no | `c_overchargehours` | project custom-field key; both entry points default to `c_overchargehours` |
   | `DRY_RUN` | no | `true` | defaults to `true` when unset; accepts only `true`/`false` (case-insensitive), any other value fails startup |
   | `EMAIL_FROM` | no | `zembr.overcharge.calculator@gmail.com` | SES-verified sender |
   | `EMAIL_REPORT_TO` | no | see below | HTML report recipients — **live runs only** (`DRY_RUN=false`) |
   | `EMAIL_LOG_TO` | no | `you@zembr.co` | ops/audit log — **every run** (HTML + plain text, full calculation detail, write ledger) |
   | `EMAIL_TESTING_TO` | recommended | `you@zembr.co` | dry-run recipients and partial-failure alerts for both dry and live runs |
   | `EMAIL_TO` | no | — | legacy alias for `EMAIL_REPORT_TO` |
   | `SES_REGION` | no | `eu-north-1` | defaults to `AWS_REGION` |
   | `MAX_WORKERS` | no | `8` | thread-pool size for parallel Scoro fetches |
   | `REQS_PER_SEC` | no | `30` | global Scoro request rate cap (429 backoff built in) |
   | `TASK_FETCH_LOOKBACK_DAYS` | no | `14` | pad the task `modified_date` filter back from period start |
   | `OVERCHARGE_RATE_PRODUCT_CODES` | no | `{"BK":"SCORO_61","BD":"SCORO_65","EA":"SCORO_66","SA":"SCORO_67"}` | JSON map of service line → Scoro product code (the example is the value deployed today; the code's built-in default maps each line to its own name, e.g. `BK`→`BK`) |
   | `OVERCHARGE_PRICE_LIST` | no | `1` | optional Scoro price list id when product prices differ by list |

   Report recipients (`EMAIL_REPORT_TO`, prod):

   ```
   cassia.dalziel@zembr.co,paris.galluccio@zembr.co,olivia.wilson@zembr.co,nolita.leflaive@zembr.co,georgina.turnbull@zembr.co
   ```

   Set `EMAIL_LOG_TO` to your own address for the detailed ops log email.

   There is no global email on/off variable. Email is gated three ways: the
   `send_email` **event** field (lowercase, and only honoured under
   `trigger_mode` — see below), `DRY_RUN` (which routes recipients rather than
   suppressing), and an empty recipient list (the actual off switch — clear the
   relevant `EMAIL_*_TO` variable). Both functions previously carried inert
   `SEND_EMAIL` and `SCORO_BASE_CURRENCY` variables that nothing read; these
   were deleted on 2026-08-01. Don't reintroduce a `SEND_EMAIL` that isn't
   wired up — it reads like a kill switch and isn't one.

4. **Schedule it** — two EventBridge rules currently target these functions:

   | Rule | Schedule | State | Targets | Payload |
   |------|----------|-------|---------|---------|
   | `weekly_call` | `cron(0 6 ? * SUN *)` | enabled | dev, prod, `scoro_overcharge_ts` | none (full run + emails) |
   | `zembr-overcharge-last-working-day-hourly` | `cron(0 * 26-31 * ? *)` | enabled | **dev only** | `{"trigger_mode":"last_n_working_days","days":3,"send_email":false}` |

   The weekly rule runs on Sunday, when no time is being logged, so the snapshot
   is frozen and can't drift. The hourly rule exists because cron can't express
   "the last N working days of the month": it fires hourly across days 26–31 and
   the handler's `last_n_working_days` guard turns every off-window firing into a
   no-op before any Scoro call.

   The intended window is `"days": 3` — the **last three working days** of the
   month, so a client whose retainer lands late still gets picked up. Bank
   holidays are ignored; only Mon–Fri counts. In August 2026 that resolves to the
   27th, 28th and 31st.

   > **Granularity caveat.** The guard compares *dates*, so all 24 firings on an
   > in-window day pass it — `days: 3` yields 72 runs per month, not 3. Values are
   > recomputed from scratch each run so the result converges, but it is 72× the
   > Scoro API load. A daily cron (`cron(0 6 26-31 * ? *)`) would give one run per
   > in-window day while keeping `days: 3`.

   > **The hourly rule targets dev only, on purpose.** Prod's deployed zip
   > predates the guard (commit `5773ccc`) — its `handler.py` has no
   > `last_n_working_days`, no `trigger_mode` and no `send_email`, so it would
   > treat every firing as a full live run and email `EMAIL_REPORT_TO` each time.
   > Deploy the current zip before adding prod back as a target. On dev the rule
   > is inert: `DRY_RUN=true` means no writes, and `send_email: false` means no
   > email.

## First run — always dry-run

Keep `DRY_RUN=true` and trigger a test invocation (any empty `{}` event). Check
CloudWatch logs: each project logs the overcharge value it *would* write, plus a
run summary (`dry_run`, `eligible_projects`, `computed`, `processed`, `written`,
`skipped`, `ineligible`, `errors`). Confirm the numbers look right, confirm
`OVERCHARGE_FIELD_KEY`, then set `DRY_RUN=false`.

Partial project failures do not fail the Lambda invocation or trigger an
automatic full-run retry. Instead, a concise alert containing the failed
project IDs is sent to `EMAIL_TESTING_TO`; detailed errors remain in CloudWatch
and the operations log.

### Event fields

| Field | Purpose |
|-------|---------|
| `only_project_ids` | restrict the run to specific project ids (targeted write-back smoke test) |
| `max_projects` | cap to the first N eligible projects |
| `trigger_mode` | `"last_n_working_days"` enables the month-end guard; anything else runs unconditionally |
| `days` | window size for that guard (default `1`); bank holidays are ignored |
| `send_email` | only honoured under `trigger_mode`; `false` suppresses both report and log emails |

```json
{"only_project_ids": [12345], "max_projects": 5}
```

Off-window guarded firings return early with
`{"skipped": true, "reason": "not_in_last_n_working_days", "days": N, "run_date": …}`.

### Return payload

```
summary, results, ineligible, skipped, cancelled_subs, write_ledger, errors
```

`write_ledger` is the ordered per-project trail of write-backs (each also emitted
as a single-line `writeback` JSON log event at write time), so a run that dies
part-way still shows exactly which projects carry this week's value. Rows are
only recorded after a successful modify, and `summary.written` is counted off
the ledger so the two can't disagree.

## Local dev

Dry-run locally (requires Scoro env vars):

```bash
DRY_RUN=true SCORO_API_KEY=… SCORO_COMPANY_ACCOUNT_ID=zembrpty python3 handler.py > run_output.json
python3 generate_example_email.py run_output.json
python3 generate_example_email.py --log run_output.json
```

`python3 write_overcharge.py` runs the same handler locally and prints its JSON
result. It defaults to dry-run; live writes require `DRY_RUN=false`.

`run_output.json` and `example_email.html` are gitignored — regenerate as needed.

## Notes / known follow-ups

- Writes the **overcharge portion** to the custom field (per "track overcharge
  value"): `(logged_hours - planned_hours) * overcharge_rate` when over budget,
  otherwise `0`.
- The field's existing value is read before the write and carried into the
  emails as `previous → new` with a signed delta, so a value that moved sharply
  for the wrong reason is visible at a glance. Last week's value is the field
  itself — no extra storage.
- Overcharge rates are fetched once per run from Scoro products via
  `load_overcharge_rates()` (product code defaults to the service line:
  BK, BD, EA, SA). Override mapping with `OVERCHARGE_RATE_PRODUCT_CODES`.
- Scoro data is fetched in three stages:
  1. **Projects** — list all, filter to those with a non-zero `retainer_id` and
     an eligible status: active (`additional6`), at risk (`additional8`),
     handover in progress (`pending`) or on hold (`future`). Projects whose
     status is `completed` display as "Subscription cancelled" and are reported
     separately for the previous calendar month.
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
