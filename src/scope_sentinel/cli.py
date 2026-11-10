"""sentinel: command line for the Scope Change Sentinel accelerator."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import Config
from .pipeline import generate_report, ingest_file, make_sdk, open_workspace


def _cfg(a) -> Config:
    return Config.load(a.home, tz=getattr(a, "tz", None), memory_backend=getattr(a, "memory_backend", None),
                       budget_usd=getattr(a, "budget_usd", None), model=getattr(a, "model", None))


def _print_ingest(st, warnings):
    if st.already_ingested:
        print(f"already ingested as run {st.run_id} ('{st.label}'): nothing to do")
        return
    ev = ", ".join(f"{k}={v}" for k, v in sorted((st.events_by_type or {}).items()))
    print(f"ingested run {st.run_id} '{st.label}' @ {st.taken_at}: {st.stories_seen} stories "
          f"({st.new_stories} new to memory), {st.events_added} events [{ev}]")
    if st.removed:
        print(f"  {st.removed} stories absent from this export were marked removed (use --partial for filtered exports)")
    if st.upgraded_to_changelog:
        print(f"  {st.upgraded_to_changelog} stories upgraded from snapshot guesses to exact changelog history")
    if st.reconciled:
        print(f"  {st.reconciled} fields reconciled against the changelog (truncated or untracked history)")
    for w in warnings:
        print(f"  warning: {w}")


def cmd_ingest(a):
    ws = open_workspace(_cfg(a), use_llm=False)
    st, w = ingest_file(ws, a.file, a.label, a.taken_at, not a.partial, a.format, a.force_order)
    _print_ingest(st, w)


def _report(ws, a, ingest_stats=None):
    use_llm = not a.no_llm
    res = generate_report(ws, since=a.since, since_run=a.since_run, until=a.until, label=a.report_label, use_llm=use_llm,
                          title=a.title, refresh_exec=a.refresh_exec, ingest_stats=ingest_stats, progress=lambda m: print("  " + m))
    S = res["analysis"]["summary"]
    print(f"report: {res['report']}")
    print(f"window {res['window'][0]} → {res['window'][1]} | {S['total']} stories | changed {S['n_changed']} | "
          f"new {S['n_new']} | severity {S['severity']} | gate breaches {S['n_signoff_breach']} | regressions {S['n_regression']}")
    if use_llm:
        j = res["judge"]
        print(f"claude: judged {j['judged']}, reused {j['cached']}, deterministic {j['fallback']} | spend ≈ ${res['spend_usd']:.2f}")
        for e in j["errors"][:5]:
            print(f"  note: {e}")
        if res["exec"] and res["exec"].get("unverified_figures"):
            print(f"  CHECK: unverified figures in the summary: {res['exec']['unverified_figures']}")
    elif res["unreviewed"]:
        print(f"{len(res['unreviewed'])} stories carry change signals with deterministic narratives only (--no-llm)")


def cmd_report(a):
    ws = open_workspace(_cfg(a), use_llm=not a.no_llm)
    _report(ws, a)


def cmd_run(a):
    ws = open_workspace(_cfg(a), use_llm=not a.no_llm)
    st, w = ingest_file(ws, a.file, a.label, a.taken_at, not a.partial, a.format, a.force_order)
    _print_ingest(st, w)
    _report(ws, a, st)


def cmd_history(a):
    from .memory.dossier import story_path
    ws = open_workspace(_cfg(a), use_llm=False)
    if ws.mem.story_row(a.key) is None:
        print(f"{a.key} is not in memory"); return 1
    ws.dossiers.write(a.key)
    print(ws.backend.read(story_path(a.key)))


def cmd_actions(a):
    ws = open_workspace(_cfg(a), use_llm=False)
    if a.op in ("close", "reopen"):
        ws.mem.set_action(a.id, "resolved" if a.op == "close" else "open", a.note or "")
    for r in ws.mem.db.execute("SELECT * FROM actions ORDER BY id").fetchall():
        print(f"#{r['id']:<3} [{r['status']:<8}] run {r['run_id']} · {r['who']}: {r['what']} {json.loads(r['keys'])}"
              + (f"  ({r['note']})" if r["note"] else ""))


def cmd_memory(a):
    cfg = _cfg(a)
    if a.op == "init":
        from .memory.backends import ManagedStoreBackend
        sdk = make_sdk()
        mem = open_workspace(Config.load(a.home, memory_backend="local"), use_llm=False).mem
        try:
            sid = ManagedStoreBackend.create_store(sdk)
        except Exception as e:   # SDK/API errors: turn into an actionable message instead of a stack trace
            code = getattr(e, "status_code", None)
            msg = str(e)
            if code == 403 and "sandbox" in msg.lower():
                hint = ("Memory stores cannot be created or changed from an Anthropic-hosted sandbox with restricted network "
                        "access. Run this command from your own machine, or from an environment with unrestricted network access.")
            elif code in (401, 403, 404):
                hint = "Check the API key, and that your organisation has the Managed Agents memory-store beta enabled."
            else:
                hint = "See the API error above."
            raise RuntimeError(f"could not create the memory store (HTTP {code}): {msg[:300]}\n  {hint}") from e
        mem.kv_set("memory_store_id", sid)
        print(f"created memory store {sid}. Set `memory_backend = \"managed\"` in {cfg.home / 'sentinel.toml'} "
              f"(or pass --memory-backend managed) to use it.")
    elif a.op == "pull":
        ws = open_workspace(cfg, use_llm=False)
        n = ws.backend.pull() if hasattr(ws.backend, "pull") else 0
        print(f"pulled {n} changed documents into {cfg.mirror_dir}")
    elif a.op == "rebuild":
        # dossiers are derived data: facts from the event log, understanding from the last stored judgment
        ws = open_workspace(cfg, use_llm=False)
        for key in ws.mem.keys():
            j = ws.mem.last_judgment(key)
            nar = json.loads(j["narrative"]) if j else {}
            ws.dossiers.write(key, nar.get("dossier_note") or None, nar.get("open_questions") or None)
        ws.dossiers.rebuild_index()
        print(f"rebuilt {len(ws.mem.keys())} dossiers in {cfg.memory_backend} memory from the ledger "
              "(project-level notes under /project are not derivable; restore them from the mirror or the store's versions)")
    elif a.op == "status":
        ws = open_workspace(cfg, use_llm=False)
        runs = ws.mem.runs()
        ev = ws.mem.db.execute("SELECT COUNT(*) c FROM events").fetchone()["c"]
        print(f"home: {cfg.home} | backend: {cfg.memory_backend} | runs: {len(runs)} | stories: {len(ws.mem.keys())} | events: {ev}")
        for r in runs:
            print(f"  run {r['run_id']} '{r['label']}' @ {r['taken_at']} ({r['source_kind']}, {r['n_stories']} stories, {r['n_events']} events)")
        print(f"  documents in memory backend: {len(ws.backend.list('/'))}")


def cmd_doctor(a):
    from .ingest.detect import load_export
    cfg = _cfg(a)
    exp = load_export(a.file, cfg, a.format)
    unknown = sorted({s.status for s in exp.stories if s.status and not cfg.workflow.is_known(s.status)})
    print(f"{exp.kind}: {len(exp.stories)} stories | exact history: {exp.capabilities.get('exact_history')} | "
          f"comments: {sum(len(s.comments) for s in exp.stories)} | max updated: {exp.max_updated}")
    for w in exp.warnings:
        print(f"  degrades: {w}")
    if unknown:
        print(f"  statuses not in the workflow (add them to sentinel.toml [workflow]): {unknown}")
    if not exp.warnings and not unknown:
        print("  looks good")


def cmd_demo(a):
    from . import demo
    dest = Path(a.dest)
    files = demo.write_demo(dest / "data")
    cfg = Config.load(dest / "home")
    ws = open_workspace(cfg, use_llm=False)
    for name in ("day0", "day1", "day2"):
        st, w = ingest_file(ws, str(files[f"csv_{name}"]), name, demo.EXPORT_TIMES[name])
        _print_ingest(st, w)

    class A: pass
    b = A(); b.no_llm = not a.llm; b.since = "2026-10-26"; b.since_run = None; b.until = None; b.report_label = None
    b.title = "Scope Change Report (synthetic demo)"; b.refresh_exec = False
    ws.close()
    ws = open_workspace(cfg, use_llm=a.llm)
    _report(ws, b)
    print(f"synthetic data and workspace written under {dest}")


def main(argv=None):
    p = argparse.ArgumentParser(prog="sentinel", description=__doc__)
    p.add_argument("--home", default=None, help="workspace directory (default ./sentinel_home or $SENTINEL_HOME)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common_ingest(s):
        s.add_argument("file")
        s.add_argument("--label"); s.add_argument("--taken-at", help="'YYYY-MM-DD HH:MM' (default: newest Updated in the dump)")
        s.add_argument("--format", choices=["csv", "jira_json"]); s.add_argument("--tz", default=None)
        s.add_argument("--partial", action="store_true", help="the dump is a filtered subset: do not mark missing stories as removed")
        s.add_argument("--force-order", action="store_true")

    def common_report(s):
        s.add_argument("--since", help="fixed baseline date YYYY-MM-DD (cumulative drift)")
        s.add_argument("--since-run", help="baseline = an earlier run label")
        s.add_argument("--until"); s.add_argument("--report-label"); s.add_argument("--title", default="Scope Change Report")
        s.add_argument("--no-llm", action="store_true", help="deterministic analysis and narratives only; nothing leaves this machine")
        s.add_argument("--refresh-exec", action="store_true"); s.add_argument("--budget-usd", type=float)
        s.add_argument("--model"); s.add_argument("--memory-backend", choices=["local", "managed"])

    s = sub.add_parser("ingest", help="load a Jira dump (CSV or REST JSON) into memory"); common_ingest(s); s.set_defaults(fn=cmd_ingest)
    s = sub.add_parser("report", help="analyse a window from memory and build the HTML report"); common_report(s); s.set_defaults(fn=cmd_report)
    s = sub.add_parser("run", help="ingest + report in one step"); common_ingest(s); common_report(s); s.set_defaults(fn=cmd_run)
    s = sub.add_parser("history", help="show a story's dossier"); s.add_argument("key"); s.set_defaults(fn=cmd_history)
    s = sub.add_parser("actions", help="list / close tracked decisions"); s.add_argument("op", nargs="?", default="list", choices=["list", "close", "reopen"])
    s.add_argument("id", nargs="?", type=int); s.add_argument("--note"); s.set_defaults(fn=cmd_actions)
    s = sub.add_parser("memory", help="memory backend maintenance"); s.add_argument("op", choices=["init", "pull", "rebuild", "status"])
    s.add_argument("--memory-backend", choices=["local", "managed"]); s.set_defaults(fn=cmd_memory)
    s = sub.add_parser("doctor", help="check a dump before ingesting"); s.add_argument("file"); s.add_argument("--format", choices=["csv", "jira_json"]); s.add_argument("--tz", default=None)
    s.set_defaults(fn=cmd_doctor)
    s = sub.add_parser("demo", help="generate synthetic data and run end to end"); s.add_argument("--dest", default="demo_data")
    s.add_argument("--llm", action="store_true", help="also call Claude (needs credentials; synthetic data only)"); s.set_defaults(fn=cmd_demo)
    a = p.parse_args(argv)
    try:
        return a.fn(a) or 0
    except (RuntimeError, ValueError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
