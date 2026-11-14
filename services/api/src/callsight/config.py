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
    oidc_issuer: str = ""  # single sign-on via any OpenID Connect provider; empty = disabled
    oidc_client_id: str = ""
    oidc_client_secret: str = ""
    public_url: str = "http://localhost:8000"  # where users reach callsight; the OIDC redirect URI is built from it
    diarization: str = ""  # "pyannote" separates speakers in mono recordings (needs the diarize extra + HF_TOKEN)
    diarization_model: str = "pyannote/speaker-diarization-community-1"
    hf_token: str = ""
    retention_days: int = 0  # delete calls (and recordings) older than this; 0 = keep forever
    analyzers: tuple[str, ...] = ()  # empty = pipeline.DEFAULT_ANALYZERS (or DEFAULT_LLM_ANALYZERS with an LLM)
    whisper_model: str = "small"  # faster-whisper size: tiny, base, small, medium, large-v3
    embed_model: str = ""  # Ollama embedding model for semantic search, e.g. mxbai-embed-large; empty = off

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
            oidc_issuer=env.get("CALLSIGHT_OIDC_ISSUER", "").rstrip("/"),
            oidc_client_id=env.get("CALLSIGHT_OIDC_CLIENT_ID", ""),
            oidc_client_secret=env.get("CALLSIGHT_OIDC_CLIENT_SECRET", ""),
            public_url=env.get("CALLSIGHT_PUBLIC_URL", cls.public_url).rstrip("/"),
            diarization=env.get("CALLSIGHT_DIARIZATION", "").strip().lower(),
            diarization_model=env.get("CALLSIGHT_DIARIZATION_MODEL", cls.diarization_model),
            hf_token=env.get("HF_TOKEN", ""),
            retention_days=int(env.get("CALLSIGHT_RETENTION_DAYS") or 0),
            analyzers=_csv(env.get("CALLSIGHT_ANALYZERS")),
            whisper_model=env.get("CALLSIGHT_WHISPER_MODEL", cls.whisper_model),
            embed_model=env.get("CALLSIGHT_EMBED_MODEL", "").strip(),
        )
