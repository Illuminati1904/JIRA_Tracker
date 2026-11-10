"""Windowed scope-change analysis over the memory.

One question, asked of the event log: "between t0 and t1, what changed in each story?"
  * story known at t0 (memory has coverage)  -> EXACT/OBSERVED path: diff state_at(t0) vs state_at(t1)
  * story created inside the window          -> NEW path
  * story older than memory (first CSV only) -> INFERRED path (the skill's 'audit mode' heuristics)

Output is analysis.json-compatible with the skill's report template, plus `history` and `confidence`.
Hard facts (diffs, transitions, sign-off breaches) are deterministic and are *never* overridden by the LLM.
"""
from __future__ import annotations

import difflib
import json
import re
from collections import Counter
from datetime import datetime, timedelta

from ..memory.replay import signoff_periods, state_at, status_path
from ..memory.store import Memory
from ..model import META_FIELDS, SCOPE_FIELDS
from ..textproc import (SEVERITY_ORDER, ac_changed, diff_acs, diff_text, max_sev, normalise,
                        rate_text_change)
from ..timeutil import FMT
from ..workflow import Workflow
from .signals import CHANGE_TAGS, SCOPE_SIGNAL_TAGS, score_comments

PROTECTED_FLAGS = {  # deterministic facts: the judge may not remove these
    "POST_SIGNOFF_EDIT", "REGRESSION_FROM_SIGNOFF", "NEW_STORY", "REMOVED_FROM_SCOPE", "NEWLY_SIGNED_OFF", "FORWARD",
}
_META_LABEL = {"points": "Story points", "sprint": "Sprint", "fix_version": "Fix version", "epic_key": "Parent epic",
               "priority": "Priority", "assignee": "Assignee", "capability": "Capability", "labels": "Labels"}
_DATE_IN_TEXT = re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b")


def bulk_minutes(records: list[dict], threshold: int = 5) -> set[str]:
    c = Counter(r["updated"] for r in records if r.get("updated"))
    bulk = {m for m, n in c.items() if n >= threshold}
    for m in list(c):
        t = datetime.strptime(m, FMT)
        near = sum(c.get((t + timedelta(minutes=d)).strftime(FMT), 0) for d in (-1, 0, 1))
        if near >= threshold:
            bulk.add(m)
    return bulk


def _dated_annotations(text: str, t0: str, t1: str) -> list[dict]:
    d0, d1 = datetime.strptime(t0, FMT), datetime.strptime(t1, FMT)
    hits = []
    nt = normalise(text)
    for m in _DATE_IN_TEXT.finditer(nt):
        mo, dy, yr = int(m.group(1)), int(m.group(2)), m.group(3)
        yr = (int(yr) + (2000 if int(yr) < 100 else 0)) if yr else d0.year
        try:
            d = datetime(yr, mo, dy)
        except ValueError:
            continue
        if d0.date() <= d.date() <= d1.date() + timedelta(days=1):
            hits.append({"date": d.strftime("%Y-%m-%d"), "context": nt[max(0, m.start() - 60): m.end() + 80].replace("\n", " ")})
    return hits[:3]


def _event_detail(mem: Memory, ev, wf: Workflow) -> str:
    f = ev["field"]
    if ev["type"] == "STATUS_CHANGED":
        return f"{ev['old'] or '?'} → {ev['new']}"
    if ev["type"] == "COMMENT_ADDED":
        return "Comment added"
    if ev["type"] in ("CREATED", "FIRST_SEEN", "REMOVED", "REAPPEARED"):
        return {"CREATED": "Story created", "FIRST_SEEN": "First seen in memory", "REMOVED": "Removed from export",
                "REAPPEARED": "Reappeared in export"}[ev["type"]]
    if f in SCOPE_FIELDS:
        old, new = mem.decode(f, ev["old"]), mem.decode(f, ev["new"])
        label = {"summary": "Title", "description": "Description", "ac": "Acceptance criteria"}[f]
        if f == "ac":
            d = diff_acs(old, new)
            bits = [f"{k} added" for k in d["added"]] + [f"{k} removed" for k in d["removed"]] + \
                   [f"{e['ac']} edited ({e['change_pct']}%)" for e in d["edited"]]
            return f"{label}: " + (", ".join(bits) if bits else "formatting only")
        d = diff_text(old, new)
        return f"{label} {'edited (%s%%)' % d['change_pct'] if d['changed'] else 'formatting only'}"
    return f"{_META_LABEL.get(f, f)}: {ev['old'] or '—'} → {ev['new'] or '—'}"


