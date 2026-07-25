"""Run emails via AWS SES: HTML report for the team, multipart log for ops.

Entry points: send_run_email(), send_log_email(). Never raise — all exceptions
are logged so the Lambda return value is unaffected.
"""

import base64
import json
import logging
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

import calc
from rates import get_all_overcharge_rates

_LOGO_PATH = Path(__file__).with_name("assets") / "zembr-logo.png"
_LOGO_DATA_URI = None

log = logging.getLogger("overcharge_calculator")

# Zembr logo dot pink (sampled from assets/zembr-logo.png)
_ACCENT = "#eb0453"
_PROGRESS = "#8e44ad"
_HEADER_BG = "#232b3a"
_SECTION_BG = "#fafafa"

_STYLE = """
  body  { font-family: Arial, sans-serif; font-size: 14px; color: #222; margin: 20px; }
"""

# Service line badge colours
_SL_COLOUR = {
    "BK": "#2980b9",
    "EA": "#8e44ad",
    "EA North": "#8e44ad",
    "EA South": "#9b59b6",
    "SA": "#16a085",
    "BD": "#d35400",
}

_EXCLUDED_WRAP = "font-size:12px;line-height:1.35;"
_EXCLUDED_NAME = "font-weight:bold;font-size:12px;line-height:1.35;word-break:break-word;"
_EXCLUDED_PID = "color:#888;font-size:11px;margin:2px 0 0;"
_EXCLUDED_DETAIL = "color:#666;font-size:11px;line-height:1.35;margin:4px 0 0;"

# Scoro raw status → display badge (labels from Zembr status table; colours from Scoro UI).
_STATUS_BADGE = {
    "additional6": {
        "label": "Active Client",
        "bg": "#afe2ad",
        "color": "#1d1f22",
    },
    "additional8": {
        "label": "At risk",
        "bg": "#f1798d",
        "color": "#ffffff",
    },
    "pending": {
        "label": "Handover in progress",
        "bg": "#e0e0e0",
        "color": "#1d1f22",
    },
    "inprogress": {
        "label": "Future client",
        "bg": "#ffe986",
        "color": "#1d1f22",
    },
    "future": {
        "label": "On hold",
        "bg": "#a9dbf8",
        "color": "#1d1f22",
    },
    "additional7": {
        "label": "Internal (Z | projects)",
        "bg": "#e0e0e0",
        "color": "#1d1f22",
    },
    "cancelled": {
        "label": "Project completed",
        "bg": "#a7b2c5",
        "color": "#ffffff",
    },
    "completed": {
        "label": "Subscription cancelled",
        "bg": "#666e7d",
        "color": "#ffffff",
    },
}

_GRID_CELL = (
    "width:33.33%;vertical-align:top;padding:8px 12px;"
    "border:1px solid #e0e0e0;background:#fff;"
)
_GRID_CELL_EMPTY = "width:33.33%;padding:0;border:none;background:transparent;"

_DETAIL_BLOCK = (
    "margin:0 0 14px;padding:14px 16px;border:1px solid #e0e0e0;"
    "border-radius:4px;background:#fff;"
)
_DETAIL_META = "color:#666;font-size:11px;line-height:1.5;margin:4px 0 8px;"
_ENTRY_TABLE = (
    "width:100%;border-collapse:collapse;font-size:11px;margin:8px 0;"
)
_ENTRY_TH = (
    "text-align:left;padding:4px 6px;border-bottom:1px solid #ddd;"
    "color:#888;font-weight:bold;"
)
_ENTRY_TD = "padding:3px 6px;border-bottom:1px solid #f0f0f0;"
_MAX_LOG_ENTRIES = 100

_KNOWN_NAME_PREFIXES = "BK, EA North, EA NA, EA South, SA, BD"

_VALID_PREFIXES_UPPER = frozenset({
    "BK", "EA NORTH", "EA UK", "EA NA", "EA SOUTH", "EA S", "SA", "BD",
})
_EA_NORTH_PREFIXES = frozenset({"EA NORTH", "EA UK", "EA NA"})
_EA_SOUTH_PREFIXES = frozenset({"EA SOUTH", "EA S"})

_ELIGIBLE_STATUSES = frozenset(
    {"additional6", "additional8", "pending", "future"}
)

_IGNORED_PREFIX_LABELS = {
    "LUNAR": "Lunar",
    "Z": "Z",
    "FUP": "FUP",
    "AP": "AP",
    "TEST PROJECT": "Test Project",
}


def _raw_name_prefix(name):
    if "|" not in name:
        return ""
    return name.split("|", 1)[0].strip()


def _is_recognised_prefix(name):
    prefix = _raw_name_prefix(name).upper()
    if prefix in _EA_NORTH_PREFIXES:
        return True
    if prefix in _EA_SOUTH_PREFIXES:
        return True
    return prefix in _VALID_PREFIXES_UPPER


def _ignored_prefix_bucket(name):
    upper = _raw_name_prefix(name).upper()
    return _IGNORED_PREFIX_LABELS.get(upper)


def _partition_data_error_items(items, *, require_recognised_prefix=False):
    """Hide ignored-prefix projects; optionally require recognised prefix for showable tiles."""
    showable = []
    counts = Counter()
    for r in items:
        name = r.get("name") or ""
        bucket = _ignored_prefix_bucket(name)
        if bucket:
            counts[bucket] += 1
            continue
        if require_recognised_prefix and not _is_recognised_prefix(name):
            continue
        showable.append(r)
    return showable, counts


def _partition_no_retainer_items(items):
    """Log-email helper: recognised-prefix tiles only; bucket the rest."""
    showable = []
    ignored = Counter()
    other = 0
    for r in items:
        name = r.get("name") or ""
        if _is_recognised_prefix(name):
            showable.append(r)
            continue
        bucket = _ignored_prefix_bucket(name)
        if bucket:
            ignored[bucket] += 1
        else:
            other += 1
    return showable, ignored, other


def _meets_active_criteria(record):
    status = record.get("status")
    if not status:
        return False
    return status in _ELIGIBLE_STATUSES


def _project_count_phrase(count):
    word = "project" if count == 1 else "projects"
    return f"({count} {word})"


def _no_retainer_reason_blurb(ignored, other):
    base = (
        "Project is not linked to a retainer in Scoro. "
        "Only retainer projects are included in the overcharge run. "
        "Only projects with a recognised service-line prefix are listed below"
    )
    has_hidden = bool(ignored or other)
    if not has_hidden:
        return (
            f'<p style="color:#777;font-size:12px;margin:0 0 10px;line-height:1.5;">'
            f"{_h(base)}.</p>"
        )
    bullets = []
    for label in ("Lunar", "Z", "FUP", "AP", "Test Project"):
        n = ignored.get(label, 0)
        if n:
            bullets.append(f"{label} {_project_count_phrase(n)}")
    if other:
        bullets.append(f"Other untracked prefixes {_project_count_phrase(other)}")
    li_html = "".join(
        f'<li style="margin:0 0 4px;">{_h(label)}</li>' for label in bullets
    )
    return (
        f'<p style="color:#777;font-size:12px;margin:0 0 4px;line-height:1.5;">'
        f"{_h(base)}; the others are:</p>"
        f'<ul style="color:#777;font-size:12px;margin:0 0 10px;padding-left:20px;'
        f'line-height:1.5;">{li_html}</ul>'
    )


