"""Run the guarded overcharge pipeline from a local CLI.

This is intentionally a thin wrapper around ``handler.handler`` so local runs
use the same project filtering, fetch/error handling, alerts, and write gates
as AWS Lambda.

Dry run:
    DRY_RUN=true SCORO_API_KEY=… SCORO_COMPANY_ACCOUNT_ID=… \
        python3 write_overcharge.py

Live run:
    DRY_RUN=false ENABLE_LIVE_WRITES=true SCORO_API_KEY=… \
        SCORO_COMPANY_ACCOUNT_ID=… python3 write_overcharge.py
"""

import json

from handler import handler as run_handler


def main():
    result = run_handler()
    print(json.dumps(result, indent=2, default=str))
    return result


if __name__ == "__main__":
    main()
