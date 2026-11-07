import hashlib
import hmac
import io
import json
import os
import socket
import uuid
from contextlib import nullcontext

import pytest

from callsight import remote
from callsight.recordings import parse_range

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from callsight import worker  # noqa: E402
from callsight.api import app  # noqa: E402
from callsight.config import Settings  # noqa: E402

anon = TestClient(app)
SECRET = "test-secret"
FAKE_ORG = uuid.uuid4()
TRANSCRIPT = "[00:00] Alex: Hi\n[00:05] Priya: Honestly the price is too expensive.\n"


def signed(payload, secret=SECRET):
    body = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return body, {"x-callsight-signature": sig, "content-type": "application/json"}


def post(payload, secret=SECRET, name="generic", org_id=FAKE_ORG, client=anon):
    body, headers = signed(payload, secret)
    return client.post(f"/v1/orgs/{org_id}/connectors/{name}/webhook", content=body, headers=headers)


# --- pure helpers -------------------------------------------------------------------------


@pytest.mark.parametrize("header,expected", [
    (None, None), ("bytes=0-99", (0, 99)), ("bytes=100-", (100, 999)), ("bytes=-100", (900, 999)),
    ("bytes=900-5000", (900, 999)), ("items=0-1", None), ("bytes=0-1,5-6", None),
])
def test_parse_range(header, expected):
    assert parse_range(header, 1000) == expected


@pytest.mark.parametrize("header", ["bytes=1000-", "bytes=50-10", "bytes=-0"])
def test_parse_range_unsatisfiable(header):
    with pytest.raises(ValueError):
        parse_range(header, 1000)


@pytest.mark.parametrize("url", [
    "http://example.com/a.mp3",          # not https
    "https://127.0.0.1/a.mp3",           # loopback
    "https://10.0.0.5/a.mp3",            # private
    "https://169.254.169.254/latest",    # cloud metadata
    "https://[::1]/a.mp3",
    "file:///etc/passwd",
])
def test_unsafe_urls_are_rejected(url):
    with pytest.raises(remote.UnsafeURL):
        remote.check_url(url)


def test_fetch_connects_to_the_checked_address_only(monkeypatch, tmp_path):
    """DNS rebinding: a second lookup could return an internal IP, so fetch must not look up again."""
    lookups, connects = [], []

    def getaddrinfo(host, port, **kw):
        lookups.append(host)
        ip = "93.184.216.34" if len(lookups) == 1 else "127.0.0.1"  # rebinds after the first answer
        return [(None, None, None, None, (ip, port))]

    def create_connection(address, timeout=None):
        connects.append(address)
        raise ConnectionRefusedError("stop here")  # no real network in tests

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(socket, "create_connection", create_connection)
    with pytest.raises(ConnectionRefusedError):
        remote.fetch("https://files.example.com/a.mp3", str(tmp_path / "a.mp3"))
    assert lookups == ["files.example.com"]
    assert connects == [("93.184.216.34", 443)]


def test_pinned_connection_still_verifies_the_hostname():
    conn = remote._PinnedHTTPSConnection("files.example.com", "93.184.216.34", 443, 5)
    assert conn.host == "files.example.com"  # used as server_hostname for SNI and certificate checks
    assert conn._context.check_hostname and conn._context.verify_mode == remote.ssl.CERT_REQUIRED


def test_hostnames_resolving_to_private_addresses_are_rejected(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(None, None, None, None, ("192.168.1.10", 443))])
    with pytest.raises(remote.UnsafeURL, match="non-public"):
        remote.check_url("https://recordings.example.com/a.mp3")


# --- webhook endpoint (validation happens before any DB/S3 access) --------------------------


@pytest.fixture
def webhook_env(monkeypatch):
    """Pretend FAKE_ORG exists with SECRET; validation fails before anything is stored."""
    from callsight import store

    monkeypatch.setenv("CALLSIGHT_DATABASE_URL", "postgresql://unused@127.0.0.1:1/x")
    monkeypatch.setattr(store, "connect", lambda url: nullcontext(None))
    monkeypatch.setattr(store, "org_webhook_secret", lambda conn, org_id: SECRET if org_id == FAKE_ORG else None)


def test_webhook_rejects_bad_signature(webhook_env):
    assert post({"transcript": TRANSCRIPT}, secret="wrong").status_code == 401
    url = f"/v1/orgs/{FAKE_ORG}/connectors/generic/webhook"
    assert anon.post(url, json={"transcript": TRANSCRIPT}).status_code == 401


def test_webhook_validates_payload(webhook_env):
    assert post({"call_id": "x"}).status_code == 422                                      # neither
    assert post({"transcript": TRANSCRIPT, "recording_url": "https://a.com/x.mp3"}).status_code == 422  # both
    assert post({"recording_url": "https://127.0.0.1/x.mp3"}).status_code == 422          # SSRF
    assert post({"transcript": 5}).status_code == 422