def analyze(mem: Memory, wf: Workflow, t0: str, t1: str, label: str, baseline_label: str | None = None,
            jira_base: str = "") -> dict:
    keys = mem.keys()
    records = {k: json.loads(mem.story_row(k)["record"]) for k in keys}
    bulk = bulk_minutes(list(records.values()))
    rows: list[dict] = []
    for key in keys:
        row = _analyze_story(mem, wf, key, records[key], t0, t1, bulk)
        if row:
            rows.append(row)

    # hygiene: duplicate / unfinished titles
    norm_titles = Counter(re.sub(r"\W+", " ", r["summary"]).strip().lower() for r in rows)
    for r in rows:
        t = re.sub(r"\W+", " ", r["summary"]).strip().lower()
        if t and norm_titles[t] > 1:
            r["flags"].append("DUPLICATE_TITLE"); r["severity_reasons"].append("Another story has an identical title")
        if re.search(r"[-–:]\s*$", r["summary"].strip()):
            r["flags"].append("INCOMPLETE_TITLE"); r["severity_reasons"].append("Title ends mid-sentence")

    rows.sort(key=lambda r: (-SEVERITY_ORDER[r["severity"]], -len(r["flags"]), r["key"]))
    cov = Counter(r["confidence"] for r in rows if r["movement"] not in ("new", "removed"))
    covered = cov["Exact"] + cov["Observed"]
    mode = "snapshot" if covered >= cov["Inferred"] else "audit"
    meta = {"mode": mode, "baseline": baseline_label or t0[:10], "baseline_at": t0, "current": label, "current_at": t1,
            "jira_base": jira_base, "coverage": dict(cov)}
    return {"meta": meta, "summary": rollup(rows, wf), "stories": rows}


def rollup(rows: list[dict], wf: Workflow) -> dict:
    sev = Counter(r["severity"] for r in rows)
    flags = Counter(f for r in rows for f in r["flags"])
    trans = Counter((r["baseline_status"], r["status"]) for r in rows
                    if r["baseline_status"] and r["baseline_status"] != r["status"])
    return {
        "total": len(rows), "severity": dict(sev), "flags": dict(flags),
        "lanes": dict(Counter(r["lane"] for r in rows)), "status_counts": dict(Counter(r["status"] for r in rows)),
        "workflow_order": wf.order,
        "transitions": [{"from": a, "to": b, "n": n} for (a, b), n in trans.most_common()],
        "n_changed": sum(1 for r in rows if r["severity"] != "None" or r["movement"] != "same"),
        "n_new": sum(1 for r in rows if r["movement"] == "new"),
        "n_removed": sum(1 for r in rows if r["movement"] == "removed"),
        "n_signoff_breach": sum(1 for r in rows if {"POST_SIGNOFF_EDIT", "POST_SIGNOFF_CHANGE"} & set(r["flags"])),
        "n_signoff_pressure": sum(1 for r in rows if "SIGNOFF_PRESSURE" in r["flags"]),
        "n_regression": sum(1 for r in rows if {"REGRESSION_FROM_SIGNOFF", "POSSIBLE_REGRESSION"} & set(r["flags"])),
        "n_bulk_noise": sum(1 for r in rows if "BULK_EDIT_NOISE" in r["flags"]),
        "n_past_gate": sum(1 for r in rows if r["signed_off_now"]),
        "n_baseline": sum(1 for r in rows if r["movement"] != "new"),
    }