def _ignored_prefix_summary_line(counts):
    total = sum(counts.values())
    if total == 0:
        return ""
    parts = []
    for label in ("Lunar", "Z", "FUP", "AP", "Test Project"):
        n = counts.get(label, 0)
        if n:
            parts.append(f"{label} ({n})")
    word = "project" if total == 1 else "projects"
    return (
        f'<p style="color:#999;font-size:12px;margin:24px 0 0;line-height:1.5;">'
        f"Not shown &mdash; {', '.join(parts)}: {total} {word} with "
        f"internal/test prefixes.</p>"
    )


_SKIP_REASON_BLURBS = {
    "No current period": (
        "Eligible retainer project, but Scoro has no current retainer period — "
        "allowance and date range are unknown. Check the retainer setup in Scoro."
    ),
    "Zero allowance": (
        "The current retainer period has 0 hours allowance (duration is 0). "
        "Set the period allowance before overcharge can be calculated."
    ),
    "No rate": (
        "The current retainer period has a zero sum (hourly rate basis). "
        "Set the period sum in Scoro before overcharge can be calculated."
    ),
    "Unrecognised project prefix": (
        "Project name must start with a known service line — "
        "BK, EA North, EA NA, EA South, SA, or BD — before the first “|”. "
        "Rename the project or fix the prefix."
    ),
    "Zero time entries in period": (
        "No billable time entries were logged against this project "
        "during the current retainer period."
    ),
    "Zero billable time": (
        "Tasks were fetched but total billable hours for the period is zero."
    ),
}

_DATA_ERROR_BLURBS = {
    "No retainer": (
        "Active project with a recognised service-line prefix, but not linked to "
        "a retainer in Scoro."
    ),
    "No current period": (
        "Eligible retainer project, but Scoro has no current retainer period — "
        "allowance and date range are unknown. Check the retainer setup in Scoro."
    ),
    "Zero time entries in period": (
        "No billable time entries were logged against this project "
        "during the current retainer period."
    ),
}

def _excluded_tile(name, pid, detail=None, corner_badge=None, sl=None):
    """Shared layout for skipped and ineligible project cells.

    Badges (service line + status) sit on one line above the project info.
    ``corner_badge`` is kept as the status-badge HTML for call-site compat.
    """
    detail_html = ""
    if detail:
        detail_html = f'<div style="{_EXCLUDED_DETAIL}">{detail}</div>'
    badge_row = _badge_row(sl, corner_badge)
    return (
        f"{badge_row}"
        f'<div style="{_EXCLUDED_WRAP}">'
        f'<div style="{_EXCLUDED_NAME}">{name}</div>'
        f'<div style="{_EXCLUDED_PID}">Project #{pid}</div>'
        f"{detail_html}"
        f"</div>"
    )


def _badge_row(sl=None, status_badge=None):
    """Service-line + status badges on one line above project content."""
    parts = []
    if sl:
        parts.append(_sl_badge(sl))
    if status_badge:
        parts.append(status_badge)
    if not parts:
        return ""
    cells = "".join(
        f'<td style="vertical-align:middle;padding:0 6px 0 0;white-space:nowrap;">'
        f"{p}</td>"
        for p in parts
    )
    return (
        f'<table style="border-collapse:collapse;margin:0 0 6px;">'
        f"<tr>{cells}</tr></table>"
    )


def _h2(title, first=False):
    margin = "margin:40px 0 12px" if first else "margin:48px 0 12px"
    return (
        f'<h2 style="color:#1a1a2e;{margin};font-size:17px;font-weight:bold;">{title}</h2>'
    )


def _section_panel(body):
    return (
        f'<div style="background:{_SECTION_BG};padding:18px 22px;border-radius:6px;">'
        f"{body}</div>"
    )


def _section(title, body, first=False):
    return _h2(title, first=first) + _section_panel(body)


def _h3(title, count=None, colour="#555", first=False, extra=None):
    margin = "margin:0 0 8px" if first else "margin:24px 0 8px"
    suffix_parts = []
    if count is not None:
        suffix_parts.append(f"({count})")
    if extra:
        suffix_parts.append(extra)
    suffix = ""
    if suffix_parts:
        suffix = (
            f' <span style="color:#aaa;font-weight:normal;text-transform:none;'
            f'letter-spacing:0;">{" &middot; ".join(suffix_parts)}</span>'
        )
    return (
        f'<h3 style="color:{colour};{margin};font-size:13px;font-weight:bold;'
        f'text-transform:uppercase;letter-spacing:0.5px;">{_h(title)}{suffix}</h3>'
    )


def _service_line_overcharge_extra(total_oc):
    """Formatted overcharge total for a service-line subheading."""
    money = f"AUD {_fmt_money(total_oc)}"
    if total_oc > 0:
        return (
            f'<span style="color:{_ACCENT};font-weight:bold;">{money}</span>'
        )
    return money


def _logo_img():
    global _LOGO_DATA_URI
    if _LOGO_DATA_URI is None:
        data = base64.b64encode(_LOGO_PATH.read_bytes()).decode("ascii")
        _LOGO_DATA_URI = f"data:image/png;base64,{data}"
    return (
        f'<img src="{_LOGO_DATA_URI}" alt="ZEMBR." '
        f'style="height:28px;width:auto;display:block;margin-right:14px;" />'
    )


def _header_row(run_date, badge):
    return (
        f'<div style="display:flex;justify-content:space-between;align-items:center;">'
        f'<div style="display:flex;align-items:center;">'
        f"{_logo_img()}"
        f"<div>"
        f'<div style="font-size:17px;font-weight:bold;letter-spacing:0.3px;">'
        f"Monthly Overcharge Calculator</div>"
        f'<div style="font-size:12px;color:#8899aa;margin-top:4px;">{_h(run_date)}</div>'
        f"</div></div>{badge}</div>"
    )


_HERO_DIVIDER = "border-top:1px solid #3d4d62;margin-top:16px;padding-top:14px;"


def _hero_section(body, title=None):
    label = ""
    if title:
        label = (
            f'<div style="font-size:11px;color:#8899aa;letter-spacing:1px;text-transform:uppercase;'
            f'margin-bottom:4px;">{title}</div>'
        )
    return f'<div style="{_HERO_DIVIDER}">{label}{body}</div>'


def _hero_banner(run_date, badge, summary):
    stats_and_rates = _summary_stats(summary, dark=True) + _rates_inline(dark=True)
    return (
        f'<div style="background:{_HEADER_BG};color:#fff;padding:18px 22px;border-radius:6px;">'
        f"{_header_row(run_date, badge)}"
        f"{_hero_section(stats_and_rates)}"
        f"</div>"
    )


def _mode_badge(dry_run):
    if dry_run:
        return (
            '<span style="background:#e67e22;color:#fff;padding:5px 13px;border-radius:4px;'
            'font-size:11px;font-weight:bold;letter-spacing:1.5px;">DRY RUN</span>'
        )
    return (
        '<span style="background:#27ae60;color:#fff;padding:5px 13px;border-radius:4px;'
        'font-size:11px;font-weight:bold;letter-spacing:1.5px;">LIVE</span>'
    )


def _fmt_money(value):
    return f"{value:,.2f}"


