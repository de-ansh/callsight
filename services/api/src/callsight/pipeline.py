"""The analysis pipeline: Transcript -> analyzers -> (optional LLM summary) -> CallReport."""

from __future__ import annotations

import logging
import re
from typing import Sequence

from .interfaces import LLM, Analyzer
from .models import CallReport, Insight, Transcript, format_timestamp
from .registry import create

log = logging.getLogger(__name__)

DEFAULT_ANALYZERS = ("talk_ratio", "objections", "competitors", "next_step", "action_items")
# With an LLM configured: the model for objections, rules where they measured better
# (commitments, next step, exact competitor names). See evals/README.md.
DEFAULT_LLM_ANALYZERS = ("talk_ratio", "competitors", "action_items", "next_step", "llm_insights")
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


_PROMISE = re.compile(r"^(?:I'll|I will|let me|I'm going to|I am going to)\s+", re.I)
_CONCERNS = {"pricing_objection": "pricing", "timing_objection": "timing", "authority_objection": "approval",
             "need_objection": "need"}


def _join(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def _length(seconds: float) -> str:
    minutes = round(seconds / 60)
    return "under a minute" if minutes < 1 else "about 1 minute" if minutes == 1 else f"about {minutes} minutes"


def _fallback_summary(transcript: Transcript, insights: list[Insight], rep: str | None = None) -> str:
    """A readable recap built only from what the analyzers found, with times so each claim can be checked."""
    speakers = transcript.speakers
    if not speakers:
        return "Empty call."
    rep = rep if rep in speakers else speakers[0]
    others = [s for s in speakers if s != rep]
    by_kind: dict[str, list[Insight]] = {}
    for i in insights:
        by_kind.setdefault(i.kind, []).append(i)
    at = lambda i: f" ({format_timestamp(i.evidence[0].start_s)})" if i.evidence else ""

    who = f"{rep} (rep) and {_join(others)}" if others else rep
    share = transcript.talk_share().get(rep)
    parts = [f"{who} talked for {_length(transcript.duration_s)}"
             + (f"; {rep} did {share:.0%} of the talking." if share is not None and others else ".")]

    customer = _join(others) if others else "The customer"
    concerns = [f"{_CONCERNS[k]}{at(by_kind[k][0])}" for k in _CONCERNS if k in by_kind]
    competitors = [f"{i.title.removeprefix('Competitor mentioned: ')}{at(i)}" for i in by_kind.get("competitor_mention", [])]
    raised = []
    if len(concerns) == 1:
        kind = next(k for k in _CONCERNS if k in by_kind)
        raised.append(f"raised a{'n' if _CONCERNS[kind][0] in 'aeiou' else ''} {_CONCERNS[kind]} concern{at(by_kind[kind][0])}")
    elif concerns:
        raised.append(f"raised {_join(concerns)} concerns")
    if competitors:
        raised.append(f"mentioned {_join(competitors)}")
    if raised:
        parts.append(f"{customer} {' and '.join(raised)}.")
    elif others:
        parts.append(f"{customer} raised no objections.")

    commitments = [f"{_PROMISE.sub('', i.title).rstrip('.')}{at(i)}" for i in by_kind.get("action_item", [])[:3]]
    if commitments:
        parts.append(f"{rep} committed to {'; '.join(commitments)}.")
    if "no_next_step" in by_kind:
        parts.append("No next step was agreed.")
    elif "next_step" in by_kind:
        last = by_kind["next_step"][0].evidence[-1:]  # the latest matching line is where it was settled
        parts.append(f"A next step was agreed{f' ({format_timestamp(last[0].start_s)})' if last else ''}.")
    return " ".join(parts)


def analyze(
    transcript: Transcript,
    analyzers: Sequence[Analyzer],
    llm: LLM | None = None,
) -> CallReport:
    insights: list[Insight] = []
    for analyzer in analyzers:
        insights.extend(analyzer.analyze(transcript))
    insights.sort(key=lambda i: _SEVERITY_RANK.get(i.severity, 99))  # stable within a severity

    # The rep as the analyzers saw it (configured or per-call), so the recap names the same person.
    rep = next((a.rep_speaker(transcript) for a in analyzers if hasattr(a, "rep_speaker") and transcript.speakers), None)
    summary = _fallback_summary(transcript, insights, rep)
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
