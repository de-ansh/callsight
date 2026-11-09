from pathlib import Path

import pytest

from callsight.config import Settings
from callsight.models import Transcript
from callsight.pipeline import analyze, build_analyzers
from callsight.providers.text_asr import TextTranscriptParser
from callsight.registry import available, create, load_builtins
from callsight.service import analyze_text

SAMPLE = Path(__file__).parents[3] / "examples" / "sample_call.txt"


@pytest.fixture(autouse=True)
def _builtins():
    load_builtins()


def parse(text: str) -> Transcript:
    return TextTranscriptParser().transcribe(text, "t1")


def kinds(report):
    return [i.kind for i in report.insights]


# --- parsing -----------------------------------------------------------------


def test_parse_basic_and_timestamps():
    t = parse("[00:05] Alex: Hi there\n[01:02] Priya: Hello")
    assert [s.speaker for s in t.segments] == ["Alex", "Priya"]
    assert [s.start_s for s in t.segments] == [5.0, 62.0]
    assert t.duration_s == 62.0


def test_parse_hour_timestamps():
    t = parse("[1:02:03] Alex: Late in a long call")
    assert t.segments[0].start_s == 3723.0


def test_parse_continuation_lines_join_previous_turn():
    t = parse("[00:00] Alex: First part\nsecond part\n[00:10] Priya: Reply")
    assert t.segments[0].text == "First part second part"
    assert len(t.segments) == 2


def test_parse_rejects_text_without_transcript_lines():
    with pytest.raises(ValueError):
        parse("just some prose with no timestamps")


# --- analyzers ---------------------------------------------------------------


def test_talk_ratio_flags_dominant_rep():
    t = parse(
        "[00:00] Alex: " + "word " * 80 + "\n"
        "[00:30] Priya: ok\n"
    )
    report = analyze(t, build_analyzers(["talk_ratio"]))
    assert kinds(report) == ["talk_ratio"]


def test_talk_ratio_quiet_when_balanced():
    t = parse("[00:00] Alex: one two three\n[00:10] Priya: four five six")
    assert analyze(t, build_analyzers(["talk_ratio"])).insights == []


def test_pricing_objection_only_counts_prospect_speech():
    t = parse(
        "[00:00] Alex: Our pricing is very competitive.\n"
        "[00:10] Priya: Fine.\n"
    )
    assert "pricing_objection" not in kinds(analyze(t, build_analyzers(["objections"])))

    t = parse(
        "[00:00] Alex: Hello.\n"
        "[00:10] Priya: The price is too expensive for us.\n"
    )
    report = analyze(t, build_analyzers(["objections"]))
    insight = next(i for i in report.insights if i.kind == "pricing_objection")
    assert insight.evidence[0].segment_id == 1
    assert insight.evidence[0].speaker == "Priya"


def test_competitor_mention_is_case_insensitive_and_cited():
    t = parse("[00:00] Alex: Hi\n[00:10] Priya: We also looked at acmecall.")
    report = analyze(t, build_analyzers(["competitors"], competitors=("AcmeCall",)))
    insight = next(i for i in report.insights if i.kind == "competitor_mention")
    assert insight.title == "Competitor mentioned: AcmeCall"
    assert insight.evidence[0].segment_id == 1


def test_missing_next_step_is_high_severity():
    t = parse(
        "[00:00] Alex: Hi\n[00:10] Priya: Hello\n[00:20] Alex: Features.\n[00:30] Priya: Okay\n"
        "[00:40] Alex: Anything else?\n[00:50] Priya: No thanks\n"
    )
    insight = analyze(t, build_analyzers(["next_step"])).insights[0]
    assert insight.kind == "no_next_step"
    assert insight.severity == "high"


def test_next_step_detected_in_closing_segments():
    t = parse(
        "[00:00] Alex: Hi\n[00:10] Priya: Hello\n[00:20] Alex: Features.\n[00:30] Priya: Okay\n"
        "[00:40] Alex: Shall we schedule a follow-up for Tuesday?\n[00:50] Priya: Yes\n"
    )
    insight = analyze(t, build_analyzers(["next_step"])).insights[0]
    assert insight.kind == "next_step"


def test_action_items_come_from_the_rep_only():
    t = parse(
        "[00:00] Alex: I'll send the contract today.\n"
        "[00:10] Priya: I will review it internally.\n"
    )
    items = [i for i in analyze(t, build_analyzers(["action_items"])).insights]
    assert [i.title for i in items] == ["I'll send the contract today."]


def test_configured_rep_name_overrides_first_speaker():
    t = parse(
        "[00:00] Priya: Hi, my budget is tight.\n"
        "[00:10] Alex: I'll send pricing.\n"
    )
    report = analyze(t, build_analyzers(["objections", "action_items"], rep_names=("Alex",)))
    assert "pricing_objection" in kinds(report)
    assert "action_item" in kinds(report)