def _h(text):
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _fmt_hours(h):
    """41.25 -> '41h 15m', 20.0 -> '20h', -1.25 -> '-1h 15m'."""
    total_minutes = round(abs(h) * 60)
    hrs, mins = divmod(total_minutes, 60)
    sign = "-" if h < 0 else ""
    return f"{sign}{hrs}h {mins}m" if mins else f"{sign}{hrs}h"


def _rates_inline(dark=False):
    colour = "#ccc" if dark else "#555"
    label_colour = "#8899aa" if dark else "#888"
    parts = [
        f"{sl} {rate}/h"
        for sl, rate in sorted(get_all_overcharge_rates().items())
    ]
    rates_text = " &middot; ".join(parts)
    if dark:
        return (
            f'<div style="{_HERO_DIVIDER}">'
            f'<p style="color:{colour};font-size:13px;margin:0;white-space:nowrap;">'
            f'<span style="color:{_ACCENT};">Rates (AUD)</span> &middot; {rates_text}</p>'
            f"</div>"
        )
    return f'<p style="color:{colour};font-size:13px;margin:0;">Rates (AUD) &middot; {rates_text}</p>'


def _display_summary(summary, results, ineligible, skipped):
    """Hero stats from actual lists — correct even for legacy combined payloads."""
    computed = sum(1 for r in results if "skipped" not in r)
    return {
        "eligible_projects": summary.get("eligible_projects", len(results)),
        "computed": summary.get("computed", computed),
        "skipped": len(skipped),
        "ineligible": len(ineligible),
        "written": summary.get("written", 0),
        "errors": summary.get("errors", 0),
    }


def _summary_stats(summary, dark=False):
    stats = [
        ("ELIGIBLE", summary.get("eligible_projects", 0)),
        ("CALCULATED", summary.get("computed", 0)),
        ("SKIPPED", summary.get("skipped", 0)),
        ("INELIGIBLE", summary.get("ineligible", summary.get("filtered", 0))),
        ("WRITTEN", summary.get("written", 0)),
        ("ERRORS", summary.get("errors", 0)),
    ]
    value_colour = "#fff" if dark else "#222"
    label_colour = "#8899aa" if dark else "#888"
    cells = "".join(
        f'<td style="width:16.66%;text-align:center;padding:8px 4px;vertical-align:top;">'
        f'<div style="font-size:28px;font-weight:bold;line-height:1.1;color:{value_colour};">'
        f'{_h(v)}</div>'
        f'<div style="font-size:11px;color:{label_colour};letter-spacing:1px;margin-top:6px;">'
        f'{label}</div></td>'
        for label, v in stats
    )
    return (
        f'<table style="width:100%;border-collapse:collapse;margin:8px 0 0;">'
        f"<tr>{cells}</tr></table>"
    )


def _sl_badge(sl):
    sl_bg = _SL_COLOUR.get(sl, "#7f8c8d")
    return (
        f'<span style="display:inline-block;background:{sl_bg};color:#fff;'
        f'padding:2px 7px;border-radius:3px;font-size:10px;font-weight:bold;'
        f'white-space:nowrap;">{_h(sl)}</span>'
    )


def _retainer_usage_pct(logged_h, planned_h):
    if planned_h > 0:
        return round(logged_h / planned_h * 100)
    return 0


def _hours_vs_retainer_inline(logged_h, planned_h, remaining_h, *, oc_value=0, rate=0):
    """One-line logged vs retainer hours with usage %, remaining/overage, and overcharge."""
    pct = _retainer_usage_pct(logged_h, planned_h)
    line = (
        f'<span style="font-weight:bold;color:#222;">{_fmt_hours(logged_h)}</span>'
        f' <span style="color:#888;">/ {_fmt_hours(planned_h)}</span>'
        f' <span style="color:#888;">&middot;</span> '
        f'<span style="font-weight:bold;color:{_PROGRESS};">{pct}%</span>'
    )
    if remaining_h < 0:
        line += (
            f' <span style="color:{_ACCENT};font-weight:bold;">'
            f'&middot; {_fmt_hours(abs(remaining_h))} over</span>'
        )
    if oc_value > 0:
        overage_h = max(0.0, logged_h - planned_h)
        line += (
            f' <span style="color:#888;">&middot;</span> '
            f'<span style="color:{_ACCENT};font-weight:bold;">'
            f"AUD {_fmt_money(oc_value)}</span>"
            f' <span style="color:#888;">'
            f"({_fmt_hours(overage_h)} &times; AUD {rate}/h)</span>"
        )
    return line


def _project_title_line(name, pid):
    """Line 1: bold title with regular-weight project id in brackets."""
    return (
        f'<div style="font-size:12px;line-height:1.4;word-break:break-word;">'
        f'<span style="font-weight:bold;">{_h(name)}</span>'
        f' <span style="font-weight:normal;color:#888;">({_h(pid)})</span>'
        f"</div>"
    )


def _project_tile(result, compact=False, progress=False):
    """Return HTML for a single compact project card."""
    pid  = result["project_id"]
    name = result.get("project_name") or f"(project {pid})"
    sl   = result["service_line"]
    status = result.get("status")
    # Skip "Active Client" — redundant under "Active Zembr Projects".
    status_badge = (
        _status_badge(status)
        if status and status.lower() != "additional6"
        else None
    )
    badges = _badge_row(sl, status_badge)

    if compact:
        return (
            f"{badges}"
            f"{_project_title_line(name, pid)}"
        )

    planned_h = result["planned_hours"]
    logged_h = result["logged_hours"]
    remaining_h = result["remaining_hours"]
    oc_value = result.get("overcharge_value", 0)
    rate = result.get("overcharge_rate", 0)
    detail_line = _hours_vs_retainer_inline(
        logged_h, planned_h, remaining_h, oc_value=oc_value, rate=rate,
    )
    return (
        f"{badges}"
        f"{_project_title_line(name, pid)}"
        f'<div style="font-size:11px;color:#444;line-height:1.5;margin-top:3px;">'
        f"{detail_line}</div>"
    )


def _skipped_project_detail(record, label):
    """Human-readable explanation for a single skipped project."""
    reason = record.get("reason", "")
    parts = []

    if label == "No current period":
        retainer_id = record.get("retainer_id")
        if retainer_id is not None:
            parts.append(f"Retainer #{retainer_id} has no current period in Scoro.")
        else:
            parts.append("No current retainer period found.")

    elif label == "Zero allowance":
        start = record.get("period_start")
        end = record.get("period_end")
        if start and end:
            parts.append(f"Period {start} → {end}.")
        parts.append("Allowance is 0h — retainer period duration is 0.")

    elif label == "No rate":
        start = record.get("period_start")
        end = record.get("period_end")
        if start and end:
            parts.append(f"Period {start} → {end}.")
        period_sum = record.get("period_sum")
        if period_sum is not None:
            parts.append(f"Period sum is {period_sum} (must be &gt; 0).")
        else:
            parts.append("Period sum is 0 — no rate to apply.")

    elif label == "Unrecognised project prefix":
        prefix = record.get("name_prefix", "")
        if not prefix:
            name = record.get("name", "")
            if name and "|" in name:
                prefix = name.split("|", 1)[0].strip()
        if prefix:
            parts.append(f"Prefix “{_h(prefix)}” is not a recognised service line.")
        elif reason.startswith("unknown service line"):
            parts.append(_h(reason) + ".")
        else:
            parts.append("No recognised prefix before the first “|” in the project name.")

    elif label == "Zero time entries in period":
        start = record.get("period_start")
        end = record.get("period_end")
        if start and end:
            parts.append(f"No billable entries logged between {start} and {end}.")
        else:
            parts.append("No billable time entries in the current period.")

    elif label == "Zero billable time":
        start = record.get("period_start")
        end = record.get("period_end")
        allowance = record.get("period_allowance_hours")
        if start and end:
            msg = f"0 billable hours logged in {start} → {end}"
            if allowance is not None:
                msg += f" (allowance {allowance}h)"
            parts.append(msg + ".")
        else:
            parts.append("0 billable hours logged in the current period.")

    elif reason:
        parts.append(_h(reason))

    return " ".join(parts)


