"""Test doubles: a scripted Anthropic SDK and a fake Managed Agents memory store. No network."""
from __future__ import annotations

import hashlib
import json
import re
from types import SimpleNamespace as NS


def text_resp(obj_or_text, stop="end_turn", usage=(1000, 200)):
    t = obj_or_text if isinstance(obj_or_text, str) else json.dumps(obj_or_text)
    return NS(content=[NS(type="text", text=t)], stop_reason=stop, stop_details=None,
              usage=NS(input_tokens=usage[0], output_tokens=usage[1], cache_read_input_tokens=0, cache_creation_input_tokens=0))


def tool_resp(calls, usage=(500, 100)):
    blocks = [NS(type="tool_use", id=f"tu_{i}", name="memory", input=c) for i, c in enumerate(calls)]
    return NS(content=blocks, stop_reason="tool_use", stop_details=None,
              usage=NS(input_tokens=usage[0], output_tokens=usage[1], cache_read_input_tokens=0, cache_creation_input_tokens=0))


class _Messages:
    def __init__(self, sdk, beta):
        self.sdk, self.beta = sdk, beta

    def create(self, **kw):
        self.sdk.requests.append({"beta": self.beta, **kw})
        return self.sdk.handler(kw)


class FakeSDK:
    """handler(kw) -> response. Default handler answers judge/exec/memory prompts deterministically."""

    def __init__(self, handler=None):
        self.requests: list[dict] = []
        self.handler = handler or self.default_handler
        self.messages = _Messages(self, False)
        self.beta = NS(messages=_Messages(self, True), memory_stores=FakeMemoryStores())
        self.memory_script: list = []

    # ---- helpers for assertions
    def all_request_text(self) -> str:
        return json.dumps(self.requests, default=str)

    def roles(self) -> list[str]:
        out = []
        for r in self.requests:
            sysm = r["system"][0]["text"]
            out.append("judge" if "judgement step" in sysm else "exec" if "executive summary" in sysm else "memory")
        return out

    # ---- default behaviour
    def default_handler(self, kw):
        sysm = kw["system"][0]["text"]
        user = kw["messages"][0]["content"] if isinstance(kw["messages"][0]["content"], str) else ""
        if "judgement step" in sysm:
            ev = json.loads(user.split("<evidence>")[1].split("</evidence>")[0])
            f = ev["facts"]
            return text_resp({
                "severity": "None", "change_type": "Reviewed", "headline": f"Reviewed {f['key']}.",
                "desc_change": "", "ac_change": "", "status_change": "", "pm_note": "",
                "flags_add": [], "flags_remove": ["SIGNOFF_PRESSURE"], "confidence": "High",
                "dossier_note": f"{f['key']} was reviewed; scope is stable.", "open_questions": ["Is the AC final?"]})
        if "executive summary" in sysm:
            facts = json.loads(user.split("<facts>")[1].split("</facts>")[0])
            c = facts["counts"]
            keys = [s["key"] for s in facts["stories_with_movement"][:2]]
            ups = [{"action_id": a["action_id"], "status": "resolved", "reason": "Later changes show it was done."}
                   for a in facts["carried_over_actions"][:1]]
            return text_resp({
                "verdict": f"{c['new_stories']} stories were added and {c['major']} changed materially.",
                "subverdict": f"{c['with_scope_movement']} of {c['total_stories']} stories moved. "
                              f"{facts['sign_off_gate']['edited_after_signoff']} were edited after sign-off.",
                "headlede": "", "headlines": [{"title": "Scope is growing", "body": f"{c['new_stories']} new stories.", "keys": keys}],
                "actions": [{"who": "Product owner", "what": "Decide what stays in release one.", "keys": keys}],
                "action_updates": ups})
        # project memory agent: scripted tool loop
        if not self.memory_script:
            self.memory_script = [
                tool_resp([{"command": "view", "path": "/memories"}]),
                tool_resp([{"command": "create", "path": "/memories/project/patterns.md", "file_text": "# Patterns\n- Scope grows via new stories.\n"}]),
                text_resp("Memory updated."),
            ]
        return self.memory_script.pop(0)


class FakeMemoryStores:
    def __init__(self):
        self._docs: dict[str, dict] = {}
        self.memories = self
        self.versions = 0

    def create(self, *a, **kw):
        if a and not kw.get("path"):                      # memory_stores.create(name=..., description=...)
            pass
        if "path" in kw:                                  # memories.create(store_id, path=..., content=...)
            if kw["path"] in {d["path"] for d in self._docs.values()}:
                e = Exception("conflict"); e.status_code = 409; raise e
            mid = f"mem_{len(self._docs) + 1}"
            self._docs[mid] = {"path": kw["path"], "content": kw["content"]}
            self.versions += 1
            return NS(id=mid, path=kw["path"])
        return NS(id="memstore_fake")

    def list(self, store_id, path_prefix="/", **kw):
        return [NS(type="memory", id=i, path=d["path"], content_sha256=hashlib.sha256(d["content"].encode()).hexdigest(),
                   content_size_bytes=len(d["content"])) for i, d in self._docs.items() if d["path"].startswith(path_prefix)]

    def retrieve(self, mid, memory_store_id=None, **kw):
        d = self._docs[mid]
        return NS(id=mid, path=d["path"], content=d["content"])

    def update(self, mid, memory_store_id=None, content=None, precondition=None, **kw):
        d = self._docs[mid]
        if precondition and precondition["content_sha256"] != hashlib.sha256(d["content"].encode()).hexdigest():
            e = Exception("precondition"); e.status_code = 409; raise e
        d["content"] = content
        self.versions += 1
        return NS(id=mid, path=d["path"])

    def delete(self, mid, memory_store_id=None, **kw):
        self._docs.pop(mid, None)