# --------------------------------------------------------------------------------------------------
def _window_comments(mem: Memory, key: str, t0: str, t1: str) -> list[dict]:
    out = []
    for ev in mem.events(key, after=t0, until=t1, types=("COMMENT_ADDED",)):
        out.append({"ts": ev["ts"], "author": ev["actor"], "text": mem.decode("comment", ev["new"])})
    return out


def _base_row(key, rec, wf: Workflow, st1, movement, baseline_status, confidence) -> dict:
    status = st1["status"] if st1 else rec["status"]
    stage = wf.stage_of(status)[0]
    return {
        "key": key, "summary": rec["summary"], "epic": rec["epic"], "epic_key": rec["epic_key"],
        "capability": rec["capability"], "labels": rec["labels"], "status": status, "stage": stage,
        "lane": wf.lane_of(status), "assignee": rec["assignee"], "priority": rec["priority"], "sprint": rec["sprint"],
        "points": rec["points"], "fix_version": rec["fix_version"], "created": rec["created"], "updated": rec["updated"],
        "status_cat": rec["status_cat"], "status_cat_changed": rec["status_cat_changed"],
        "signed_off_now": wf.is_signed_off(status), "movement": movement, "baseline_status": baseline_status,
        "flags": [], "severity": "None", "det_severity": "None", "severity_reasons": [], "confidence": confidence,
        "evidence": {"comments": [], "comment_weight": 0, "tag_counts": {}, "dated_annotations": [], "bulk_edit": False,
                     "updated_after": False, "desc_diff": None, "ac_diff": None, "summary_diff": None, "metadata": []},
        "history": [], "narrative": None,
        "description_now": rec["description"], "ac_now": rec["ac"],
    }


def _analyze_story(mem: Memory, wf: Workflow, key: str, rec: dict, t0: str, t1: str, bulk: set[str]) -> dict | None:
    meta = mem.story_row(key)
    st1 = state_at(mem, key, t1)
    if st1 is None or not st1.exists:
        return None
    # removal handling
    removal = [e for e in mem.events(key, types=("REMOVED",)) if e["ts"] <= t1]
    if st1.removed:
        if removal and removal[-1]["ts"] <= t0:
            return None   # gone before the window
        st0 = state_at(mem, key, t0)
        r = _base_row(key, rec, wf, st1, "removed", st0["status"] if st0 and st0.exists else None, "Observed")
        r.update(status="(removed from export)", stage=None, lane="side", signed_off_now=False,
                 flags=["REMOVED_FROM_SCOPE"], severity="Major", det_severity="Major",
                 severity_reasons=["Story was in the export before and is absent now (deleted, moved project or filtered out)"])
        return r

    created = meta["created"]
    first_seen = mem.events(key, types=("FIRST_SEEN",))
    first_ts = first_seen[0]["ts"] if first_seen else t1
    is_new = (created or first_ts) > t0
    st0 = state_at(mem, key, t0)
    comments_raw = _window_comments(mem, key, t0, t1)
    hits, wscore = score_comments(comments_raw)
    tags = Counter(t for c in hits for t in c["tags"])
    change_tags = sum(tags[t] for t in CHANGE_TAGS)
    is_bulk = bool(rec.get("updated") and rec["updated"] in bulk)

    if is_new:
        r = _base_row(key, rec, wf, st1, "new", None, "Exact" if created else "Observed")
        r["flags"].append("NEW_STORY")
        r["severity_reasons"].append("Created after the window start - scope addition")
        _apply_comment_signals(r, tags, change_tags, wscore, hits, wf)
        r["evidence"].update(comments=hits, comment_weight=wscore, tag_counts=dict(tags), updated_after=True)
        r["history"] = _history(mem, key, t0, t1, wf)
        return r

    covered = bool(st0 and st0.known)
    if not covered:
        return _inferred_row(mem, wf, key, rec, st1, t0, t1, hits, wscore, tags, change_tags, is_bulk)
    return _exact_row(mem, wf, key, rec, st0, st1, t0, t1, hits, wscore, tags, change_tags, is_bulk, meta)


