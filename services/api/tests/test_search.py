"""Semantic search: the worker embeds transcript lines, /v1/search ranks them. Ollama is faked."""

import os
import uuid

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("psycopg")

DB_URL = os.environ.get("CALLSIGHT_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DB_URL, reason="CALLSIGHT_TEST_DATABASE_URL not set")

# Each "concept" is one dimension, so lines sharing a concept with the query score highest.
CONCEPTS = [{"price", "expensive", "cost", "budget"}, {"later", "spring", "wait", "quarter"},
            {"boss", "vp", "approval"}]
TRANSCRIPT = (
    "[00:00] Alex: Hi Priya, thanks for joining.\n"
    "[00:10] Priya: Honestly it is too expensive for our budget.\n"
    "[00:25] Priya: And we would rather wait until spring.\n"
)


def fake_embed(base_url, model, texts, timeout=120.0):
    def vec(text):
        words = set(text.lower().replace(".", " ").replace(",", " ").split())
        return [float(len(words & c)) for c in CONCEPTS] + [0.1]  # constant dim keeps every vector non-zero
    return [vec(t) for t in texts]


@pytest.fixture
def searching(monkeypatch):
    from fastapi.testclient import TestClient

    from callsight import api, auth, store, worker
    from callsight.config import Settings

    monkeypatch.setenv("CALLSIGHT_DATABASE_URL", DB_URL)
    monkeypatch.setenv("CALLSIGHT_EMBED_MODEL", "fake-embed")
    monkeypatch.setattr(api, "ollama_embed", fake_embed)
    monkeypatch.setattr(worker, "ollama_embed", fake_embed)

    def new_org():
        with store.connect(DB_URL) as conn:
            key = auth.create_key(conn, f"pytest-search-{uuid.uuid4().hex[:8]}", "pytest")
        return TestClient(api.app, headers={"authorization": f"Bearer {key}"})

    def index_all():
        while worker.embed_one(Settings.from_env()):
            pass

    return new_org, index_all


def test_search_ranks_lines_by_meaning_once_indexed(searching):
    new_org, index_all = searching
    c = new_org()
    call = c.post("/v1/analyze", json={"call_id": "search-me", "transcript": TRANSCRIPT}).json()
    before = c.get("/v1/search", params={"q": "what does it cost"}).json()
    assert before == {"results": [], "calls_pending": 1}

    index_all()
    hits = c.get("/v1/search", params={"q": "what does it cost", "limit": 2}).json()
    assert hits["calls_pending"] == 0
    assert [h["text"] for h in hits["results"]][0] == "Honestly it is too expensive for our budget."
    top = hits["results"][0]
    assert (top["call_id"], top["call_name"], top["idx"], top["start_s"], top["speaker"]) == (
        call["id"], "search-me", 1, 10.0, "Priya")
    assert len(hits["results"]) == 2 and hits["results"][0]["score"] > hits["results"][1]["score"]
    assert c.get("/v1/search", params={"q": "can we wait a quarter"}).json()["results"][0]["idx"] == 2


def test_search_never_crosses_orgs(searching):
    new_org, index_all = searching
    mine, other = new_org(), new_org()
    mine.post("/v1/analyze", json={"call_id": "private", "transcript": TRANSCRIPT})
    index_all()
    assert mine.get("/v1/search", params={"q": "price"}).json()["results"]
    assert other.get("/v1/search", params={"q": "price"}).json() == {"results": [], "calls_pending": 0}


def test_reanalysis_and_delete_drop_stale_vectors(searching):
    from callsight import store

    new_org, index_all = searching
    c = new_org()
    call_id = c.post("/v1/analyze", json={"call_id": "redo", "transcript": TRANSCRIPT}).json()["id"]
    index_all()
    c.post(f"/v1/calls/{call_id}/reanalyze")  # rewrites the segments, so their vectors go too
    assert c.get("/v1/search", params={"q": "price"}).json()["calls_pending"] == 1
    index_all()
    assert c.get("/v1/search", params={"q": "price"}).json()["calls_pending"] == 0
    assert c.delete(f"/v1/calls/{call_id}").status_code == 204
    with store.connect(DB_URL) as conn:
        left = conn.execute("SELECT count(*) AS n FROM segment_embeddings WHERE call_id = %s", (call_id,)).fetchone()
    assert left["n"] == 0


def test_search_off_or_embedding_server_down(searching, monkeypatch):
    from callsight import api

    new_org, _ = searching
    c = new_org()
    assert c.get("/auth/me").json()["semantic_search"] is True

    def down(*args, **kwargs):
        raise ConnectionRefusedError("no ollama")
    monkeypatch.setattr(api, "ollama_embed", down)
    assert c.get("/v1/search", params={"q": "price"}).status_code == 502

    monkeypatch.delenv("CALLSIGHT_EMBED_MODEL")
    assert c.get("/v1/search", params={"q": "price"}).status_code == 503
    assert c.get("/auth/me").json()["semantic_search"] is False
    assert c.get("/v1/search").status_code == 422  # q is required

