# Callsight

Self-hostable, open-source conversation intelligence for sales and customer calls.
Feed it a call, get back a summary and flagged risks (pricing objections, competitor
mentions, no next step, rep talking too much), with every insight linked to the exact
transcript lines that support it.

> **Status: early skeleton (v0.1).** The analysis core and Postgres persistence work and
> are tested. Audio transcription, connectors and the UI do not exist yet. See the
> [roadmap](docs/roadmap.md).

## Why

Conversation-intelligence tools are sold per seat and keep your recordings on someone
else's servers. The software around the models is the replaceable part. Callsight aims
to be a self-hosted alternative where you choose the models (local or API) and keep the data.

*The market case is not yet validated: pricing, existing open-source projects and buyer
pain still need to be checked before building past week 2 of the roadmap.*

## What works today

| Capability | State |
|---|---|
| Parse text transcripts (`[mm:ss] Speaker: text`) | Working, tested |
| Rule-based analyzers: talk ratio, pricing/timing objections, competitor mentions, next step, action items | Working, tested |
| Evidence links (segment id, timestamp, speaker, quote) on every insight | Working, tested |
| Optional LLM summary via local Ollama | Written, **not yet run against a live Ollama** |
| CLI and HTTP API (`/health`, `POST /v1/analyze`, `GET /v1/calls`, `GET /v1/calls/{id}`) | Working, tested |
| Postgres persistence (calls, segments, insights with evidence) when `CALLSIGHT_DATABASE_URL` is set | Working, tested against Postgres 16 (`CALLSIGHT_TEST_DATABASE_URL=... pytest`) |
| Audio: `POST /v1/calls/upload` → MinIO → background worker (faster-whisper, speakers split by stereo channel) → stored report | Working, tested: full stack verified with a real 2-min stereo call (about 50 s to transcribe on CPU in Docker) |
| `docker-compose.yml` (Postgres+pgvector, MinIO, API, worker, optional Ollama) | `db`, `minio`, `api` and `worker` built and run. MinIO is Chainguard's build (`minio/minio` is no longer published), pinned by digest |
| Web UI at `/`: upload, paste, call list, transcript with click-to-jump evidence and audio seek | Working; checked in headless Chrome against real calls |
| Webhook connector (`POST /v1/connectors/generic/webhook`, HMAC-signed; transcript or https recording URL) | Working, tested (SSRF guard, signatures, worker fetch) |
| Mono-recording speaker separation, telephony/meeting connectors, auth | Not started |

## Try it (no dependencies beyond Python 3.10+)

```bash
cd services/api
PYTHONPATH=src python -m callsight analyze ../../examples/sample_call.txt --competitor AcmeCall
PYTHONPATH=src python -m callsight analyze ../../examples/sample_call.txt --json
```

Run the tests:

```bash
cd services/api
pip install -e ".[dev]"
python -m pytest              # unit tests; Postgres/MinIO tests are skipped

# Full suite, as CI runs it (needs Docker):
../../scripts/test-services.sh up && eval "$(../../scripts/test-services.sh env)"
python -m pytest
../../scripts/test-services.sh down
```

## Run the stack

```bash
cp .env.example .env          # then change the passwords
docker compose up --build     # API on http://localhost:8000
docker compose --profile local-llm up   # also start Ollama (set CALLSIGHT_LLM_PROVIDER=ollama)
```

Open http://localhost:8000 to upload recordings, paste transcripts and browse calls.
Clicking an insight's quote jumps to that line and plays the recording from there.

```bash
curl -s localhost:8000/v1/analyze -H 'content-type: application/json' \
  -d '{"call_id":"demo","transcript":"[00:00] Alex: Hi\n[00:10] Priya: The price is too expensive."}'
```

### Sending calls from another system (webhook)

Set `CALLSIGHT_WEBHOOK_SECRET`, then POST JSON signed with HMAC-SHA256 of the raw body:

```bash
BODY='{"call_id":"crm-123","recording_url":"https://files.example.com/calls/123.mp3"}'
SIG=$(printf %s "$BODY" | openssl dgst -sha256 -hmac "$CALLSIGHT_WEBHOOK_SECRET" | sed 's/^.* //')
curl -s localhost:8000/v1/connectors/generic/webhook -H "X-Callsight-Signature: sha256=$SIG" -d "$BODY"
```

Send `transcript` instead of `recording_url` to analyze text right away. Recording URLs must
be https and must resolve to public addresses; the worker downloads them into storage.
New connectors implement `parse(body, headers)` and register under kind `connector`
(see `callsight/connectors.py`).

## How it is organised

```
services/api/src/callsight/
  models.py        Transcript, Segment, Insight, Evidence, CallReport
  interfaces.py    ASR / LLM / Analyzer protocols (the plugin contracts)
  registry.py      register("analyzer", "name") plugin registry
  providers/       text transcript parser, Ollama LLM
  analyzers/       rule-based baseline analyzers
  pipeline.py      transcript -> analyzers -> optional LLM summary -> report
  service.py       entry point shared by CLI and API
  cli.py, api.py   thin front-ends
```

See [docs/architecture.md](docs/architecture.md) for the diagram and design decisions.

## Writing an analyzer

```python
from callsight.models import Insight, Transcript
from callsight.registry import register

@register("analyzer", "my_check")
class MyCheck:
    name = "my_check"

    def __init__(self, **_): ...

    def analyze(self, transcript: Transcript) -> list[Insight]:
        ...
```

Add it to `DEFAULT_ANALYZERS` in `pipeline.py` (configurable selection is on the roadmap).

## Known limitations

- The baseline analyzers are keyword heuristics. They produce false positives: on the
  sample call, "Not right now, thanks." (a reply to "anything else?") is also flagged as
  a timing concern. Measuring and improving this is what the planned eval harness is for.
- Speaker roles are guessed (first speaker, or `CALLSIGHT_REP_NAMES`).
- Duration is approximate (start of the last segment).
- Only English-style keyword patterns are included.

## License

Not chosen yet (AGPL-3.0 vs Apache-2.0 is an open decision; see the roadmap). No license
file is included, so nothing is licensed for reuse until one is added.