def _apply_comment_signals(r, tags, change_tags, wscore, hits, wf: Workflow):
    if tags["conditional_approval"]:
        r["flags"].append("CONDITIONAL_APPROVAL")
    if tags["traceability_gap"]:
        r["flags"].append("TRACEABILITY_GAP")
    if tags["deferral"]:
        r["flags"].append("DEFERRAL_SIGNAL")
    sev = r["severity"]
    if wscore >= 1 or tags["traceability_gap"]:
        sev = max_sev(sev, "Minor")
    if change_tags >= 1 and wscore >= 3:
        sev = max_sev(sev, "Moderate")
    if tags["rework"] or tags["scope_remove"] or tags["split_merge"] or wscore >= 8:
        sev = max_sev(sev, "Major")
    if r["movement"] == "new" and sev != "None":
        r["severity_reasons"].append("Comments on a new story signal trimmed or disputed scope")
    r["severity"] = sev


def _history(mem: Memory, key: str, t0: str, t1: str, wf: Workflow) -> list[dict]:
    out = []
    for ev in mem.events(key, after=t0, until=t1):
        if ev["type"] == "FIRST_SEEN":
            continue
        p = json.loads(ev["payload"]) if ev["payload"] else {}
        if p.get("format_only"):
            continue
        out.append({"ts": ev["ts"], "type": ev["type"], "field": ev["field"], "detail": _event_detail(mem, ev, wf),
                    "confidence": ev["confidence"], "source": ev["source"]})
    return out[-60:]