def _skipped_cell(record, label):
    pid = record.get("project_id", "")
    name = _h(record.get("name") or f"(project {pid})")
    detail = _skipped_project_detail(record, label)
    corner_badge = None
    status = record.get("status")
    if status:
        corner_badge = _status_badge(status)
    return _excluded_tile(
        name, pid, detail, corner_badge=corner_badge, sl=record.get("service_line")
    )


def _activity_status_from_record(record):
    """Scoro project status from record field or legacy reason string."""
    status = record.get("status")
    if status:
        return status
    reason = record.get("reason", "")
    prefix = "not active (status="
    if reason.lower().startswith(prefix) and reason.endswith(")"):
        return reason[len(prefix) : -1]
    return None


def _status_badge(status):
    key = status.lower()
    style = _STATUS_BADGE.get(key)
    if style:
        label = _h(style["label"])
        bg = style["bg"]
        color = style["color"]
    else:
        label = _h(status)
        bg = "#e0e0e0"
        color = "#1d1f22"
    return (
        f'<span style="display:inline-block;background:{bg};color:{color};'
        f'padding:3px 8px;border-radius:4px;font-size:10px;font-weight:bold;'
        f'white-space:nowrap;">{label}</span>'
    )


def _ineligible_cell(record, label):
    pid = record.get("project_id", "")
    name = _h(record.get("name") or f"(project {pid})")
    corner_badge = None
    status = _activity_status_from_record(record)
    if status:
        corner_badge = _status_badge(status)
    return _excluded_tile(
        name, pid, corner_badge=corner_badge, sl=record.get("service_line")
    )


def _excluded_grid_row(tiles):
    while len(tiles) < 3:
        tiles.append(f'<td style="{_GRID_CELL_EMPTY}"></td>')
    return "<tr>" + "".join(tiles) + "</tr>"


def _excluded_grid(items, label, cell_fn=_ineligible_cell):
    """Three projects per row; cell layout depends on exclusion reason."""
    tiles = [
        f'<td style="{_GRID_CELL}">{cell_fn(f, label)}</td>'
        for f in items
    ]
    row_html = []
    for i in range(0, len(tiles), 3):
        row_html.append(_excluded_grid_row(tiles[i : i + 3]))
    return (
        f'<table cellpadding="0" cellspacing="0" '
        f'style="width:100%;border-collapse:collapse;margin:0;border:0;">'
        f'{"".join(row_html)}</table>'
    )


_INELIGIBLE_REASON_PRIORITY = {
    "No retainer ID": 0,
    "Not active": 1,
}

# Ineligible groups that show subheading + blurb only (no project tiles).
_INELIGIBLE_HIDE_TILES = frozenset({"Not active"})

_SKIP_REASON_PRIORITY = {
    "No current period": 0,
    "Zero allowance": 1,
    "No rate": 2,
    "Unrecognised project prefix": 3,
    "Zero time entries in period": 4,
    "Zero billable time": 5,
}


def _reason_label(reason):
    r = reason.lower()
    if r.startswith("not active"):
        return "Not active"
    if r in ("no retainer_id", "no retainer id"):
        return "No retainer ID"
    if r.startswith("zero allowance"):
        return "Zero allowance"
    if r.startswith("no rate"):
        return "No rate"
    if r == "unrecognised project prefix" or r.startswith("unknown service line"):
        return "Unrecognised project prefix"
    if r == "no current period":
        return "No current period"
    if r == "zero time entries in period":
        return "Zero time entries in period"
    if r == "zero billable time":
        return "Zero billable time"
    return reason.replace("_", " ").capitalize()


def _blurb_status_badges(keys):
    """Inline status badges with a small gap for use inside blurb paragraphs."""
    return "".join(
        f'<span style="display:inline-block;margin:2px 4px 2px 0;'
        f'vertical-align:middle;">{_status_badge(k)}</span>'
        for k in keys
    )


def _not_active_reason_blurb():
    equal_to = [k for k in _STATUS_BADGE if k not in _ELIGIBLE_STATUSES]
    return (
        f'<p style="color:#777;font-size:12px;margin:0 0 10px;line-height:1.5;">'
        f"Project is classified as an inactive project if the project status "
        f"is equal to one of {_blurb_status_badges(equal_to)}"
        f"</p>"
    )


def _reason_blurb(label, reason_blurbs):
    if not reason_blurbs or label not in reason_blurbs:
        return ""
    return (
        f'<p style="color:#777;font-size:12px;margin:0 0 10px;line-height:1.5;">'
        f"{_h(reason_blurbs[label])}</p>"
    )


def _excluded_section_body(
    items,
    empty_message,
    reason_priority,
    reason_blurbs=None,
    cell_fn=None,
    hide_tiles_labels=None,
):
    cell_fn = cell_fn or _ineligible_cell
    hide_tiles = hide_tiles_labels or frozenset()
    if not items:
        return f"<p><em>{empty_message}</em></p>"
    groups: dict = {}
    for f in items:
        label = _reason_label(f.get("reason", ""))
        groups.setdefault(label, []).append(f)
    sorted_groups = sorted(
        groups.items(),
        key=lambda kv: (reason_priority.get(kv[0], 99), kv[0]),
    )
    parts = []
    for i, (label, group_items) in enumerate(sorted_groups):
        items_sorted = sorted(group_items, key=lambda x: x.get("name", ""))
        grid = ""
        h3_count = len(items_sorted)
        blurb = _reason_blurb(label, reason_blurbs)
        if label == "Not active":
            blurb = _not_active_reason_blurb()
        if label == "No retainer ID":
            showable, ignored, other = _partition_no_retainer_items(
                items_sorted
            )
            items_sorted = showable
            h3_count = len(showable)
            blurb = _no_retainer_reason_blurb(ignored, other)
            if showable:
                grid = _excluded_grid(items_sorted, label, cell_fn)
        elif label not in hide_tiles:
            grid = _excluded_grid(items_sorted, label, cell_fn)
        parts.append(
            _h3(label, h3_count, first=(i == 0)) + blurb + grid
        )
    return "".join(parts)


def _filter_no_retainer_errors(ineligible):
    """Active projects without a retainer — recognised prefix shown, ignored prefix footer-only."""
    result = []
    for r in ineligible:
        if _reason_label(r.get("reason", "")) != "No retainer ID":
            continue
        if not _meets_active_criteria(r):
            continue
        name = r.get("name") or ""
        if _is_recognised_prefix(name) or _ignored_prefix_bucket(name):
            result.append(r)
    return result


