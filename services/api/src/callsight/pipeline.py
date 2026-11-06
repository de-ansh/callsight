"""The analysis pipeline: Transcript -> analyzers -> (optional LLM summary) -> CallReport."""

from __future__ import annotations

import logging
from typing import Sequence

from .interfaces import LLM, Analyzer
from .models import CallReport, Insight, Transcript, format_timestamp
from .registry import create

log = logging.getLogger(__name__)

DEFAULT_ANALYZERS = ("talk_ratio", "objections", "next_step", "action_items")
MAX_PROMPT_CHARS = 12_000
_SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2, "info": 3}

SUMMARY_PROMPT = """You are a sales-call analyst. Summarize the call below in at most 4 sentences. \
Use only facts stated in the transcript. Do not speculate.

Transcript:
{transcript}

Summary:"""


def build_analyzers(names: Sequence[str] = DEFAULT_ANALYZERS, **options: object) -> list[Analyzer]:
    return [create("analyzer", name, **options) for name in names]


def render_transcript(transcript: Transcript, max_chars: int = MAX_PROMPT_CHARS) -> str:
    text = "\n".join(
        f"[{format_timestamp(s.start_s)}] {s.speaker}: {s.text}" for s in transcript.segments
    )
    return text if len(text) <= max_chars else text[:max_chars] + "\n[...truncated]"


def _fallback_summary(transcript: Transcript, insights: list[Insight]) -> str:
    high = sum(1 for i in insights if i.severity == "high")
    return (
        f"{len(transcript.speakers)}-party call of about {format_timestamp(transcript.duration_s)} "
        f"with {len(transcript.segments)} turns; {len(insights)} insight(s) found, {high} high severity."
    )


def analyze(
    transcript: Transcript,
    analyzers: Sequence[Analyzer],
    llm: LLM | None = None,
) -> CallReport:
    insights: list[Insight] = []
    for analyzer in analyzers:
        insights.extend(analyzer.analyze(transcript))
    insights.sort(key=lambda i: _SEVERITY_RANK.get(i.severity, 99))  # stable within a severity

    summary = _fallback_summary(transcript, insights)
    if llm is not None:
        try:
            summary = llm.complete(SUMMARY_PROMPT.format(transcript=render_transcript(transcript))).strip()
        except Exception:  # a failing model must not lose the rule-based results
            log.exception("LLM summary failed; using the rule-based summary")

    return CallReport(
        call_id=transcript.call_id,
        duration_s=transcript.duration_s,
        speakers=transcript.speakers,
        talk_share=transcript.talk_share(),
        summary=summary,
        insights=insights,
    )
