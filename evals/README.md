# Evals

How well each analyzer finds what it claims to find, measured on labelled transcripts.

```bash
cd services/api
PYTHONPATH=src python -m callsight eval ../../evals/calls -v                 # rule-based baseline
CALLSIGHT_OLLAMA_URL=http://localhost:11434 PYTHONPATH=src \
  python -m callsight eval ../../evals/calls --llm ollama --analyzers llm_insights,objections -v
```

`-v` lists every false positive and negative by file and line.

## Labelling format

Each file in `calls/` is an ordinary text transcript. A tag at the end of a line marks what
that line is; the header holds call-level facts.

```
# competitors: Gong, Chorus
# no_next_step: true
[00:07] Tom: We use Gong today. It's a lot of money for what we use. {competitor_mention} {pricing_objection}
```

| Tag | Meaning |
|---|---|
| `{pricing_objection}` `{timing_objection}` `{authority_objection}` `{need_objection}` | The prospect raises that concern on this line |
| `{competitor_mention}` | A configured competitor is named |
| `{action_item}` | The **rep** commits to doing something concrete |
| `# no_next_step: true/false` | The call ends without an agreed, concrete next step |

Scoring: a line an insight cites as evidence counts as predicted. Precision = correct
predictions / all predictions; recall = labelled lines found / all labelled lines.

## Results

### Rules, on all 30 calls (52 labelled lines)

| Kind | Rules: precision / recall |
|---|---|
| pricing_objection | 67% / 60% |
| timing_objection | 100% / 33% |
| authority_objection | – / 0% (no rule) |
| need_objection | – / 0% (no rule) |
| competitor_mention | 100% / 100% |
| action_item | 100% / 95% |
| no_next_step | 87% / 100% |

**Caveat, read before quoting these:** every call was written by the developer to probe known
weaknesses, and the next-step and commitment rules were then **tuned on this same set**
(concrete times like "Monday at 4", declined follow-ups, commitment verbs). Before tuning they
scored 52% / 85% (next step) and 84% / 84% (commitments), so part of the gain is
overfitting. Treat these numbers as a regression check, not a benchmark. Replace or extend the set
with labelled real calls (calls 11–30 were added after the rules below were first written; 1–10
are the original seed set).

### Rules vs LLM vs the hybrid default, on all 30 calls

llama3.2:3b in Docker on CPU. "Hybrid" is the default when an LLM is configured
(`pipeline.DEFAULT_LLM_ANALYZERS`): rules for talk ratio, competitors, commitments and the next
step; the model for pricing, timing and authority objections.

| Kind | Rules | LLM alone (first prompt) | Hybrid (final) |
|---|---|---|---|
| pricing_objection | 67% / 60% | 89% / 80% | 67% / **80%** |
| timing_objection | **100%** / 33% | 62% / 56% | 80% / 44% |
| authority_objection | – / 0% | 67% / 50% | 60% / **75%** |
| need_objection | – / 0% | 25% / 25% | not reported (11% precision) |
| competitor_mention | **100% / 100%** | – | **100% / 100%** |
| action_item | **100% / 95%** | – | **100% / 95%** |
| no_next_step | **87% / 100%** | 59% / 100% | **87% / 100%** |

What changed between the two LLM columns: the model stopped judging the next step (the rules
were better: 87% vs 59% precision; the model missed explicit bookings like "Thursday at 10,
booked"). Removing that part of the prompt also shifted the 3B model's objection answers
(pricing precision 89% → 67%, authority recall 50% → 75%), with no change to the objection
instructions. **A 3B model is noisy: any prompt change moves every number, so rerun the eval
after each one, and prefer a larger model when the hardware allows.** "Need" objections
scored 11–25% precision across runs, so they are no longer reported.

### LLM vs rules, on the original 10 calls (earlier runs)

| Kind | Rules (then) | `llm_insights`, llama3.2:3b |
|---|---|---|
| pricing_objection | 67% / 40% | **75% / 60%** |
| timing_objection | 100% / 25% | **67% / 50%** |
| authority_objection | – / 0% (no rule) | **100% / 100%** (1 label) |
| need_objection | – / 0% (no rule) | 25% / 50% |
| competitor_mention | **100% / 100%** | – (not its job) |
| action_item | **89% / 89%** | 38% / 67% |
| no_next_step | 43% / 75% | **100% / 100%** |

LLM run: Ollama in Docker on CPU (Apple silicon), temperature 0, JSON mode, about 30 s per call.
llama3.2:3b, not the default llama3.1 8B, because the 8B model did not fit in the local Docker
disk; rerun with a larger model before drawing firm conclusions.

**Reading it:** the model is better at objections phrased without keywords and at telling
whether a next step was really agreed; the rules are better at rep commitments and exact
competitor names. That suggests a hybrid (rules for `competitor_mention` and `action_item`, the
model for objections and next steps), not a replacement.

### The first hybrid, on the original 10 calls (superseded by the 30-call table above)

`pipeline.DEFAULT_LLM_ANALYZERS`: rules for talk ratio, competitors and commitments; `llm_insights`
(which no longer extracts commitments) for objections and the next step. Same model and calls:

| Kind | Rules | LLM only | Hybrid |
|---|---|---|---|
| pricing_objection | 67% / 40% | 75% / 60% | **100% / 80%** |
| timing_objection | 100% / 25% | 67% / 50% | 67% / 50% |
| authority_objection | – / 0% | 100% / 100% | – / 0% |
| need_objection | – / 0% | 25% / 50% | 33% / 50% |
| competitor_mention | 100% / 100% | – | **100% / 100%** |
| action_item | 89% / 89% | 38% / 67% | **89% / 89%** |
| no_next_step | 43% / 75% | 100% / 100% | 57% / 100% |

The hybrid keeps the rules' strengths and adds the model's objection recall. Note the
authority and next-step rows: the logic for them did not change, yet removing commitments from
the prompt shifted the 3B model's answers on them. With one authority label and ten calls that
is within noise, which is the main limit of this seed set. Small prompt changes move small
models, so rerun the eval after every prompt change.

**Prompt finding:** the first prompt contained a JSON example with concrete values
(`"line": 3`, `"next_step_agreed": false`). The 3B model copied them: it reported a pricing
objection on line 3 in 7 of 10 calls and "no next step" on every call (pricing precision 22%).
The prompt now describes the answer format without example values. Check for this pattern
(the same line number recurring across calls in `-v` output) whenever the prompt changes.

The rules' timing precision was 33% until a polite "Not right now, thanks." answering "anything
else?" stopped counting as an objection (both false positives were that pattern).

Where the rules fail: objections without trigger words ("more than we can spend", "circle
back in March"), "price isn't the issue" read as a price objection, and next steps booked without trigger words ("Thursday at 10").

`tests/test_llm_analyzer.py::test_baseline_scores_are_stable` pins the baseline counts, so a
change to the rules shows up as a test diff.
