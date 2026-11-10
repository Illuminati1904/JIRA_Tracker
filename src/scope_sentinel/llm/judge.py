"""Per-story judgement: evidence packet -> Narrative, cached by evidence hash, floored by deterministic facts."""
from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor

from ..analysis.engine import PROTECTED_FLAGS
from ..memory.dossier import DossierStore
from ..memory.store import Memory
from ..redact import Redactor
from ..textproc import SEVERITY_ORDER, max_sev
from ..workflow import Workflow
from .client import LLM, BudgetExceeded, Refused, Truncated
from .prompts import JUDGE_SYSTEM, PROMPT_VERSION
from .schemas import Narrative

_NOISE_ONLY = {"BULK_EDIT_NOISE", "METADATA_CHANGE", "DUPLICATE_TITLE", "INCOMPLETE_TITLE", "NEW_STORY", "FORWARD"}
_LEDGER_FIELDS = ("severity", "change_type", "headline", "desc_change", "ac_change", "status_change", "pm_note", "confidence")


def _trim(s, n):
    s = s or ""
    return s if len(s) <= n else s[: n - 1] + "…"


def needs_judgment(r: dict) -> bool:
    ev = r["evidence"]
    diffs = bool(ev.get("desc_diff") or ev.get("ac_diff") or ev.get("summary_diff"))
    if r["movement"] == "new" and not ev["comments"]:
        return False
    if not diffs and not ev["comments"] and set(r["flags"]) <= _NOISE_ONLY:
        return False
    return r["severity"] != "None" or bool(ev["comments"]) or diffs or bool(set(r["flags"]) & PROTECTED_FLAGS)


def build_packet(r: dict, wf: Workflow, red: Redactor, window: tuple[str, str]) -> tuple[dict, dict]:
    """-> (facts_and_heuristics packet, memory block). Hash is computed on the evidence part only."""
    ev = r["evidence"]
    dd, ad, sd = ev.get("desc_diff"), ev.get("ac_diff"), ev.get("summary_diff")
    facts = {
        "key": r["key"], "title": r["summary"], "status": r["status"], "lane": r["lane"],
        "movement": r["movement"], "status_at_window_start": r.get("baseline_status"),
        "status_path": r.get("status_path", []), "capability": r["capability"], "epic": r["epic"],
        "jira_labels": r["labels"], "evidence_quality": r["confidence"],
        "edits_to_scope_text_in_window": ev.get("edit_count", 0),
        "description_diff": None if not dd else {
            "change_pct": dd["change_pct"], "added": [_trim(x, 300) for x in dd["added"][:6]],
            "removed": [_trim(x, 300) for x in dd["removed"][:6]],
            "modified": [{"before": _trim(m["before"], 200), "after": _trim(m["after"], 200)} for m in dd["modified"][:4]]},
        "ac_diff": None if not ad else {
            "added": {k: _trim(v, 300) for k, v in list(ad["added_text"].items())[:6]},
            "removed": {k: _trim(v, 300) for k, v in list(ad["removed_text"].items())[:6]},
            "edited": [{"ac": e["ac"], "change_pct": e["change_pct"],
                        "added": [_trim(x, 200) for x in e["diff"]["added"][:3]],
                        "removed": [_trim(x, 200) for x in e["diff"]["removed"][:3]]} for e in ad["edited"][:6]]},
        "title_diff": sd, "metadata_changes": ev.get("metadata", []),
        "deterministic_flags": r["flags"], "deterministic_reasons": r["severity_reasons"],
        "deterministic_severity_floor": r.get("det_severity", "None"),
        "current_description_excerpt": _trim(r.get("description_now", ""), 500),
    }
    heur = {"auto_severity": r["severity"], "comment_tag_counts": ev.get("tag_counts", {}),
            "comments": [{"ts": c["ts"], "author": c["author"], "tags": c["tags"], "text": _trim(c["text"], 1500)}
                         for c in ev["comments"][-12:]]}
    packet = {"facts": facts, "heuristics": heur}
    packet = red.obj(packet)
    # comment authors become stable pseudonyms
    for c in packet["heuristics"]["comments"]:
        c["author"] = red.author(c["author"]) if c["author"] else ""
    return packet, {"window": {"from": window[0], "to": window[1]}}


def evidence_hash(packet: dict, model: str) -> str:
    return hashlib.sha1((PROMPT_VERSION + model + json.dumps(packet, sort_keys=True, default=str)).encode()).hexdigest()


def validate(n: Narrative, r: dict) -> dict:
    """Clamp the model's output to what the facts allow. The model interprets; it does not override facts."""
    d = n.model_dump()
    floor = r.get("det_severity", "None")
    d["severity"] = max_sev(d["severity"], floor) if floor != "None" else d["severity"]
    d["flags_remove"] = [f for f in d["flags_remove"] if f not in PROTECTED_FLAGS]
    d["flags_add"] = [f for f in d["flags_add"] if f not in PROTECTED_FLAGS]
    d["headline"] = _trim(d["headline"].strip(), 160)
    d["open_questions"] = [_trim(q, 200) for q in d["open_questions"][:3]]
    d["dossier_note"] = _trim(d["dossier_note"].strip(), 900)
    d["_source"] = "llm"
    return d


