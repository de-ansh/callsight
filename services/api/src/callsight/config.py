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
    s3_endpoint: str = ""  # host:port of MinIO / S3; empty = audio upload disabled
    s3_access_key: str = ""
    s3_secret_key: str = ""
    s3_bucket: str = "recordings"
    s3_secure: bool = False  # True for https endpoints
    secret_key: str = ""  # signs short-lived recording links; set it when running more than one API process
    retention_days: int = 0  # delete calls (and recordings) older than this; 0 = keep forever
    analyzers: tuple[str, ...] = ()  # empty = pipeline.DEFAULT_ANALYZERS (or DEFAULT_LLM_ANALYZERS with an LLM)
    whisper_model: str = "small"  # faster-whisper size: tiny, base, small, medium, large-v3

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
            s3_endpoint=env.get("CALLSIGHT_S3_ENDPOINT", ""),
            s3_access_key=env.get("CALLSIGHT_S3_ACCESS_KEY", ""),
            s3_secret_key=env.get("CALLSIGHT_S3_SECRET_KEY", ""),
            s3_bucket=env.get("CALLSIGHT_S3_BUCKET", cls.s3_bucket),
            s3_secure=env.get("CALLSIGHT_S3_SECURE", "").strip().lower() in ("1", "true", "yes"),
            secret_key=env.get("CALLSIGHT_SECRET_KEY", ""),
            retention_days=int(env.get("CALLSIGHT_RETENTION_DAYS") or 0),
            analyzers=_csv(env.get("CALLSIGHT_ANALYZERS")),
            whisper_model=env.get("CALLSIGHT_WHISPER_MODEL", cls.whisper_model),
        )
