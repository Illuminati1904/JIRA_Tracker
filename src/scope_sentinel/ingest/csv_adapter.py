"""Jira CSV export adapter (snapshot source: no changelog).

Jira repeats headers for multi-value columns (Comment, Comment, ... / Labels, Labels, ...), so we parse
with the stdlib csv module and keep (header, value) pairs rather than a dict.
"""
from __future__ import annotations

import csv
import hashlib
import io
import re
from pathlib import Path

from ..model import Comment, Export, StoryRecord, num, sha1
from ..timeutil import fmt, parse_naive

_COMMENT_RE = re.compile(r"^(.{6,32}?\d{1,2}:\d{2}(?::\d{2})?(?:\s?[AP]M)?);([^;]*);(.*)$", re.S)

# header aliases (lowercased) -> canonical attribute
_ALIASES: dict[str, list[str]] = {
    "key": ["issue key", "key"],
    "summary": ["summary"],
    "status": ["status"],
    "description": ["description"],
    "ac": ["custom field (acceptance criteria)", "acceptance criteria"],
    "created": ["created"],
    "updated": ["updated"],
    "status_cat": ["status category"],
    "status_cat_changed": ["status category changed"],
    "assignee": ["assignee"],
    "priority": ["priority"],
    "sprint": ["sprint"],
    "fix_version": ["fix versions", "fix version/s", "fix version"],
    "points": ["custom field (story points)", "story points"],
    "epic_key": ["parent key", "parent id", "epic link"],
    "epic": ["parent summary", "epic name"],
    "capability": ["custom field (capability)", "capability"],
    "developer": ["custom field (developer assigned)"],
    "tester": ["custom field (tester assigned)"],
    "code_reviewer": ["custom field (code reviewed by)"],
    "est_dev": ["custom field (estimated dev)"],
}

# What each source column enables; used to tell the user which analysis degrades.
_DEGRADES = {
    "ac": "Acceptance Criteria column missing: AC changes cannot be detected",
    "description": "Description column missing: description changes cannot be detected",
    "status": "Status column missing: status movement and sign-off checks are disabled",
    "created": "Created column missing: new-story detection is disabled",
    "updated": "Updated column missing: bulk-edit noise detection is weaker",
    "status_cat_changed": "Status category changed column missing: inferred status movement (first run) is weaker",
    "epic": "Parent summary column missing: epic grouping is empty",
    "capability": "Capability column missing: capability chips and grouping are omitted (by-epic still works)",
}


def parse_comment(raw: str, cid_prefix: str) -> Comment:
    m = _COMMENT_RE.match(raw)
    if m:
        ts = fmt(parse_naive(m.group(1)))
        author, text = m.group(2).strip(), m.group(3).strip()
    else:
        ts, author, text = None, "", raw.strip()
    cid = sha1(cid_prefix, ts or "", author, text[:200])[:16]
    return Comment(id=cid, ts=ts, author=author, text=text)


def load_csv(path: str | Path) -> Export:
    p = Path(path)
    raw = p.read_bytes()
    text = raw.decode("utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(text))
    try:
        header = [h.strip() for h in next(reader)]
    except StopIteration:
        raise ValueError(f"{p.name} is empty")
    lower = [h.lower() for h in header]

    idx: dict[str, int] = {}
    for attr, names in _ALIASES.items():
        for n in names:
            if n in lower:
                idx[attr] = lower.index(n)
                break
    if "key" not in idx:
        raise ValueError(f"{p.name}: no 'Issue key' column. Is this a Jira CSV export?")
    comment_cols = [i for i, h in enumerate(lower) if h == "comment"]
    label_cols = [i for i, h in enumerate(lower) if h == "labels"]
    sprint_cols = [i for i, h in enumerate(lower) if h == "sprint"]

    warnings = [msg for attr, msg in _DEGRADES.items() if attr not in idx]
    if not comment_cols:
        warnings.append("No Comment columns: comment evidence is unavailable")

    def cell(row, attr):
        i = idx.get(attr)
        if i is None or i >= len(row):
            return ""
        return row[i]

    stories: list[StoryRecord] = []
    for row in reader:
        if not row or not any(c.strip() for c in row):
            continue
        key = cell(row, "key").strip()
        if not key:
            continue
        sprints = [row[i].strip() for i in sprint_cols if i < len(row) and row[i].strip()]
        s = StoryRecord(
            key=key,
            summary=cell(row, "summary").strip(),
            status=cell(row, "status").strip(),
            description=cell(row, "description"),
            ac=cell(row, "ac"),
            created=fmt(parse_naive(cell(row, "created"))),
            updated=fmt(parse_naive(cell(row, "updated"))),
            status_cat=cell(row, "status_cat").strip(),
            status_cat_changed=fmt(parse_naive(cell(row, "status_cat_changed"))),
            assignee=cell(row, "assignee").strip(),
            priority=cell(row, "priority").strip(),
            sprint=", ".join(sprints) if len(sprints) > 1 else (sprints[0] if sprints else ""),
            fix_version=cell(row, "fix_version").strip(),
            points=num(cell(row, "points").strip()),
            epic_key=cell(row, "epic_key").strip(),
            epic=cell(row, "epic").strip(),
            capability=cell(row, "capability").strip(),
            developer=cell(row, "developer").strip(),
            tester=cell(row, "tester").strip(),
            code_reviewer=cell(row, "code_reviewer").strip(),
            est_dev=num(cell(row, "est_dev").strip()),
            labels=sorted({row[i].strip() for i in label_cols if i < len(row) and row[i].strip()}),
        )
        for i in comment_cols:
            if i < len(row) and row[i].strip():
                s.comments.append(parse_comment(row[i], key))
        s.comments.sort(key=lambda c: c.ts or "")
        s.changelog = None
        stories.append(s)

    jira_base = ""
    for s in stories:
        for c in s.comments:
            m = re.search(r"https://[\w.-]+\.atlassian\.net", c.text)
            if m:
                jira_base = m.group(0)
                break
        if jira_base:
            break
    return Export(
        stories=stories, kind="csv", path=str(p), sha=hashlib.sha1(raw).hexdigest(), warnings=warnings,
        capabilities={"exact_history": False, "exact_status_path": False},
        jira_base=jira_base, max_updated=max((s.updated for s in stories if s.updated), default=None),
    )
