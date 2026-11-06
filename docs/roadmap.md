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
- Decide the license (AGPL-3.0 vs Apache-2.0 and open-core boundary): see [license-decision.md](license-decision.md)

## Week 3: audio
- Upload endpoint storing recordings in MinIO
- Local ASR provider (for example a Whisper-family model) with speaker separation
- Background worker so long files do not block requests

## Week 4: model-powered analysis and evals
- LLM analyzers (objection types, sentiment shifts, commitment extraction) behind the same interface
- Labelled transcript set and an eval harness reporting precision/recall per analyzer
- Compare against the rule-based baseline; publish the numbers

## Week 5: integrations and UI
- Connector interface; first connectors (generic webhook, one meeting/telephony source)
- Web UI: call list, transcript view with click-to-jump evidence

## Week 6: hardening and release
- Auth, multi-tenant separation, audit log
- Configurable analyzer selection and rubric weights
- Docs, versioned release, contribution guide

## Open questions
- License and open-core boundary
- Which meeting/telephony sources to support first
- Languages beyond English
- Privacy and consent requirements for recording in target markets
