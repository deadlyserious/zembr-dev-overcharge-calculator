"""Scoro overcharge calculator — canonical service-line and prefix data.

Single source of truth for the rate-table service lines, the recognised
project-name prefixes, the EA variant groupings, and the display ordering.
handler.py, rates.py and email_report.py all import from here so the copies
they used to hold by hand can never drift apart again.
"""

# Rate-table keys: one overcharge rate product per service line.
DEFAULT_SERVICE_LINES = ("BK", "BD", "EA", "SA")

# Recognised project-name prefixes (before first "|"). All others are out of scope.
VALID_PREFIXES = {
    "BK", "EA UK", "EA NA", "EA South", "EA S", "SA", "BD",
}
VALID_PREFIXES_UPPER = frozenset(p.upper() for p in VALID_PREFIXES)
# EA variants resolve to the display lines used in reports.
EA_PREFIX_TO_DISPLAY = {
    "EA UK": "EA UK",
    "EA NA": "EA NA",
}
EA_SOUTH_PREFIXES = frozenset({"EA SOUTH", "EA S"})

# Stable display ordering for service-line groupings in the report email.
SERVICE_LINE_ORDER = {
    "BK": 0, "BD": 1, "EA": 2, "EA UK": 2, "EA NA": 3, "EA South": 4, "SA": 5,
}


def service_line_from_project(project_name: str) -> str | None:
    """Return the service line code from a Scoro project name, or None if not applicable.

    Project names follow the convention: '<CODE> | <Client> | <Owner>'
    """
    prefix = project_name.split("|")[0].strip().upper()
    if prefix in EA_PREFIX_TO_DISPLAY:
        return EA_PREFIX_TO_DISPLAY[prefix]
    if prefix in EA_SOUTH_PREFIXES:
        return "EA South"
    if prefix not in VALID_PREFIXES_UPPER:
        return None
    return prefix  # BK, SA, BD


def overcharge_rate_line(display_line: str) -> str:
    """Map a display service line to the rate table key."""
    if display_line in ("EA UK", "EA NA", "EA South"):
        return "EA"
    return display_line


def known_prefixes_text() -> str:
    """Human-readable list of every recognised prefix, for report text.

    Derived from VALID_PREFIXES so the wording can never drift from the
    canonical set. Prefixes are grouped by display service line (each
    line's canonical name first, then its variants), with "or" before
    the final entry, e.g. "BK, BD, EA UK, ..., or SA".
    """
    ordered = sorted(
        VALID_PREFIXES,
        key=lambda p: (
            SERVICE_LINE_ORDER.get(service_line_from_project(p), 99),
            p != service_line_from_project(p),
            p,
        ),
    )
    return ", ".join(ordered[:-1]) + ", or " + ordered[-1]
