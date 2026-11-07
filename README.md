# Callsight

Self-hostable, open-source conversation intelligence for sales and customer calls.
Feed it a call, get back a summary and flagged risks (pricing objections, competitor
mentions, no next step, rep talking too much), with every insight linked to the exact
transcript lines that support it.

> **Status: v0.1.0, early.** Text and audio calls, rule-based and local-LLM analysis, a web
> UI, a webhook connector, API keys with per-organization separation, and an audit log all
> work and are tested. Not yet: speaker separation for mono recordings, telephony/meeting
> connectors, rate limiting. See the [roadmap](docs/roadmap.md).

## Why

Conversation-intelligence tools are sold per seat and keep your recordings on someone
else's servers. The software around the models is the replaceable part. Callsight aims
to be a self-hosted alternative where you choose the models (local or API) and keep the data.

*Pricing and existing open-source projects have been surveyed; whether buyers want this has
not been validated with users yet ([interview kit](docs/interview-kit.md)).*

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
| Webhook connector (`POST /v1/orgs/{org}/connectors/generic/webhook`, HMAC-signed per org; transcript or https recording URL) | Working, tested (SSRF guard, signatures, worker fetch) |
| API keys, organizations (each key sees only its org's calls), audit log (`GET /v1/audit`) | Working, tested including cross-org access attempts |
| LLM analyzer `llm_insights` (objection types incl. authority/need, commitments, next step, sentiment shifts) with an eval harness | Working; see [evals/README.md](evals/README.md) for measured precision/recall |
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
cp .env.example .env          # then change the passwords and set CALLSIGHT_SECRET_KEY
docker compose up --build     # API on http://localhost:8000
docker compose --profile local-llm up   # also start Ollama (set CALLSIGHT_LLM_PROVIDER=ollama)

# Create an API key (the org is created if it does not exist; the key is shown once):
docker compose exec api python -m callsight.admin create-key --org acme --name alice
```

Open http://localhost:8000, enter the key, then upload recordings, paste transcripts and browse
calls. Clicking an insight's quote jumps to that line and plays the recording from there.

```bash
curl -s localhost:8000/v1/analyze -H "Authorization: Bearer $KEY" -H 'content-type: application/json' \
  -d '{"call_id":"demo","transcript":"[00:00] Alex: Hi\n[00:10] Priya: The price is too expensive."}'
```

### Access control

- **Organizations and keys.** Every API key belongs to one organization and can only see and
  create that organization's calls; another org's call returns 404. Keys are stored as
  SHA-256 hashes and can be revoked: `python -m callsight.admin revoke-key --org acme --name alice`.
- **Audit log.** Creating or reading a call, playing its recording and creating keys are
  recorded with who did it: `GET /v1/audit` (or *Audit log* in the UI).
- **Recordings** are played through short-lived signed links (6 hours) because audio players
  cannot send an API key. Set `CALLSIGHT_SECRET_KEY` so links survive restarts and work across
  API replicas.
- **Deleting data.** `DELETE /v1/calls/{id}` (or *Delete call* in the UI) removes the call, its
  transcript, insights and recording from storage. `CALLSIGHT_RETENTION_DAYS=90` makes the worker
  delete calls older than 90 days, checked hourly. The audit log keeps a record that a deletion
  happened, but not the deleted content.
- Without a database, `POST /v1/analyze` is a stateless open endpoint (nothing is stored).

### Sending calls from another system (webhook)

Each organization has its own webhook secret and URL:
`python -m callsight.admin webhook-secret --org acme` prints both. POST JSON signed with
HMAC-SHA256 of the raw body:

```bash
BODY='{"call_id":"crm-123","recording_url":"https://files.example.com/calls/123.mp3"}'
SIG=$(printf %s "$BODY" | openssl dgst -sha256 -hmac "$WEBHOOK_SECRET" | sed 's/^.* //')
curl -s "localhost:8000/v1/orgs/$ORG_ID/connectors/generic/webhook" -H "X-Callsight-Signature: sha256=$SIG" -d "$BODY"
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
  providers/       text transcript parser, Whisper (audio), Ollama LLM
  analyzers/       rule-based baseline analyzers, LLM analyzer (llm_insights)
  pipeline.py      transcript -> analyzers -> optional LLM summary -> report
  service.py       entry point shared by CLI, API and worker
  store.py         Postgres persistence, job queue, audit log (all queries scoped by org)
  migrations/      numbered SQL migrations, applied at startup
  auth.py          API keys, orgs, signed recording links
  connectors.py    inbound webhook connectors
  worker.py        background transcription/analysis of recordings
  evals.py         precision/recall harness (python -m callsight eval)
  static/          the web UI (one HTML file, no build step)
  cli.py, api.py, admin.py   front-ends
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

Turn it on with `CALLSIGHT_ANALYZERS=talk_ratio,objections,competitors,next_step,action_items,my_check`,
or add it to `DEFAULT_ANALYZERS` in `pipeline.py`.

## Known limitations

- The baseline analyzers are keyword heuristics. They produce false positives: on the
  sample call, "Not right now, thanks." (a reply to "anything else?") is also flagged as
  a timing concern. Measuring and improving this is what the planned eval harness is for.
- Speaker roles are guessed (first speaker, or `CALLSIGHT_REP_NAMES`).
- Duration is approximate (start of the last segment).
- Only English-style keyword patterns are included.

## License

[Apache License 2.0](LICENSE). Third-party models you download (Whisper weights, Ollama
models) keep their own licenses. Why Apache: [docs/license-decision.md](docs/license-decision.md).
