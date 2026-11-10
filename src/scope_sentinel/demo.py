"""Synthetic Jira scenario generator (fictional project, no client data).

Renders ONE underlying history as (a) Jira-style CSV snapshots and (b) Jira REST JSON with changelog,
so both adapters can be exercised and compared. Used by the tests and by `sentinel demo`.
"""
from __future__ import annotations

import csv
import json
from copy import deepcopy
from datetime import datetime
from pathlib import Path

WF_ORDER = ["New", "Story Writing", "Pending AIC Approval", "Design Writing", "Internal Design Review",
            "Pending CIT PO Review", "Pending Business SME Review", "Pending Business Signoff - AIC", "ToDo",
            "Dev In Progress", "Ready For Testing", "In Testing", "Ready For PO Review", "Done"]
USERS = {"u-ba": "Alex BA", "u-po": "Sam PO", "u-rv1": "Reviewer One", "u-rv2": "Reviewer Two", "u-dev": "Dev Dana"}

_AC = "AC1 The user can open the list.\nAC2 The list shows name and status.\nAC3 The list is sorted by name."
_AC_EDITED = "AC1 The user can open the list.\nAC2 The list shows name, status and owner.\nAC3 The list is sorted by name."
_DESC = "As a rep I want a list of accounts. The list loads in two seconds. Filters are out of scope."
_DESC_REWRITE = "As a manager I need a territory dashboard with maps, KPIs and exports. The dashboard supports drill down. Data refreshes hourly."


def _story(key, summary, status, created, cap="Account Management", epic="Accounts", desc=_DESC, ac=_AC, points="3",
           labels=("Pod-1",), sprint="Sprint 10"):
    return {"key": key, "summary": summary, "status": status, "created": created, "capability": cap, "epic": epic,
            "epic_key": "DEMO-100", "description": desc, "ac": ac, "points": points, "labels": list(labels),
            "sprint": sprint, "priority": "Medium", "assignee": "Alex BA", "fix_version": "Release 1"}


