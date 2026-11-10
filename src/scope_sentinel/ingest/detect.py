from __future__ import annotations

from pathlib import Path

from ..config import Config
from ..model import Export
from .csv_adapter import load_csv
from .jira_json import load_jira_json


def load_export(path: str | Path, cfg: Config, kind: str | None = None) -> Export:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(path)
    if kind is None:
        suf = p.suffix.lower()
        if suf in (".json", ".jsonl", ".ndjson"):
            kind = "jira_json"
        elif suf in (".csv", ".tsv"):
            kind = "csv"
        else:
            head = p.read_bytes()[:2048].lstrip(b"\xef\xbb\xbf").lstrip()
            kind = "jira_json" if head[:1] in (b"{", b"[") else "csv"
    exp = load_jira_json(p, cfg.tz, cfg.field_aliases) if kind == "jira_json" else load_csv(p)
    if not exp.jira_base and cfg.jira_base:
        exp.jira_base = cfg.jira_base
    return exp