def _filter_skipped_by_label(skipped, label):
    return [
        r for r in skipped
        if _reason_label(r.get("reason", "")) == label
    ]


def _cancelled_sub_cell(record, label=None):
    """Tile for a recently subscription-cancelled project."""
    pid = record.get("project_id", "")
    name = _h(record.get("name") or f"(project {pid})")
    cancelled = (
        record.get("cancellation_month")
        or record.get("modified_date")
        or ""
    )
    detail = f"Cancelled: {_h(cancelled)}" if cancelled else None
    corner_badge = _status_badge(record.get("status", "completed"))
    return _excluded_tile(
        name,
        pid,
        detail,
        corner_badge=corner_badge,
        sl=record.get("service_line"),
    )


def _previous_calendar_month_label(run_date):
    """Human-readable month name for the calendar month before run_date."""
    try:
        d = datetime.strptime(run_date, "%Y-%m-%d").date()
    except ValueError:
        return "the previous calendar month"
    first_this_month = d.replace(day=1)
    last_prev = first_this_month - timedelta(days=1)
    return last_prev.strftime("%B %Y")


def _cancelled_subs_intro(run_date):
    month = _previous_calendar_month_label(run_date)
    return (
        f'<p style="color:#555;font-size:13px;margin:0 0 12px;line-height:1.5;">'
        f"Covers the <strong>previous calendar month</strong> ({month}). "
        f"Cancellation month is based on each project&rsquo;s "
        f"<code>c_cancellationmonth</code> in Scoro."
        f"</p>"
    )


def _build_cancelled_subs_section(cancelled_subs, run_date):
    """Team-report section: subscription-cancelled retainer projects (previous month)."""
    intro = _cancelled_subs_intro(run_date)
    month = _previous_calendar_month_label(run_date)
    if not cancelled_subs:
        return (
            intro
            + f"<p><em>No subscriptions cancelled in {month}.</em></p>"
        )
    return intro + _excluded_grid(cancelled_subs, "Cancelled subs", _cancelled_sub_cell)


def _data_error_blurb(label):
    text = _DATA_ERROR_BLURBS.get(label, "")
    if not text:
        return ""
    return (
        f'<p style="color:#777;font-size:12px;margin:0 0 10px;line-height:1.5;">'
        f"{_h(text)}</p>"
    )


def _build_data_errors_section(ineligible, skipped):
    """Team-report section: three data-error subcategories with ignored-prefix filtering."""
    all_ignored = Counter()
    total_raw = 0
    total_showable = 0
    parts = []

    subsections = (
        (
            "a. No retainer",
            _filter_no_retainer_errors(ineligible),
            "No retainer",
            "No retainer ID",
            _ineligible_cell,
            True,
        ),
        (
            "b. No current period",
            _filter_skipped_by_label(skipped, "No current period"),
            "No current period",
            "No current period",
            _skipped_cell,
            False,
        ),
        (
            "c. Zero time entries in period",
            _filter_skipped_by_label(skipped, "Zero time entries in period"),
            "Zero time entries in period",
            "Zero time entries in period",
            _skipped_cell,
            False,
        ),
    )

    for i, (heading, raw_items, blurb_key, cell_label, cell_fn, req_prefix) in enumerate(
        subsections
    ):
        total_raw += len(raw_items)
        showable, counts = _partition_data_error_items(
            sorted(raw_items, key=lambda x: x.get("name", "")),
            require_recognised_prefix=req_prefix,
        )
        all_ignored.update(counts)
        total_showable += len(showable)
        grid = (
            _excluded_grid(showable, cell_label, cell_fn)
            if showable
            else ""
        )
        parts.append(
            _h3(heading, len(showable), first=(i == 0))
            + _data_error_blurb(blurb_key)
            + grid
        )

    if total_raw == 0:
        body = "<p><em>No potential data errors this run.</em></p>"
    else:
        body = "".join(parts)

    return body, all_ignored, total_showable


def _project_grid(computed, compact=False, progress=False):
    """Render all project tiles in a 3-per-row table."""
    td_tiles = [
        f'<td style="{_GRID_CELL}">'
        f'{_project_tile(r, compact=compact, progress=progress)}</td>'
        for r in computed
    ]
    row_html = []
    for i in range(0, len(td_tiles), 3):
        row_html.append(_excluded_grid_row(td_tiles[i : i + 3]))
    return (
        f'<table cellpadding="0" cellspacing="0" '
        f'style="width:100%;border-collapse:collapse;margin:0;border:0;">'
        f'{"".join(row_html)}</table>'
    )


_SERVICE_LINE_ORDER = {
    "BK": 0, "BD": 1, "EA": 2, "EA North": 2, "EA South": 3, "SA": 4,
}


def _group_by_service_line(items):
    """Group project results by service line in a stable display order."""
    groups = {}
    for r in items:
        sl = r.get("service_line") or "?"
        groups.setdefault(sl, []).append(r)
    return sorted(
        groups.items(),
        key=lambda kv: (_SERVICE_LINE_ORDER.get(kv[0], 99), kv[0]),
    )


def _project_grid_by_service_line(items, compact=False, progress=False):
    """Render project tiles grouped under service-line subheadings."""
    if not items:
        return ""
    parts = []
    for i, (sl, group) in enumerate(_group_by_service_line(items)):
        sorted_group = sorted(
            group,
            key=lambda r: r.get("remaining_hours", 0),
            reverse=True,
        )
        colour = _SL_COLOUR.get(sl, "#555")
        total_oc = sum(r.get("overcharge_value", 0) for r in sorted_group)
        parts.append(
            _h3(
                sl,
                len(sorted_group),
                colour,
                first=(i == 0),
                extra=_service_line_overcharge_extra(total_oc),
            )
            + _project_grid(sorted_group, compact=compact, progress=progress)
        )
    return "".join(parts)


def _enrich_computed_results(results, projects_by_pid):
    """Attach project names/status and return only successfully computed results."""
    computed = [r for r in results if "skipped" not in r]
    enriched = []
    for r in computed:
        needs_name = "project_name" not in r
        needs_status = "status" not in r
        if (needs_name or needs_status) and projects_by_pid:
            proj = projects_by_pid.get(r["project_id"])
            if proj:
                r = dict(r)
                if needs_name:
                    r["project_name"] = (
                        proj.get("project_name") or proj.get("name") or ""
                    )
                if needs_status:
                    status = proj.get("status")
                    if status is not None:
                        r["status"] = status
        enriched.append(r)
    return enriched


def _audit_pre(record):
    """Monospace dump of a skipped/ineligible audit record."""
    payload = _h(json.dumps(record, indent=2, default=str))
    return (
        f'<pre style="font-size:10px;color:#555;margin:8px 0 0;white-space:pre-wrap;'
        f'word-break:break-word;background:#f5f5f5;padding:6px 8px;border-radius:3px;">'
        f"{payload}</pre>"
    )


def _skipped_cell_with_audit(record, label):
    return _skipped_cell(record, label) + _audit_pre(record)


def _ineligible_cell_with_audit(record, label):
    return _ineligible_cell(record, label) + _audit_pre(record)


