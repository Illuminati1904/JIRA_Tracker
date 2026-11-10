"""Workflow model: stage numbers define forward vs backward; ToDo is the business sign-off gate.

Ported from the Scope Change Sentinel skill (references/workflow.md). Instance-based instead of
module globals so tests and multi-project use are safe.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

DEFAULT_ORDER = [
    "New", "Story Writing", "Pending AIC Approval", "Design Writing", "Internal Design Review",
    "Pending CIT PO Review", "Pending Business SME Review", "Pending Business Signoff - AIC",
    "ToDo",  # <-- sign-off gate
    "Dev In Progress", "Unit Testing Complete", "Ready For Code Review", "Ready For Deployment",
    "Ready For Testing", "In Testing", "Ready For PO Review", "Done",
]
SIDE_STATES = ("Blocked", "Deferred", "Cancelled")


def _norm(s: str | None) -> str:
    return re.sub(r"[\s_\-]+", "", (s or "").strip().lower())


@dataclass
class Workflow:
    order: list[str] = field(default_factory=lambda: list(DEFAULT_ORDER))
    signoff: str = "ToDo"

    def __post_init__(self):
        self._lookup = {_norm(s): i for i, s in enumerate(self.order)}
        self._lookup.setdefault("todo", self._lookup.get(_norm(self.signoff)))
        self._side = {_norm(s): s for s in SIDE_STATES}
        if _norm(self.signoff) not in self._lookup:
            raise ValueError(f"signoff status {self.signoff!r} is not in the workflow order")
        self.signoff_stage = self._lookup[_norm(self.signoff)]

    def stage_of(self, status: str | None):
        """-> (stage_index | None, canonical_name, is_side_state)"""
        n = _norm(status)
        if n in self._lookup and self._lookup[n] is not None:
            i = self._lookup[n]
            return i, self.order[i], False
        if n in self._side:
            return None, self._side[n], True
        return None, (status or "").strip(), False

    def is_known(self, status: str | None) -> bool:
        i, _, side = self.stage_of(status)
        return i is not None or side

    def is_signed_off(self, status: str | None) -> bool:
        i, _, _ = self.stage_of(status)
        return i is not None and i >= self.signoff_stage

    def lane_of(self, status: str | None) -> str:
        i, _, side = self.stage_of(status)
        if side:
            return "side"
        if i is None:
            return "unknown"
        if i < self.signoff_stage:
            return "requirements"
        return "signed-off" if i == self.signoff_stage else "delivery"

    @classmethod
    def from_dict(cls, d: dict | None) -> "Workflow":
        if not d:
            return cls()
        return cls(order=list(d.get("order") or DEFAULT_ORDER), signoff=d.get("signoff", "ToDo"))
