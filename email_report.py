"""HTML email report for each overcharge calculator run, sent via AWS SES.

Entry point: send_run_email(). Never raises — all exceptions are logged so
the Lambda return value is unaffected.
"""

import base64
import logging
from pathlib import Path

from rates import get_all_overcharge_rates

_LOGO_PATH = Path(__file__).with_name("assets") / "zembr-logo.png"
_LOGO_DATA_URI = None

log = logging.getLogger("overcharge_calculator")

# Zembr logo dot pink (sampled from assets/zembr-logo.png)
_ACCENT = "#eb0453"
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

_KNOWN_NAME_PREFIXES = "BK, EA North, EA NA, EA South, SA, BD"

_VALID_PREFIXES_UPPER = frozenset({
    "BK", "EA NORTH", "EA UK", "EA NA", "EA SOUTH", "EA S", "SA", "BD",
})
_EA_NORTH_PREFIXES = frozenset({"EA NORTH", "EA UK", "EA NA"})
_EA_SOUTH_PREFIXES = frozenset({"EA SOUTH", "EA S"})


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


def _no_retainer_hidden_bucket(name):
    upper = _raw_name_prefix(name).upper()
    if upper == "LUNAR":
        return "lunar"
    if upper == "Z":
        return "z"
    return "other"


def _partition_no_retainer_items(items):
    showable = []
    lunar = z_count = other = 0
    for r in items:
        name = r.get("name") or ""
        if _is_recognised_prefix(name):
            showable.append(r)
            continue
        bucket = _no_retainer_hidden_bucket(name)
        if bucket == "lunar":
            lunar += 1
        elif bucket == "z":
            z_count += 1
        else:
            other += 1
    return showable, lunar, z_count, other


def _project_count_phrase(count):
    word = "project" if count == 1 else "projects"
    return f"({count} {word})"


def _no_retainer_reason_blurb(lunar, z_count, other):
    base = (
        "Project is not linked to a retainer in Scoro. "
        "Only retainer projects are included in the overcharge run. "
        "Only projects with a recognised service-line prefix are listed below"
    )
    has_hidden = bool(lunar or z_count or other)
    if not has_hidden:
        return (
            f'<p style="color:#777;font-size:12px;margin:0 0 10px;line-height:1.5;">'
            f"{_h(base)}.</p>"
        )
    bullets = []
    if lunar:
        bullets.append(f"Lunar {_project_count_phrase(lunar)}")
    if z_count:
        bullets.append(f"Z {_project_count_phrase(z_count)}")
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

_INELIGIBLE_REASON_BLURBS = {
    "Not active": (
        "Project status is not active (additional6) or at risk (additional8). "
        "Reactivate the project in Scoro or exclude it from reporting."
    ),
}