def _errors_section_html(errors, first=False):
    if not errors:
        return ""
    rows = "".join(
        f'<div style="margin:0 0 10px;padding:10px 12px;background:#fdecea;'
        f'border-left:4px solid #c0392b;border-radius:3px;">'
        f'<div style="font-weight:bold;color:#c0392b;">Project #{_h(e.get("project_id", "?"))}</div>'
        f'<div style="font-size:12px;color:#444;margin-top:4px;">{_h(e.get("error", ""))}</div>'
        f"</div>"
        for e in errors
    )
    return _section(
        f"0 &mdash; Errors ({len(errors)})",
        rows,
        first=first,
    )


def _project_detail_block(result, project, period, tasks, dry_run, field_key):
    """Full calculation drill-down for one computed project."""
    pid = result["project_id"]
    name = result.get("project_name") or f"(project {pid})"
    sl = result["service_line"]
    retainer_id = (project or {}).get("retainer_id", "—")

    period_lines = ["Period: (unknown)"]
    entries = []
    if period:
        pstart, pend = calc.period_bounds(period)
        duration_secs = int(period.get("duration") or 0)
        allowance_h = round(duration_secs / 3600.0, 4) if duration_secs else 0
        period_id = period.get("id", "—")
        period_sum = period.get("sum", "—")
        start = (period.get("start_date") or "")[:10]
        end = (period.get("end_date") or "")[:10]
        period_lines = [
            f"Period: {start} → {end} (id {period_id})",
            (
                f"Allowance: {allowance_h:.4f}h ({duration_secs}s) "
                f"| Period sum: {period_sum}"
            ),
        ]
        if tasks is not None and pstart and pend:
            entries = calc.list_all_period_entries(tasks, pstart, pend)

    counted = sum(1 for e in entries if e["billable"])
    entry_rows = []
    shown = entries[:_MAX_LOG_ENTRIES]
    for e in shown:
        status = "✓" if e["billable"] else "✗"
        reason = f' — {_h(e["reason"])}' if e.get("reason") else ""
        colour = "#27ae60" if e["billable"] else "#c0392b"
        entry_rows.append(
            f"<tr>"
            f'<td style="{_ENTRY_TD}color:{colour};">{status}</td>'
            f'<td style="{_ENTRY_TD}">{_h(e["datetime"][:10])}</td>'
            f'<td style="{_ENTRY_TD}">{_h(e["task_id"])}</td>'
            f'<td style="{_ENTRY_TD}">{_h(e["time_entry_id"])}</td>'
            f'<td style="{_ENTRY_TD}">{e["duration_hours"]:.4f}h</td>'
            f'<td style="{_ENTRY_TD}color:#888;">{reason}</td>'
            f"</tr>"
        )
    overflow = len(entries) - len(shown)
    overflow_row = ""
    if overflow > 0:
        overflow_row = (
            f'<tr><td colspan="6" style="{_ENTRY_TD}color:#888;font-style:italic;">'
            f"… and {overflow} more entries not shown</td></tr>"
        )

    entries_table = ""
    if entries:
        entries_table = (
            f'<div style="font-size:11px;color:#555;margin:6px 0 4px;">'
            f"Time entries ({len(entries)} in period, {counted} counted):</div>"
            f'<table style="{_ENTRY_TABLE}">'
            f"<thead><tr>"
            f'<th style="{_ENTRY_TH}"></th>'
            f'<th style="{_ENTRY_TH}">Date</th>'
            f'<th style="{_ENTRY_TH}">Task</th>'
            f'<th style="{_ENTRY_TH}">Entry</th>'
            f'<th style="{_ENTRY_TH}">Hours</th>'
            f'<th style="{_ENTRY_TH}">Note</th>'
            f"</tr></thead><tbody>"
            f"{''.join(entry_rows)}{overflow_row}"
            f"</tbody></table>"
        )
    else:
        entries_table = (
            '<div style="font-size:11px;color:#888;margin:6px 0;">'
            "No time entries in period.</div>"
        )

    planned_h = result["planned_hours"]
    logged_h = result["logged_hours"]
    remaining_h = result["remaining_hours"]
    rate = result["overcharge_rate"]
    oc_value = result["overcharge_value"]
    overage_h = max(0.0, logged_h - planned_h)

    formula = (
        f"max(0, {logged_h:.4f} − {planned_h:.4f}) × AUD {rate}/h "
        f"= AUD {_fmt_money(oc_value)}"
    )
    if oc_value <= 0:
        formula = (
            f"Within allowance ({logged_h:.4f}h ≤ {planned_h:.4f}h) → AUD 0.00"
        )

    if dry_run:
        write_line = (
            f"[DRY_RUN] would write overcharge_value={oc_value:.2f} to {field_key}"
        )
    else:
        write_line = f"Wrote overcharge_value={oc_value:.2f} to {field_key}"

    period_html = "".join(
        f'<div style="{_DETAIL_META}">{_h(line)}</div>' for line in period_lines
    )

    status = result.get("status") or (project or {}).get("status")
    status_badge = _status_badge(status) if status else None

    return (
        f'<div style="{_DETAIL_BLOCK}">'
        f"{_badge_row(sl, status_badge)}"
        f'<div style="font-weight:bold;font-size:13px;line-height:1.4;word-break:break-word;">'
        f"{_h(name)}</div>"
        f'<div style="{_DETAIL_META}">Project #{pid} &middot; Retainer {retainer_id}</div>'
        f"{period_html}"
        f"{entries_table}"
        f'<div style="font-size:11px;color:#444;line-height:1.6;margin-top:8px;">'
        f"Planned: {_fmt_hours(planned_h)} ({planned_h:.4f}h) &middot; "
        f"Logged: {_fmt_hours(logged_h)} ({logged_h:.4f}h) &middot; "
        f"Remaining: {_fmt_hours(remaining_h)} ({remaining_h:.4f}h)"
        f"</div>"
        f'<div style="font-size:11px;color:#444;line-height:1.6;margin-top:4px;">'
        f"Formula: {formula}"
        f"</div>"
        f'<div style="font-size:11px;color:#666;margin-top:4px;">{write_line}</div>'
        f"</div>"
    )


def _project_details_by_service_line(
    items,
    projects_by_pid,
    period_by_pid,
    tasks_by_project,
    dry_run,
    field_key,
):
    """Render full detail blocks grouped under service-line subheadings."""
    if not items:
        return ""
    parts = []
    for i, (sl, group) in enumerate(_group_by_service_line(items)):
        sorted_group = sorted(
            group,
            key=lambda r: r.get("remaining_hours", 0),
            reverse=True,
        )
        colour = _SL_COLOUR.get(sl, "#555")
        total_oc = sum(r.get("overcharge_value", 0) for r in sorted_group)
        blocks = []
        for r in sorted_group:
            pid = r["project_id"]
            project = (projects_by_pid or {}).get(pid)
            period = (period_by_pid or {}).get(pid)
            tasks = (tasks_by_project or {}).get(pid, [])
            blocks.append(
                _project_detail_block(
                    r, project, period, tasks, dry_run, field_key
                )
            )
        parts.append(
            _h3(
                sl,
                len(sorted_group),
                colour,
                first=(i == 0),
                extra=_service_line_overcharge_extra(total_oc),
            )
            + "".join(blocks)
        )
    return "".join(parts)


