import json
import tempfile
import unittest
from pathlib import Path

from scope_sentinel import demo
from scope_sentinel.config import Config
from scope_sentinel.ingest.adf import to_text
from scope_sentinel.ingest.csv_adapter import parse_comment
from scope_sentinel.ingest.detect import load_export
from scope_sentinel.textproc import diff_acs, diff_text, normalise, rate_text_change
from scope_sentinel.workflow import Workflow


class TextProc(unittest.TestCase):
    def test_formatting_only_edit_is_not_a_change(self):
        a = 'As a rep I want *an account* header. See [the doc|http://x] {color:red}now{color}'
        b = 'As a rep I want “an account” header.   See the doc now'
        self.assertEqual(normalise(a).replace('"', ''), normalise(b).replace('"', ''))
        self.assertFalse(diff_text("The list loads fast.", "The list  loads fast.")["changed"])

    def test_ac_diff_and_rubric(self):
        old = "AC1 One.\nAC2 Two.\nAC3 Three."
        new = "AC1 One.\nAC2 Two.\nAC3 Three.\nAC4 Four.\nAC5 Five.\nAC6 Six."
        d = diff_acs(old, new)
        self.assertEqual(d["added"], ["AC4", "AC5", "AC6"])
        sev, why = rate_text_change(None, d, False, 0)
        self.assertEqual(sev, "Major")           # 3+ ACs added
        sev, _ = rate_text_change(None, diff_acs("AC1 One.", "AC1 One.\nAC2 Two."), False, 0)
        self.assertEqual(sev, "Moderate")
        sev, _ = rate_text_change(diff_text("The list loads fast today.", "The list loads quickly today."), None, False, 0)
        self.assertEqual(sev, "Minor")

    def test_rewrite_over_30pct_is_major(self):
        d = diff_text("As a rep I want a list of accounts. Filters are out of scope.",
                      "As a manager I need a territory dashboard with maps and KPIs. Data refreshes hourly.")
        self.assertGreaterEqual(d["change_pct"], 30)
        self.assertEqual(rate_text_change(d, None, False, 0)[0], "Major")

    def test_workflow_gate(self):
        wf = Workflow()
        self.assertTrue(wf.is_signed_off("ToDo") and wf.is_signed_off("Done"))
        self.assertFalse(wf.is_signed_off("Pending Business Signoff - AIC"))
        self.assertEqual(wf.lane_of("Blocked"), "side")
        self.assertEqual(wf.stage_of("to do")[0], wf.signoff_stage)


class Adapters(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = Path(tempfile.mkdtemp())
        cls.files = demo.write_demo(cls.d)
        cls.cfg = Config.load(home=cls.d / "h")

    def test_csv_repeated_headers_and_comments(self):
        e = load_export(self.files["csv_day1"], self.cfg)
        self.assertEqual(e.kind, "csv")
        s = next(x for x in e.stories if x.key == "DEMO-2")
        self.assertEqual(len(s.comments), 2)
        self.assertEqual(s.comments[0].ts, "2026-10-28 09:12")
        self.assertEqual(s.points, "3")                       # '3.0' normalised
        self.assertEqual(s.labels, ["Pod-1"])
        self.assertIsNone(s.changelog)
        self.assertFalse(e.capabilities["exact_history"])

    def test_csv_missing_columns_warn_instead_of_failing(self):
        p = self.d / "thin.csv"
        p.write_text("Summary,Issue key,Status\nA title,X-1,ToDo\n")
        e = load_export(p, self.cfg)
        self.assertEqual(len(e.stories), 1)
        joined = " ".join(e.warnings)
        self.assertIn("Acceptance Criteria", joined)
        self.assertIn("Created", joined)

    def test_not_a_jira_csv_is_rejected(self):
        p = self.d / "bad.csv"
        p.write_text("a,b\n1,2\n")
        with self.assertRaises(ValueError):
            load_export(p, self.cfg)

    def test_json_adf_and_changelog(self):
        e = load_export(self.files["json_history"], self.cfg)
        self.assertEqual(e.kind, "jira_json")
        self.assertTrue(e.capabilities["exact_history"])
        self.assertEqual(e.jira_base, "https://demo.atlassian.net")
        s = next(x for x in e.stories if x.key == "DEMO-1")
        self.assertIn("AC2 The list shows name, status and owner.", s.ac)
        self.assertEqual([c.field for c in s.changelog], ["ac"])
        self.assertEqual(s.capability, "Account Management")
        self.assertEqual(s.points, "3")

    def test_json_truncated_changelog_detected(self):
        p = self.d / "trunc.json"
        demo.render_json(demo.EXPORT_TIMES["day2"], p, truncate_changelog=True)
        e = load_export(p, self.cfg)
        self.assertTrue(any("truncated changelog" in w for w in e.warnings))
        self.assertTrue(any(not s.changelog_complete for s in e.stories))

    def test_json_without_changelog_degrades_with_warning(self):
        p = self.d / "nolog.json"
        demo.render_json(demo.EXPORT_TIMES["day2"], p, with_changelog=False)
        e = load_export(p, self.cfg)
        self.assertFalse(e.capabilities["exact_history"])
        self.assertTrue(any("No changelog" in w for w in e.warnings))

    def test_json_without_names_map_warns(self):
        p = self.d / "nonames.json"
        data = json.loads(Path(self.files["json_history"]).read_text())
        data.pop("names")
        p.write_text(json.dumps(data))
        e = load_export(p, self.cfg)
        self.assertTrue(any("names" in w for w in e.warnings))

    def test_jsonl_and_bare_list(self):
        data = json.loads(Path(self.files["json_history"]).read_text())
        p = self.d / "x.jsonl"
        p.write_text("\n".join(json.dumps(i) for i in data["issues"][:3]))
        self.assertEqual(len(load_export(p, self.cfg).stories), 3)

    def test_adf_to_text(self):
        doc = {"type": "doc", "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "Hello "}, {"type": "mention", "attrs": {"id": "abc"}}]},
            {"type": "bulletList", "content": [{"type": "listItem", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "one"}]}]}]}]}
        t = to_text(doc)
        self.assertIn("[~accountid:abc]", t)
        self.assertIn("* one", t)

    def test_comment_formats(self):
        self.assertEqual(parse_comment("10/28/2026 9:12 AM;acc1;Hello; world", "K").text, "Hello; world")
        self.assertEqual(parse_comment("28/Oct/26 9:12 AM;acc1;Hi", "K").ts, "2026-10-28 09:12")
        c = parse_comment("no timestamp here", "K")
        self.assertIsNone(c.ts)


if __name__ == "__main__":
    unittest.main()
