"""LLM-powered analyzer: objection types, agreed next step, sentiment shifts.

Rep commitments are left to the rule-based action_items analyzer, which measured better
(evals/README.md).

The model sees numbered transcript lines and must cite line numbers. Its answer is treated
as untrusted: citations to lines that don't exist and objections attributed to the rep are
dropped, so every insight still points at real evidence. Setting CALLSIGHT_LLM_PROVIDER=ollama
turns it on as part of pipeline.DEFAULT_LLM_ANALYZERS.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ..interfaces import LLM
from ..models import Insight, Transcript
from ..registry import register
from .baseline import _Base, _evidence

log = logging.getLogger(__name__)

OBJECTION_TYPES = {
    "pricing": "Pricing or budget concern raised",
    "timing": "Timing or priority concern raised",
    "authority": "Decision needs someone else's approval",
    "need": "Prospect questions the need or value",
}
MAX_PROMPT_LINES = 400

# No concrete example values: small models copy example line numbers and booleans into their
# answers (measured: llama3.2:3b put a pricing objection on "line 3" in 7 of 10 calls).
PROMPT = """Below is a sales call transcript. Each line starts with its line number in [brackets].
The sales rep is "{rep}"; everyone else is the prospect.

Transcript:
{lines}

Read the whole transcript, then report only what is actually said in it:
- objections: concerns the PROSPECT raises that could block the deal. type is one of
  pricing (cost, budget, discounts), timing (not now, later, delays), authority (needs approval
  from someone else), need (doubts they need it or see the value). Do NOT count statements that
  something is NOT a concern, or a polite "no thanks" answering "anything else?".
- next_step_agreed: true only if both sides agreed a concrete next meeting or action with a time.
- sentiment_shifts: lines where the prospect's attitude clearly turns positive or negative.

Answer with one JSON object with exactly these keys:
- "objections": list of objects with "type" (pricing, timing, authority or need) and "line" (the line number)
- "next_step_agreed": true or false
- "next_step_line": the line number where the next step was agreed, or null
- "sentiment_shifts": list of objects with "line", "direction" (positive or negative) and "note"
Use empty lists when there is nothing to report. Every line number must be one shown in the transcript.
"""


def _lines(transcript: Transcript) -> str:
    return "\n".join(f"[{s.id}] {s.speaker}: {s.text}" for s in transcript.segments[:MAX_PROMPT_LINES])


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


@register("analyzer", "llm_insights")
class LLMInsightAnalyzer(_Base):
    name = "llm_insights"

    def __init__(self, *, llm: LLM | None = None, **options: Any) -> None:
        super().__init__(**options)
        if llm is None:
            raise ValueError("llm_insights needs an LLM: set CALLSIGHT_LLM_PROVIDER (e.g. ollama)")
        self.llm = llm

    def analyze(self, transcript: Transcript) -> list[Insight]:
        rep = self.rep_speaker(transcript)
        try:
            raw = self.llm.complete(PROMPT.format(rep=rep, lines=_lines(transcript)), json_mode=True)
            data = json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
            if not isinstance(data, dict):
                raise ValueError("model did not return a JSON object")
        except Exception:  # a failing or rambling model must not break the rule-based report
            log.exception("llm_insights failed for call %s", transcript.call_id)
            return []
        return self.to_insights(data, transcript, rep)

    @staticmethod
    def to_insights(data: dict[str, Any], transcript: Transcript, rep: str) -> list[Insight]:
        """Validate the model's answer against the transcript and turn it into insights."""
        by_id = {s.id: s for s in transcript.segments}
        insights: list[Insight] = []

        grouped: dict[str, list[int]] = {}
        for item in data.get("objections") or []:
            if not isinstance(item, dict):
                continue
            kind, line = item.get("type"), _int(item.get("line"))
            seg = by_id.get(line)
            if kind in OBJECTION_TYPES and seg and seg.speaker != rep and line not in grouped.get(kind, []):
                grouped.setdefault(kind, []).append(line)
        for kind, lines in grouped.items():
            insights.append(Insight(
                kind=f"{kind}_objection", title=OBJECTION_TYPES[kind], severity="medium",
                evidence=[_evidence(by_id[i]) for i in sorted(lines)],
            ))

        agreed = data.get("next_step_agreed") is True
        seg = by_id.get(_int(data.get("next_step_line")))
        if agreed and seg:
            insights.append(Insight(kind="next_step", title="Next step agreed", evidence=[_evidence(seg)]))
        elif not agreed and transcript.segments:
            insights.append(Insight(
                kind="no_next_step", title="No clear next step agreed", severity="high",
                detail="The model found no concrete, agreed next meeting or action.",
                evidence=[_evidence(s) for s in transcript.segments[-2:]],
            ))
        # agreed but without a valid line: no claim either way rather than an uncited one

        for item in data.get("sentiment_shifts") or []:
            seg = by_id.get(_int(item.get("line"))) if isinstance(item, dict) else None
            direction = item.get("direction") if isinstance(item, dict) else None
            if seg and seg.speaker != rep and direction in ("positive", "negative"):
                note = str(item.get("note") or "").strip()[:120]
                insights.append(Insight(
                    kind="sentiment_shift", title=f"Prospect turned {direction}",
                    detail=note, severity="low" if direction == "positive" else "medium",
                    evidence=[_evidence(seg)],
                ))
        return insights