def _log_config_line(field_key, lookback_days):
    return (
        f'<p style="color:#777;font-size:12px;margin:0 0 12px;line-height:1.5;">'
        f"Config: field_key={_h(field_key)} &middot; "
        f"task_fetch_lookback_days={lookback_days}</p>"
    )


def build_log_html_body(
    run_date,
    dry_run,
    summary,
    results,
    ineligible,
    skipped,
    errors,
    projects_by_pid,
    period_by_pid,
    tasks_by_project,
    field_key="overcharge_value",
    lookback_days=14,
):
    """Return the full HTML log email body with calculation drill-down."""
    badge = _mode_badge(dry_run)
    display_summary = _display_summary(summary, results, ineligible, skipped)
    enriched = _enrich_computed_results(results, projects_by_pid)
    total_oc = sum(r.get("overcharge_value", 0) for r in enriched)

    section1_intro = (
        _log_config_line(field_key, lookback_days)
        + f'<p style="color:#555;font-size:13px;">'
        f"All {len(enriched)} calculated &nbsp;&middot;&nbsp; "
        f'Total overcharge: <strong style="color:{_ACCENT}">'
        f"AUD {_fmt_money(total_oc)}</strong>"
        f"</p>"
    )

    section1 = _section(
        f"1 &mdash; Eligible Projects ({len(enriched)})",
        section1_intro
        + _project_details_by_service_line(
            enriched,
            projects_by_pid,
            period_by_pid,
            tasks_by_project,
            dry_run,
            field_key,
        ),
        first=not errors,
    )

    section2_body = _excluded_section_body(
        skipped,
        "No projects were skipped this run.",
        _SKIP_REASON_PRIORITY,
        reason_blurbs=_SKIP_REASON_BLURBS,
        cell_fn=_skipped_cell_with_audit,
    )

    section3_body = _excluded_section_body(
        ineligible,
        "No projects were ineligible this run.",
        _INELIGIBLE_REASON_PRIORITY,
        cell_fn=_ineligible_cell_with_audit,
        hide_tiles_labels=_INELIGIBLE_HIDE_TILES,
    )

    section2 = _section(
        f"2 &mdash; Skipped Projects ({len(skipped)})",
        section2_body,
    )

    section3 = _section(
        f"3 &mdash; Ineligible Projects ({len(ineligible)})",
        section3_body,
    )

    body_sections = [_hero_banner(run_date, badge, display_summary)]
    if errors:
        body_sections.append(_errors_section_html(errors, first=True))
    body_sections.extend([section1, section2, section3])

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<style>{_STYLE}</style>
</head>
<body>

{"".join(body_sections)}

</body>
</html>"""


def _project_detail_text(result, project, period, tasks, dry_run, field_key):
    """Plain-text calculation drill-down for one computed project."""
    pid = result["project_id"]
    name = result.get("project_name") or f"(project {pid})"
    sl = result["service_line"]
    retainer_id = (project or {}).get("retainer_id", "—")

    lines = [
        f'[{sl}] project {pid} "{name}" | retainer={retainer_id}',
    ]

    entries = []
    if period:
        pstart, pend = calc.period_bounds(period)
        duration_secs = int(period.get("duration") or 0)
        lines.append(
            f"  period: {(period.get('start_date') or '')[:10]} → "
            f"{(period.get('end_date') or '')[:10]} | id={period.get('id', '—')}"
        )
        lines.append(
            f"  allowance={result['planned_hours']:.4f}h ({duration_secs}s) | "
            f"sum={period.get('sum', '—')}"
        )
        if tasks is not None and pstart and pend:
            entries = calc.list_all_period_entries(tasks, pstart, pend)

    counted = sum(1 for e in entries if e["billable"])
    lines.append(f"  time entries ({len(entries)} in period, {counted} counted):")
    for e in entries[:_MAX_LOG_ENTRIES]:
        mark = "✓" if e["billable"] else "✗"
        reason = f" — {e['reason']}" if e.get("reason") else ""
        lines.append(
            f"    {mark} task {e['task_id']} / entry {e['time_entry_id']} | "
            f"{e['datetime'][:10]} | {e['duration_hours']:.4f}h{reason}"
        )
    overflow = len(entries) - min(len(entries), _MAX_LOG_ENTRIES)
    if overflow > 0:
        lines.append(f"    … and {overflow} more entries not shown")

    planned_h = result["planned_hours"]
    logged_h = result["logged_hours"]
    remaining_h = result["remaining_hours"]
    rate = result["overcharge_rate"]
    oc_value = result["overcharge_value"]

    lines.extend([
        (
            f"  totals: planned={planned_h:.4f}h | logged={logged_h:.4f}h | "
            f"remaining={remaining_h:.4f}h"
        ),
        (
            f"  overcharge_rate={rate}/h | overcharge_value={oc_value:.2f}"
        ),
    ])
    if dry_run:
        lines.append(
            f"  [DRY_RUN] would write overcharge_value={oc_value:.2f} to {field_key}"
        )
    else:
        lines.append(
            f"  wrote overcharge_value={oc_value:.2f} to {field_key}"
        )
    return "\n".join(lines)


def build_log_text_body(
    run_date,
    dry_run,
    summary,
    results,
    ineligible,
    skipped,
    errors,
    projects_by_pid,
    period_by_pid,
    tasks_by_project,
    field_key="overcharge_value",
    lookback_days=14,
):
    """Plain-text log email mirroring the HTML log structure."""
    mode = "DRY RUN" if dry_run else "LIVE"
    display = _display_summary(summary, results, ineligible, skipped)
    enriched = _enrich_computed_results(results, projects_by_pid)

    lines = [
        f"Overcharge Run Log — {run_date} [{mode}]",
        "",
        (
            f"Eligible: {display['eligible_projects']} | "
            f"Calculated: {display['computed']} | "
            f"Skipped: {display['skipped']} | "
            f"Ineligible: {display['ineligible']} | "
            f"Written: {display['written']} | "
            f"Errors: {display['errors']}"
        ),
        f"Config: field_key={field_key} | task_fetch_lookback_days={lookback_days}",
        "",
    ]

    if errors:
        lines.append(f"ERRORS ({len(errors)})")
        lines.append("=" * 40)
        for e in errors:
            lines.append(f"  Project #{e.get('project_id', '?')}: {e.get('error', '')}")
        lines.append("")

    lines.append(f"ELIGIBLE PROJECTS ({len(enriched)})")
    lines.append("=" * 40)
    for sl, group in _group_by_service_line(enriched):
        sorted_group = sorted(
            group,
            key=lambda r: r.get("remaining_hours", 0),
            reverse=True,
        )
        total_oc = sum(r.get("overcharge_value", 0) for r in sorted_group)
        lines.append(f"\n--- {sl} ({len(sorted_group)}) — AUD {total_oc:.2f} ---")
        for r in sorted_group:
            pid = r["project_id"]
            project = (projects_by_pid or {}).get(pid)
            period = (period_by_pid or {}).get(pid)
            tasks = (tasks_by_project or {}).get(pid, [])
            lines.append("")
            lines.append(
                _project_detail_text(
                    r, project, period, tasks, dry_run, field_key
                )
            )

    lines.extend(["", f"SKIPPED ({len(skipped)})", "=" * 40])
    if not skipped:
        lines.append("  (none)")
    else:
        for record in skipped:
            label = _reason_label(record.get("reason", ""))
            pid = record.get("project_id", "?")
            name = record.get("name") or f"(project {pid})"
            lines.append(f"\n  [{label}] #{pid} {name}")
            lines.append(json.dumps(record, indent=4, default=str))

    lines.extend(["", f"INELIGIBLE ({len(ineligible)})", "=" * 40])
    if not ineligible:
        lines.append("  (none)")
    else:
        for record in ineligible:
            label = _reason_label(record.get("reason", ""))
            pid = record.get("project_id", "?")
            name = record.get("name") or f"(project {pid})"
            lines.append(f"\n  [{label}] #{pid} {name}")
            lines.append(json.dumps(record, indent=4, default=str))

    return "\n".join(lines)


def build_html_body(
    run_date,
    dry_run,
    summary,
    results,
    ineligible,
    skipped,
    cancelled_subs,
    projects_by_pid,
    period_by_pid,
    tasks_by_project,
):
    """Return the full HTML email body as a string."""
    badge = _mode_badge(dry_run)
    display_summary = _display_summary(summary, results, ineligible, skipped)

    enriched = _enrich_computed_results(results, projects_by_pid)

    overcharged = sorted(
        [r for r in enriched if r.get("overcharge_value", 0) > 0],
        key=lambda r: r.get("remaining_hours", 0),
        reverse=True,
    )
    within_budget = sorted(
        [r for r in enriched if r.get("overcharge_value", 0) <= 0],
        key=lambda r: r.get("remaining_hours", 0),
    )
    total_oc = sum(r.get("overcharge_value", 0) for r in enriched)

    section1_intro = (
        f'<p style="color:#555;font-size:13px;">'
        f'All {len(enriched)} calculated &nbsp;&middot;&nbsp; '
        f'<strong>{len(overcharged)}</strong> beyond contract hours &nbsp;&middot;&nbsp; '
        f'Total: <strong style="color:{_ACCENT}">AUD {_fmt_money(total_oc)}</strong>'
        f' &nbsp;&middot;&nbsp; Sorted by: time differential (ascending)'
        f'</p>'
    )

    section2_body = _build_cancelled_subs_section(cancelled_subs, run_date)

    section3_body, ignored_counts, data_errors_count = _build_data_errors_section(
        ineligible, skipped
    )
    footer = _ignored_prefix_summary_line(ignored_counts)

    section1 = _section(
        f"1 &mdash; Active Zembr Projects ({len(enriched)})",
        section1_intro
        + _h3("Beyond contract hours", len(overcharged), _ACCENT)
        + _project_grid_by_service_line(overcharged, progress=True)
        + _h3("Within contract hours", len(within_budget), _ACCENT)
        + _project_grid_by_service_line(within_budget, progress=True),
        first=True,
    )

    section2 = _section(
        f"2 &mdash; Cancelled Subs ({len(cancelled_subs)})",
        section2_body,
    )

    section3 = _section(
        f"3 &mdash; Potential Data Errors ({data_errors_count})",
        section3_body,
    )

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<style>{_STYLE}</style>
</head>
<body>

{_hero_banner(run_date, badge, display_summary)}

{section1}

{section2}

{section3}

{footer}

</body>
</html>"""


