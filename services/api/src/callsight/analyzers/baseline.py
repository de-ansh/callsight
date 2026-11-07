"""Rule-based baseline analyzers.

These are deliberately simple, deterministic heuristics. They give the platform a
working, testable, zero-cost baseline and a reference for the LLM-powered
analyzers that will be evaluated against them. Every insight cites the transcript
segments it is based on.
"""

from __future__ import annotations

import math
import re

from ..models import Evidence, Insight, Segment, Transcript
from ..registry import register

REP_TALK_SHARE_LIMIT = 0.65
QUOTE_MAX_CHARS = 160
MAX_EVIDENCE = 5

_PRICING = re.compile(r"\b(too expensive|expensive|budget|pric(?:e|es|ing)|discount|cheaper|costs?)\b", re.I)
_TIMING = re.compile(r"\b(next quarter|next year|not a priority|not right now|bad timing|revisit)\b", re.I)
# "Anything else I can help with?" - "Not right now, thanks." is a polite close, not an objection.
_ANYTHING_ELSE = re.compile(r"\banything else\b[^.!]*\?", re.I)
_POLITE_NO = re.compile(r"^\W*(no,?\s*)?not right now\b[\s,.!]*(thanks?|thank you)?\b[^?]{0,40}$", re.I)
_NEXT_STEP = re.compile(
    r"\b(next steps?|follow[- ]up|schedule|calendar|send (?:you|over)|book a|set up a|let'?s meet|demo on)\b",
    re.I,
)
_COMMITMENT = re.compile(r"\b(I'll|I will)\b[^.?!]*[.?!]?", re.I)


def _name_pattern(name: str) -> re.Pattern:
    """Match a name whatever the spacing: speech-to-text writes "AcmeCall" as "Acme call" or "ACME-Call"."""
    parts = re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+", name) or [name]
    return re.compile(r"\b" + r"[\s.\-]{0,2}".join(re.escape(p) for p in parts) + r"\b", re.I)


def _is_polite_close(transcript: Transcript, segment: Segment) -> bool:
    """A short "not right now, thanks" straight after the rep asks "anything else...?"."""
    previous = transcript.segments[segment.id - 1] if segment.id > 0 else None
    return bool(previous and _ANYTHING_ELSE.search(previous.text) and _POLITE_NO.search(segment.text))


def _evidence(segment: Segment) -> Evidence:
    quote = segment.text if len(segment.text) <= QUOTE_MAX_CHARS else segment.text[:QUOTE_MAX_CHARS] + "..."
    return Evidence(segment.id, segment.start_s, segment.speaker, quote)


class _Base:
    name = ""

    def __init__(
        self,
        *,
        competitors: tuple[str, ...] = (),
        rep_names: tuple[str, ...] = (),
        **_: object,
    ) -> None:
        self.competitors = tuple(competitors)
        self.rep_names = tuple(n.lower() for n in rep_names)

    def rep_speaker(self, transcript: Transcript) -> str:
        """The sales rep: a configured name if present, otherwise whoever speaks first."""
        for speaker in transcript.speakers:
            if speaker.lower() in self.rep_names:
                return speaker
        return transcript.speakers[0]

    def analyze(self, transcript: Transcript) -> list[Insight]:  # pragma: no cover - interface
        raise NotImplementedError


@register("analyzer", "talk_ratio")
class TalkRatioAnalyzer(_Base):
    name = "talk_ratio"

    def analyze(self, transcript: Transcript) -> list[Insight]:
        if len(transcript.speakers) < 2:
            return []
        rep = self.rep_speaker(transcript)
        share = transcript.talk_share().get(rep, 0.0)
        if share <= REP_TALK_SHARE_LIMIT:
            return []
        return [
            Insight(
                kind="talk_ratio",
                title="Rep dominated the conversation",
                detail=f"{rep} spoke {share:.0%} of the words (limit {REP_TALK_SHARE_LIMIT:.0%}).",
                severity="medium",
            )
        ]


@register("analyzer", "objections")
class ObjectionAnalyzer(_Base):
    name = "objections"

    def analyze(self, transcript: Transcript) -> list[Insight]:
        rep = self.rep_speaker(transcript)
        prospect_segments = [s for s in transcript.segments if s.speaker != rep]
        insights: list[Insight] = []

        for kind, title, pattern in (
            ("pricing_objection", "Pricing or budget concern raised", _PRICING),
            ("timing_objection", "Timing or priority concern raised", _TIMING),
        ):
            hits = [s for s in prospect_segments if pattern.search(s.text)]
            if kind == "timing_objection":
                hits = [s for s in hits if not _is_polite_close(transcript, s)]
            if hits:
                insights.append(
                    Insight(
                        kind=kind,
                        title=title,
                        severity="medium",
                        evidence=[_evidence(s) for s in hits[:MAX_EVIDENCE]],
                    )
                )
        return insights


@register("analyzer", "competitors")
class CompetitorAnalyzer(_Base):
    name = "competitors"

    def analyze(self, transcript: Transcript) -> list[Insight]:
        insights: list[Insight] = []
        for competitor in self.competitors:
            pattern = _name_pattern(competitor)
            hits = [s for s in transcript.segments if pattern.search(s.text)]
            if hits:
                insights.append(
                    Insight(
                        kind="competitor_mention",
                        title=f"Competitor mentioned: {competitor}",
                        severity="medium",
                        evidence=[_evidence(s) for s in hits[:MAX_EVIDENCE]],
                    )
                )
        return insights


@register("analyzer", "next_step")
class NextStepAnalyzer(_Base):
    name = "next_step"

    def analyze(self, transcript: Transcript) -> list[Insight]:
        segments = transcript.segments
        closing_count = max(3, math.ceil(len(segments) * 0.25))
        closing = segments[-closing_count:]
        hits = [s for s in closing if _NEXT_STEP.search(s.text)]
        if hits:
            return [
                Insight(
                    kind="next_step",
                    title="Next step discussed near the end of the call",
                    evidence=[_evidence(s) for s in hits[:MAX_EVIDENCE]],
                )
            ]
        return [
            Insight(
                kind="no_next_step",
                title="No clear next step in the closing part of the call",
                detail="No scheduling or follow-up language found in the final segments.",
                severity="high",
                evidence=[_evidence(s) for s in closing[-2:]],
            )
        ]


@register("analyzer", "action_items")
class ActionItemAnalyzer(_Base):
    name = "action_items"

    def analyze(self, transcript: Transcript) -> list[Insight]:
        rep = self.rep_speaker(transcript)
        insights: list[Insight] = []
        for segment in transcript.segments:
            if segment.speaker != rep:
                continue
            match = _COMMITMENT.search(segment.text)
            if match:
                insights.append(
                    Insight(
                        kind="action_item",
                        title=match.group(0).strip(),
                        evidence=[_evidence(segment)],
                    )
                )
        return insights
