#!/usr/bin/env python3
"""Generate example report or log HTML from a Lambda run JSON payload.

Usage:
    python generate_example_email.py run_output.json [output.html]
    python generate_example_email.py --log run_output.json [output.html]
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from email_report import _reason_label, build_html_body, build_log_html_body
import rates


_INELIGIBLE_LABELS = frozenset({"No retainer ID", "Not active"})


def _split_excluded(data):
    """Return (ineligible, skipped) from payload; supports legacy ``filtered`` key."""
    skipped = data.get("skipped")
    ineligible = data.get("ineligible")
    if ineligible is None:
        ineligible = data.get("filtered", [])
    if skipped is not None:
        return ineligible, skipped
    ineligible_only, skipped_only = [], []
    for row in ineligible:
        if _reason_label(row.get("reason", "")) in _INELIGIBLE_LABELS:
            ineligible_only.append(row)
        else:
            skipped_only.append(row)
    return ineligible_only, skipped_only


def _seed_rates_from_payload(results):
    if rates.get_all_overcharge_rates():
        return
    seeded = {}
    for r in results:
        sl = r.get("service_line")
        rate = r.get("overcharge_rate")
        if sl and rate is not None:
            seeded[sl] = rate
    if seeded:
        rates._overcharge = seeded


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--log",
        action="store_true",
        help="Generate the ops log email (full calculation detail) instead of the team report",
    )
    parser.add_argument("run_json", type=Path, help="Lambda run output JSON")
    parser.add_argument(
        "output_html",
        nargs="?",
        type=Path,
        default=None,
        help="Output HTML path (default: example_log_email.html or example_email.html)",
    )
    args = parser.parse_args()

    if not args.run_json.is_file():
        print(f"File not found: {args.run_json}", file=sys.stderr)
        sys.exit(1)

    default_out = "example_log_email.html" if args.log else "example_email.html"
    out = args.output_html or Path(default_out)
    data = json.loads(args.run_json.read_text())
    _seed_rates_from_payload(data.get("results", []))
    ineligible, skipped = _split_excluded(data)
    run_date = data.get("run_date") or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    summary = data["summary"]
    results = data.get("results", [])
    errors = data.get("errors", [])

    if args.log:
        html = build_log_html_body(
            run_date=run_date,
            dry_run=summary.get("dry_run", True),
            summary=summary,
            results=results,
            ineligible=ineligible,
            skipped=skipped,
            errors=errors,
            projects_by_pid={},
            period_by_pid={},
            tasks_by_project={},
        )
    else:
        html = build_html_body(
            run_date=run_date,
            dry_run=summary.get("dry_run", True),
            summary=summary,
            results=results,
            ineligible=ineligible,
            skipped=skipped,
            projects_by_pid={},
            period_by_pid={},
            tasks_by_project={},
        )

    out.write_text(html)
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
