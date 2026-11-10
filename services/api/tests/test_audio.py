"""Audio path: channel merge, upload validation, and upload -> worker -> stored report.

The end-to-end test needs CALLSIGHT_TEST_DATABASE_URL and CALLSIGHT_TEST_S3_ENDPOINT
(MinIO, credentials in CALLSIGHT_TEST_S3_ACCESS_KEY / _SECRET_KEY). It swaps Whisper for a
fake ASR so it stays fast; real Whisper was checked by hand on a stereo rendering of
examples/sample_call.txt (macOS `say`, one voice per channel).
"""

import io
import os

import pytest

from callsight.models import Segment, Transcript
from callsight.providers.whisper_asr import merge_channels


def test_merge_channels_interleaves_by_time_and_drops_blank_text():
    merged = merge_channels([
        ("Speaker 1", [(0.0, " Hi there."), (9.5, "Price is fine."), (12.0, "  ")]),
        ("Speaker 2", [(4.2, "Hello!")]),
    ])
    assert merged == [
        Segment(0, 0.0, "Speaker 1", "Hi there."),
        Segment(1, 4.2, "Speaker 2", "Hello!"),
        Segment(2, 9.5, "Speaker 1", "Price is fine."),
    ]


pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from callsight import worker  # noqa: E402
from callsight.api import app  # noqa: E402
from callsight.config import Settings  # noqa: E402

anon = TestClient(app)


@pytest.fixture
def dummy_backends(monkeypatch, fake_principal):
    # Validation runs before any connection is made, so unreachable addresses are fine here.
    monkeypatch.setenv("CALLSIGHT_DATABASE_URL", "postgresql://unused@127.0.0.1:1/x")
    monkeypatch.setenv("CALLSIGHT_S3_ENDPOINT", "127.0.0.1:1")


def upload(name: str, data: bytes, client=anon, **form):
    return client.post("/v1/calls/upload", files={"file": (name, io.BytesIO(data))}, data=form)


def test_upload_rejects_unsupported_type(dummy_backends):
    assert upload("notes.txt", b"hello").status_code == 415
    assert upload("../../etc/passwd", b"x").status_code == 415


def test_upload_rejects_empty_file(dummy_backends):
    assert upload("call.wav", b"").status_code == 422


def test_upload_needs_storage(monkeypatch, fake_principal):
    monkeypatch.setenv("CALLSIGHT_DATABASE_URL", "postgresql://unused@127.0.0.1:1/x")
    monkeypatch.delenv("CALLSIGHT_S3_ENDPOINT", raising=False)
    assert upload("call.wav", b"RIFF").status_code == 503


DB_URL = os.environ.get("CALLSIGHT_TEST_DATABASE_URL")
S3 = os.environ.get("CALLSIGHT_TEST_S3_ENDPOINT")


class FakeASR:
    def __init__(self):
        self.paths = []

    def transcribe(self, source, call_id):
        self.paths.append(source)
        assert open(source, "rb").read() == b"fake-audio-bytes"  # the worker got the uploaded object
        return Transcript(call_id, [
            Segment(0, 0.0, "Speaker 1", "Thanks for joining."),
            Segment(1, 5.0, "Speaker 2", "Honestly the price is too expensive."),
        ])


class BrokenASR:
    def transcribe(self, source, call_id):
        raise ValueError("no speech found in the recording")


@pytest.mark.skipif(not (DB_URL and S3), reason="needs CALLSIGHT_TEST_DATABASE_URL and CALLSIGHT_TEST_S3_ENDPOINT")
def test_upload_is_processed_by_worker(monkeypatch, client):
    monkeypatch.setenv("CALLSIGHT_S3_ENDPOINT", S3)
    monkeypatch.setenv("CALLSIGHT_S3_ACCESS_KEY", os.environ["CALLSIGHT_TEST_S3_ACCESS_KEY"])
    monkeypatch.setenv("CALLSIGHT_S3_SECRET_KEY", os.environ["CALLSIGHT_TEST_S3_SECRET_KEY"])
    settings = Settings.from_env()
    while worker.process_one(settings, BrokenASR()):  # drain jobs left by earlier runs
        pass

    ok = upload("call.mp3", b"fake-audio-bytes", client=client, call_id="audio-ok")
    bad = upload("call.wav", b"fake-audio-bytes", client=client, call_id="audio-bad")
    assert ok.status_code == bad.status_code == 202
    assert client.get(f"/v1/calls/{ok.json()['id']}").json()["status"] == "queued"

    assert worker.process_one(settings, FakeASR())
    assert worker.process_one(settings, BrokenASR())
    assert not worker.process_one(settings, FakeASR())  # queue empty

    done = client.get(f"/v1/calls/{ok.json()['id']}").json()
    assert done["status"] == "done" and done["call_id"] == "audio-ok"
    assert done["speakers"] == ["Speaker 1", "Speaker 2"]
    assert any(i["kind"] == "pricing_objection" for i in done["insights"])

    failed = client.get(f"/v1/calls/{bad.json()['id']}").json()
    assert failed["status"] == "failed" and "no speech" in failed["error"]