def _excluded_tile(name, pid, detail=None, corner_badge=None):
    """Shared layout for skipped and ineligible project cells."""
    detail_html = ""
    if detail:
        detail_html = f'<div style="{_EXCLUDED_DETAIL}">{detail}</div>'
    body = (
        f'<div style="{_EXCLUDED_WRAP}">'
        f'<div style="{_EXCLUDED_NAME}">{name}</div>'
        f'<div style="{_EXCLUDED_PID}">Project #{pid}</div>'
        f"{detail_html}"
        f"</div>"
    )
    if not corner_badge:
        return body
    return (
        f'<table style="width:100%;border-collapse:collapse;">'
        f"<tr>"
        f'<td style="vertical-align:top;padding:0;">{body}</td>'
        f'<td style="vertical-align:top;text-align:right;padding:0 0 0 8px;'
        f'width:1%;white-space:nowrap;">{corner_badge}</td>'
        f"</tr></table>"
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


def _project_tile(result, compact=False):
    """Return HTML for a single compact project card."""
    pid  = result["project_id"]
    name = result.get("project_name") or f"(project {pid})"
    sl   = result["service_line"]

    if compact:
        return (
            f'<table style="width:100%;border-collapse:collapse;">'
            f'<tr>'
            f'<td style="width:36px;vertical-align:top;padding:0 8px 0 0;">{_sl_badge(sl)}</td>'
            f'<td style="vertical-align:top;padding:0;">'
            f'<div style="font-weight:bold;font-size:12px;line-height:1.4;word-break:break-word;">'
            f'{_h(name)}</div>'
            f'</td></tr></table>'
        )

    planned_h   = result["planned_hours"]
    logged_h    = result["logged_hours"]
    remaining_h = result["remaining_hours"]
    rate        = result["overcharge_rate"]
    oc_value    = result["overcharge_value"]
    overage_h   = max(0.0, logged_h - planned_h)

    logged_line = (
        f'Logged: {_fmt_hours(logged_h)} / {_fmt_hours(planned_h)}'
        f' &middot; Remaining: {_fmt_hours(remaining_h)}'
    )
    if oc_value > 0:
        logged_line += (
            f' &middot; <span style="color:{_ACCENT};font-weight:bold;">'
            f'AUD {_fmt_money(oc_value)}</span>'
            f' ({_fmt_hours(overage_h)} &times; AUD {rate}/h)'
        )

    return (
        f'<table style="width:100%;border-collapse:collapse;">'
        f'<tr>'
        f'<td style="width:36px;vertical-align:top;padding:0 8px 0 0;">{_sl_badge(sl)}</td>'
        f'<td style="vertical-align:top;padding:0;">'
        f'<div style="font-weight:bold;font-size:12px;line-height:1.4;word-break:break-word;">'
        f'{_h(name)}</div>'
        f'<div style="color:#888;font-size:11px;margin:2px 0 4px;">Project #{pid}</div>'
        f'<div style="font-size:11px;color:#444;line-height:1.5;">{logged_line}</div>'
        f'</td></tr></table>'
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
    return _excluded_tile(name, pid, detail)


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
    if label == "Not active":
        status = _activity_status_from_record(record)
        if status:
            corner_badge = _status_badge(status)
    return _excluded_tile(name, pid, corner_badge=corner_badge)


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
        if label == "No retainer ID":
            showable, lunar, z_count, other = _partition_no_retainer_items(
                items_sorted
            )
            items_sorted = showable
            h3_count = len(showable)
            blurb = _no_retainer_reason_blurb(lunar, z_count, other)
            if showable:
                grid = _excluded_grid(items_sorted, label, cell_fn)
        elif label not in hide_tiles:
            grid = _excluded_grid(items_sorted, label, cell_fn)
        parts.append(
            _h3(label, h3_count, first=(i == 0)) + blurb + grid
        )
    return "".join(parts)


def _project_grid(computed, compact=False):
    """Render all project tiles in a 3-per-row table."""
    td_tiles = [
        f'<td style="{_GRID_CELL}">{_project_tile(r, compact=compact)}</td>'
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


def _project_grid_by_service_line(items, compact=False):
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
            + _project_grid(sorted_group, compact=compact)
        )
    return "".join(parts)


def build_html_body(
    run_date,
    dry_run,
    summary,
    results,
    ineligible,
    skipped,
    projects_by_pid,
    period_by_pid,
    tasks_by_project,
):
    """Return the full HTML email body as a string."""
    badge = _mode_badge(dry_run)
    display_summary = _display_summary(summary, results, ineligible, skipped)

    computed = [r for r in results if "skipped" not in r]

    enriched = []
    for r in computed:
        if "project_name" not in r and projects_by_pid:
            proj = projects_by_pid.get(r["project_id"])
            if proj:
                r = dict(r)
                r["project_name"] = proj.get("project_name") or proj.get("name") or ""
        enriched.append(r)

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

    section2_body = _excluded_section_body(
        skipped,
        "No projects were skipped this run.",
        _SKIP_REASON_PRIORITY,
        reason_blurbs=_SKIP_REASON_BLURBS,
        cell_fn=_skipped_cell,
    )

    section3_body = _excluded_section_body(
        ineligible,
        "No projects were ineligible this run.",
        _INELIGIBLE_REASON_PRIORITY,
        reason_blurbs=_INELIGIBLE_REASON_BLURBS,
        cell_fn=_ineligible_cell,
        hide_tiles_labels=_INELIGIBLE_HIDE_TILES,
    )

    eligible_count = summary.get("eligible_projects", len(enriched))

    section1 = _section(
        f"1 &mdash; Eligible Projects ({eligible_count})",
        section1_intro
        + _h3("Beyond contract hours", len(overcharged), _ACCENT, first=True)
        + _project_grid_by_service_line(overcharged)
        + _h3("Within contract hours", len(within_budget), _ACCENT)
        + _project_grid_by_service_line(within_budget, compact=True),
        first=True,
    )

    section2 = _section(
        f"2 &mdash; Skipped Projects ({len(skipped)})",
        section2_body,
    )

    section3 = _section(
        f"3 &mdash; Ineligible Projects ({len(ineligible)})",
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

</body>
</html>"""


def send_ses_email(html_body, subject, from_addr, to_addrs, region):
    import boto3  # pyright: ignore[reportMissingImports]  # Lambda runtime provides boto3

    ses = boto3.client("ses", region_name=region)
    return ses.send_email(
        Source=from_addr,
        Destination={"ToAddresses": to_addrs},
        Message={
            "Subject": {"Data": subject, "Charset": "UTF-8"},
            "Body": {"Html": {"Data": html_body, "Charset": "UTF-8"}},
        },
    )


def send_run_email(
    run_date,
    dry_run,
    summary,
    results,
    ineligible,
    skipped,
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
            projects_by_pid, period_by_pid, tasks_by_project,
        )
        mode    = "DRY RUN" if dry_run else "LIVE"
        subject = f"Overcharge Run — {run_date} [{mode}]"
        send_ses_email(html, subject, from_addr, to_addrs, ses_region)
        log.info("email report sent to %s", to_addrs)
    except Exception:
        log.exception("email report failed — run result unaffected")
