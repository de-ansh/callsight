# Roadmap

## Done (v0.1–v0.7)

- Text and audio calls: upload to MinIO, a background worker with faster-whisper, stereo channels
  as speakers, opt-in speaker separation for mono recordings (pyannote).
- Analysis: rule-based analyzers and an optional local LLM (Ollama), every insight linked to the
  transcript lines behind it. A 30-call labelled eval set with published precision and recall
  ([evals/README.md](../evals/README.md)).
- Web UI: call list, transcript with click-to-jump evidence and audio seek, Trends, re-analysis,
  CSV export, printable summaries, opt-in search by meaning.
- Integrations: a signed generic webhook (transcript or recording URL).
- Operations: API keys and organizations, single sign-on (OIDC) by allowed email domain, admin
  roles, an audit log, retention, a production compose file with backups.

## Next

- **Measure on real calls.** Every number so far comes from synthetic calls written to probe known
  weaknesses. Labelled real calls (consented and anonymized) are the most useful contribution.
- **A meeting or telephony connector** (Zoom, Google Meet, or a dialer), so calls arrive without
  uploads. Needs an account to build and test against.
- **Analysis quality:** pricing recall (70%) and authority objections (75%) are the weakest numbers
  with the default 8B model; "doubts the need" objections are not reported because no model tried
  so far gets them right often enough.

## Later, if users ask

- A call score with adjustable rubric weights. Held back until the detectors it would combine are
  proven on real calls.
- Languages beyond English: Whisper and the LLM already handle many languages; the rules don't.
- An approximate-nearest-neighbour index for search by meaning, once organizations pass about
  100,000 transcript lines.

## Open questions

- Open-core boundary (what, if anything, goes in a commercial `ee/`).
- Which meeting/telephony sources to support first.
- Privacy and consent requirements for recording in target markets.
