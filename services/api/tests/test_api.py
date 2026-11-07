import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from callsight.api import app  # noqa: E402

client = TestClient(app)


def test_health():
    assert client.get("/health").json()["status"] == "ok"


def test_analyze_returns_report_with_evidence():
    body = {
        "call_id": "c1",
        "transcript": "[00:00] Alex: Hi\n[00:10] Priya: The price is too expensive.",
    }
    data = client.post("/v1/analyze", json=body).json()
    assert data["call_id"] == "c1"
    assert any(i["kind"] == "pricing_objection" for i in data["insights"])


def test_analyze_rejects_unparseable_transcript():
    assert client.post("/v1/analyze", json={"transcript": "no timestamps here"}).status_code == 422


def test_read_endpoints_need_a_key_and_a_database(monkeypatch):
    monkeypatch.delenv("CALLSIGHT_DATABASE_URL", raising=False)
    assert client.get("/v1/calls").status_code == 401
    assert client.get("/v1/calls", headers={"authorization": "Bearer cs_x"}).status_code == 503


def test_oversized_bodies_are_rejected_while_streaming(monkeypatch):
    from callsight import api

    monkeypatch.setattr(api, "MAX_BODY_BYTES", 100)
    monkeypatch.setattr(api, "MAX_UPLOAD_BYTES", 1000)
    big = "[00:00] A: " + "x" * 200
    # Declared size over the limit: rejected before reading.
    assert client.post("/v1/analyze", json={"transcript": big}).status_code == 413
    # No Content-Length (chunked): rejected once the streamed bytes pass the limit.
    def chunks():
        yield b'{"transcript": "'
        yield b"x" * 200
        yield b'"}'
    res = client.post("/v1/analyze", content=chunks(), headers={"content-type": "application/json"})
    assert res.status_code == 413
    # Uploads get their own, larger limit; a small JSON body is fine.
    assert client.post("/v1/analyze", json={"transcript": "[00:00] A: hi"}).status_code == 200
    def upload_chunks():
        yield b"--b\r\nContent-Disposition: form-data; name=\"file\"; filename=\"c.wav\"\r\n\r\n"
        yield b"x" * 2000
        yield b"\r\n--b--\r\n"
    from callsight.auth import Principal

    monkeypatch.setitem(api.app.dependency_overrides, api.require_key, lambda: Principal(None, "test"))
    res = client.post("/v1/calls/upload", content=upload_chunks(),
                      headers={"content-type": "multipart/form-data; boundary=b", "authorization": "Bearer cs_x"})
    assert res.status_code == 413


def test_uploads_are_authenticated_before_the_body_is_read(monkeypatch):
    monkeypatch.delenv("CALLSIGHT_DATABASE_URL", raising=False)
    read = []

    def body():
        read.append(1)
        yield b"x" * 10

    res = client.post("/v1/calls/upload", content=body(), headers={"content-type": "multipart/form-data; boundary=b"})
    assert res.status_code == 401 and read == []  # rejected without consuming the upload