def send_ses_email(subject, from_addr, to_addrs, region, *, html_body=None, text_body=None):
    import boto3  # pyright: ignore[reportMissingImports]  # Lambda runtime provides boto3

    body = {}
    if text_body is not None:
        body["Text"] = {"Data": text_body, "Charset": "UTF-8"}
    if html_body is not None:
        body["Html"] = {"Data": html_body, "Charset": "UTF-8"}
    if not body:
        raise ValueError("send_ses_email requires html_body and/or text_body")

    ses = boto3.client("ses", region_name=region)
    return ses.send_email(
        Source=from_addr,
        Destination={"ToAddresses": to_addrs},
        Message={
            "Subject": {"Data": subject, "Charset": "UTF-8"},
            "Body": body,
        },
    )


def send_error_alert(
    run_date,
    dry_run,
    summary,
    errors,
    from_addr,
    to_addrs,
    ses_region,
):
    """Send a concise failure alert to the testing recipients. Never raises."""
    try:
        mode = "DRY RUN" if dry_run else "LIVE"
        project_ids = [
            str(error.get("project_id", "unknown"))
            for error in errors
        ]
        text = "\n".join(
            [
                f"Overcharge run failures — {run_date} [{mode}]",
                "",
                f"Errors: {len(errors)}",
                f"Eligible projects: {summary.get('eligible_projects', 0)}",
                f"Processed: {summary.get('processed', 0)}",
                f"Written: {summary.get('written', 0)}",
                "",
                "Failed project IDs:",
                *(f"- {project_id}" for project_id in project_ids),
                "",
                "The run completed without an automatic retry. "
                "Check CloudWatch logs and the operations report for details.",
            ]
        )
        subject = f"[ALERT] Overcharge Run Failures — {run_date} [{mode}]"
        send_ses_email(
            subject,
            from_addr,
            to_addrs,
            ses_region,
            text_body=text,
        )
        log.info("error alert sent to %s", to_addrs)
    except Exception:
        log.exception("error alert email failed — run result unaffected")


def send_log_email(
    run_date,
    dry_run,
    summary,
    results,
    ineligible,
    skipped,
    errors,
    projects_by_pid,
    period_by_pid,
    tasks_by_project,
    from_addr,
    to_addrs,
    ses_region,
    field_key="overcharge_value",
    lookback_days=14,
):
    """Build and send the multipart log email (HTML + plain text). Never raises."""
    try:
        common = dict(
            run_date=run_date,
            dry_run=dry_run,
            summary=summary,
            results=results,
            ineligible=ineligible,
            skipped=skipped,
            errors=errors,
            projects_by_pid=projects_by_pid,
            period_by_pid=period_by_pid,
            tasks_by_project=tasks_by_project,
            field_key=field_key,
            lookback_days=lookback_days,
        )
        html = build_log_html_body(**common)
        text = build_log_text_body(**common)
        mode = "DRY RUN" if dry_run else "LIVE"
        subject = f"Overcharge Run Log — {run_date} [{mode}]"
        send_ses_email(
            subject, from_addr, to_addrs, ses_region,
            html_body=html, text_body=text,
        )
        log.info("log email sent to %s", to_addrs)
    except Exception:
        log.exception("log email failed — run result unaffected")


def send_run_email(
    run_date,
    dry_run,
    summary,
    results,
    ineligible,
    skipped,
    cancelled_subs,
    projects_by_pid,
    period_by_pid,
    tasks_by_project,
    from_addr,
    to_addrs,
    ses_region,
):
    """Build and send the run report email. Never raises."""
    try:
        html = build_html_body(
            run_date, dry_run, summary, results, ineligible, skipped,
            cancelled_subs, projects_by_pid, period_by_pid, tasks_by_project,
        )
        mode    = "DRY RUN" if dry_run else "LIVE"
        subject = f"Overcharge Run — {run_date} [{mode}]"
        send_ses_email(
            subject, from_addr, to_addrs, ses_region, html_body=html
        )
        log.info("report email sent to %s", to_addrs)
    except Exception:
        log.exception("report email failed — run result unaffected")