def _exact_row(mem, wf, key, rec, st0, st1, t0, t1, hits, wscore, tags, change_tags, is_bulk, meta) -> dict:
    flags: list[str] = []
    reasons: list[str] = []
    evs = mem.events(key, after=t0, until=t1, types=("FIELD_CHANGED", "STATUS_CHANGED"))
    # Exact only when a changelog covers the story; snapshot-only stories are "Observed" (bounded by the run times)
    observed = meta["coverage"] != "changelog" or any(e["confidence"] != "exact" for e in evs)
    r = _base_row(key, rec, wf, st1, "same", st0["status"], "Observed" if observed else "Exact")

    # --- net text diffs between the two points in time (formatting noise already normalised away)
    desc_d = diff_text(st0["description"], st1["description"])
    ac_d = diff_acs(st0["ac"], st1["ac"])
    sc = normalise(st0["summary"]) != normalise(st1["summary"])
    sp = round((1 - difflib.SequenceMatcher(None, st0["summary"], st1["summary"]).ratio()) * 100, 1) if sc else 0
    sum_d = {"before": st0["summary"], "after": st1["summary"], "change_pct": sp} if sc else None
    sev, sev_r = rate_text_change(desc_d, ac_d, sc, sp)
    reasons += sev_r
    desc_d = desc_d if desc_d["changed"] else None
    ac_d = ac_d if ac_changed(ac_d) else None
    scope_edits = [e for e in evs if e["field"] in SCOPE_FIELDS and not (json.loads(e["payload"] or "{}").get("format_only"))]
    if len(scope_edits) >= 3:
        reasons.append(f"Scope text edited {len(scope_edits)} times in the window")

    # --- status movement (exact path when a changelog exists)
    path = status_path(mem, key, t0, t1, wf)
    status_changed = st0["status"] != st1["status"]
    if path:
        r["movement"] = "moved" if (status_changed or any(p["kind"] in ("regression", "rework") for p in path)) else "same"
    for p in path:
        if p["kind"] == "forward":
            if "FORWARD" not in flags:
                flags.append("FORWARD")
            reasons.append(f"{p['from']} → {p['to']}")
        elif p["kind"] == "regression":
            flags.append("REGRESSION_FROM_SIGNOFF")
            reasons.append(f"Moved BACK from {p['from']} to {p['to']} after sign-off")
            sev = max_sev(sev, "Major")
        elif p["kind"] == "rework":
            flags.append("REWORK_LOOP")
            reasons.append(f"Backward move {p['from']} → {p['to']}")
            sev = max_sev(sev, "Minor")
        if (p["to"] or "").lower() in ("deferred", "cancelled"):
            flags.append("DESCOPED"); reasons.append(f"Now {p['to']}")
            sev = max_sev(sev, "Major")
        if (p["to"] or "").lower() == "blocked":
            flags.append("BLOCKED")
    r["status_path"] = path

    # --- sign-off gate, derived from events
    periods = signoff_periods(mem, key, wf)
    breach_period = None
    for e in scope_edits:
        for p in periods:
            if p.start < e["ts"] and (p.end is None or e["ts"] <= p.end):
                breach_period = p
    if breach_period:
        flags.append("POST_SIGNOFF_EDIT")
        reasons.append("Scope text edited while the story was at or past ToDo (business sign-off)")
        # drift against the text frozen at sign-off decides how serious the net change is
        fz = breach_period.frozen
        ld = diff_text(fz.get("description", ""), st1["description"])
        la = diff_acs(fz.get("ac", ""), st1["ac"])
        net = ld["changed"] or ac_changed(la) or normalise(fz.get("summary", "")) != normalise(st1["summary"])
        if net:
            sev = max_sev(sev, "Major" if (ld["change_pct"] >= 15 or la["added"] or la["removed"]) else "Moderate")
            if not desc_d and ld["changed"]:
                desc_d = ld
            if not ac_d and ac_changed(la):
                ac_d = la
            reasons.append(f"Net drift from the version signed off on {breach_period.start[:10]}"
                           f"{'' if breach_period.exact else ' (sign-off time estimated from snapshots)'}")
        else:
            sev = max_sev(sev, "Minor")
            reasons.append("Edited after sign-off but the text ends up identical to the signed-off version (reverted)")
    # edit made at or before sign-off inside the window -> not a breach, but confirm frozen text
    for p in periods:
        if t0 < p.start <= t1 and any(e["ts"] <= p.start for e in scope_edits):
            flags.append("CHANGED_THEN_SIGNED_OFF")
            note = "Scope changed, then the story reached ToDo"
            if r["confidence"] != "Exact":
                note += " (order of edit and sign-off cannot be established from snapshots)"
            reasons.append(note)
            break
    for p in periods:
        if t0 < p.start <= t1 and p.from_status and not wf.is_signed_off(p.from_status):
            flags.append("NEWLY_SIGNED_OFF"); reasons.append(f"Reached {p.to_status}: business sign-off")
            break

    # --- metadata drift
    metas = []
    for f in META_FIELDS:
        a, b = st0[f], st1[f]
        if f == "epic":
            continue
        if (sorted(a) != sorted(b)) if f == "labels" else ((a or "") != (b or "")):
            metas.append({"field": _META_LABEL.get(f, f),
                          "before": ", ".join(a) if f == "labels" else a, "after": ", ".join(b) if f == "labels" else b})
    if metas:
        flags.append("METADATA_CHANGE")

    # --- comment evidence (heuristic). `det_severity` is the floor the judge cannot lower: facts only.
    det_sev = sev
    _apply_comment_signals(r, tags, change_tags, wscore, hits, wf)
    sev = max_sev(sev, r["severity"])
    # pressure = change requests made AFTER the story entered the sign-off zone (the log tells us when)
    cur_period = next((p for p in reversed(periods) if p.end is None), None)
    post_change = sum(1 for c in hits if cur_period and c["ts"] > cur_period.start and any(t in CHANGE_TAGS for t in c["tags"]))
    if post_change and wf.is_signed_off(st1["status"]) and "POST_SIGNOFF_EDIT" not in flags:
        flags.append("SIGNOFF_PRESSURE")
        reasons.append("Comments request changes after the story reached sign-off; no edit recorded yet")
    flags += [f for f in r["flags"] if f not in flags]
    if (tags["rework"] or tags["scope_remove"] or tags["split_merge"]) and sev in ("None", "Minor"):
        sev = "Moderate"
    if is_bulk and not hits and not scope_edits and not path:
        flags.append("BULK_EDIT_NOISE")

    r.update(flags=list(dict.fromkeys(flags)), severity=sev, det_severity=det_sev,
             severity_reasons=list(dict.fromkeys(reasons)))
    r["evidence"].update(comments=hits, comment_weight=wscore, tag_counts=dict(tags), bulk_edit=is_bulk, updated_after=True,
                         desc_diff=desc_d, ac_diff=ac_d, summary_diff=sum_d, metadata=metas,
                         edit_count=len(scope_edits))
    r["history"] = _history(mem, key, t0, t1, wf)
    return r


