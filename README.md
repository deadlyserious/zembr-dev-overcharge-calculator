# Scoro overcharge calculator (dev)

Lambda `zembr-dev-overcharge-calculator` (`eu-north-1`, x86_64). Calculates each Zembr client's monthly retainer overcharge from Scoro and writes it to a project custom field.

Core logic is stdlib. `email_report.py` and `scoro_api_key.py` use `boto3`
(included in the Lambda runtime) for SES and Secrets Manager.

## Layout


| File                        | Role                                                                  |
| --------------------------- | --------------------------------------------------------------------- |
| `handler.py`                | Lambda entry: fetch, per-project pipeline, write-back, optional email |
| `calc.py`                   | Pure calculation core (no I/O)                                        |
| `scoro_api_key.py`          | Scoro API key from Secrets Manager (cached, env-var fallback)         |
| `scoro_client.py`           | Scoro API v2 client                                                   |
| `rates.py`                  | Overcharge rate lookup from Scoro products                            |
| `service_lines.py`          | Service-line codes and project-name prefix mapping                    |
| `email_report.py`           | HTML report + multipart log emails via SES                            |
| `write_overcharge.py`       | Local CLI wrapper around the guarded Lambda pipeline                  |
| `generate_example_email.py` | Render `example_email.html` from a saved Lambda JSON payload          |
| `test_*.py`                 | Regression suite (stdlib `unittest`)                                  |


```bash
SCORO_API_KEY=dummy python3 -m unittest discover
```

Importing `handler` locally needs `boto3` (`pip install boto3`). The dummy
`SCORO_API_KEY` satisfies the Secrets Manager fallback offline.

## Configuration

Scoro API key: Secrets Manager secret `zembr/dev/scoro-api-key`, JSON shape
`{"SCORO_API_KEY": "…"}`. Derived from the Lambda function name unless
`SCORO_SECRET_NAME` overrides. `SCORO_API_KEY` env is fallback only.


| Variable                        | Required    | Notes                                                                                 |
| ------------------------------- | ----------- | ------------------------------------------------------------------------------------- |
| `SCORO_COMPANY_ACCOUNT_ID`      | yes         | Scoro subdomain                                                                       |
| `SCORO_SECRET_NAME`             | no          | override auto-derived secret name (e.g. `zembr/dev/scoro-api-key`)                    |
| `SCORO_API_KEY`                 | no          | used only if Secrets Manager fetch fails                                              |
| `OVERCHARGE_FIELD_KEY`          | no          | project custom-field key (defaults to `c_overchargehours`)                            |
| `DRY_RUN`                       | no          | defaults to `true`; only `true`/`false` (case-insensitive); other values fail startup |
| `EMAIL_FROM`                    | no          | SES-verified sender                                                                   |
| `EMAIL_REPORT_TO`               | no          | HTML report; **live runs only** (`DRY_RUN=false`)                                     |
| `EMAIL_LOG_TO`                  | no          | ops/audit log; **every run**                                                          |
| `EMAIL_TESTING_TO`              | recommended | dry-run recipients + partial-failure alerts                                           |
| `EMAIL_TO`                      | no          | legacy alias for `EMAIL_REPORT_TO`                                                    |
| `SES_REGION`                    | no          | defaults to `AWS_REGION` / `eu-north-1`                                               |
| `MAX_WORKERS`                   | no          | thread-pool size for parallel Scoro fetches (default `8`)                             |
| `REQS_PER_SEC`                  | no          | global Scoro request rate cap (default `30`; 429 backoff built in)                    |
| `TASK_FETCH_LOOKBACK_DAYS`      | no          | pad task `modified_date` filter back from period start (default `14`)                 |
| `OVERCHARGE_RATE_PRODUCT_CODES` | no          | JSON map of service line → Scoro product code (default: line→itself)                  |
| `OVERCHARGE_PRICE_LIST`         | no          | Scoro price list id when prices differ by list                                        |


Email is gated by:

1. the `send_email` **event** field (only under `trigger_mode`),
2. `DRY_RUN` (routes recipients; does not suppress),
3. an empty recipient list (clear the relevant `EMAIL_*_TO` to disable).

## Deploy

1. Create the function: runtime Python 3.10+, handler `handler.handler`,
  timeout 300s, memory 256 MB.
2. Upload `zembr-dev-overcharge-calculator.zip`.
3. Set the env vars above; ensure the execution role can read
  `zembr/dev/scoro-api-key` and send via SES.
