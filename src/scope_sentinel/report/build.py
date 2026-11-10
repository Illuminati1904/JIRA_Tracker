"""Merge analysis + narratives + executive summary + memory block and render the single-file HTML report."""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

HERE = Path(__file__).parent
SEV = {"None": 0, "Minor": 1, "Moderate": 2, "Major": 3}
_NAR_FIELDS = ("change_type", "headline", "desc_change", "ac_change", "status_change", "pm_note", "confidence")


def merge(analysis: dict, narratives: dict) -> list[str]:
    """Attach narratives. Returns keys that carry change signals but only a deterministic narrative."""
    unreviewed: list[str] = []
    for r in analysis["stories"]:
        nar = narratives.get(r["key"])
        if nar:
            llm = nar.get("_source", "llm") == "llm"
            r["reviewed"] = llm
            if llm:
                r["severity"] = nar.get("severity", r["severity"])
                fl = [f for f in r["flags"] if f not in set(nar.get("flags_remove", []))]
                for f in nar.get("flags_add", []):
                    if f not in fl:
                        fl.append(f)
                r["flags"] = fl
            else:
                unreviewed.append(r["key"])
            r["narrative"] = {k: nar.get(k, "") for k in _NAR_FIELDS}
        elif r["movement"] == "new":
            created = (r.get("created") or "")[:10]
            r["narrative"] = {"change_type": "New story", "headline": f"Created {created}: scope addition.", "desc_change": "",
                              "ac_change": "", "status_change": "", "pm_note": "", "confidence": "High"}
            r["reviewed"] = True
        else:
            r["narrative"], r["reviewed"] = None, True
        r.pop("description_now", None)
        r.pop("ac_now", None)
    return unreviewed


def recompute(analysis: dict) -> None:
    rows, S = analysis["stories"], analysis["summary"]
    S["severity"] = dict(Counter(r["severity"] for r in rows))
    S["flags"] = dict(Counter(f for r in rows for f in r["flags"]))
    S["n_changed"] = sum(1 for r in rows if r["severity"] != "None" or r["movement"] != "same")
    S["n_new"] = sum(1 for r in rows if r["movement"] == "new")
    S["n_signoff_breach"] = sum(1 for r in rows if {"POST_SIGNOFF_EDIT", "POST_SIGNOFF_CHANGE"} & set(r["flags"]))
    S["n_signoff_pressure"] = sum(1 for r in rows if "SIGNOFF_PRESSURE" in r["flags"])
    S["n_regression"] = sum(1 for r in rows if {"REGRESSION_FROM_SIGNOFF", "POSSIBLE_REGRESSION"} & set(r["flags"]))
    S["n_past_gate"] = sum(1 for r in rows if r["signed_off_now"])
    S["n_baseline"] = sum(1 for r in rows if r["movement"] != "new")
    rows.sort(key=lambda r: (-SEV[r["severity"]], -len(r["flags"]), r["key"]))


def render(analysis: dict, out: str | Path, title: str = "Scope Change Report") -> Path:
    tpl = (HERE / "template.html").read_text(encoding="utf-8")
    analysis["title"] = title
    blob = json.dumps(analysis, default=str, ensure_ascii=False).replace("<", "\\u003c").replace("\u2028", " ").replace("\u2029", " ")
    assert "/*__DATA__*/null" in tpl
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(tpl.replace("/*__DATA__*/null", blob, 1), encoding="utf-8")
    return out
