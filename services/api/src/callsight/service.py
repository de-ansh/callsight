"""Shared entry point used by both the CLI and the HTTP API."""

from __future__ import annotations

from typing import Sequence

from .config import Settings
from .models import CallReport, Transcript
from .pipeline import DEFAULT_ANALYZERS, analyze, build_analyzers
from .registry import create, load_builtins


def analyze_text(
    text: str,
    *,
    call_id: str = "adhoc",
    settings: Settings | None = None,
    extra_competitors: Sequence[str] = (),
) -> CallReport:
    return analyze_text_with_transcript(
        text, call_id=call_id, settings=settings, extra_competitors=extra_competitors
    )[1]


def analyze_text_with_transcript(
    text: str,
    *,
    call_id: str = "adhoc",
    settings: Settings | None = None,
    extra_competitors: Sequence[str] = (),
) -> tuple[Transcript, CallReport]:
    """Like analyze_text, but also returns the parsed transcript (needed to persist segments)."""
    settings = settings or Settings.from_env()
    load_builtins()

    transcript = create("asr", "text").transcribe(text, call_id)
    competitors = tuple(dict.fromkeys((*settings.competitors, *extra_competitors)))
    analyzers = build_analyzers(DEFAULT_ANALYZERS, competitors=competitors, rep_names=settings.rep_names)

    llm = None
    if settings.llm_provider != "none":
        llm = create("llm", settings.llm_provider, base_url=settings.ollama_url, model=settings.ollama_model)
    return transcript, analyze(transcript, analyzers, llm)
