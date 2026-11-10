"""Core data models. Plain dataclasses, no third-party dependencies."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


def format_timestamp(seconds: float) -> str:
    """Render seconds as m:ss, or h:mm:ss for calls longer than an hour."""
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


@dataclass(frozen=True)
class Segment:
    id: int
    start_s: float
    speaker: str
    text: str


@dataclass
class Transcript:
    call_id: str
    segments: list[Segment]
    stereo: bool = False  # speakers come from stereo channels (Speaker 1 = left, Speaker 2 = right)

    @property
    def duration_s(self) -> float:
        """Approximate duration: the start time of the last segment."""
        return self.segments[-1].start_s if self.segments else 0.0

    @property
    def speakers(self) -> list[str]:
        seen: list[str] = []
        for seg in self.segments:
            if seg.speaker not in seen:
                seen.append(seg.speaker)
        return seen

    def talk_share(self) -> dict[str, float]:
        """Fraction of spoken words per speaker (sums to ~1.0)."""
        counts: dict[str, int] = {}
        for seg in self.segments:
            counts[seg.speaker] = counts.get(seg.speaker, 0) + len(seg.text.split())
        total = sum(counts.values())
        if total == 0:
            return {}
        return {speaker: round(n / total, 3) for speaker, n in counts.items()}


@dataclass(frozen=True)
class Evidence:
    """A pointer back to the exact transcript segment that supports an insight."""

    segment_id: int
    start_s: float
    speaker: str
    quote: str


@dataclass
class Insight:
    kind: str
    title: str
    detail: str = ""
    severity: str = "info"  # info | low | medium | high
    evidence: list[Evidence] = field(default_factory=list)


@dataclass
class CallReport:
    call_id: str
    duration_s: float
    speakers: list[str]
    talk_share: dict[str, float]
    summary: str
    insights: list[Insight]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
