import json
import tempfile
import unittest
from pathlib import Path

from scope_sentinel import demo
from scope_sentinel.analysis.engine import analyze
from scope_sentinel.config import Config
from scope_sentinel.ingest.detect import load_export
from scope_sentinel.memory.replay import signoff_periods, state_at
from scope_sentinel.memory.store import Memory

T = demo.EXPORT_TIMES


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = Path(tempfile.mkdtemp())
        cls.f = demo.write_demo(cls.d)
        cls.cfg = Config.load(home=cls.d / "h")
        cls.wf = cls.cfg.workflow

    def csv_mem(self):
        m = Memory(":memory:")
        self.addCleanup(m.close)
        for n in ("day0", "day1", "day2"):
            m.ingest(load_export(self.f["csv_" + n], self.cfg), n, T[n])
        return m

    def json_mem(self, path=None):
        m = Memory(":memory:")
        self.addCleanup(m.close)
        m.ingest(load_export(path or self.f["json_history"], self.cfg), "day2", T["day2"])
        return m

    @staticmethod
    def by_key(res):
        return {r["key"]: r for r in res["stories"]}


class MemorySemantics(Base):
    def test_reingest_same_file_is_noop(self):
        m = Memory(":memory:")
        self.addCleanup(m.close)
        e = load_export(self.f["csv_day0"], self.cfg)
        a = m.ingest(e, "day0", T["day0"])
        b = m.ingest(e, "again", T["day0"])
        self.assertTrue(b.already_ingested)
        self.assertEqual(b.run_id, a.run_id)
        self.assertEqual(len(m.runs()), 1)

    def test_events_are_idempotent_across_overlapping_dumps(self):
        m = self.json_mem()
        n1 = m.db.execute("SELECT COUNT(*) c FROM events").fetchone()["c"]
        # a later dump that repeats the same history must not duplicate events
        later = self.d / "later.json"
        demo.render_json(T["day2"], later)
        data = json.loads(later.read_text()); data["issues"][0]["fields"]["summary"] += ""   # byte-different, same content
        later.write_text(json.dumps(data, indent=2))
        m.ingest(load_export(later, self.cfg), "day2b", "2026-11-04 10:00")
        n2 = m.db.execute("SELECT COUNT(*) c FROM events").fetchone()["c"]
        self.assertEqual(n2, n1)                      # identical history re-delivered: zero new events
        self.assertEqual(m.db.execute("SELECT COUNT(*) c FROM events WHERE type='CREATED'").fetchone()["c"], 17)

    def test_out_of_order_dump_is_rejected(self):
        m = self.csv_mem()
        p = self.d / "old.csv"
        demo.render_csv("2026-10-31 09:00", p)
        with self.assertRaises(ValueError):
            m.ingest(load_export(p, self.cfg), "old", "2026-10-31 09:00")

    def test_state_at_time_travel(self):
        m = self.json_mem()
        before = state_at(m, "DEMO-1", "2026-11-01 00:00")
        after = state_at(m, "DEMO-1", "2026-11-04 00:00")
        self.assertIn("AC2 The list shows name and status.", before["ac"])
        self.assertIn("name, status and owner", after["ac"])
        self.assertEqual(state_at(m, "DEMO-3", "2026-11-01 00:00")["status"], "ToDo")
        self.assertEqual(state_at(m, "DEMO-3", "2026-11-04 00:00")["status"], "Story Writing")
        self.assertFalse(state_at(m, "DEMO-5", "2026-10-30 00:00").exists)      # created later

    def test_signoff_frozen_text_is_exact_with_changelog(self):
        m = self.json_mem()
        p = signoff_periods(m, "DEMO-2", self.wf)
        self.assertEqual(len(p), 1)
        self.assertEqual(p[0].start, "2026-11-02 16:30")
        self.assertTrue(p[0].exact)
        self.assertIn("territory dashboard", p[0].frozen["description"])       # rewrite happened BEFORE sign-off
        p1 = signoff_periods(m, "DEMO-1", self.wf)
        self.assertIn("name and status.", p1[0].frozen["ac"])                  # frozen text predates the later edit
        p3 = signoff_periods(m, "DEMO-3", self.wf)
        self.assertEqual(p3[0].end, "2026-11-03 11:15")                        # regression closes the period

    def test_csv_then_json_upgrade_supersedes_snapshot_guesses(self):
        m = self.csv_mem()
        cov = m.story_row("DEMO-1")["coverage"]
        self.assertEqual(cov, "observed")
        st = m.ingest(load_export(self.f["json_history"], self.cfg), "json", "2026-11-04 09:30")
        self.assertGreater(st.upgraded_to_changelog, 0)
        self.assertEqual(m.story_row("DEMO-1")["coverage"], "changelog")
        self.assertEqual(m.story_row("DEMO-1")["known_since"], "2026-10-01 09:00")
        obs = m.db.execute("SELECT COUNT(*) c FROM events WHERE source='observed' AND type IN ('FIELD_CHANGED','STATUS_CHANGED') "
                           "AND payload NOT LIKE '%reconcile%'").fetchone()["c"]
        self.assertEqual(obs, 0)
        # history is now exact even for a window that started before the first snapshot
        res = analyze(m, self.wf, "2026-10-10 00:00", "2026-11-04 09:30", "x")
        self.assertEqual(self.by_key(res)["DEMO-1"]["confidence"], "Exact")

    def test_truncated_changelog_is_reconciled_not_trusted(self):
        p = self.d / "trunc2.json"
        demo.render_json(T["day2"], p, truncate_changelog=True)
        m = self.json_mem(p)
        # whatever the truncated log says, state at the end must equal the current record
        for k in ("DEMO-1", "DEMO-2", "DEMO-3"):
            rec = m.story_record(k)
            self.assertEqual(state_at(m, k, "2026-11-05 00:00")["status"], rec["status"])
            self.assertEqual(state_at(m, k, "2026-11-05 00:00")["ac"], rec["ac"])

    def test_removed_then_reappeared(self):
        m = self.csv_mem()
        self.assertEqual(m.story_row("DEMO-10")["active"], 0)
        p = self.d / "back.csv"
        demo.render_csv("2026-11-02 09:00", p)       # DEMO-10 present in this render (before removal time)
        m.ingest(load_export(p, self.cfg), "back", "2026-11-05 09:00")
        self.assertEqual(m.story_row("DEMO-10")["active"], 1)
        self.assertTrue(m.events("DEMO-10", types=("REAPPEARED",)))

    def test_partial_export_does_not_remove(self):
        m = Memory(":memory:")
        self.addCleanup(m.close)
        m.ingest(load_export(self.f["csv_day0"], self.cfg), "d0", T["day0"])
        p = self.d / "subset.csv"
        text = Path(self.f["csv_day1"]).read_text(encoding="utf-8-sig").splitlines()
        p.write_text("\n".join(text[:3]) + "\n", encoding="utf-8")
        st = m.ingest(load_export(p, self.cfg), "d1", T["day1"], full_export=False)
        self.assertEqual(st.removed, 0)
        self.assertEqual(len(m.keys(active_only=True)), 15)


