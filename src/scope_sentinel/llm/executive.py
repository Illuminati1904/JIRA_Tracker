"""Executive summary + action tracking, with a number-grounding check.

The skill's rule is 'every number must come from analysis.json'. We enforce it: figures in the generated
text are checked against the facts block (plus derived percentages and key counts); unverifiable figures trigger
one repair call, and anything still unverified is surfaced in the report instead of silently shipped.
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict

from ..analysis.engine import _event_detail
from ..memory.store import Memory
from ..redact import Redactor
from ..workflow import Workflow
from .client import LLM
from .prompts import EXEC_SYSTEM
from .schemas import ExecSummary

_KEY = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")
_MONTH = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*"
_DATE_RES = [re.compile(r"\b\d{4}-\d{2}-\d{2}\b"), re.compile(rf"\b\d{{1,2}}\s+{_MONTH}\b", re.I),
             re.compile(rf"\b{_MONTH}\s+\d{{1,2}}\b", re.I), re.compile(r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b"),
             re.compile(r"\bR\d\b"), re.compile(r"\b(?:Release|Sprint|Pod|POD)[ -]?\d+\b", re.I)]


def _group(rows, fn, top=8):
    g: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        k = fn(r) or "(none)"
        g[k]["total"] += 1
        if r["severity"] != "None":
            g[k]["touched"] += 1
            g[k][r["severity"].lower()] += 1
        if r["movement"] == "new":
            g[k]["new"] += 1
    arr = [{"name": k, **dict(v)} for k, v in g.items() if v["touched"] or v["new"]]
    arr.sort(key=lambda x: (-(x.get("major", 0) * 100 + x.get("moderate", 0) * 10 + x.get("minor", 0)), x["name"]))
    return arr[:top]


def build_facts(analysis: dict, narratives: dict, mem: Memory, red: Redactor, wf: Workflow, prev_exec: dict | None,
                carried: list[dict]) -> dict:
    S, M, rows = analysis["summary"], analysis["meta"], analysis["stories"]
    stories = []
    for r in rows:
        if r["severity"] == "None" and r["movement"] == "same":
            continue
        n = narratives.get(r["key"]) or {}
        stories.append({"key": r["key"], "title": red.text(r["summary"])[:110], "status": r["status"],
                        "capability": red.text(r["capability"]), "severity": n.get("severity", r["severity"]),
                        "movement": r["movement"], "flags": r["flags"], "change_type": n.get("change_type", ""),
                        "headline": red.text(n.get("headline", ""))})
    sev_rank = {"Major": 0, "Moderate": 1, "Minor": 2, "None": 3}
    stories.sort(key=lambda s: (sev_rank[s["severity"]], s["key"]))
    gate = {"stories_at_or_past_signoff": S["n_past_gate"], "edited_after_signoff": S["n_signoff_breach"],
            "change_requested_after_signoff": S["n_signoff_pressure"], "regressions_from_signoff": S["n_regression"]}
    return {
        "window": {"from": M["baseline_at"], "to": M["current_at"], "evidence": "exact/observed history" if M["mode"] == "snapshot" else "inferred from comments and timestamps"},
        "coverage_by_story": M.get("coverage", {}),
        "counts": {"total_stories": S["total"], "stories_at_window_start": S["n_baseline"], "with_scope_movement": S["n_changed"],
                   "new_stories": S["n_new"], "removed_stories": S["n_removed"], "major": S["severity"].get("Major", 0),
                   "moderate": S["severity"].get("Moderate", 0), "minor": S["severity"].get("Minor", 0),
                   "bulk_edit_noise_ignored": S["n_bulk_noise"]},
        "sign_off_gate": gate,
        "by_capability": _group(rows, lambda r: red.text(r["capability"])),
        "by_epic": _group(rows, lambda r: red.text(r["epic"]), top=6),
        "stories_with_movement": stories[:90],
        "previous_report": None if not prev_exec else {"window_to": prev_exec.get("window_to"), "verdict": prev_exec.get("verdict")},
        "carried_over_actions": carried,
    }


def carried_over(mem: Memory, red: Redactor, wf: Workflow, until: str) -> list[dict]:
    out = []
    for a in mem.open_actions():
        run = mem.db.execute("SELECT taken_at FROM runs WHERE run_id=?", (a["run_id"],)).fetchone()
        since = run["taken_at"] if run else ""
        ev_lines = []
        for k in json.loads(a["keys"])[:6]:
            if mem.story_row(k) is None:
                ev_lines.append(f"{k}: no longer in memory")
                continue
            evs = [e for e in mem.events(k, after=since, until=until) if e["type"] not in ("FIRST_SEEN", "COMMENT_ADDED")]
            for e in evs[-4:]:
                ev_lines.append(f"{k} {e['ts'][:10]} {_event_detail(mem, e, wf)}")
            if mem.events(k, after=since, until=until, types=("COMMENT_ADDED",)):
                ev_lines.append(f"{k}: new comments since the action was raised")
        out.append({"action_id": a["id"], "who": a["who"], "what": red.text(a["what"]), "keys": json.loads(a["keys"]),
                    "raised_in_run": a["run_id"], "evidence_since": [red.text(x) for x in ev_lines[:14]]})
    return out


def _numbers_in(text: str) -> set[str]:
    t = _KEY.sub(" ", text or "")
    for rx in _DATE_RES:
        t = rx.sub(" ", t)
    return {str(int(float(x))) for x in re.findall(r"\d+(?:\.\d+)?", t)}


def _fact_numbers(o, acc: set[int]):
    if isinstance(o, bool):
        return
    if isinstance(o, int):
        acc.add(o)
    elif isinstance(o, dict):
        for k, v in o.items():
            if k != "action_id":
                _fact_numbers(v, acc)
    elif isinstance(o, list):
        for v in o:
            _fact_numbers(v, acc)


def verify_numbers(ex: dict, facts: dict) -> list[str]:
    """Return figures in the generated text that cannot be traced to the facts."""
    ints: set[int] = set()
    _fact_numbers({k: v for k, v in facts.items() if k not in ("stories_with_movement",)}, ints)
    ints |= {sum(1 for s in facts.get("stories_with_movement", []) if s["severity"] == sev) for sev in ("Major", "Moderate", "Minor")}
    allowed = {str(i) for i in ints}
    base = sorted(i for i in ints if i > 0)
    if len(base) <= 40:
        for a in base:
            for b in base:
                if a <= b:
                    allowed.add(str(round(100 * a / b)))
    parts = [ex.get("verdict", ""), ex.get("subverdict", ""), ex.get("headlede", "")]
    for h in ex.get("headlines", []):
        parts += [h.get("title", ""), h.get("body", "")]
        allowed.add(str(len(h.get("keys", []))))
    for a in ex.get("actions", []):
        parts.append(a.get("what", ""))
        allowed.add(str(len(a.get("keys", []))))
    bad = sorted(n for n in set().union(*[_numbers_in(p) for p in parts]) if n not in allowed)
    return bad


def clean(ex: ExecSummary, valid_keys: set[str], open_ids: set[int]) -> dict:
    d = ex.model_dump()
    d["headlines"] = [{**h, "keys": [k for k in h["keys"] if k in valid_keys]} for h in d["headlines"][:5]]
    d["actions"] = [{**a, "keys": [k for k in a["keys"] if k in valid_keys]} for a in d["actions"][:5]]
    d["action_updates"] = [u for u in d["action_updates"] if u["action_id"] in open_ids]
    return d


def run_exec(llm: LLM, facts: dict, valid_keys: set[str], effort: str):
    """-> (exec dict, [Result...], unverified figures)"""
    open_ids = {a["action_id"] for a in facts["carried_over_actions"]}
    user = "<facts>\n" + json.dumps(facts, ensure_ascii=False, indent=1) + "\n</facts>\nWrite the executive summary JSON."
    msgs = [{"role": "user", "content": user}]
    results = []
    res = llm.call_raw(system=EXEC_SYSTEM, messages=msgs, effort=effort, schema=ExecSummary, max_tokens=8000)
    results.append(res)
    ex = clean(llm.parse(res, ExecSummary), valid_keys, open_ids)
    bad = verify_numbers(ex, facts)
    if bad:
        msgs += [{"role": "assistant", "content": res.text},
                 {"role": "user", "content": f"These figures could not be traced to the facts: {', '.join(bad)}. "
                  "Rewrite the whole JSON using only figures present in the facts (or counts of the keys you list)."}]
        res2 = llm.call_raw(system=EXEC_SYSTEM, messages=msgs, effort=effort, schema=ExecSummary, max_tokens=8000)
        results.append(res2)
        ex = clean(llm.parse(res2, ExecSummary), valid_keys, open_ids)
        bad = verify_numbers(ex, facts)
    return ex, results, bad
