"""Background worker: transcribes and analyzes uploaded recordings. Run: python -m callsight.worker

Jobs are rows in `calls` with status 'queued'; several workers can run side by side
(claims use FOR UPDATE SKIP LOCKED).
"""

from __future__ import annotations

import logging
import tempfile
import time
from pathlib import PurePath

from . import recordings, store
from .config import Settings
from .registry import create, load_builtins
from .service import analyze_transcript

log = logging.getLogger("callsight.worker")
IDLE_SLEEP_S = 2.0


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
            path = f"{tmp}/recording{PurePath(job['recording_key']).suffix}"
            recordings.download(settings, job["recording_key"], path)
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


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings.from_env()
    if not settings.database_url or not settings.s3_endpoint:
        raise SystemExit("worker needs CALLSIGHT_DATABASE_URL and CALLSIGHT_S3_ENDPOINT")
    load_builtins()
    log.info("loading whisper model '%s'", settings.whisper_model)
    asr = create("asr", "whisper", model=settings.whisper_model)
    log.info("worker ready")
    while True:
        if not process_one(settings, asr):
            time.sleep(IDLE_SLEEP_S)


if __name__ == "__main__":
    main()