4. Attach schedules (EventBridge), for example:

  | Rule             | Schedule                | Payload                                                              |
  | ---------------- | ----------------------- | -------------------------------------------------------------------- |
  | weekly           | `cron(0 6 ? * SUN *)`   | none (full run + emails)                                             |
  | month-end hourly | `cron(0 * 26-31 * ? *)` | `{"trigger_mode":"last_n_working_days","days":3,"send_email":false}` |

   Sunday weekly run freezes the snapshot when no time is being logged. The
   hourly rule exists because cron can't express "last N working days": it fires
   across days 26–31 and the handler's `last_n_working_days` guard no-ops every
   off-window firing before any Scoro call. Bank holidays are ignored; only
   Mon–Fri counts.
  > **Granularity.** The guard compares *dates*, so all 24 firings on an
  > in-window day pass — `days: 3` → 72 runs/month. Values recompute each run
  > and converge, at 72× Scoro load. A daily cron (`cron(0 6 26-31 * ? *)`)
  > would cut that to one run per in-window day.

## First run

Keep `DRY_RUN=true`, invoke with `{}`, check CloudWatch: per-project would-write
values plus run summary (`dry_run`, `eligible_projects`, `computed`,
`processed`, `written`, `skipped`, `ineligible`, `errors`). Confirm
`OVERCHARGE_FIELD_KEY`, then set `DRY_RUN=false` for live writes.

Partial project failures do not fail the invocation. A concise alert with failed
project IDs goes to `EMAIL_TESTING_TO`; detail stays in CloudWatch and the ops
log.

### Event fields


| Field              | Purpose                                                                                 |
| ------------------ | --------------------------------------------------------------------------------------- |
| `only_project_ids` | restrict to specific project ids                                                        |
| `max_projects`     | cap to the first N eligible projects                                                    |
| `trigger_mode`     | `"last_n_working_days"` enables the month-end guard; anything else runs unconditionally |
| `days`             | window size for that guard (default `1`)                                                |
| `send_email`       | only under `trigger_mode`; `false` suppresses report and log emails                     |


```json
{"only_project_ids": [12345], "max_projects": 5}
```

Off-window guarded firings return early:

```json
{"skipped": true, "reason": "not_in_last_n_working_days", "days": N, "run_date": "…"}
```

### Return payload

```
summary, results, ineligible, skipped, cancelled_subs, write_ledger, errors
```

`write_ledger` is the ordered per-project trail of successful write-backs (also
emitted as single-line `writeback` JSON log events). `summary.written` is counted
off the ledger so the two can't disagree.

## Local

```bash
DRY_RUN=true SCORO_API_KEY=… SCORO_COMPANY_ACCOUNT_ID=zembrpty python3 handler.py > run_output.json
python3 generate_example_email.py run_output.json
python3 generate_example_email.py --log run_output.json
```

`python3 write_overcharge.py` runs the same handler and prints JSON. Defaults to
dry-run; live writes need `DRY_RUN=false`.

`run_output.json` and `example_email.html` are gitignored.

## Behaviour notes

- Overcharge written to the custom field: `(logged_hours - planned_hours) * overcharge_rate`   
when over budget, else `0`.
- Existing field value is read before write and shown in emails as
`previous → new` with a signed delta.
- Rates load once per run via `load_overcharge_rates()` (default product code =
service line: BK, BD, EA, SA).
- Fetch stages:
  1. **Projects** — non-zero `retainer_id` and status active (`additional6`),
    at risk (`additional8`), handover (`pending`), or on hold (`future`).
     `completed` → "Subscription cancelled", reported for the previous calendar
     month.
  2. **Retainers** — bulk `retainers/list` (falls back to per-id `view`).
  3. **Tasks** — per-project `tasks/list` with `detailed_response=True` in a
    thread pool; `modified_date` filter from period start minus
     `TASK_FETCH_LOOKBACK_DAYS` (fetch only — `calc.py` still buckets by
     `completed_datetime` within the period).
- Flat `timeEntries` list was dropped: rows lack `project_id`.
- Billable duration comes from nested `billable_duration`. If overcharges suddenly
read as 0, re-check a live nested entry — Scoro may have renamed fields.
- Local sandbox/Cowork can't reach `*.scoro.com`; Cloudflare also blocks requests
without a browser User-Agent. Live runs go through AWS.

