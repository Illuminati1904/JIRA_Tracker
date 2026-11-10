"""Structured-output schemas. Kept free of numeric/length constraints (not supported by the API's schema
subset); limits are enforced in code after parsing."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

Severity = Literal["None", "Minor", "Moderate", "Major"]
Confidence = Literal["High", "Medium", "Low"]
# Flags the judge may add. Deterministic facts (POST_SIGNOFF_EDIT, REGRESSION_FROM_SIGNOFF, NEW_STORY,
# REMOVED_FROM_SCOPE, NEWLY_SIGNED_OFF, FORWARD) are computed by code and cannot be added or removed here.
JudgeFlag = Literal[
    "POST_SIGNOFF_CHANGE", "SIGNOFF_PRESSURE", "CHANGED_THEN_SIGNED_OFF", "POSSIBLE_REGRESSION", "REWORK_LOOP",
    "DESCOPED", "CONDITIONAL_APPROVAL", "TRACEABILITY_GAP", "DEFERRAL_SIGNAL", "METADATA_CHANGE", "BULK_EDIT_NOISE",
    "STATUS_MOVED",
]


class Narrative(BaseModel):
    severity: Severity
    change_type: str          # 2-5 words, scannable in the ledger
    headline: str             # one sentence, plain language, < 160 chars
    desc_change: str          # empty when no evidence
    ac_change: str
    status_change: str
    pm_note: str              # what a PM would do or watch; may be empty
    flags_add: list[JudgeFlag]
    flags_remove: list[JudgeFlag]
    confidence: Confidence
    dossier_note: str         # durable 2-4 sentence understanding for next run's memory
    open_questions: list[str]


class Headline(BaseModel):
    title: str
    body: str
    keys: list[str]


class Action(BaseModel):
    who: str                  # a role, never a person
    what: str                 # one decision, one sentence
    keys: list[str]


class ActionUpdate(BaseModel):
    action_id: int
    status: Literal["open", "resolved", "stale"]
    reason: str


class ExecSummary(BaseModel):
    verdict: str
    subverdict: str
    headlede: str
    headlines: list[Headline]
    actions: list[Action]
    action_updates: list[ActionUpdate]
