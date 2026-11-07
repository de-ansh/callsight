"""Eval harness: precision/recall of each insight kind against labelled transcripts.

A labelled call is a normal text transcript where lines carry inline tags and a header holds
call-level facts (see evals/calls/):

    # competitors: Gong, Chorus
    # no_next_step: true
    [00:07] Tom: We use Gong today. It's a lot of money. {competitor_mention} {pricing_objection}

Segment kinds score per transcript line: a predicted line is the evidence an insight cites.
`no_next_step` scores per call.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from pathlib import Path

from .config import Settings
from .models import Transcript
from .providers.text_asr import TextTranscriptParser
from .service import analyze_transcript

SEGMENT_KINDS = (
    "pricing_objection", "timing_objection", "authority_objection", "need_objection",
    "competitor_mention", "action_item",
)
CALL_KINDS = ("no_next_step",)
_TAG = re.compile(r"\s*\{([a-z_]+)\}")


@dataclass
class LabelledCall:
    name: str
    transcript: Transcript
    competitors: tuple[str, ...]
    segment_labels: dict[str, set[int]]
    call_labels: dict[str, bool]


@dataclass
class Score:
    tp: int = 0
    fp: int = 0
    fn: int = 0
    errors: list[str] = field(default_factory=list)  # "call: FP line 3" for inspection

    @property
    def precision(self) -> float | None:
        return self.tp / (self.tp + self.fp) if self.tp + self.fp else None

    @property
    def recall(self) -> float | None:
        return self.tp / (self.tp + self.fn) if self.tp + self.fn else None


def load_call(path: Path) -> LabelledCall:
    header = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if m := re.match(r"#\s*([a-z_]+)\s*:\s*(.*)$", line):
            header[m.group(1)] = m.group(2).strip()
    unknown = set(header) - {"competitors", *CALL_KINDS}
    if unknown:
        raise ValueError(f"{path.name}: unknown header keys {sorted(unknown)}")
    body = "\n".join(l for l in path.read_text(encoding="utf-8").splitlines() if not l.startswith("#"))
    parsed = TextTranscriptParser().transcribe(body, path.stem)

    labels: dict[str, set[int]] = {k: set() for k in SEGMENT_KINDS}
    segments = []
    for seg in parsed.segments:
        for tag in _TAG.findall(seg.text):
            if tag not in labels:
                raise ValueError(f"{path.name}: unknown tag {{{tag}}}")
            labels[tag].add(seg.id)
        segments.append(replace(seg, text=_TAG.sub("", seg.text).strip()))
    call_labels = {k: header[k].lower() == "true" for k in CALL_KINDS if k in header}
    missing = set(CALL_KINDS) - set(call_labels)
    if missing:
        raise ValueError(f"{path.name}: missing header {sorted(missing)}")
    competitors = tuple(c.strip() for c in header.get("competitors", "").split(",") if c.strip())
    return LabelledCall(path.stem, Transcript(path.stem, segments), competitors, labels, call_labels)


def evaluate(calls: list[LabelledCall], settings: Settings) -> dict[str, Score]:
    scores = {k: Score() for k in (*SEGMENT_KINDS, *CALL_KINDS)}
    for call in calls:
        report = analyze_transcript(call.transcript, settings=settings, extra_competitors=call.competitors)
        predicted: dict[str, set[int]] = {k: set() for k in SEGMENT_KINDS}
        for insight in report.insights:
            if insight.kind in predicted:
                predicted[insight.kind].update(e.segment_id for e in insight.evidence)
        for kind in SEGMENT_KINDS:
            gold, got, s = call.segment_labels[kind], predicted[kind], scores[kind]
            s.tp += len(gold & got)
            s.fp += len(got - gold)
            s.fn += len(gold - got)
            s.errors += [f"{call.name}: FP line {i}" for i in sorted(got - gold)]
            s.errors += [f"{call.name}: FN line {i}" for i in sorted(gold - got)]
        kinds_found = {i.kind for i in report.insights}
        for kind in CALL_KINDS:
            gold, got, s = call.call_labels[kind], kind in kinds_found, scores[kind]
            s.tp += gold and got
            s.fp += got and not gold
            s.fn += gold and not got
            if got != gold:
                s.errors.append(f"{call.name}: {'FP' if got else 'FN'}")
    return scores


def load_dir(path: Path) -> list[LabelledCall]:
    calls = [load_call(p) for p in sorted(path.glob("*.txt"))]
    if not calls:
        raise ValueError(f"no labelled calls (*.txt) in {path}")
    return calls


def format_scores(scores: dict[str, Score], verbose: bool = False) -> str:
    def pct(v: float | None) -> str:
        return "   -" if v is None else f"{v:4.0%}"

    lines = [f"{'kind':<22} {'TP':>3} {'FP':>3} {'FN':>3}  prec  recall", "-" * 46]
    for kind, s in scores.items():
        lines.append(f"{kind:<22} {s.tp:>3} {s.fp:>3} {s.fn:>3}  {pct(s.precision)}  {pct(s.recall)}")
        if verbose:
            lines += [f"    {e}" for e in s.errors]
    return "\n".join(lines)
