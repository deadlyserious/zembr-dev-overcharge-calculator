#!/usr/bin/env python3
"""Generate example_email.html from a Lambda run JSON payload.

Usage:
    python generate_example_email.py run_output.json [output.html]
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from email_report import _reason_label, build_html_body
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
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <run.json> [output.html]", file=sys.stderr)
        sys.exit(1)

    src = Path(sys.argv[1])
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("example_email.html")
    data = json.loads(src.read_text())
    _seed_rates_from_payload(data.get("results", []))
    ineligible, skipped = _split_excluded(data)

    html = build_html_body(
        run_date=data.get("run_date")
        or datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        dry_run=data["summary"].get("dry_run", True),
        summary=data["summary"],
        results=data["results"],
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
