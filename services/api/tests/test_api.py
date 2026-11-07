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
