# Roadmap

Rough six-week plan for v0.1. Dates are intentionally relative; adjust after each week.

## Week 1: skeleton (done)
- Repo layout, plugin interfaces, registry
- Text transcript parser, four baseline analyzers with evidence
- CLI, FastAPI wrapper, Docker Compose, tests

## Week 2: persistence and validation
- Write calls, segments and insights to Postgres; read endpoints (`GET /v1/calls`, `/v1/calls/{id}`)
- **Validate the market before going further:** check current pricing of commercial tools,
  survey existing open-source projects, and talk to a handful of target users
  (script, outreach and go/no-go criteria: [interview-kit.md](interview-kit.md))
- License: decided, Apache-2.0 (see [license-decision.md](license-decision.md))

## Week 3: audio (done)
- Upload endpoint storing recordings in MinIO
- Local ASR provider (for example a Whisper-family model) with speaker separation
- Background worker so long files do not block requests

## Week 4: model-powered analysis and evals (done; numbers in evals/README.md)
- LLM analyzers (objection types, sentiment shifts, commitment extraction) behind the same interface
- Labelled transcript set and an eval harness reporting precision/recall per analyzer
- Compare against the rule-based baseline; publish the numbers

## Week 5: integrations and UI (done, except a telephony/meeting connector)
- Connector interface; first connectors (generic webhook done; a meeting/telephony source needs a real account to build against)
- Web UI: call list, transcript view with click-to-jump evidence

## Week 6: hardening and release (done except the release)
- Auth, multi-tenant separation, audit log: done
- Configurable analyzer selection: done (`CALLSIGHT_ANALYZERS`). Rubric weights: not built, because
  there is no call score to weight yet; decide after user interviews whether teams want one
- Docs and contribution guide: done. Versioned release: v0.1.0

## Open questions
- Open-core boundary (what, if anything, goes in a commercial `ee/`)
- Which meeting/telephony sources to support first
- Languages beyond English
- Privacy and consent requirements for recording in target markets
