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

| Kind | Baseline precision | Baseline recall |
|---|---|---|
| pricing_objection | 67% | 40% |
| timing_objection | 33% | 25% |
| authority_objection | – (no rule) | 0% |
| need_objection | – (no rule) | 0% |
| competitor_mention | 100% | 100% |
| action_item | 89% | 89% |
| no_next_step | 43% | 75% |

Where the rules fail: objections without trigger words ("more than we can spend", "circle
back in March"), "price isn't the issue" read as a price objection, a polite "not right now"
read as a timing objection, and next steps booked without trigger words ("Thursday at 10").

`tests/test_llm_analyzer.py::test_baseline_scores_are_stable` pins the baseline counts, so a
change to the rules shows up as a test diff.