def scenario() -> tuple[list[dict], list[tuple], list[tuple]]:
    """-> (initial stories, change events, comments). Change event: (ts, key, field, value)."""
    stories = [
        _story("DEMO-1", "Account list: display and filter", "ToDo", "2026-10-01 09:00"),
        _story("DEMO-2", "Territory dashboard", "Pending AIC Approval", "2026-10-05 09:00", cap="Territory Management", epic="Territories"),
        _story("DEMO-3", "Visit report: capture notes", "ToDo", "2026-10-06 09:00", cap="Interaction Management", epic="Visits"),
        _story("DEMO-4", "Consent: edit channel preferences", "Done", "2026-09-10 09:00", cap="Privacy & Consent", epic="Consent"),
        _story("DEMO-7", "Sample request: approvals", "In Testing", "2026-10-07 09:00", cap="Sample Management", epic="Samples"),
        _story("DEMO-8", "Account detail: header", "Story Writing", "2026-10-08 09:00",
               desc='As a rep I want "an account header". The header shows name and tier.'),
        _story("DEMO-9", "Account detail: tabs", "Design Writing", "2026-10-09 09:00"),
        _story("DEMO-10", "Legacy import wizard", "Story Writing", "2026-10-10 09:00"),
        _story("DEMO-11", "Order capture: line items", "Story Writing", "2026-10-11 09:00", cap="Interaction Management", epic="Visits",
               ac="AC1 The rep can add a line item.\nAC2 Quantity is required."),
    ]
    for i in range(12, 18):   # filler stories touched by one bulk label update
        stories.append(_story(f"DEMO-{i}", f"Reference data: object {i}", "Story Writing", "2026-10-12 09:00", epic="Reference"))
    ev = [
        # DEMO-1: signed off 10-20 (ToDo from start in this model); AC2 edited AFTER sign-off on 11-03
        ("2026-11-03 14:05", "DEMO-1", "ac", _AC_EDITED),
        # DEMO-2: description rewritten before sign-off, then signed off
        ("2026-10-29 10:00", "DEMO-2", "description", _DESC_REWRITE),
        ("2026-11-02 16:30", "DEMO-2", "status", "ToDo"),
        # DEMO-3: regression from sign-off
        ("2026-11-03 11:15", "DEMO-3", "status", "Story Writing"),
        # DEMO-7: deferred
        ("2026-11-03 09:40", "DEMO-7", "status", "Deferred"),
        # DEMO-8: formatting-only edit (smart quotes) - must NOT count as scope change
        ("2026-11-01 08:15", "DEMO-8", "description", "As a rep I want “an account header”. The header shows name and tier."),
        # DEMO-9: metadata only
        ("2026-11-01 12:00", "DEMO-9", "points", "5"),
        # DEMO-11: three ACs added before sign-off -> Major
        ("2026-10-30 13:00", "DEMO-11", "ac", "AC1 The rep can add a line item.\nAC2 Quantity is required.\nAC3 Price is shown.\nAC4 Discounts are applied.\nAC5 Totals update live."),
        ("2026-11-01 09:30", "DEMO-11", "status", "Pending AIC Approval"),
    ]
    # bulk label update, same minute, six stories
    for i in range(12, 18):
        ev.append(("2026-11-03 10:00", f"DEMO-{i}", "labels", ["Pod-1", "SPRINT-11"]))
    comments = [
        ("2026-10-28 09:12", "DEMO-2", "u-rv1", "Please add a roll-up rule to AC2, approval is contingent on it."),
        ("2026-10-30 09:00", "DEMO-2", "u-po", "Approved with the new description. Aligned with intent."),
        ("2026-11-02 08:00", "DEMO-4", "u-rv2", "Please add a primary/secondary rule to this consent story; the current AC conflicts with US review."),
        ("2026-11-03 10:20", "DEMO-7", "u-po", "Parking this for Release 2, push to the next release."),
        ("2026-10-31 15:00", "DEMO-1", "u-rv1", "Approved. Signed off."),
    ]
    # new stories created inside the window
    new = [
        (_story("DEMO-5", "Order history: view", "New", "2026-11-01 10:00", cap="Interaction Management", epic="Visits"), ),
        (_story("DEMO-6", "Order history: view", "New", "2026-11-02 10:00", cap="Interaction Management", epic="Visits"), ),
        (_story("DEMO-18", "Pricing rules:", "Story Writing", "2026-11-03 09:00", cap="Sample Management", epic="Samples"), ),
    ]
    for (s,) in new:
        stories.append(s)
    return stories, ev, comments


def _apply(stories: list[dict], events: list[tuple], t: str) -> dict[str, dict]:
    st = {s["key"]: deepcopy(s) for s in stories if s["created"] <= t}
    st_updated = {k: v["created"] for k, v in st.items()}
    for ts, key, f, val in sorted(events):
        if ts <= t and key in st:
            st[key][f] = val
            st_updated[key] = ts
    for k in st:
        st[k]["updated"] = st_updated[k]
    return st


def _csv_ts(ts: str) -> str:
    d = datetime.strptime(ts, "%Y-%m-%d %H:%M")
    return d.strftime("%m/%d/%Y %I:%M %p").lstrip("0").replace("/0", "/")


def render_csv(t: str, path: Path) -> None:
    stories, events, comments = scenario()
    st = _apply(stories, events, t)
    removed_after = "2026-11-03 12:00"
    if t >= removed_after:
        st.pop("DEMO-10", None)       # deleted from Jira between exports
    cmts: dict[str, list] = {}
    for ts, key, au, text in comments:
        if ts <= t:
            cmts.setdefault(key, []).append(f"{_csv_ts(ts)};{au};{text}")
    maxc = max([len(v) for v in cmts.values()] + [1])
    maxl = max([len(s["labels"]) for s in st.values()] + [1])
    header = ["Summary", "Issue key", "Status", "Description", "Custom field (Acceptance Criteria)", "Created", "Updated",
              "Status Category", "Status Category Changed", "Assignee", "Priority", "Custom field (Story Points)",
              "Sprint", "Fix versions", "Parent key", "Parent summary", "Custom field (Capability)"] + \
             ["Labels"] * maxl + ["Comment"] * maxc
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(header)
        for key in sorted(st, key=lambda k: int(k.split("-")[1])):
            s = st[key]
            cat = "Done" if s["status"] == "Done" else ("In Progress" if s["status"] in ("Dev In Progress", "In Testing") else "To Do")
            row = [s["summary"], key, s["status"], s["description"], s["ac"], _csv_ts(s["created"]), _csv_ts(s["updated"]),
                   cat, _csv_ts(s["updated"]), s["assignee"], s["priority"], s["points"] + ".0", s["sprint"],
                   s["fix_version"], s["epic_key"], s["epic"], s["capability"]]
            row += s["labels"] + [""] * (maxl - len(s["labels"]))
            cc = cmts.get(key, [])
            row += cc + [""] * (maxc - len(cc))
            w.writerow(row)