def deterministic_narrative(r: dict) -> dict:
    """Used with --no-llm, on refusal/budget stop, or API failure: factual, never speculative."""
    f = set(r["flags"])
    ev = r["evidence"]
    ctype, parts = "Change signal", []
    order = [("REMOVED_FROM_SCOPE", "Removed from scope"), ("REGRESSION_FROM_SIGNOFF", "Moved back from sign-off"),
             ("POST_SIGNOFF_EDIT", "Edited after sign-off"), ("DESCOPED", "Descoped or deferred"),
             ("CHANGED_THEN_SIGNED_OFF", "Changed, then signed off"), ("SIGNOFF_PRESSURE", "Change requested after sign-off"),
             ("NEW_STORY", "New story"), ("REWORK_LOOP", "Rework loop"), ("CONDITIONAL_APPROVAL", "Conditional approval"),
             ("TRACEABILITY_GAP", "Traceability gap"), ("NEWLY_SIGNED_OFF", "Newly signed off")]
    for flag, label in order:
        if flag in f:
            ctype = label
            break
    else:
        if ev.get("desc_diff") or ev.get("ac_diff") or ev.get("summary_diff"):
            ctype = "Scope text edited"
        elif r["movement"] == "moved":
            ctype = "Status only"
    dd, ad = ev.get("desc_diff"), ev.get("ac_diff")
    desc = f"Description changed ({dd['change_pct']}% of text)." if dd else ""
    ac = ""
    if ad:
        bits = [f"{k} added" for k in ad["added"]] + [f"{k} removed" for k in ad["removed"]] + \
               [f"{e['ac']} edited ({e['change_pct']}%)" for e in ad["edited"]]
        ac = ", ".join(bits) + "."
    status = ""
    if r.get("baseline_status") and r["baseline_status"] != r["status"]:
        status = f"{r['baseline_status']} → {r['status']}."
    headline = r["severity_reasons"][0] if r["severity_reasons"] else ctype
    if r["movement"] == "new":
        headline = f"Created {(r.get('created') or '')[:10]}: scope addition."
    return {"severity": r["severity"], "change_type": ctype, "headline": _trim(headline, 160), "desc_change": desc,
            "ac_change": ac, "status_change": status, "pm_note": "", "flags_add": [], "flags_remove": [],
            "confidence": {"Exact": "High", "Observed": "Medium", "Inferred": "Low"}.get(r["confidence"], "Medium"),
            "dossier_note": "", "open_questions": [], "_source": "fallback"}


def judge_all(rows: list[dict], *, llm: LLM | None, mem: Memory, dossiers: DossierStore | None, red: Redactor,
              wf: Workflow, window: tuple[str, str], run_id: int, effort: str, max_workers: int = 4,
              budget_usd: float | None = None, progress=lambda m: None) -> tuple[dict, dict]:
    """-> (narratives {key: dict}, stats). Cached by evidence hash; falls back per story on any LLM failure."""
    narratives: dict[str, dict] = {}
    stats = {"judged": 0, "cached": 0, "fallback": 0, "skipped": 0, "refused": 0, "budget_stopped": False, "errors": []}
    todo: list[tuple[dict, dict, str]] = []
    for r in rows:
        if not needs_judgment(r):
            stats["skipped"] += 1
            continue
        packet, memblock = build_packet(r, wf, red, window)
        h = evidence_hash(packet, llm.model if llm else "none")
        hit = mem.judgment_by_hash(r["key"], h) if llm else None
        if hit:
            narratives[r["key"]] = json.loads(hit["narrative"])
            stats["cached"] += 1
            continue
        if llm is None:
            narratives[r["key"]] = deterministic_narrative(r)
            stats["fallback"] += 1
            continue
        packet["memory"] = memblock
        if dossiers is not None:
            d = dossiers.read(r["key"])
            j = mem.last_judgment(r["key"])
            packet["memory"].update(
                prior_understanding=red.text(d["note"]), prior_open_questions=[red.text(q) for q in d["questions"]],
                previous_judgment=None if not j else {
                    "judged_to": j["window_to"], **{k: red.text(json.loads(j["narrative"]).get(k, "")) for k in ("severity", "headline")}})
        todo.append((r, packet, h))

    def work(item):
        r, packet, h = item
        user = ("<evidence>\n" + json.dumps(packet, ensure_ascii=False, indent=1) +
                "\n</evidence>\nProduce the judgement JSON for this story.")
        try:
            res = llm.call_raw(system=JUDGE_SYSTEM, messages=[{"role": "user", "content": user}], effort=effort,
                               schema=Narrative, max_tokens=4000)
            return r, h, res, llm.parse(res, Narrative), None
        except Exception as e:     # one story failing must never sink the report
            return r, h, None, None, e

    for i in range(0, len(todo), max(1, max_workers)):
        if budget_usd is not None and mem.llm_spend(run_id) >= budget_usd:
            stats["budget_stopped"] = True
            for r, _, _ in todo[i:]:
                narratives[r["key"]] = deterministic_narrative(r)
                stats["fallback"] += 1
            break
        chunk = todo[i: i + max_workers]
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            outs = list(ex.map(work, chunk))
        if i == 0 and outs and all(o[4] is not None for o in outs) and \
                any(getattr(o[4], "status_code", None) in (401, 403, 404) for o in outs):
            bad = next(o[4] for o in outs if getattr(o[4], "status_code", None) in (401, 403, 404))
            raise RuntimeError(f"Claude API rejected the request ({getattr(bad, 'status_code', '?')}): {bad}. "
                               "Check credentials and the model id, or re-run with --no-llm.")
        for r, h, res, parsed, err in outs:
            if err is not None:
                if isinstance(err, Refused):
                    stats["refused"] += 1
                stats["errors"].append(f"{r['key']}: {type(err).__name__}: {err}")
                narratives[r["key"]] = deterministic_narrative(r)
                stats["fallback"] += 1
                continue
            mem.log_llm(run_id, "judge", r["key"], llm.model, res.usage, res.usd)
            n = validate(parsed, r)
            narratives[r["key"]] = n
            mem.save_judgment(r["key"], h, run_id, window[0], window[1], llm.model, n)
            stats["judged"] += 1
        progress(f"judged {min(i + max_workers, len(todo))}/{len(todo)}")
    return narratives, stats
