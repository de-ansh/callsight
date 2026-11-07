"""Background worker: transcribes and analyzes uploaded recordings. Run: python -m callsight.worker

Jobs are rows in `calls` with status 'queued'; several workers can run side by side
(claims use FOR UPDATE SKIP LOCKED).
"""

from __future__ import annotations

import logging
import tempfile
import time
from pathlib import PurePath
from urllib.parse import urlsplit

from . import recordings, remote, retention, store
from .config import Settings
from .migrate import migrate
from .registry import create, load_builtins
from .service import analyze_transcript

log = logging.getLogger("callsight.worker")
IDLE_SLEEP_S = 2.0
PURGE_EVERY_S = 3600


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
        report = analyze_transcript(transcript, settings=settings)
        with store.connect(settings.database_url) as conn:
            store.complete_job(conn, call_id, transcript, report)
        log.info("call %s done: %d segments, %d insights", call_id, len(transcript.segments), len(report.insights))
    except Exception as exc:  # one bad recording must not stop the worker
        log.exception("call %s failed", call_id)
        with store.connect(settings.database_url) as conn:
            store.fail_job(conn, call_id, f"{type(exc).__name__}: {exc}")
    return True


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
    asr = create("asr", "whisper", model=settings.whisper_model)
    log.info("worker ready")
    next_purge = 0.0
    while True:
        if time.monotonic() >= next_purge:
            try:
                retention.purge_expired(settings)
            except Exception:  # a storage hiccup must not stop transcription
                log.exception("retention purge failed; retrying next round")
            next_purge = time.monotonic() + PURGE_EVERY_S
        if not process_one(settings, asr):
            time.sleep(IDLE_SLEEP_S)


if __name__ == "__main__":
    main()