def _adf(text: str) -> dict:
    return {"type": "doc", "version": 1, "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": line}]} for line in text.split("\n") if line.strip()]}


def _iso(ts: str) -> str:
    return ts.replace(" ", "T") + ":00.000+0000"


def render_json(t: str, path: Path, with_changelog: bool = True, truncate_changelog: bool = False) -> None:
    stories, events, comments = scenario()
    st = _apply(stories, events, t)
    if t >= "2026-11-03 12:00":
        st.pop("DEMO-10", None)
    names = {"customfield_10001": "Acceptance Criteria", "customfield_10002": "Story Points",
             "customfield_10003": "Sprint", "customfield_10004": "Capability"}
    issues = []
    for key, s in st.items():
        fields = {
            "summary": s["summary"], "description": _adf(s["description"]),
            "status": {"name": s["status"], "statusCategory": {"name": "To Do"}},
            "created": _iso(s["created"]), "updated": _iso(s["updated"]), "statuscategorychangedate": _iso(s["updated"]),
            "labels": s["labels"], "assignee": {"accountId": "u-ba", "displayName": s["assignee"]},
            "priority": {"name": s["priority"]}, "fixVersions": [{"name": s["fix_version"]}],
            "parent": {"key": s["epic_key"], "fields": {"summary": s["epic"]}},
            "customfield_10001": _adf(s["ac"]), "customfield_10002": float(s["points"]),
            "customfield_10003": [{"name": s["sprint"]}], "customfield_10004": {"value": s["capability"]},
            "comment": {"comments": [
                {"id": str(1000 + i), "author": {"accountId": au}, "created": _iso(ts), "body": _adf(text)}
                for i, (ts, k2, au, text) in enumerate(comments) if k2 == key and ts <= t]},
        }
        issue = {"key": key, "self": f"https://demo.atlassian.net/rest/api/3/issue/{key}", "fields": fields}
        if with_changelog:
            cur = deepcopy(next(x for x in stories if x["key"] == key))
            hist = []
            for ts, k2, f, val in sorted(events):
                if k2 != key or ts > t:
                    continue
                ov, nv = cur[f], val
                cur[f] = val
                fld = {"ac": "Acceptance Criteria", "points": "Story Points"}.get(f, f)
                if f == "labels":
                    ov, nv = " ".join(ov), " ".join(nv)
                hist.append({"id": str(len(hist) + 1), "author": {"accountId": "u-ba"}, "created": _iso(ts),
                             "items": [{"field": fld, "fieldtype": "jira", "fromString": ov, "toString": nv}]})
            if truncate_changelog:
                issue["changelog"] = {"histories": hist[-1:], "total": len(hist) + 5, "maxResults": 100}
            else:
                issue["changelog"] = {"histories": hist, "total": len(hist), "maxResults": 100}
        issues.append(issue)
    Path(path).write_text(json.dumps({"issues": issues, "names": names}, indent=1))


EXPORT_TIMES = {"day0": "2026-10-26 09:00", "day1": "2026-11-01 18:00", "day2": "2026-11-04 09:00"}


def write_demo(dest: str | Path) -> dict[str, Path]:
    d = Path(dest)
    d.mkdir(parents=True, exist_ok=True)
    out = {}
    for name, t in EXPORT_TIMES.items():
        p = d / f"demo_{name}.csv"
        render_csv(t, p)
        out[f"csv_{name}"] = p
    pj = d / "demo_history.json"
    render_json(EXPORT_TIMES["day2"], pj)
    out["json_history"] = pj
    return out
