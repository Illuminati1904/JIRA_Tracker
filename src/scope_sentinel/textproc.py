"""Text normalisation, sentence-level diffing, AC splitting and the severity rubric.

Ported from the skill's sentinel_lib.py. Formatting-only edits (wiki markup, smart quotes,
whitespace, account-id mentions) are normalised away so they never register as scope change.
"""
from __future__ import annotations

import difflib
import re

SEVERITY_ORDER = {"None": 0, "Minor": 1, "Moderate": 2, "Major": 3}


def max_sev(a: str, b: str) -> str:
    return a if SEVERITY_ORDER[a] >= SEVERITY_ORDER[b] else b


def normalise(text: str | None) -> str:
    if not text:
        return ""
    t = text
    t = re.sub(r"\[~accountid:[^\]]+\]", "@user", t)
    t = re.sub(r"\{color[^}]*\}", "", t)
    t = re.sub(r"\{[a-z]+(:[^}]*)?\}", "", t)
    t = re.sub(r"\[([^\]|]+)\|[^\]]+\]", r"\1", t)  # [text|url] -> text
    t = t.replace("*", "").replace("_", " ")
    t = re.sub(r"[‘’]", "'", t)
    t = re.sub(r"[“”]", '"', t)
    t = re.sub(r"[​‌‍﻿]", "", t)
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{2,}", "\n", t)
    return t.strip()


def _sentences(text: str | None) -> list[str]:
    units: list[str] = []
    for line in normalise(text).split("\n"):
        line = line.strip()
        if not line:
            continue
        parts = re.split(r"(?<=[.!?])\s+(?=[A-Z\"'(])", line)
        units.extend(p.strip() for p in parts if p.strip())
    return units


def diff_text(old: str | None, new: str | None) -> dict:
    a, b = _sentences(old), _sentences(new)
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    added: list[str] = []
    removed: list[str] = []
    modified: list[dict] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "insert":
            added += b[j1:j2]
        elif tag == "delete":
            removed += a[i1:i2]
        elif tag == "replace":
            olds, news = a[i1:i2], b[j1:j2]
            used: set[int] = set()
            for o in olds:
                best, bs = None, 0.0
                for k, n in enumerate(news):
                    if k in used:
                        continue
                    sc = difflib.SequenceMatcher(None, o, n).ratio()
                    if sc > bs:
                        best, bs = k, sc
                if best is not None and bs >= 0.55:
                    used.add(best)
                    modified.append({"before": o, "after": news[best], "similarity": round(bs, 2)})
                else:
                    removed.append(o)
            added += [n for k, n in enumerate(news) if k not in used]
    na, nb = normalise(old), normalise(new)
    ratio = difflib.SequenceMatcher(None, na, nb, autojunk=False).ratio() if (na or nb) else 1.0
    return {
        "added": added, "removed": removed, "modified": modified,
        "similarity": round(ratio, 3), "change_pct": round((1 - ratio) * 100, 1),
        "changed": bool(added or removed or modified), "old_len": len(na), "new_len": len(nb),
    }


_AC_HEAD = re.compile(r"^\s*\*?\s*(AC\s*\d+[a-z]?)\b", re.I)


def split_acs(ac_text: str | None) -> dict[str, str]:
    t = normalise(ac_text)
    if not t:
        return {}
    blocks: dict[str, str] = {}
    cur: list[str] = []
    cur_key = "Intro"
    for line in t.split("\n"):
        m = _AC_HEAD.match(line)
        if m:
            if "".join(cur).strip():
                blocks[cur_key] = "\n".join(cur).strip()
            cur_key, cur = m.group(1).upper().replace(" ", ""), [line]
        else:
            cur.append(line)
    if "".join(cur).strip():
        blocks[cur_key] = "\n".join(cur).strip()
    return blocks


def diff_acs(old: str | None, new: str | None) -> dict:
    A, B = split_acs(old), split_acs(new)
    added = [k for k in B if k not in A]
    removed = [k for k in A if k not in B]
    edited = []
    for k in B:
        if k in A and normalise(A[k]) != normalise(B[k]):
            d = diff_text(A[k], B[k])
            if d["changed"]:
                edited.append({"ac": k, "change_pct": d["change_pct"], "diff": d})
    return {"added": added, "removed": removed, "edited": edited,
            "added_text": {k: B[k] for k in added}, "removed_text": {k: A[k] for k in removed}}


def ac_changed(d: dict | None) -> bool:
    return bool(d and (d["added"] or d["removed"] or d["edited"]))


def rate_text_change(desc_diff, ac_diff, summary_changed: bool, summary_pct: float):
    """Apply the agreed rubric (references/severity-and-flags.md) to an exact diff."""
    reasons: list[str] = []
    sev = "None"

    def bump(level: str, why: str):
        nonlocal sev
        sev = max_sev(sev, level)
        reasons.append(why)

    if desc_diff and desc_diff["changed"]:
        if desc_diff["change_pct"] >= 30:
            bump("Major", f"Description rewritten ({desc_diff['change_pct']}% changed)")
        elif desc_diff["added"] or desc_diff["removed"]:
            bump("Moderate", f"Description content added/removed ({desc_diff['change_pct']}%)")
        else:
            bump("Minor", f"Description reworded ({desc_diff['change_pct']}%)")
    if ac_diff:
        n_add, n_rm = len(ac_diff["added"]), len(ac_diff["removed"])
        if n_add or n_rm:
            bump("Moderate", f"{n_add} AC added, {n_rm} AC removed")
            if n_add + n_rm >= 3:
                bump("Major", "3+ acceptance criteria added/removed in one period")
        for e in ac_diff["edited"]:
            if e["change_pct"] >= 30:
                bump("Major", f"{e['ac']} rewritten ({e['change_pct']}% changed)")
            elif e["diff"]["added"] or e["diff"]["removed"]:
                bump("Moderate", f"{e['ac']} content added/removed")
            else:
                bump("Minor", f"{'AC text' if e['ac'] == 'Intro' else e['ac']} reworded")
    if summary_changed:
        bump("Major" if summary_pct >= 40 else "Moderate", f"Summary changed ({summary_pct}%)")
    return sev, reasons
