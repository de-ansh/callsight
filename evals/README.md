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

The **seed set** has 10 calls and 31 labels. **Caveat:** the developer wrote these calls to
probe known weaknesses of keyword rules, so they are harder for the baseline than real calls
would be, and too few for the percentages to be precise. Treat them as a regression check and a
direction, not a benchmark. Replace them with labelled real calls once there are users.

| Kind | Rules: precision / recall | `llm_insights`, llama3.2:3b: precision / recall |
|---|---|---|
| pricing_objection | 67% / 40% | **75% / 60%** |
| timing_objection | 33% / 25% | **67% / 50%** |
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

### The hybrid (default when an LLM is configured)

`pipeline.DEFAULT_LLM_ANALYZERS`: rules for talk ratio, competitors and commitments; `llm_insights`
(which no longer extracts commitments) for objections and the next step. Same model and calls:

| Kind | Rules | LLM only | Hybrid |
|---|---|---|---|
| pricing_objection | 67% / 40% | 75% / 60% | **100% / 80%** |
| timing_objection | 33% / 25% | 67% / 50% | 67% / 50% |
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

Where the rules fail: objections without trigger words ("more than we can spend", "circle
back in March"), "price isn't the issue" read as a price objection, a polite "not right now"
read as a timing objection, and next steps booked without trigger words ("Thursday at 10").

`tests/test_llm_analyzer.py::test_baseline_scores_are_stable` pins the baseline counts, so a
change to the rules shows up as a test diff.