def test_split_on_pauses_starts_a_turn_at_each_silence():
    from callsight.providers.whisper_asr import split_on_pauses

    words = [(0.0, 0.4, " Thanks"), (0.5, 0.9, " for"), (1.0, 1.5, " joining."),
             (38.0, 38.5, " Understood,"), (38.6, 39.0, " sure.")]
    assert split_on_pauses(words) == [(0.0, "Thanks for joining."), (38.0, "Understood, sure.")]
    assert split_on_pauses([]) == []


def test_storage_failure_is_a_clear_503(dummy_backends, monkeypatch):
    from minio.error import S3Error
    from urllib3.exceptions import MaxRetryError

    from callsight import recordings

    for exc in (
        S3Error("XMinioStorageFull", "Storage backend has reached its minimum free drive threshold.", "r", "id", "h", None),
        MaxRetryError(None, "/recordings", "connection refused"),
    ):
        def fail(*args, exc=exc, **kwargs):
            raise exc

        monkeypatch.setattr(recordings, "put", fail)
        res = upload("call.wav", b"RIFF")
        assert res.status_code == 503 and "storage is unavailable" in res.json()["detail"]


@pytest.mark.skipif(not (DB_URL and S3), reason="needs CALLSIGHT_TEST_DATABASE_URL and CALLSIGHT_TEST_S3_ENDPOINT")
def test_calls_still_processing_cannot_be_reanalyzed(monkeypatch, client):
    monkeypatch.setenv("CALLSIGHT_S3_ENDPOINT", S3)
    monkeypatch.setenv("CALLSIGHT_S3_ACCESS_KEY", os.environ["CALLSIGHT_TEST_S3_ACCESS_KEY"])
    monkeypatch.setenv("CALLSIGHT_S3_SECRET_KEY", os.environ["CALLSIGHT_TEST_S3_SECRET_KEY"])
    queued = upload("q.wav", b"RIFF", client=client).json()["id"]
    assert client.post(f"/v1/calls/{queued}/reanalyze").status_code == 409
    client.delete(f"/v1/calls/{queued}")


def test_assign_speakers_follows_who_was_talking():
    from callsight.providers.whisper_asr import assign_speakers

    words = [(0.0, 0.4, " Hi"), (0.5, 0.9, " there."),            # SPEAKER_07 talks first
             (1.2, 1.6, " Hello!"),                                # SPEAKER_02 answers (no pause needed)
             (1.7, 2.0, " Price"), (2.1, 2.5, " is"), (2.6, 3.0, " high."),
             (9.0, 9.4, " Okay.")]                                 # back to SPEAKER_07 after a gap in turns
    turns = [(0.0, 1.0, "SPEAKER_07"), (1.1, 3.2, "SPEAKER_02"), (8.8, 9.6, "SPEAKER_07")]
    assert assign_speakers(words, turns) == [
        (0.0, "Speaker 1", "Hi there."),
        (1.2, "Speaker 2", "Hello! Price is high."),
        (9.0, "Speaker 1", "Okay."),
    ]


def test_words_between_turns_go_to_the_nearest_speaker_and_pauses_split_turns():
    from callsight.providers.whisper_asr import assign_speakers

    words = [(0.0, 0.3, " One"), (1.05, 1.15, " two"), (5.0, 5.3, " three")]  # 'two' falls in a gap
    turns = [(0.0, 1.0, "A"), (1.3, 6.0, "B")]
    assert assign_speakers(words, turns) == [(0.0, "Speaker 1", "One two"), (5.0, "Speaker 2", "three")]
    assert assign_speakers([(0.0, 0.3, " a"), (3.0, 3.3, " b")], [(0.0, 9.0, "A")]) == [
        (0.0, "Speaker 1", "a"), (3.0, "Speaker 1", "b")]  # same speaker, but a pause starts a new turn


def test_no_diarization_turns_means_one_speaker():
    from callsight.providers.whisper_asr import assign_speakers

    assert assign_speakers([(0.0, 0.3, " Hi")], []) == [(0.0, "Speaker 1", "Hi")]


def test_diarization_misconfiguration_fails_loudly(monkeypatch):
    """Runs with or without the asr/diarize extras installed (CI has neither), never skipped."""
    import importlib.util
    import sys
    import types

    from callsight.providers.whisper_asr import WhisperASR

    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=lambda *a, **k: object()))
    with pytest.raises(ValueError, match="unknown CALLSIGHT_DIARIZATION"):
        WhisperASR(diarization="magic")
    if importlib.util.find_spec("pyannote") is None:
        with pytest.raises(RuntimeError, match="diarize"):
            WhisperASR(diarization="pyannote", hf_token="x")
    else:
        import huggingface_hub

        monkeypatch.setattr(huggingface_hub, "get_token", lambda: None)  # no `hf auth login` token either
        with pytest.raises(RuntimeError, match="HF_TOKEN"):
            WhisperASR(diarization="pyannote", hf_token="")
