"""Canonical story model. Both adapters (CSV, Jira REST JSON) produce these."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, asdict

# Fields whose changes are *scope* (the user's business rule). Everything else is metadata drift.
SCOPE_FIELDS = ("summary", "description", "ac")
# Metadata fields tracked for drift, stored inline (short values).
META_FIELDS = ("points", "sprint", "fix_version", "epic_key", "epic", "priority", "assignee", "capability", "labels")
TRACKED_FIELDS = SCOPE_FIELDS + ("status",) + META_FIELDS


def num(v) -> str:
    """Normalise story points: '3.0' -> '3'."""
    if v in (None, ""):
        return ""
    try:
        f = float(v)
        return str(int(f)) if f == int(f) else str(f)
    except (TypeError, ValueError):
        return str(v).strip()


def sha1(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode("utf-8", "replace")).hexdigest()


@dataclass
class Comment:
    id: str
    ts: str | None          # 'YYYY-MM-DD HH:MM' or None when the export had no timestamp
    author: str             # account id (never a display name)
    text: str


@dataclass
class FieldChange:
    """One item from a Jira changelog (exact history)."""
    ts: str
    field: str              # canonical field key from TRACKED_FIELDS
    old: str
    new: str
    author: str = ""
    history_id: str = ""


@dataclass
class StoryRecord:
    key: str
    summary: str = ""
    status: str = ""
    description: str = ""
    ac: str = ""
    created: str | None = None
    updated: str | None = None
    status_cat: str = ""
    status_cat_changed: str | None = None
    assignee: str = ""
    priority: str = ""
    sprint: str = ""
    fix_version: str = ""
    points: str = ""
    epic_key: str = ""
    epic: str = ""
    capability: str = ""
    developer: str = ""
    tester: str = ""
    code_reviewer: str = ""
    est_dev: str = ""
    labels: list[str] = field(default_factory=list)
    comments: list[Comment] = field(default_factory=list)
    # None  = the source carries no history (CSV).  list = changelog present (maybe empty).
    changelog: list[FieldChange] | None = None
    changelog_complete: bool = True

    def project(self) -> dict:
        """Projection stored in the memory DB (no comments / changelog: those are events)."""
        d = asdict(self)
        d.pop("comments")
        d.pop("changelog")
        d.pop("changelog_complete")
        return d


@dataclass
class Export:
    stories: list[StoryRecord]
    kind: str                       # 'csv' | 'jira_json'
    path: str = ""
    sha: str = ""
    warnings: list[str] = field(default_factory=list)
    capabilities: dict[str, bool] = field(default_factory=dict)  # which analyses this source supports
    jira_base: str = ""
    max_updated: str | None = None
