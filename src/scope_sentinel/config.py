"""Run configuration. Loaded from `sentinel.toml` in the workspace (optional) + env overrides."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .workflow import Workflow


@dataclass
class Config:
    home: Path = field(default_factory=lambda: Path(os.environ.get("SENTINEL_HOME", "sentinel_home")))
    tz: str = "UTC"                      # project timezone for stored timestamps
    workflow: Workflow = field(default_factory=Workflow)
    # --- LLM layer -----------------------------------------------------------
    model: str = "claude-opus-5-5"
    judge_effort: str = "medium"         # Opus 5.5 defaults to medium; set explicitly
    exec_effort: str = "high"
    use_fallbacks: bool = True           # server-side refusal fallback (beta header), per API guidance
    max_workers: int = 4
    budget_usd: float | None = None      # hard stop for LLM spend per run
    # --- Memory --------------------------------------------------------------
    memory_backend: str = "local"        # local | managed
    memory_store_id: str | None = None
    # --- Governance ----------------------------------------------------------
    redact_terms: list[str] = field(default_factory=list)   # e.g. client / product names
    # --- Field mapping (override when your Jira custom fields are named differently) ----
    field_aliases: dict[str, list[str]] = field(default_factory=dict)
    jira_base: str = ""

    @property
    def db_path(self) -> Path:
        return self.home / "sentinel.sqlite"

    @property
    def mirror_dir(self) -> Path:
        return self.home / "memory"

    @property
    def reports_dir(self) -> Path:
        return self.home / "reports"

    @classmethod
    def load(cls, home: str | Path | None = None, **overrides) -> "Config":
        cfg = cls()
        if home:
            cfg.home = Path(home)
        path = cfg.home / "sentinel.toml"
        if path.exists():
            d = tomllib.loads(path.read_text())
            for k, v in d.items():
                if k == "workflow":
                    cfg.workflow = Workflow.from_dict(v)
                elif hasattr(cfg, k):
                    setattr(cfg, k, v)
        for k, v in overrides.items():
            if v is not None and hasattr(cfg, k):
                setattr(cfg, k, v)
        cfg.home = Path(cfg.home)
        if os.environ.get("SENTINEL_MEMORY_STORE_ID") and not cfg.memory_store_id:
            cfg.memory_store_id = os.environ["SENTINEL_MEMORY_STORE_ID"]
        cfg.home.mkdir(parents=True, exist_ok=True)
        return cfg