def _inferred_row(mem, wf, key, rec, st1, t0, t1, hits, wscore, tags, change_tags, is_bulk) -> dict:
    """Story older than memory coverage: the skill's audit heuristics (timestamps + comments)."""
    r = _base_row(key, rec, wf, st1, "same", None, "Inferred")
    cat_after = bool(rec.get("status_cat_changed") and rec["status_cat_changed"] >= t0)
    updated_after = bool(rec.get("updated") and rec["updated"] >= t0)
    ann = _dated_annotations((rec["description"] or "") + "\n" + (rec["ac"] or ""), t0, t1)
    lane = wf.lane_of(st1["status"])
    artefacts = bool(rec.get("tester") or rec.get("code_reviewer"))
    delivered_before = artefacts and lane in ("requirements", "side") and cat_after
    flags: list[str] = []
    reasons: list[str] = []
    if cat_after:
        r["movement"] = "moved"
        flags.append("STATUS_MOVED"); reasons.append(f"Status category changed {rec['status_cat_changed']}")
    if tags["deferral"]:
        flags.append("DEFERRAL_SIGNAL"); reasons.append("Comment signals deferral / re-release")
    if st1["status"].lower() in ("deferred", "cancelled"):
        flags.append("DESCOPED"); reasons.append(f"Status is {st1['status']}")
    if tags["conditional_approval"]:
        flags.append("CONDITIONAL_APPROVAL"); reasons.append("Approval given with conditions / contingent on changes")
    if tags["traceability_gap"]:
        flags.append("TRACEABILITY_GAP"); reasons.append("Reviewer flagged requirement-to-story gaps")
    if ann:
        flags.append("DATED_EDIT_IN_BODY"); reasons.append("Description/AC contains a dated note inside the window")
    signed = wf.is_signed_off(st1["status"])
    if signed and (change_tags or ann):
        flags.append("POST_SIGNOFF_CHANGE"); reasons.append("Signed-off/delivery story with change signals inside the window")
    if delivered_before:
        flags.append("POSSIBLE_REGRESSION")
        reasons.append(f"In '{st1['status']}' but has tester/code-review artefacts: likely moved backwards from delivery")
    sev = "None"
    if wscore >= 1 or ann or tags["traceability_gap"]:
        sev = "Minor"
    if change_tags >= 1 and wscore >= 3:
        sev = max_sev(sev, "Moderate")
    if tags["rework"] or tags["scope_remove"] or tags["split_merge"] or wscore >= 8:
        sev = max_sev(sev, "Major")
    if "POST_SIGNOFF_CHANGE" in flags and sev == "Minor":
        sev = "Moderate"
    if "POSSIBLE_REGRESSION" in flags and cat_after:
        sev = max_sev(sev, "Moderate")
    if is_bulk and not hits:
        flags.append("BULK_EDIT_NOISE")
    r.update(flags=flags, severity=sev, severity_reasons=reasons)
    r["evidence"].update(comments=hits, comment_weight=wscore, tag_counts=dict(tags), dated_annotations=ann,
                         bulk_edit=is_bulk, updated_after=updated_after)
    r["history"] = _history(mem, key, t0, t1, wf)
    return r
