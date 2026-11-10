"""Redaction at the trust boundary.

Raw Jira text lives only in the local SQLite file. Anything that leaves the machine (Claude API calls, the
managed memory store) passes through here first:
  * account ids / @mentions   -> stable pseudonyms (P1, P2 ...), mapping kept only in the local DB
  * e-mail addresses, URLs    -> [email], [jira]/browse/KEY, [link]
  * configured client terms   -> [client]
  * obvious secrets           -> [secret]   (defence in depth: memory must never hold credentials)
Display names are never sent at all.
"""
from __future__ import annotations

import re
from typing import Any

from .memory.store import Memory

_MENTION = re.compile(r"\[~accountid:([^\]]+)\]")
_ACCOUNT = re.compile(r"\b(?:\d{5,7}:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9a-f]{24})\b", re.I)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_URL = re.compile(r"https?://[^\s\]|)>\"']+")
_SECRETS = [
    re.compile(r"\bsk-ant-[A-Za-z0-9_-]{10,}"), re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"(?i)\b(bearer|token|api[_-]?key|secret|password)\b\s*[:=]\s*\S{8,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
]
_BROWSE = re.compile(r"/browse/([A-Z][A-Z0-9]+-\d+)")


class Redactor:
    def __init__(self, mem: Memory | None = None, terms: list[str] | None = None):
        self.mem = mem
        self.terms = [re.compile(re.escape(t), re.I) for t in (terms or []) if t.strip()]
        self._local: dict[str, str] = {}

    def author(self, account_id: str) -> str:
        if not account_id:
            return ""
        if self.mem is not None:
            return self.mem.pseudonym(account_id)
        return self._local.setdefault(account_id, f"P{len(self._local) + 1}")

    def text(self, s: Any) -> str:
        if not s:
            return ""
        t = str(s)
        t = _MENTION.sub(lambda m: "@" + self.author(m.group(1)), t)
        t = _ACCOUNT.sub(lambda m: self.author(m.group(0)), t)
        for rx in _SECRETS:
            t = rx.sub("[secret]", t)
        t = _EMAIL.sub("[email]", t)

        def url(m):
            b = _BROWSE.search(m.group(0))
            return f"[jira]/browse/{b.group(1)}" if b else "[link]"
        t = _URL.sub(url, t)
        for rx in self.terms:
            t = rx.sub("[client]", t)
        return t

    def obj(self, o: Any) -> Any:
        if isinstance(o, str):
            return self.text(o)
        if isinstance(o, list):
            return [self.obj(x) for x in o]
        if isinstance(o, dict):
            return {k: self.obj(v) for k, v in o.items()}
        return o


def leaks(text: str) -> list[str]:
    """Names of sensitive patterns still present (used as a last gate before writing to a managed store)."""
    found = []
    if _MENTION.search(text) or _ACCOUNT.search(text):
        found.append("account id")
    if _EMAIL.search(text):
        found.append("email")
    if any(rx.search(text) for rx in _SECRETS):
        found.append("secret")
    return found
