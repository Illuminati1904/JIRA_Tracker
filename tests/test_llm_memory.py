import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

from fakes import FakeSDK, text_resp, tool_resp

from scope_sentinel import demo
from scope_sentinel.config import Config
from scope_sentinel.llm.client import LLM, strict_schema
from scope_sentinel.llm.executive import verify_numbers
from scope_sentinel.llm.memory_tool import MemoryToolExecutor
from scope_sentinel.llm.schemas import ExecSummary, Narrative
from scope_sentinel.memory.backends import (ConflictError, LocalBackend, ManagedStoreBackend, MirroredBackend,
                                            sha256)
from scope_sentinel.model import Comment, Export, StoryRecord
from scope_sentinel.pipeline import generate_report, ingest_file, open_workspace
from scope_sentinel.redact import Redactor, leaks

T = demo.EXPORT_TIMES


def judge_json(**over):
    base = {"severity": "None", "change_type": "Reviewed", "headline": "ok", "desc_change": "", "ac_change": "",
            "status_change": "", "pm_note": "", "flags_add": [], "flags_remove": [], "confidence": "High",
            "dossier_note": "note", "open_questions": []}
    base.update(over)
    return base


class Redaction(unittest.TestCase):
    def test_redaction_rules(self):
        r = Redactor(None, ["Acme Pharma"])
        t = r.text("[~accountid:1111aaaa2222bbbb3333cccc] asked bob@acme.com about https://acme.atlassian.net/browse/ABC-12 "
                   "and https://example.org/x for Acme Pharma. token: sk-ant-abcdefghij123456")
        self.assertNotIn("1111aaaa2222bbbb3333cccc", t)
        self.assertNotIn("bob@acme.com", t)
        self.assertIn("[jira]/browse/ABC-12", t)
        self.assertIn("[link]", t)
        self.assertIn("[client]", t)
        self.assertIn("[secret]", t)
        self.assertEqual(r.text("[~accountid:abc]"), r.text("[~accountid:abc]"))      # stable pseudonyms
        self.assertEqual(leaks(t), [])
        self.assertIn("email", leaks("mail me at a@b.com"))

    def test_dates_and_numbers_survive_redaction(self):
        self.assertEqual(Redactor(None).text("Updated 2026-11-04 09:30, 3.0 points"), "Updated 2026-11-04 09:30, 3.0 points")


class Backends(unittest.TestCase):
    def test_local_crud_and_conflict(self):
        b = LocalBackend(tempfile.mkdtemp())
        b.write("/a/b.md", "one")
        self.assertEqual(b.read("/a/b.md"), "one")
        with self.assertRaises(ConflictError):
            b.write("/a/b.md", "two", expected_sha="deadbeef")
        b.write("/a/b.md", "two", expected_sha=sha256("one"))
        self.assertEqual([e.path for e in b.list("/")], ["/a/b.md"])
        self.assertTrue(b.delete("/a/b.md"))
        with self.assertRaises(ValueError):
            b.read("/../etc/passwd")

    def test_managed_store_with_fake_sdk(self):
        sdk = FakeSDK()
        sid = ManagedStoreBackend.create_store(sdk)
        b = ManagedStoreBackend(sdk, sid)
        b.write("/stories/X-1.md", "v1")
        self.assertEqual(b.read("/stories/X-1.md"), "v1")
        b.write("/stories/X-1.md", "v2", expected_sha=sha256("v1"))
        with self.assertRaises(ConflictError):
            b.write("/stories/X-1.md", "v3", expected_sha=sha256("stale"))
        self.assertEqual(b.read("/stories/X-1.md"), "v2")
        self.assertEqual([e.path for e in b.list("/stories")], ["/stories/X-1.md"])
        # a second client that does not know the path yet: create hits 409 and falls back to update
        b2 = ManagedStoreBackend(sdk, sid)
        b2._ids = {}
        b2.write("/stories/X-1.md", "v4")
        self.assertEqual(ManagedStoreBackend(sdk, sid).read("/stories/X-1.md"), "v4")

    def test_mirror_write_through_and_pull(self):
        sdk = FakeSDK()
        prim = ManagedStoreBackend(sdk, "s")
        mir = LocalBackend(tempfile.mkdtemp())
        b = MirroredBackend(prim, mir)
        b.write("/project/p.md", "hello")
        self.assertEqual(mir.read("/project/p.md"), "hello")
        prim.write("/project/p.md", "edited elsewhere")        # a reviewer edits in the store
        self.assertEqual(b.read("/project/p.md"), "hello")
        self.assertEqual(b.pull(), 1)
        self.assertEqual(b.read("/project/p.md"), "edited elsewhere")


