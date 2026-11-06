"""Round-trip through Postgres. Runs only when CALLSIGHT_TEST_DATABASE_URL points at a db with db/schema.sql loaded."""

import os
import uuid

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("psycopg")

DB_URL = os.environ.get("CALLSIGHT_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DB_URL, reason="CALLSIGHT_TEST_DATABASE_URL not set")

from fastapi.testclient import TestClient  # noqa: E402

from callsight.api import app  # noqa: E402

client = TestClient(app)

TRANSCRIPT = (
    "[00:00] Alex: Hi Priya, thanks for joining.\n"
    "[00:10] Priya: Honestly the price is too expensive for us.\n"
    "[00:25] Alex: Understood. Can we book a follow-up next Tuesday?\n"
)


@pytest.fixture(autouse=True)
def _db(monkeypatch):
    monkeypatch.setenv("CALLSIGHT_DATABASE_URL", DB_URL)


def test_analyze_persists_and_reads_back():
    external_id = f"test-{uuid.uuid4()}"
    posted = client.post("/v1/analyze", json={"call_id": external_id, "transcript": TRANSCRIPT}).json()

    stored = client.get(f"/v1/calls/{posted['id']}").json()
    assert stored["call_id"] == external_id
    assert [s["text"] for s in stored["segments"]][1] == "Honestly the price is too expensive for us."
    assert stored["speakers"] == posted["speakers"]
    assert stored["talk_share"] == posted["talk_share"]
    assert stored["insights"] == posted["insights"]  # same order, same evidence

    listed = client.get("/v1/calls", params={"limit": 200}).json()
    row = next(c for c in listed if c["id"] == posted["id"])
    assert row["insight_count"] == len(posted["insights"])


def test_unknown_call_is_404_and_bad_id_is_422():
    assert client.get(f"/v1/calls/{uuid.uuid4()}").status_code == 404
    assert client.get("/v1/calls/not-a-uuid").status_code == 422