class AnalysisScenario(Base):
    def expected(self, rows):
        r = rows
        # DEMO-1: AC edited AFTER sign-off -> the headline flag
        self.assertIn("POST_SIGNOFF_EDIT", r["DEMO-1"]["flags"])
        self.assertEqual(r["DEMO-1"]["severity"], "Moderate")
        # DEMO-2: rewritten BEFORE sign-off -> not a breach
        self.assertIn("CHANGED_THEN_SIGNED_OFF", r["DEMO-2"]["flags"])
        self.assertNotIn("POST_SIGNOFF_EDIT", r["DEMO-2"]["flags"])
        self.assertEqual(r["DEMO-2"]["severity"], "Major")
        self.assertNotIn("SIGNOFF_PRESSURE", r["DEMO-2"]["flags"])      # its change request predates sign-off
        # DEMO-3: regression from sign-off
        self.assertIn("REGRESSION_FROM_SIGNOFF", r["DEMO-3"]["flags"])
        self.assertEqual(r["DEMO-3"]["severity"], "Major")
        # DEMO-4: Done story challenged by a reviewer, no edit
        self.assertIn("SIGNOFF_PRESSURE", r["DEMO-4"]["flags"])
        self.assertNotIn("POST_SIGNOFF_EDIT", r["DEMO-4"]["flags"])
        # DEMO-7: deferred
        self.assertIn("DESCOPED", r["DEMO-7"]["flags"])
        # DEMO-8: formatting-only edit is not scope change
        self.assertEqual(r["DEMO-8"]["severity"], "None")
        self.assertIsNone(r["DEMO-8"]["evidence"]["desc_diff"])
        # DEMO-9: metadata only
        self.assertEqual(r["DEMO-9"]["severity"], "None")
        self.assertIn("METADATA_CHANGE", r["DEMO-9"]["flags"])
        # DEMO-11: three ACs added -> Major
        self.assertEqual(r["DEMO-11"]["severity"], "Major")
        self.assertEqual(r["DEMO-11"]["evidence"]["ac_diff"]["added"], ["AC3", "AC4", "AC5"])
        # bulk label update across six stories is noise
        for i in range(12, 18):
            self.assertIn("BULK_EDIT_NOISE", r[f"DEMO-{i}"]["flags"])
        # new stories + hygiene
        for k in ("DEMO-5", "DEMO-6", "DEMO-18"):
            self.assertEqual(r[k]["movement"], "new")
        self.assertIn("DUPLICATE_TITLE", r["DEMO-5"]["flags"])
        self.assertIn("INCOMPLETE_TITLE", r["DEMO-18"]["flags"])

    def test_csv_snapshots_rolling_three_runs(self):
        m = self.csv_mem()
        res = analyze(m, self.wf, T["day0"], T["day2"], "day2")
        rows = self.by_key(res)
        self.expected(rows)
        self.assertIn("REMOVED_FROM_SCOPE", rows["DEMO-10"]["flags"])
        self.assertEqual(rows["DEMO-10"]["severity"], "Major")
        self.assertEqual(rows["DEMO-1"]["confidence"], "Observed")           # snapshot-derived, honest label

    def test_json_changelog_single_ingest_is_exact_on_first_run(self):
        m = self.json_mem()
        res = analyze(m, self.wf, T["day0"], T["day2"], "day2")
        rows = self.by_key(res)
        self.expected(rows)
        self.assertEqual(rows["DEMO-1"]["confidence"], "Exact")
        self.assertEqual(res["meta"]["coverage"].get("Inferred", 0), 0)
        self.assertEqual(res["meta"]["mode"], "snapshot")

    def test_csv_and_json_paths_agree(self):
        a = self.by_key(analyze(self.csv_mem(), self.wf, T["day0"], T["day2"], "x"))
        b = self.by_key(analyze(self.json_mem(), self.wf, T["day0"], T["day2"], "x"))
        for k in b:
            self.assertEqual(a[k]["severity"], b[k]["severity"], k)
            self.assertEqual(sorted(a[k]["flags"]), sorted(b[k]["flags"]), k)

    def test_first_csv_only_falls_back_to_inferred_audit_mode(self):
        m = Memory(":memory:")
        self.addCleanup(m.close)
        m.ingest(load_export(self.f["csv_day2"], self.cfg), "only", T["day2"])
        res = analyze(m, self.wf, "2026-10-26 00:00", T["day2"], "only")
        rows = self.by_key(res)
        self.assertEqual(rows["DEMO-1"]["confidence"], "Inferred")
        self.assertEqual(res["meta"]["mode"], "audit")
        self.assertIsNone(rows["DEMO-1"]["evidence"]["ac_diff"])             # never fabricate before/after text
        self.assertIn("DEFERRAL_SIGNAL", rows["DEMO-7"]["flags"])

    def test_any_window_can_be_asked_after_the_fact(self):
        m = self.json_mem()
        early = self.by_key(analyze(m, self.wf, "2026-10-26 09:00", "2026-11-01 18:00", "early"))
        self.assertNotIn("POST_SIGNOFF_EDIT", early["DEMO-1"]["flags"])      # edit happened on 11-03
        late = self.by_key(analyze(m, self.wf, "2026-11-01 18:00", "2026-11-04 09:00", "late"))
        self.assertIn("POST_SIGNOFF_EDIT", late["DEMO-1"]["flags"])
        self.assertIn("REGRESSION_FROM_SIGNOFF", late["DEMO-3"]["flags"])

    def test_edit_then_revert_after_signoff_is_minor(self):
        d = Path(tempfile.mkdtemp())
        p = d / "e.json"
        demo.render_json(T["day2"], p)
        data = json.loads(p.read_text())
        issue = next(i for i in data["issues"] if i["key"] == "DEMO-1")
        ac0, ac1 = demo._AC, demo._AC_EDITED
        issue["changelog"]["histories"] = [
            {"id": "1", "created": "2026-11-02T10:00:00.000+0000", "author": {"accountId": "u"},
             "items": [{"field": "Acceptance Criteria", "fromString": ac0, "toString": ac1}]},
            {"id": "2", "created": "2026-11-03T10:00:00.000+0000", "author": {"accountId": "u"},
             "items": [{"field": "Acceptance Criteria", "fromString": ac1, "toString": ac0}]}]
        issue["fields"]["customfield_10001"] = demo._adf(ac0)
        p.write_text(json.dumps(data))
        m = self.json_mem(p)
        r = self.by_key(analyze(m, self.wf, T["day0"], T["day2"], "x"))["DEMO-1"]
        self.assertIn("POST_SIGNOFF_EDIT", r["flags"])
        self.assertEqual(r["det_severity"], "Minor")
        self.assertTrue(any("reverted" in x for x in r["severity_reasons"]))

    def test_history_attached_and_format_only_hidden(self):
        r = self.by_key(analyze(self.json_mem(), self.wf, T["day0"], T["day2"], "x"))
        self.assertTrue(any("AC2 edited" in h["detail"] for h in r["DEMO-1"]["history"]))
        self.assertFalse(r["DEMO-8"]["history"])                              # only a formatting edit


if __name__ == "__main__":
    unittest.main()