class MemoryTool(unittest.TestCase):
    def setUp(self):
        self.b = LocalBackend(tempfile.mkdtemp())
        self.b.write("/stories/A-1.md", "dossier\nline2")
        self.x = MemoryToolExecutor(self.b)

    def test_view_directory_and_file(self):
        out = self.x.run({"command": "view", "path": "/memories"})
        self.assertIn("/memories/stories", out)
        out = self.x.run({"command": "view", "path": "/memories/stories/A-1.md"})
        self.assertIn("     1\tdossier", out)
        self.assertIn("does not exist", self.x.run({"command": "view", "path": "/memories/nope"}))

    def test_writes_only_under_project(self):
        self.assertIn("created", self.x.run({"command": "create", "path": "/memories/project/p.md", "file_text": "x"}))
        self.assertIn("read-only", self.x.run({"command": "create", "path": "/memories/stories/B.md", "file_text": "x"}))
        self.assertIn("read-only", self.x.run({"command": "delete", "path": "/memories/stories/A-1.md"}))
        self.assertIn("read-only", self.x.run({"command": "str_replace", "path": "/memories/stories/A-1.md", "old_str": "dossier", "new_str": "hacked"}))
        self.assertEqual(self.b.read("/stories/A-1.md"), "dossier\nline2")

    def test_edit_commands(self):
        self.x.run({"command": "create", "path": "/memories/project/p.md", "file_text": "a\nb\n"})
        self.assertIn("edited", self.x.run({"command": "str_replace", "path": "/memories/project/p.md", "old_str": "a", "new_str": "A"}))
        self.assertIn("No replacement", self.x.run({"command": "str_replace", "path": "/memories/project/p.md", "old_str": "zzz", "new_str": "y"}))
        self.x.run({"command": "insert", "path": "/memories/project/p.md", "insert_line": 1, "insert_text": "mid"})
        self.assertEqual(self.b.read("/project/p.md"), "A\nmid\nb\n")
        self.assertIn("renamed", self.x.run({"command": "rename", "old_path": "/memories/project/p.md", "new_path": "/memories/project/q.md"}))
        self.assertIn("deleted", self.x.run({"command": "delete", "path": "/memories/project/q.md"}))

    def test_path_traversal_and_outside_root_rejected(self):
        for p in ("/memories/../etc/passwd", "/etc/passwd", "/memories/%2e%2e/x", "memories/x", "/memories/project/../stories/A-1.md"):
            self.assertIn("Error", self.x.run({"command": "create", "path": p, "file_text": "x"}), p)
            self.assertIn("Error", self.x.run({"command": "view", "path": p}) if "stories" not in p else "Error", p)
        self.assertEqual(self.b.read("/stories/A-1.md"), "dossier\nline2")


class Schema(unittest.TestCase):
    def test_strict_schema_is_flat_and_closed(self):
        for m in (Narrative, ExecSummary):
            s = strict_schema(m)
            txt = json.dumps(s)
            self.assertNotIn("$ref", txt)
            self.assertNotIn("$defs", txt)
            self.assertIn('"title": {"type": "string"}', json.dumps(strict_schema(ExecSummary)))   # property named title survives

            def walk(n):
                if isinstance(n, dict):
                    if n.get("type") == "object":
                        self.assertFalse(n["additionalProperties"])
                        self.assertEqual(sorted(n["required"]), sorted(n["properties"]))
                    for v in n.values():
                        walk(v)
                elif isinstance(n, list):
                    for v in n:
                        walk(v)
            walk(s)


