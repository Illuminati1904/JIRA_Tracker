"""End-to-end orchestration: ingest -> analyse -> judge -> dossiers -> executive summary -> project memory -> report."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from .analysis.engine import analyze
from .config import Config
from .ingest.detect import load_export
from .llm import executive
from .llm.client import LLM
from .llm.judge import judge_all
from .llm.project_memory import update_project_memory
from .memory.backends import LocalBackend, ManagedStoreBackend, MemoryBackend, MirroredBackend
from .memory.dossier import DossierStore
from .memory.store import IngestStats, Memory
from .redact import Redactor
from .report import build as rb
from .timeutil import FMT


@dataclass
class Workspace:
    cfg: Config
    mem: Memory
    backend: MemoryBackend
    dossiers: DossierStore
    red: Redactor
    sdk: object | None = None

    def close(self):
        self.mem.close()


def make_sdk():
    try:
        import anthropic
    except ImportError as e:   # pragma: no cover
        raise RuntimeError("The 'anthropic' package is not installed. `pip install anthropic`, or run with --no-llm.") from e
    return anthropic.Anthropic()


def open_workspace(cfg: Config, use_llm: bool = True, sdk=None) -> Workspace:
    mem = Memory(cfg.db_path)
    red = Redactor(mem, cfg.redact_terms)
    if use_llm or cfg.memory_backend == "managed":
        sdk = sdk or make_sdk()
    if cfg.memory_backend == "managed":
        store_id = cfg.memory_store_id or mem.kv_get("memory_store_id")
        if not store_id:
            raise RuntimeError("memory_backend is 'managed' but no store exists yet. Run `sentinel memory init`.")
        backend: MemoryBackend = MirroredBackend(ManagedStoreBackend(sdk, store_id), LocalBackend(cfg.mirror_dir))
    else:
        backend = LocalBackend(cfg.mirror_dir)
    return Workspace(cfg, mem, backend, DossierStore(backend, mem, cfg.workflow, red), red, sdk)


def ingest_file(ws: Workspace, path: str, label: str | None = None, taken_at: str | None = None, full_export: bool = True,
                kind: str | None = None, force_order: bool = False) -> tuple[IngestStats, list[str]]:
    exp = load_export(path, ws.cfg, kind)
    taken = taken_at or exp.max_updated or __import__("datetime").datetime.now().strftime(FMT)
    lab = label or taken[:10]
    stats = ws.mem.ingest(exp, lab, taken, full_export=full_export, force_order=force_order)
    if exp.jira_base:
        ws.mem.kv_set("jira_base", exp.jira_base)
    return stats, exp.warnings


def _window(mem: Memory, since, since_run, until):
    latest = mem.last_run()
    if latest is None:
        raise RuntimeError("Nothing ingested yet. Run `sentinel ingest <file>` first.")
    t1 = until or latest["taken_at"]
    if since:
        t0, base_label = f"{since} 00:00" if len(since) == 10 else since, since
        if len(since) == 10:
            # a date-only baseline means "the dump taken that day": snap to it so coverage starts exactly there
            same_day = [r for r in mem.runs() if r["taken_at"][:10] == since]
            if same_day:
                t0 = same_day[0]["taken_at"]
    elif since_run:
        r = mem.run_by_label(since_run)
        if not r:
            raise RuntimeError(f"No run labelled {since_run!r}. Known: {', '.join(x['label'] for x in mem.runs())}")
        t0, base_label = r["taken_at"], r["label"]
    else:
        prev = mem.last_run(before_run_id=latest["run_id"])
        if prev is None:
            raise RuntimeError("Only one dump is in memory, so there is no previous run to compare with. "
                               "Pass --since YYYY-MM-DD (the agreed baseline date): history before this dump is inferred.")
        t0, base_label = prev["taken_at"], prev["label"]
    return latest, t0, t1, base_label


def generate_report(ws: Workspace, *, since: str | None = None, since_run: str | None = None, until: str | None = None,
                    label: str | None = None, use_llm: bool = True, title: str = "Scope Change Report",
                    refresh_exec: bool = False, ingest_stats: IngestStats | None = None, progress=lambda m: None) -> dict:
    cfg, mem, wf = ws.cfg, ws.mem, ws.cfg.workflow
    latest, t0, t1, base_label = _window(mem, since, since_run, until)
    run_id = latest["run_id"]
    label = label or latest["label"]
    analysis = analyze(mem, wf, t0, t1, label, baseline_label=base_label, jira_base=mem.kv_get("jira_base", "") or cfg.jira_base)
    rows = analysis["stories"]
    progress(f"analysed {len(rows)} stories ({t0} → {t1})")

    llm = LLM(ws.sdk, cfg.model, cfg.use_fallbacks) if (use_llm and ws.sdk is not None) else None
    narratives, jstats = judge_all(
        rows, llm=llm, mem=mem, dossiers=ws.dossiers if llm else None, red=ws.red, wf=wf, window=(t0, t1), run_id=run_id,
        effort=cfg.judge_effort, max_workers=cfg.max_workers, budget_usd=cfg.budget_usd, progress=progress)

    # --- dossiers: facts always, understanding when Claude produced one
    judged_keys = {k for k, n in narratives.items() if n.get("_source") == "llm"}
    for r in rows:
        k = r["key"]
        if r["movement"] == "removed":
            continue
        n = narratives.get(k) or {}
        has_events = bool(r["history"])
        exists = ws.backend.exists(f"/stories/{k}.md")
        if k in judged_keys and n.get("dossier_note"):
            ws.dossiers.write(k, n["dossier_note"], n.get("open_questions", []))
        elif has_events or not exists:
            ws.dossiers.write(k)
    ws.dossiers.rebuild_index()

    # --- executive summary + action tracking
    prev = mem.last_exec(before_run_id=run_id)
    prev_exec = None if not prev else {**json.loads(prev["body"]), "window_to": prev["window_to"]}
    carried = executive.carried_over(mem, ws.red, wf, t1)
    ex, unverified, resolved = None, [], []
    stored = mem.last_exec()
    if llm and stored and stored["run_id"] == run_id and (stored["window_from"], stored["window_to"]) == (t0, t1) and not refresh_exec:
        ex = json.loads(stored["body"])           # same run + window already summarised: do not spend again or duplicate actions
        progress("reused stored executive summary for this run and window")
    elif llm:
        facts = executive.build_facts(analysis, narratives, mem, ws.red, wf, prev_exec, carried)
        try:
            ex, results, unverified = executive.run_exec(llm, facts, {r["key"] for r in rows}, cfg.exec_effort)
            for res in results:
                mem.log_llm(run_id, "exec", None, llm.model, res.usage, res.usd)
            for u in ex.get("action_updates", []):
                if u["status"] in ("resolved", "stale"):
                    mem.set_action(u["action_id"], u["status"], u["reason"], run_id)
                    a = next((c for c in carried if c["action_id"] == u["action_id"]), None)
                    if a:
                        resolved.append({"id": a["action_id"], "who": a["who"], "what": a["what"], "keys": a["keys"], "note": u["reason"]})
            ids = mem.add_actions(run_id, ex["actions"])
            for a, i in zip(ex["actions"], ids):
                a["id"] = i
            ex["unverified_figures"] = unverified
            mem.save_exec(run_id, t0, t1, ex)
        except Exception as e:
            progress(f"executive summary failed ({type(e).__name__}: {e}); report will use the automatic verdict")
            ex = None
    still_open = [{"id": a["id"], "who": a["who"], "what": ws.red.text(a["what"]), "keys": json.loads(a["keys"]),
                   "note": ""} for a in mem.open_actions() if a["run_id"] != run_id]
    if ex is not None:
        ex.pop("action_updates", None)

    # --- project memory (agentic, memory tool)
    pm = None
    if llm and ex is not None:
        digest = {"window": {"from": t0, "to": t1}, "run": label, "verdict": ex.get("verdict"),
                  "headlines": [{"title": h["title"], "body": h["body"]} for h in ex.get("headlines", [])],
                  "decisions": [{"who": a["who"], "what": a["what"], "keys": a["keys"]} for a in ex.get("actions", [])],
                  "resolved_decisions": resolved,
                  "notable_stories": [{"key": r["key"], "capability": ws.red.text(r["capability"]), "severity": r["severity"],
                                       "change_type": (narratives.get(r["key"]) or {}).get("change_type", ""),
                                       "headline": ws.red.text((narratives.get(r["key"]) or {}).get("headline", ""))}
                                      for r in rows if r["severity"] in ("Major", "Moderate")][:40]}
        try:
            results, executor = update_project_memory(llm, ws.backend, digest, effort=cfg.judge_effort)
            for res in results:
                mem.log_llm(run_id, "project_memory", None, llm.model, res.usage, res.usd)
            pm = {"turns": len(results), "writes": sum(1 for c in executor.calls if c.get("command") != "view")}
        except Exception as e:
            progress(f"project memory update skipped ({type(e).__name__}: {e})")

    # --- memory block for the report
    n_events = mem.db.execute("SELECT COUNT(*) c FROM events").fetchone()["c"]
    analysis["memory"] = {
        "runs": len(mem.runs()), "events_total": n_events,
        "prev": None if not mem.last_run(before_run_id=run_id) else {"label": mem.last_run(before_run_id=run_id)["label"]},
        "ingest": None if not ingest_stats else {"events_added": ingest_stats.events_added, "new_stories": ingest_stats.new_stories},
        "coverage": analysis["meta"].get("coverage", {}), "open": still_open, "resolved": resolved,
        "llm": {**{k: jstats[k] for k in ("judged", "cached", "fallback")}, "usd": round(mem.llm_spend(run_id), 4)} if llm else {},
        "project_memory": pm,
    }
    unrev = rb.merge(analysis, narratives)
    rb.recompute(analysis)
    analysis["exec"] = ex
    out = rb.render(analysis, cfg.reports_dir / f"Scope_Change_Report_{label}.html", title)
    (cfg.reports_dir / f"analysis_{label}.json").write_text(json.dumps(analysis, indent=1, default=str))
    return {"report": out, "analysis": analysis, "judge": jstats, "unreviewed": unrev, "window": (t0, t1),
            "spend_usd": mem.llm_spend(run_id), "exec": ex}
