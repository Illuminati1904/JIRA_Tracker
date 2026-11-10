"""Jira REST JSON adapter (history source).

Accepts any of:
  * a search response  {"issues": [...], "names": {...}}   (GET /rest/api/{2,3}/search[/jql]?expand=changelog,names&fields=*all)
  * a list of such pages, or a bare list of issues, or one issue
  * JSON Lines (one issue or page per line)

With `expand=changelog` every Summary / Description / Acceptance Criteria / status change arrives with
timestamp and author, so analysis is *exact on the first run*. Jira caps changelog at 100 histories per
issue in search responses; truncated changelogs are detected and reconciled against current values.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from ..model import Comment, Export, FieldChange, StoryRecord, sha1
from ..timeutil import fmt, parse_iso_to_tz
from .adf import to_text

# canonical attribute -> accepted Jira *names* (lowercased)
_NAME_ALIASES: dict[str, list[str]] = {
    "ac": ["acceptance criteria"],
    "points": ["story points", "story point estimate"],
    "sprint": ["sprint"],
    "capability": ["capability"],
    "developer": ["developer assigned"],
    "tester": ["tester assigned"],
    "code_reviewer": ["code reviewed by"],
    "est_dev": ["estimated dev"],
}
# changelog `field` (lowercased) -> canonical tracked field
_CHANGELOG_FIELDS = {
    "summary": "summary", "description": "description", "status": "status",
    "acceptance criteria": "ac", "story points": "points", "story point estimate": "points",
    "sprint": "sprint", "fix version": "fix_version", "fixversions": "fix_version",
    "labels": "labels", "priority": "priority", "assignee": "assignee", "capability": "capability",
}


def _rich(v) -> str:
    if v is None:
        return ""
    if isinstance(v, dict) and v.get("type") == "doc":
        return to_text(v)
    if isinstance(v, (dict, list)):
        return to_text(v)
    return str(v)


def _num(v) -> str:
    if v in (None, ""):
        return ""
    try:
        f = float(v)
        return str(int(f)) if f == int(f) else str(f)
    except (TypeError, ValueError):
        return str(v)


def _person(d) -> str:
    if not isinstance(d, dict):
        return ""
    return d.get("accountId") or d.get("key") or d.get("name") or ""


def _display(d) -> str:
    if not isinstance(d, dict):
        return ""
    return d.get("displayName") or d.get("name") or d.get("accountId") or ""


def _read_issues(path: Path) -> tuple[list[dict], dict[str, str]]:
    text = path.read_text(encoding="utf-8-sig")
    names: dict[str, str] = {}
    issues: list[dict] = []

    def absorb(obj):
        if isinstance(obj, list):
            for o in obj:
                absorb(o)
        elif isinstance(obj, dict):
            if "issues" in obj and isinstance(obj["issues"], list):
                if isinstance(obj.get("names"), dict):
                    names.update(obj["names"])
                issues.extend(obj["issues"])
            elif "key" in obj and "fields" in obj:
                if isinstance(obj.get("names"), dict):
                    names.update(obj["names"])
                issues.append(obj)

    try:
        absorb(json.loads(text))
    except json.JSONDecodeError:
        for line in text.splitlines():
            line = line.strip()
            if line:
                absorb(json.loads(line))
    if not issues:
        raise ValueError(f"{path.name}: no issues found. Expected a Jira REST search response, a list of issues or JSON Lines.")
    return issues, names


def load_jira_json(path: str | Path, tz: str = "UTC", aliases: dict[str, list[str]] | None = None) -> Export:
    p = Path(path)
    raw = p.read_bytes()
    issues, names = _read_issues(p)
    aliases = aliases or {}

    name_to_id: dict[str, str] = {}
    for fid, nm in names.items():
        name_to_id.setdefault(str(nm).strip().lower(), fid)
    field_id: dict[str, str] = {}
    for attr, nms in _NAME_ALIASES.items():
        for a in list(aliases.get(attr, [])) + nms:
            a_l = a.lower()
            if a.startswith("customfield_"):
                field_id[attr] = a
                break
            if a_l in name_to_id:
                field_id[attr] = name_to_id[a_l]
                break

    warnings: list[str] = []
    if not names:
        warnings.append("No 'names' map in the dump (add expand=names): custom fields such as Acceptance Criteria, "
                        "Story Points, Sprint and Capability cannot be resolved unless 'field_aliases' maps them to customfield ids")
    else:
        for attr in ("ac", "points", "capability"):
            if attr not in field_id:
                warnings.append(f"Custom field for '{attr}' not found among field names; set field_aliases.{attr} in sentinel.toml")

    stories: list[StoryRecord] = []
    n_trunc = 0
    jira_base = ""
    for it in issues:
        f = it.get("fields") or {}
        key = it.get("key", "")
        if not key:
            continue
        if not jira_base:
            m = re.match(r"(https://[\w.-]+\.atlassian\.net)", it.get("self", "") or "")
            jira_base = m.group(1) if m else ""

        def cf(attr):
            fid = field_id.get(attr)
            return f.get(fid) if fid else None

        status = f.get("status") or {}
        parent = f.get("parent") or {}
        sprint_raw = cf("sprint")
        sprints = []
        for sp in (sprint_raw if isinstance(sprint_raw, list) else ([sprint_raw] if sprint_raw else [])):
            sprints.append(sp.get("name", "") if isinstance(sp, dict) else str(sp))
        cap = cf("capability")
        s = StoryRecord(
            key=key,
            summary=(f.get("summary") or "").strip(),
            status=(status.get("name") or "").strip(),
            description=_rich(f.get("description")),
            ac=_rich(cf("ac")),
            created=fmt(parse_iso_to_tz(f.get("created"), tz)),
            updated=fmt(parse_iso_to_tz(f.get("updated"), tz)),
            status_cat=((status.get("statusCategory") or {}).get("name") or "").strip(),
            status_cat_changed=fmt(parse_iso_to_tz(f.get("statuscategorychangedate"), tz)),
            assignee=_display(f.get("assignee")),
            priority=((f.get("priority") or {}).get("name") or "").strip(),
            sprint=", ".join(x for x in sprints if x),
            fix_version=", ".join(v.get("name", "") for v in (f.get("fixVersions") or []) if isinstance(v, dict)),
            points=_num(cf("points")),
            epic_key=parent.get("key", "") or "",
            epic=((parent.get("fields") or {}).get("summary") or "").strip(),
            capability=(cap.get("value") if isinstance(cap, dict) else (cap or "")) or "",
            developer=_display(cf("developer")) if isinstance(cf("developer"), dict) else str(cf("developer") or ""),
            tester=_display(cf("tester")) if isinstance(cf("tester"), dict) else str(cf("tester") or ""),
            code_reviewer=_display(cf("code_reviewer")) if isinstance(cf("code_reviewer"), dict) else str(cf("code_reviewer") or ""),
            est_dev=_num(cf("est_dev")),
            labels=sorted({l for l in (f.get("labels") or []) if l}),
        )
        # comments
        craw = f.get("comment")
        clist = craw.get("comments", []) if isinstance(craw, dict) else (craw or [])
        for c in clist:
            body = _rich(c.get("body")).strip()
            if not body:
                continue
            cid = str(c.get("id") or sha1(key, c.get("created", ""), body[:200])[:16])
            s.comments.append(Comment(id=f"{key}:{cid}", ts=fmt(parse_iso_to_tz(c.get("created"), tz)),
                                      author=_person(c.get("author")), text=body))
        s.comments.sort(key=lambda c: c.ts or "")
        # changelog
        cl = it.get("changelog")
        if cl is not None:
            hist = cl.get("histories") or []
            s.changelog = []
            for h in hist:
                ts = fmt(parse_iso_to_tz(h.get("created"), tz))
                if not ts:
                    continue
                for item in h.get("items") or []:
                    canon = _CHANGELOG_FIELDS.get(str(item.get("field", "")).strip().lower())
                    if not canon:
                        # a custom-field-id keyed item (some DC versions): map through aliases
                        fid = item.get("fieldId")
                        canon = next((a for a, i in field_id.items() if i == fid and a in ("ac", "points", "sprint", "capability")), None)
                    if not canon:
                        continue
                    old, new = item.get("fromString") or "", item.get("toString") or ""
                    if canon == "labels":
                        old = "|".join(sorted(old.split())) if old else ""
                        new = "|".join(sorted(new.split())) if new else ""
                    if canon == "points":
                        old, new = _num(old), _num(new)
                    s.changelog.append(FieldChange(ts=ts, field=canon, old=old, new=new,
                                                   author=_person(h.get("author")), history_id=str(h.get("id", ""))))
            s.changelog.sort(key=lambda c: (c.ts, c.history_id))
            total = cl.get("total")
            if isinstance(total, int) and total > len(hist):
                s.changelog_complete = False
                n_trunc += 1
        stories.append(s)

    if n_trunc:
        warnings.append(f"{n_trunc} stories have a truncated changelog (Jira search returns at most 100 histories per issue). "
                        "Current values are reconciled, but older history is incomplete; fetch /issue/{key}/changelog for those.")
    with_hist = sum(1 for s in stories if s.changelog is not None)
    if with_hist == 0:
        warnings.append("No changelog in the dump (add expand=changelog): behaves like a CSV snapshot, history starts at this run")
    return Export(
        stories=stories, kind="jira_json", path=str(p), sha=hashlib.sha1(raw).hexdigest(), warnings=warnings,
        capabilities={"exact_history": with_hist > 0, "exact_status_path": with_hist > 0},
        jira_base=jira_base, max_updated=max((s.updated for s in stories if s.updated), default=None),
    )
