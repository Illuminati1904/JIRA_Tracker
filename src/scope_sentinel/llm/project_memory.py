"""Agentic curation of project-level memory through Claude's client-side memory tool (`memory_20250818`)."""
from __future__ import annotations

import json

from ..memory.backends import MemoryBackend
from .client import LLM, Result
from .memory_tool import MemoryToolExecutor
from .prompts import PROJECT_MEMORY_SYSTEM

TOOLS = [{"type": "memory_20250818", "name": "memory"}]


def update_project_memory(llm: LLM, backend: MemoryBackend, digest: dict, effort: str = "medium",
                          max_turns: int = 10) -> tuple[list[Result], MemoryToolExecutor]:
    ex = MemoryToolExecutor(backend)
    msgs: list[dict] = [{"role": "user", "content":
                         "<run_digest>\n" + json.dumps(digest, ensure_ascii=False, indent=1) +
                         "\n</run_digest>\nReview project memory and update it for this run."}]
    results: list[Result] = []
    for _ in range(max_turns):
        res = llm.call_raw(system=PROJECT_MEMORY_SYSTEM, messages=msgs, effort=effort, tools=TOOLS, max_tokens=8000)
        results.append(res)
        calls = [b for b in res.resp.content if getattr(b, "type", "") == "tool_use"]
        if getattr(res.resp, "stop_reason", None) != "tool_use" or not calls:
            break
        msgs.append({"role": "assistant", "content": res.resp.content})
        msgs.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": b.id, "content": ex.run(dict(b.input))} for b in calls]})
    return results, ex
