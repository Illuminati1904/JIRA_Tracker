"""Heuristic comment-signal scan (first pass only; the LLM judge or the PM confirms).

Ported from the skill. These over-fire ('removed' in a status note, 'added' describing a test), which is
exactly why the judge step exists and why deterministic facts are never overridden by them.
"""
from __future__ import annotations

import re

SIGNALS = [
    ("approval", r"\b(approved|i approve|considering this story approved|sign[- ]?off|signed off|aligned (to|with) this story|gcc commercial sales approval)\b"),
    ("conditional_approval", r"\b(approved with|approval contingent|contingent (on|to)|subject to|approved,? but|conditional)\b"),
    ("scope_add", r"\b(added|adding|add (a|an|new|the)|new (ac|story|requirement|section|field|widget)|additional|net[- ]new|include[sd]?)\b"),
    ("scope_remove", r"\b(removed|remove|removing|de-?scop|out of scope|no longer|dropped|not needed|deprecat|retire|cancel)\b"),
    ("rework", r"\b(rewrit|re-?work|replaced|replace|restructur|redefin|revised|revamp|overhaul|re-?scope|re-?baseline|pivot)\b"),
    ("split_merge", r"\b(split|merged?|combine|consolidat|moved to (another|a new|[A-Z]+-)|new story (created|needed)|NEW US needed)\b"),
    ("traceability_gap", r"\b(requirements? without a delivering story|traceability|not covered|partially covered|gap|missing|unmapped|no delivering story)\b"),
    ("change_request", r"\b(please (add|update|remove|change|revise|clarify)|can we add|can this be added|should (we|this) (add|include)|needs? to be (added|updated|changed))\b"),
    ("deferral", r"\b(defer|deferred|push(ed)? to R2|tagged for R2|next release|release 2|post[- ]?launch|phase 2|backlog)\b"),
    ("blocker", r"\b(blocked|blocker|dependency|waiting on|pending (decision|clarification|input)|open question)\b"),
    ("estimate", r"\b(story points?|re-?size|sized|estimate|effort)\b"),
]
SIGNAL_RES = [(n, re.compile(p, re.I)) for n, p in SIGNALS]
SIGNAL_WEIGHT = {
    "scope_add": 2, "scope_remove": 3, "rework": 3, "split_merge": 3, "conditional_approval": 3,
    "traceability_gap": 2, "change_request": 2, "deferral": 2, "approval": 1, "blocker": 1, "estimate": 1,
}
CHANGE_TAGS = ("scope_add", "scope_remove", "rework", "split_merge", "conditional_approval", "change_request")
SCOPE_SIGNAL_TAGS = CHANGE_TAGS + ("deferral", "traceability_gap")


def classify_comment(text: str) -> list[str]:
    return [n for n, rx in SIGNAL_RES if rx.search(text or "")]


def score_comments(comments: list[dict]) -> tuple[list[dict], int]:
    """comments: [{ts, author, text}] already filtered to the window. Adds tags + weight."""
    out, score = [], 0
    for c in comments:
        tags = classify_comment(c["text"])
        w = sum(SIGNAL_WEIGHT.get(t, 0) for t in tags)
        if len(c["text"]) > 600 and tags:
            w += 1
        score += w
        out.append({**c, "tags": tags, "weight": w})
    return out, score
