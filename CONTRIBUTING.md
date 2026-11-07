# Contributing

Thanks for helping. The bar for a change: it is tested, every insight still cites the transcript
lines that justify it, and the README states what it does and does not do.

**Before your first PR:** the project has not chosen its license yet
([docs/license-decision.md](docs/license-decision.md)), so outside contributions cannot be merged
until it has. Open an issue to discuss in the meantime.

## Set up

```bash
cd services/api
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"          # add ,asr for local Whisper transcription
python -m pytest                  # unit tests

# Full suite with Postgres + MinIO (needs Docker), exactly as CI runs it:
../../scripts/test-services.sh up && eval "$(../../scripts/test-services.sh env)"
python -m pytest
../../scripts/test-services.sh down
```

## Making changes

- **Analyzers** live in `callsight/analyzers/` and register with `@register("analyzer", "name")`.
  Every insight that makes a claim about the call must carry `Evidence` pointing at real segments.
  If you change detection, run `python -m callsight eval ../../evals/calls -v` and include the before
  and after numbers in the PR. `test_baseline_scores_are_stable` pins the rule-based counts on
  purpose: update it in the same PR and say why.
- **Eval data** goes in `evals/calls/` using the inline tag format described in `evals/README.md`.
  Real, consented, anonymized calls are worth more than more synthetic ones.
- **Schema changes** are a new numbered file in `callsight/migrations/`. Never edit one that has
  been released.
- **Every query on call data filters by `org_id`.** Tenancy has tests in `tests/test_store.py`; add a
  cross-org check for any new endpoint.
- **Untrusted input:** transcripts, uploaded files, webhook bodies and model output are untrusted.
  The UI renders call data with `textContent` only; keep it that way.

## Pull requests

Keep them small and focused, explain the why, and note anything you did not verify. CI must pass;
it fails if any test is skipped, so do not rely on skips.
