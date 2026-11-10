"""Timestamps are stored as naive 'YYYY-MM-DD HH:MM' strings in the *project timezone*.

Lexicographic order == chronological order, which keeps SQL and JSON simple.
CSV exports are assumed to already be in the project timezone (Jira exports in the
exporting user's zone). REST JSON carries offsets and is converted.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

FMT = "%Y-%m-%d %H:%M"

_CSV_FORMATS = [
    "%m/%d/%Y %I:%M %p", "%d/%b/%y %I:%M %p", "%d/%b/%Y %I:%M %p", "%d/%b/%y %H:%M",
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%m/%d/%Y %H:%M", "%Y-%m-%d",
]


def fmt(dt: datetime | None) -> str | None:
    return dt.strftime(FMT) if dt else None


def parse_naive(s: str | None) -> datetime | None:
    """Parse a Jira CSV-style timestamp (no zone) or one of our own stored strings."""
    if not s or not isinstance(s, str):
        return None
    s = s.strip()
    if not s:
        return None
    for f in _CSV_FORMATS:
        try:
            return datetime.strptime(s, f)
        except ValueError:
            continue
    return None


def parse_iso_to_tz(s: str | None, tz: str) -> datetime | None:
    """Parse a Jira REST timestamp ('2026-10-28T09:12:00.000+0000') into a naive project-tz datetime."""
    if not s or not isinstance(s, str):
        return None
    s = s.strip()
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        m = re.match(r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})(?::(\d{2}))?", s)
        if not m:
            return None
        dt = datetime.strptime(f"{m.group(1)} {m.group(2)}", FMT)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=None)
    return dt.astimezone(ZoneInfo(tz) if tz != "UTC" else timezone.utc).replace(tzinfo=None)


def to_day(ts: str | None) -> str:
    return (ts or "")[:10]