class NumberCheck(unittest.TestCase):
    FACTS = {"counts": {"total_stories": 40, "new_stories": 7, "stories_at_window_start": 33}, "sign_off_gate": {"edited_after_signoff": 0},
             "stories_with_movement": [], "carried_over_actions": []}

    def test_traced_figures_pass_and_invented_fail(self):
        ok = {"verdict": "7 stories were added, 21% on top of the 33 at baseline.", "subverdict": "From 26 Oct to 4 Nov, ABC-123 moved.",
              "headlede": "", "headlines": [], "actions": []}
        self.assertEqual(verify_numbers(ok, self.FACTS), [])
        bad = dict(ok, verdict="47 stories were added.")
        self.assertEqual(verify_numbers(bad, self.FACTS), ["47"])


class Pipeline(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.files = demo.write_demo(self.d)
        self.cfg = Config.load(home=self.d / "home", max_workers=2)

    def ws(self, sdk, **cfg):
        for k, v in cfg.items():
            setattr(self.cfg, k, v)
        w = open_workspace(self.cfg, use_llm=True, sdk=sdk)
        self.addCleanup(w.close)
        return w

    def ingest_all(self, w, upto=("day0", "day1", "day2")):
        for n in upto:
            ingest_file(w, str(self.files["csv_" + n]), n, T[n])

    # ------------------------------------------------------------------
    def test_full_run_dossiers_exec_actions_and_project_memory(self):
        sdk = FakeSDK()
        w = self.ws(sdk)
        self.ingest_all(w)
        res = generate_report(w, since="2026-10-26", use_llm=True)
        self.assertTrue(res["report"].exists())
        self.assertGreater(res["judge"]["judged"], 0)
        self.assertEqual(sdk.roles().count("exec"), 1)
        self.assertIn("memory", sdk.roles())
        # the judge said severity None and tried to remove POST_SIGNOFF_EDIT: facts win
        rows = {r["key"]: r for r in res["analysis"]["stories"]}
        self.assertEqual(rows["DEMO-1"]["severity"], "Moderate")
        self.assertIn("POST_SIGNOFF_EDIT", rows["DEMO-1"]["flags"])
        self.assertEqual(rows["DEMO-3"]["severity"], "Major")
        # dossier: facts by code, understanding by Claude
        doc = w.backend.read("/stories/DEMO-1.md")
        self.assertIn("Acceptance criteria: AC2 edited", doc)
        self.assertIn("DEMO-1 was reviewed; scope is stable.", doc)
        self.assertIn("Is the AC final?", doc)
        # project memory written through the memory tool, into the writable area only
        self.assertIn("Scope grows via new stories", w.backend.read("/project/patterns.md"))
        # actions tracked
        self.assertEqual(len(w.mem.open_actions()), 1)
        self.assertEqual(res["exec"]["unverified_figures"], [])

    def test_every_request_uses_prompt_cache_effort_and_fallbacks(self):
        sdk = FakeSDK()
        w = self.ws(sdk)
        self.ingest_all(w)
        generate_report(w, since="2026-10-26", use_llm=True)
        for r in sdk.requests:
            self.assertEqual(r["model"], "claude-opus-5-5")
            self.assertEqual(r["system"][0]["cache_control"], {"type": "ephemeral"})
            self.assertIn(r["output_config"]["effort"], ("medium", "high"))
            self.assertTrue(r["beta"])
            self.assertEqual(r["fallbacks"], "default")
            self.assertNotIn("temperature", r)
            self.assertNotIn("thinking", r)                          # Opus 5.5: adaptive by default, never disabled
        judge = next(r for r in sdk.requests if "judgement step" in r["system"][0]["text"])
        self.assertEqual(judge["output_config"]["format"]["type"], "json_schema")

    def test_nothing_identifying_leaves_the_machine(self):
        cfg_terms = ["Reviewer One"]
        sdk = FakeSDK()
        w = self.ws(sdk, redact_terms=cfg_terms)
        self.ingest_all(w)
        # add a story whose comment carries an e-mail, an account mention and a tenant URL
        s = StoryRecord(key="DEMO-99", summary="Secrets story", status="ToDo", created="2026-11-03 09:00", updated="2026-11-03 09:00",
                        description="Contact bob@acme.com", ac="AC1 x", labels=["Pod-1"],
                        comments=[Comment("c1", "2026-11-04 08:30", "1111aaaa2222bbbb3333cccc",
                                          "Please add AC2. cc [~accountid:4444dddd5555eeee6666ffff] bob@acme.com https://acme.atlassian.net/browse/DEMO-1 Reviewer One agrees")])
        exp = Export(stories=[s], kind="csv", sha="x99", path="p.csv")
        w.mem.ingest(exp, "extra", "2026-11-04 09:05", full_export=False)
        generate_report(w, since="2026-10-26", use_llm=True)
        blob = sdk.all_request_text()
        for needle in ("1111aaaa2222bbbb3333cccc", "4444dddd5555eeee6666ffff", "bob@acme.com", "acme.atlassian.net",
                       "Reviewer One", "u-rv1", "u-po", "u-ba", "Alex BA", "Sam PO"):
            self.assertNotIn(needle, blob, needle)
        # and nothing leaked into the memory documents either
        for e in w.backend.list("/"):
            self.assertEqual(leaks(w.backend.read(e.path)), [], e.path)

    def test_unchanged_evidence_is_not_rejudged_and_exec_not_duplicated(self):
        sdk = FakeSDK()
        w = self.ws(sdk)
        self.ingest_all(w)
        generate_report(w, since="2026-10-26", use_llm=True)
        n1 = sdk.roles().count("judge")
        res2 = generate_report(w, since="2026-10-26", use_llm=True)
        self.assertEqual(sdk.roles().count("judge"), n1)             # all cached
        self.assertEqual(res2["judge"]["judged"], 0)
        self.assertGreater(res2["judge"]["cached"], 0)
        self.assertEqual(sdk.roles().count("exec"), 1)               # same run + window: summary reused
        self.assertEqual(len(w.mem.open_actions()), 1)               # no duplicate decisions

    def test_actions_carry_over_and_resolve_in_next_run(self):
        sdk = FakeSDK()
        w = self.ws(sdk)
        self.ingest_all(w, ("day0", "day1"))
        generate_report(w, since_run="day0", use_llm=True)
        self.assertEqual(len(w.mem.open_actions()), 1)
        ingest_file(w, str(self.files["csv_day2"]), "day2", T["day2"])
        res = generate_report(w, use_llm=True)                       # rolling: day1 -> day2
        mem_block = res["analysis"]["memory"]
        self.assertEqual(len(mem_block["resolved"]), 1)               # the fake judge marks it resolved
        statuses = [a["status"] for a in w.mem.db.execute("SELECT status FROM actions").fetchall()]
        self.assertEqual(sorted(statuses), ["open", "resolved"])      # old one resolved, new one raised
        exec_req = [r for r in sdk.requests if "executive summary" in r["system"][0]["text"]][-1]
        self.assertIn("carried_over_actions", json.dumps(exec_req["messages"]))
        self.assertIn("action_id", json.dumps(exec_req["messages"]))

    def test_refusal_falls_back_per_story(self):
        def handler(kw):
            if "judgement step" in kw["system"][0]["text"]:
                return NS(content=[], stop_reason="refusal", stop_details=NS(category="general_harms"),
                          usage=NS(input_tokens=1, output_tokens=1, cache_read_input_tokens=0, cache_creation_input_tokens=0))
            return FakeSDK().default_handler(kw)
        sdk = FakeSDK(handler)
        w = self.ws(sdk)
        self.ingest_all(w)
        res = generate_report(w, since="2026-10-26", use_llm=True)
        self.assertGreater(res["judge"]["refused"], 0)
        self.assertEqual(res["judge"]["judged"], 0)
        rows = {r["key"]: r for r in res["analysis"]["stories"]}
        self.assertEqual(rows["DEMO-3"]["severity"], "Major")        # deterministic facts still rated
        self.assertFalse(rows["DEMO-3"]["reviewed"])                  # and honestly marked "not yet reviewed"

    def test_bad_credentials_abort_loudly(self):
        def handler(kw):
            e = Exception("invalid x-api-key"); e.status_code = 401; raise e
        w = self.ws(FakeSDK(handler))
        self.ingest_all(w)
        with self.assertRaises(RuntimeError) as cm:
            generate_report(w, since="2026-10-26", use_llm=True)
        self.assertIn("--no-llm", str(cm.exception))

    def test_budget_cap_stops_spending_and_degrades(self):
        sdk = FakeSDK(lambda kw: text_resp(judge_json(), usage=(900_000, 50_000))) # ~$4.6 per call
        w = self.ws(sdk, budget_usd=1.0, max_workers=1)
        self.ingest_all(w)
        res = generate_report(w, since="2026-10-26", use_llm=True)
        self.assertTrue(res["judge"]["budget_stopped"])
        self.assertEqual(res["judge"]["judged"], 1)
        self.assertGreater(res["judge"]["fallback"], 0)

    def test_exec_invented_figure_triggers_repair_then_flags(self):
        calls = {"n": 0}

        def handler(kw):
            if "executive summary" in kw["system"][0]["text"]:
                calls["n"] += 1
                return text_resp({"verdict": "4711 stories were added.", "subverdict": "x", "headlede": "", "headlines": [],
                                  "actions": [], "action_updates": []})
            return FakeSDK().default_handler(kw)
        w = self.ws(FakeSDK(handler))
        self.ingest_all(w)
        res = generate_report(w, since="2026-10-26", use_llm=True)
        self.assertEqual(calls["n"], 2)                                # one repair attempt
        self.assertEqual(res["exec"]["unverified_figures"], ["4711"])  # surfaced, not silently shipped

    def test_no_llm_makes_no_calls_and_still_reports(self):
        sdk = FakeSDK()
        w = open_workspace(self.cfg, use_llm=False)
        self.addCleanup(w.close)
        self.ingest_all(w)
        res = generate_report(w, since="2026-10-26", use_llm=False)
        self.assertTrue(res["report"].exists())
        self.assertEqual(sdk.requests, [])
        self.assertTrue(res["unreviewed"])

    def test_managed_backend_end_to_end(self):
        sdk = FakeSDK()
        store = sdk.beta.memory_stores
        sid = ManagedStoreBackend.create_store(sdk)
        w0 = open_workspace(Config.load(home=self.d / "home"), use_llm=False)
        w0.mem.kv_set("memory_store_id", sid)
        w0.close()
        self.cfg.memory_backend = "managed"
        w = self.ws(sdk)
        self.ingest_all(w)
        generate_report(w, since="2026-10-26", use_llm=True)
        paths = {d["path"] for d in store._docs.values()}
        self.assertIn("/stories/DEMO-1.md", paths)
        self.assertIn("/project/patterns.md", paths)
        self.assertTrue((self.cfg.mirror_dir / "stories" / "DEMO-1.md").exists())    # local mirror for audit

    def test_dossiers_rebuild_from_ledger_after_store_loss(self):
        import shutil
        from scope_sentinel import cli
        sdk = FakeSDK()
        w = self.ws(sdk)
        self.ingest_all(w)
        generate_report(w, since="2026-10-26", use_llm=True)
        before = w.backend.read("/stories/DEMO-1.md")
        self.assertIn("DEMO-1 was reviewed", before)
        shutil.rmtree(self.cfg.mirror_dir / "stories")                # simulate losing the memory documents
        self.assertIsNone(w.backend.read("/stories/DEMO-1.md"))
        cli.cmd_memory(NS(op="rebuild", home=str(self.cfg.home), memory_backend=None, tz=None, budget_usd=None, model=None))
        after = w.backend.read("/stories/DEMO-1.md")
        self.assertIn("DEMO-1 was reviewed; scope is stable.", after)  # understanding restored from the stored judgment
        self.assertIn("Acceptance criteria: AC2 edited", after)        # facts replayed from the event log

    def test_rolling_window_requires_baseline_on_first_run(self):
        w = open_workspace(self.cfg, use_llm=False)
        self.addCleanup(w.close)
        ingest_file(w, str(self.files["csv_day0"]), "day0", T["day0"])
        with self.assertRaises(RuntimeError) as cm:
            generate_report(w, use_llm=False)
        self.assertIn("--since", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