# --- pipeline ----------------------------------------------------------------


class FakeLLM:
    name = "fake"

    def complete(self, prompt: str) -> str:
        assert "Transcript:" in prompt
        return "  A short summary.  "


class BrokenLLM:
    name = "broken"

    def complete(self, prompt: str) -> str:
        raise RuntimeError("model offline")


def test_llm_summary_is_used_when_available():
    t = parse("[00:00] Alex: Hi\n[00:10] Priya: Hello")
    assert analyze(t, [], FakeLLM()).summary == "A short summary."


def test_llm_failure_falls_back_without_losing_insights():
    t = parse("[00:00] Alex: Hi\n[00:10] Priya: The price is too expensive.")
    report = analyze(t, build_analyzers(["objections"]), BrokenLLM())
    assert "pricing_objection" in kinds(report)
    assert "insight(s) found" in report.summary


def test_insights_sorted_by_severity():
    report = analyze_text(SAMPLE.read_text(), settings=Settings(competitors=("AcmeCall",)))
    ranks = {"high": 0, "medium": 1, "low": 2, "info": 3}
    order = [ranks[i.severity] for i in report.insights]
    assert order == sorted(order)


# --- registry / settings / end to end ---------------------------------------


def test_registry_lists_builtins_and_rejects_unknown():
    assert {"talk_ratio", "objections", "competitors", "next_step", "action_items"} <= set(available("analyzer"))
    assert "text" in available("asr") and "ollama" in available("llm")
    with pytest.raises(KeyError):
        create("analyzer", "does_not_exist")


def test_settings_from_env():
    s = Settings.from_env({"CALLSIGHT_COMPETITORS": "A, B ,", "CALLSIGHT_LLM_PROVIDER": "Ollama"})
    assert s.competitors == ("A", "B")
    assert s.llm_provider == "ollama"
    assert Settings.from_env({}).llm_provider == "none"


def test_sample_call_end_to_end():
    report = analyze_text(SAMPLE.read_text(), call_id="sample", settings=Settings(competitors=("AcmeCall",)))
    found = set(kinds(report))
    assert {"talk_ratio", "pricing_objection", "timing_objection", "competitor_mention",
            "no_next_step", "action_item"} <= found
    assert report.insights[0].severity == "high"
    assert report.to_dict()["call_id"] == "sample"


def test_competitor_matches_speech_to_text_spacing():
    t = parse("[00:00] Alex: Hi\n[00:05] Priya: The quote from ACME call was lower.\n[00:09] Priya: Acme calling back?")
    report = analyze(t, build_analyzers(["competitors"], competitors=("AcmeCall",)))
    insight = next(i for i in report.insights if i.kind == "competitor_mention")
    assert [e.segment_id for e in insight.evidence] == [1]  # "Acme calling" is not a mention


def test_polite_not_right_now_after_anything_else_is_not_a_timing_objection():
    t = parse("[00:00] Alex: Anything else I can help with?\n[00:03] Priya: Not right now, thanks.\n"
              "[00:06] Alex: Could we start next month?\n[00:09] Priya: Not right now, the budget is frozen until March.\n")
    insight = next(i for i in analyze(t, build_analyzers(["objections"])).insights if i.kind == "timing_objection")
    assert [e.segment_id for e in insight.evidence] == [3]  # the real objection, not the polite close


def test_concrete_times_count_as_next_steps():
    for line in ["Thursday at 3 works.", "Let's review it Monday at 4.", "Yes, the 20th at noon.", "March 10 is fine."]:
        t = parse(f"[00:00] Alex: Shall we meet again?\n[00:05] Priya: {line}\n[00:09] Alex: Great.")
        assert "no_next_step" not in kinds(analyze(t, build_analyzers(["next_step"]))), line


def test_a_declined_follow_up_is_not_a_next_step():
    t = parse("[00:00] Alex: Can I schedule a follow-up demo next week?\n"
              "[00:05] Priya: No, I don't think so.\n[00:09] Alex: Understood.")
    assert "no_next_step" in kinds(analyze(t, build_analyzers(["next_step"])))
    t = parse("[00:00] Alex: Can I schedule a follow-up for Friday?\n[00:05] Priya: No problem, Friday works.")
    assert "no_next_step" not in kinds(analyze(t, build_analyzers(["next_step"])))  # "no problem" is a yes


def test_commitments_need_a_concrete_action():
    t = parse("[00:00] Alex: I'll be honest, this was a tough quarter.\n"
              "[00:05] Alex: Let me tell you about our roadmap.\n"
              "[00:09] Alex: Let me get back to you by Friday with pricing.\n"
              "[00:14] Alex: I'm going to set up a sandbox for you.\n[00:20] Priya: Thanks.")
    items = [i for i in analyze(t, build_analyzers(["action_items"])).insights if i.kind == "action_item"]
    assert [i.evidence[0].segment_id for i in items] == [2, 3]
