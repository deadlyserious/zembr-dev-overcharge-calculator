#!/usr/bin/env bash
# Scoped live-write smoke test for a single Scoro project, run from AWS CloudShell
# (the sandbox this session runs in can't reach *.scoro.com — Cloudflare blocks it).
#
# Target: https://zembrpty.scoro.com/projects/view/492/taskList?retainerPeriodId=7600
#         project_id=492, company account zembrpty.
#
# This calls the real handler.handler() with the documented
# {"only_project_ids": [...], "max_projects": N} event fields (see README.md /
# handler.py docstring), so it exercises the actual pipeline — eligibility
# check, period resolution, calc, write-back — for just this one project,
# instead of poking Scoro directly.
#
# Usage:
#   1. Get the code into CloudShell (skip if already there):
#        git clone https://<your-PAT>@github.com/deadlyserious/zembr-overcharge-calculator.git
#        cd zembr-overcharge-calculator
#        pip3 install --user boto3   # email_report.py imports boto3 at module load
#
#   2. Fill in SCORO_API_KEY below (or export it before running).
#
#   3. Run once with LIVE=0 (default) to see the computed overcharge value
#      without writing anything. Check the printed JSON: confirm project 492
#      shows up in "results" (not "skipped"/"errors") and overcharge_value
#      looks right.
#
#   4. Re-run with LIVE=1 to actually write it, then verify in the Scoro UI
#      at the URL above that c_overchargehours updated.

set -euo pipefail

: "${SCORO_API_KEY:?Set SCORO_API_KEY first (export SCORO_API_KEY=...)}"
export SCORO_API_KEY
export SCORO_COMPANY_ACCOUNT_ID="zembrpty"
export OVERCHARGE_FIELD_KEY="c_overchargehours"

# Toggle: LIVE=0 (default) previews only; LIVE=1 actually writes to Scoro.
LIVE="${LIVE:-0}"
if [ "$LIVE" = "1" ]; then
  echo "*** LIVE MODE — this WILL write to project 492's c_overchargehours field. ***"
  export DRY_RUN="false"
else
  echo "--- Dry run (preview only, nothing written). Set LIVE=1 to write for real. ---"
  export DRY_RUN="true"
fi

# Leave email vars unset — no report/log/alert emails for a one-off smoke test.
unset EMAIL_REPORT_TO EMAIL_LOG_TO EMAIL_TESTING_TO EMAIL_TO EMAIL_FROM 2>/dev/null || true

python3 - <<'PY'
import json
from handler import handler

result = handler({"only_project_ids": [492], "max_projects": 1})
print(json.dumps(result, indent=2, default=str))

# Quick pass/fail read without scrolling the full JSON.
hits = [r for r in result["results"] if r.get("project_id") == 492]
if not hits:
    print("\n>>> project 492 did not appear in results at all — check 'errors' above.")
elif "skipped" in hits[0]:
    print(f"\n>>> project 492 was SKIPPED: {hits[0]['skipped']}")
else:
    r = hits[0]
    print(
        f"\n>>> project 492: logged={r['logged_hours']}h planned={r['planned_hours']}h "
        f"overcharge_value={r['overcharge_value']} "
        f"({'WOULD write' if result['summary']['dry_run'] else 'WROTE'} to "
        f"{r.get('service_line')})"
    )
PY