def test_webhook_unknown_connector_or_org(webhook_env):
    assert post({"transcript": TRANSCRIPT}, name="nope").status_code == 404
    assert post({"transcript": TRANSCRIPT}, org_id=uuid.uuid4()).status_code == 404


def test_one_orgs_secret_does_not_work_for_another(webhook_env, monkeypatch):
    from callsight import store

    other = uuid.uuid4()
    monkeypatch.setattr(store, "org_webhook_secret", lambda conn, org_id: {FAKE_ORG: SECRET, other: "other"}[org_id])
    assert post({"transcript": TRANSCRIPT}, org_id=other).status_code == 401


def test_ui_is_served():
    res = anon.get("/")
    assert res.status_code == 200 and "<title>Callsight</title>" in res.text
    assert "innerHTML" not in res.text  # call data must only be rendered as text


# --- with Postgres + MinIO ------------------------------------------------------------------

DB_URL = os.environ.get("CALLSIGHT_TEST_DATABASE_URL")
S3 = os.environ.get("CALLSIGHT_TEST_S3_ENDPOINT")
needs_backends = pytest.mark.skipif(not (DB_URL and S3), reason="needs test Postgres and MinIO")


@pytest.fixture
def backends(monkeypatch, client):
    monkeypatch.setenv("CALLSIGHT_S3_ENDPOINT", S3)
    monkeypatch.setenv("CALLSIGHT_S3_ACCESS_KEY", os.environ["CALLSIGHT_TEST_S3_ACCESS_KEY"])
    monkeypatch.setenv("CALLSIGHT_S3_SECRET_KEY", os.environ["CALLSIGHT_TEST_S3_SECRET_KEY"])
    return Settings.from_env()


class FakeASR:
    def transcribe(self, source, call_id):
        from callsight.providers.text_asr import TextTranscriptParser

        assert open(source, "rb").read() == b"remote-audio"
        return TextTranscriptParser().transcribe(TRANSCRIPT, call_id)


@needs_backends
def test_webhook_transcript_is_analyzed_and_stored(backends, client, org):
    res = post({"call_id": "hook-text", "transcript": TRANSCRIPT}, secret=org["webhook_secret"], org_id=org["id"])
    assert res.status_code == 200 and res.json()["status"] == "done"
    call = client.get(f"/v1/calls/{res.json()['id']}").json()
    assert call["source"] == "generic" and call["has_recording"] is False
    assert any(i["kind"] == "pricing_objection" for i in call["insights"])


@needs_backends
def test_webhook_recording_url_is_fetched_by_worker_and_playable(backends, monkeypatch, client, org):
    while worker.process_one(backends, FakeASR()):  # drain leftovers from other tests
        pass

    def fake_fetch(url, path, max_bytes=remote.MAX_BYTES):
        assert url == "https://recordings.example.com/calls/42.mp3"
        open(path, "wb").write(b"remote-audio")

    monkeypatch.setattr(remote, "check_url", lambda url: None)  # example.com is not actually resolvable here
    monkeypatch.setattr(remote, "fetch", fake_fetch)
    res = post({"call_id": "hook-audio", "recording_url": "https://recordings.example.com/calls/42.mp3"},
               secret=org["webhook_secret"], org_id=org["id"])
    assert res.status_code == 200 and res.json()["status"] == "queued"
    assert worker.process_one(backends, FakeASR())

    call_id = res.json()["id"]
    call = client.get(f"/v1/calls/{call_id}").json()
    assert call["status"] == "done" and call["has_recording"] is True
    whole = client.get(f"/v1/calls/{call_id}/recording")
    assert whole.status_code == 200 and whole.content == b"remote-audio" and whole.headers["accept-ranges"] == "bytes"
    part = client.get(f"/v1/calls/{call_id}/recording", headers={"range": "bytes=7-"})
    assert part.status_code == 206 and part.content == b"audio" and part.headers["content-range"] == "bytes 7-11/12"
    assert client.get(f"/v1/calls/{call_id}/recording", headers={"range": "bytes=50-"}).status_code == 416
    # The signed link from the detail response plays without an API key; a tampered one does not.
    link = call["recording_link"]
    assert anon.get(link).content == b"remote-audio"
    assert anon.get(link.replace("sig=", "sig=0")).status_code == 401
    assert anon.get(f"/v1/calls/{call_id}/recording").status_code == 401
    assert anon.get(link.replace("by=key%3Apytest", "by=key%3Amallory")).status_code == 401  # forged listener
    reads = [r["actor"] for r in client.get("/v1/audit").json()
             if r["action"] == "recording.read" and r["target"] == str(call_id)]
    assert "key:pytest (link)" in reads


@needs_backends
def test_uploaded_recording_is_playable(backends, client):
    res = client.post("/v1/calls/upload", files={"file": ("c.wav", io.BytesIO(b"RIFFdata"))})
    assert client.get(f"/v1/calls/{res.json()['id']}/recording").content == b"RIFFdata"
