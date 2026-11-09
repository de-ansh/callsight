import json
from pathlib import Path

import pytest

from callsight.analyzers.llm import LLMInsightAnalyzer
from callsight.evals import evaluate, load_call, load_dir
from callsight.config import Settings
from callsight.providers.text_asr import TextTranscriptParser
from callsight.registry import load_builtins

EVALS = Path(__file__).parents[3] / "evals" / "calls"
CALL = TextTranscriptParser().transcribe(
    "[00:00] Alex: Hi Priya.\n"
    "[00:05] Priya: That's more than we can spend.\n"
    "[00:10] Alex: I'll send a smaller quote today.\n"
    "[00:15] Priya: Thursday at 10 works.\n",
    "c1",
)


class FakeLLM:
    def __init__(self, answer):
        self.answer = answer
        self.prompts = []

    def complete(self, prompt, *, json_mode=False):
        self.prompts.append((prompt, json_mode))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer if isinstance(self.answer, str) else json.dumps(self.answer)


def run(answer):
    llm = FakeLLM(answer)
    return LLMInsightAnalyzer(llm=llm).analyze(CALL), llm


def test_valid_answer_becomes_cited_insights():
    insights, llm = run({
        "objections": [{"type": "pricing", "line": 1}],
        "commitments": [{"line": 2, "text": "ignored: commitments come from the rules"}],
        "next_step_agreed": True, "next_step_line": 3,
        "sentiment_shifts": [{"line": 3, "direction": "positive", "note": "agrees to meet"}],
    })
    assert [(i.kind, [e.segment_id for e in i.evidence]) for i in insights] == [
        ("pricing_objection", [1]), ("next_step", [3]), ("sentiment_shift", [3]),
    ]
    prompt, json_mode = llm.prompts[0]
    assert json_mode and '"Alex"' in prompt and "[1] Priya: That's more than we can spend." in prompt


def test_answers_that_do_not_match_the_transcript_are_dropped():
    insights, _ = run({
        "objections": [
            {"type": "pricing", "line": 99},      # no such line
            {"type": "pricing", "line": 0},       # the rep's own line
            {"type": "made_up", "line": 1},       # unknown type
            {"type": "timing", "line": "1"},      # line must be an int
            "garbage",
        ],
        "next_step_agreed": True, "next_step_line": 42,  # agreed but uncited: no claim either way
        "sentiment_shifts": [{"line": 1, "direction": "sideways"}],
    })
    assert insights == []


def test_no_agreed_next_step_is_reported_with_closing_lines():
    insights, _ = run({"next_step_agreed": False})
    assert [i.kind for i in insights] == ["no_next_step"]
    assert [e.segment_id for e in insights[0].evidence] == [2, 3]


@pytest.mark.parametrize("answer", ["not json at all", "[1, 2]", RuntimeError("model down")])
def test_broken_model_output_yields_no_insights_instead_of_crashing(answer):
    insights, _ = run(answer)
    assert insights == []


def test_json_wrapped_in_chatter_is_accepted():
    insights, _ = run('Sure! Here it is: {"objections": [{"type": "need", "line": 1}], "next_step_agreed": false} Hope that helps.')
    assert [i.kind for i in insights] == ["need_objection", "no_next_step"]


def test_llm_analyzer_requires_an_llm():
    with pytest.raises(ValueError, match="CALLSIGHT_LLM_PROVIDER"):
        LLMInsightAnalyzer()


# --- eval harness ---------------------------------------------------------------------------


def test_every_labelled_call_loads_with_known_tags():
    calls = load_dir(EVALS)
    assert len(calls) >= 30
    assert all("{" not in s.text for c in calls for s in c.transcript.segments)  # tags stripped


def test_tags_map_to_segment_ids(tmp_path):
    p = tmp_path / "x.txt"
    p.write_text("# no_next_step: true\n[00:00] A: Hi\n[00:05] B: Too pricey. {pricing_objection}\n")
    call = load_call(p)
    assert call.segment_labels["pricing_objection"] == {1}
    assert call.call_labels == {"no_next_step": True}
    p.write_text("# no_next_step: true\n[00:00] A: Hi {typo_tag}\n")
    with pytest.raises(ValueError, match="unknown tag"):
        load_call(p)


def test_baseline_scores_are_stable():
    """Pins the rule-based numbers so a change to an analyzer shows up as a diff here."""
    load_builtins()
    s = evaluate(load_dir(EVALS), Settings())
    assert (s["pricing_objection"].tp, s["pricing_objection"].fp, s["pricing_objection"].fn) == (6, 3, 4)
    assert (s["competitor_mention"].tp, s["competitor_mention"].fp, s["competitor_mention"].fn) == (6, 0, 0)
    assert (s["timing_objection"].tp, s["timing_objection"].fp, s["timing_objection"].fn) == (3, 0, 6)
    assert (s["action_item"].tp, s["action_item"].fp, s["action_item"].fn) == (18, 0, 1)
    assert (s["no_next_step"].tp, s["no_next_step"].fp, s["no_next_step"].fn) == (13, 2, 0)


def test_hybrid_is_the_default_when_an_llm_is_configured(monkeypatch):
    """With an LLM: rules for talk ratio, competitors and commitments; the model for objections and next steps."""
    from callsight import service
    from callsight.pipeline import DEFAULT_ANALYZERS, DEFAULT_LLM_ANALYZERS

    seen = {}
    monkeypatch.setattr(service, "create", lambda kind, name, **kw: FakeLLM({"next_step_agreed": False}))
    monkeypatch.setattr(service, "build_analyzers", lambda names, **kw: seen.setdefault("names", names) and [])
    service.analyze_transcript(CALL, settings=Settings(llm_provider="ollama"))
    assert seen["names"] == DEFAULT_LLM_ANALYZERS and "llm_insights" in DEFAULT_LLM_ANALYZERS
    seen.clear()
    service.analyze_transcript(CALL, settings=Settings())
    assert seen["names"] == DEFAULT_ANALYZERS and "llm_insights" not in DEFAULT_ANALYZERS
