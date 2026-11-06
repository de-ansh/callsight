"""Settings, read from CALLSIGHT_* environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping


def _csv(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(part.strip() for part in value.split(",") if part.strip())


@dataclass(frozen=True)
class Settings:
    llm_provider: str = "none"  # "none" = rule-based only; "ollama" = local LLM
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.1"
    competitors: tuple[str, ...] = ()
    rep_names: tuple[str, ...] = ()
    database_url: str = ""

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        env = os.environ if env is None else env
        return cls(
            llm_provider=env.get("CALLSIGHT_LLM_PROVIDER", "none").strip().lower() or "none",
            ollama_url=env.get("CALLSIGHT_OLLAMA_URL", cls.ollama_url),
            ollama_model=env.get("CALLSIGHT_OLLAMA_MODEL", cls.ollama_model),
            competitors=_csv(env.get("CALLSIGHT_COMPETITORS")),
            rep_names=_csv(env.get("CALLSIGHT_REP_NAMES")),
            database_url=env.get("CALLSIGHT_DATABASE_URL", ""),
        )
