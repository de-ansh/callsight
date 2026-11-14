"""Background worker: transcribes and analyzes uploaded recordings. Run: python -m callsight.worker

Jobs are rows in `calls` with status 'queued'; several workers can run side by side
(claims use FOR UPDATE SKIP LOCKED).
"""

from __future__ import annotations

import logging
from dataclasses import replace
import tempfile
import time
from pathlib import PurePath
from urllib.parse import urlsplit

from . import recordings, remote, retention, store
from .config import Settings
from .models import Transcript
from .migrate import migrate
from .providers.ollama_llm import ollama_embed
from .registry import create, load_builtins
from .service import analyze_transcript

log = logging.getLogger("callsight.worker")
IDLE_SLEEP_S = 2.0
PURGE_EVERY_S = 3600
EMBED_BATCH = 64
EMBED_RETRY_S = 60  # wait this long after the embedding server fails before trying again


def process_one(settings: Settings, asr) -> bool:
    """Process the next job if there is one; returns False when the queue is empty."""
    # Connections are opened only around DB work: transcription can take many minutes.
    with store.connect(settings.database_url) as conn:
        job = store.claim_job(conn)
    if job is None:
        return False
    call_id = job["id"]
    log.info("processing call %s", call_id)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            if job["recording_key"]:
                path = f"{tmp}/recording{PurePath(job['recording_key']).suffix}"
                recordings.download(settings, job["recording_key"], path)
            else:  # handed over by a connector as a URL: fetch it into storage first
                path = _fetch_into_storage(settings, call_id, job["recording_url"], tmp)
            transcript = asr.transcribe(path, job["external_id"])
        transcript, rep = name_speakers(transcript, job.get("speaker_names"), job.get("rep_speaker"))
        with store.connect(settings.database_url) as conn:
            competitors = store.org_competitors(conn, job["org_id"])
        report = analyze_transcript(transcript, settings=settings, extra_competitors=competitors, rep_speaker=rep)
        with store.connect(settings.database_url) as conn:
            store.complete_job(conn, call_id, transcript, report, rep)
        log.info("call %s done: %d segments, %d insights", call_id, len(transcript.segments), len(report.insights))
    except Exception as exc:  # one bad recording must not stop the worker
        log.exception("call %s failed", call_id)
        with store.connect(settings.database_url) as conn:
            store.fail_job(conn, call_id, f"{type(exc).__name__}: {exc}")
    return True


def embed_one(settings: Settings) -> bool:
    """Embed the lines of one call that semantic search can't see yet; False when there is none."""
    model = settings.embed_model
    with store.connect(settings.database_url) as conn:
        call_id = store.call_to_embed(conn, model)
        rows = store.segments_to_embed(conn, call_id, model) if call_id else []
    if not rows:
        return False
    for start in range(0, len(rows), EMBED_BATCH):
        batch = rows[start:start + EMBED_BATCH]
        texts = [r["text"] or "-" for r in batch]  # an empty line must not stall the queue
        vectors = ollama_embed(settings.ollama_url, model, texts)
        with store.connect(settings.database_url) as conn:
            store.save_embeddings(conn, call_id, model, [(r["idx"], v) for r, v in zip(batch, vectors)])
    log.info("call %s: embedded %d lines for search", call_id, len(rows))
    return True


def name_speakers(transcript: Transcript, names: dict[str, str] | None, rep: str | None
                  ) -> tuple[Transcript, str | None]:
    """Apply names given at upload. Only for stereo recordings: there the channels say who is who,
    while in mono "Speaker 1" is just whoever spoke first."""
    if not names or not getattr(transcript, "stereo", False):
        return transcript, rep if rep in transcript.speakers else None
    segments = [replace(s, speaker=names.get(s.speaker, s.speaker)) for s in transcript.segments]
    renamed = Transcript(transcript.call_id, segments, stereo=True)
    return renamed, rep if rep in renamed.speakers else None


def _fetch_into_storage(settings: Settings, call_id, url: str, tmp: str) -> str:
    ext = PurePath(urlsplit(url).path).suffix.lower()
    if ext not in recordings.AUDIO_TYPES:
        raise ValueError(f"recording_url must end in one of {sorted(recordings.AUDIO_TYPES)}")
    path = f"{tmp}/recording{ext}"
    remote.fetch(url, path)
    key = f"{call_id}/recording{ext}"
    recordings.upload_file(settings, key, path, recordings.AUDIO_TYPES[ext])
    with store.connect(settings.database_url) as conn:
        store.set_recording_key(conn, call_id, key)  # retries and playback use the stored copy
    return path


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings.from_env()
    if not settings.database_url or not settings.s3_endpoint:
        raise SystemExit("worker needs CALLSIGHT_DATABASE_URL and CALLSIGHT_S3_ENDPOINT")
    migrate(settings.database_url)
    load_builtins()
    log.info("loading whisper model '%s'", settings.whisper_model)
    asr = create("asr", "whisper", model=settings.whisper_model, diarization=settings.diarization,
                 diarization_model=settings.diarization_model, hf_token=settings.hf_token)
    if settings.diarization:
        log.info("speaker diarization for mono recordings: %s (%s)", settings.diarization, settings.diarization_model)
    log.info("worker ready")
    if settings.embed_model:
        log.info("semantic search: embedding transcripts with '%s' via %s", settings.embed_model, settings.ollama_url)
    next_purge = next_embed = 0.0
    while True:
        if time.monotonic() >= next_purge:
            try:
                retention.purge_expired(settings)
            except Exception:  # a storage hiccup must not stop transcription
                log.exception("retention purge failed; retrying next round")
            next_purge = time.monotonic() + PURGE_EVERY_S
        if process_one(settings, asr):
            continue
        if settings.embed_model and time.monotonic() >= next_embed:  # transcription goes first
            try:
                if embed_one(settings):
                    continue
            except Exception:  # Ollama down or the model not pulled: transcription must carry on
                log.exception("embedding for search failed; retrying in %ds", EMBED_RETRY_S)
                next_embed = time.monotonic() + EMBED_RETRY_S
        time.sleep(IDLE_SLEEP_S)


if __name__ == "__main__":
    main()
