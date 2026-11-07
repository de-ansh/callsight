"""Deleting calls on request and by retention period. Needs test Postgres + MinIO."""

import io
import os
import uuid

import pytest

pytest.importorskip("fastapi")
from callsight import recordings, retention, store  # noqa: E402
from callsight.config import Settings  # noqa: E402

DB_URL = os.environ.get("CALLSIGHT_TEST_DATABASE_URL")
S3 = os.environ.get("CALLSIGHT_TEST_S3_ENDPOINT")
pytestmark = pytest.mark.skipif(not (DB_URL and S3), reason="needs test Postgres and MinIO")
TRANSCRIPT = "[00:00] Alex: Hi\n[00:05] Priya: The price is too expensive.\n"


@pytest.fixture
def settings(monkeypatch, client):
    monkeypatch.setenv("CALLSIGHT_S3_ENDPOINT", S3)
    monkeypatch.setenv("CALLSIGHT_S3_ACCESS_KEY", os.environ["CALLSIGHT_TEST_S3_ACCESS_KEY"])
    monkeypatch.setenv("CALLSIGHT_S3_SECRET_KEY", os.environ["CALLSIGHT_TEST_S3_SECRET_KEY"])
    return Settings.from_env()


def object_exists(settings, key):
    try:
        recordings.size(settings, key)
        return True
    except Exception:
        return False


def test_delete_removes_call_recording_and_is_audited(settings, client):
    call_id = client.post("/v1/calls/upload", files={"file": ("c.mp3", io.BytesIO(b"audio"))}).json()["id"]
    with store.connect(DB_URL) as conn:
        key = conn.execute("SELECT recording_key FROM calls WHERE id = %s", (call_id,)).fetchone()["recording_key"]
    assert object_exists(settings, key)

    assert client.delete(f"/v1/calls/{call_id}").status_code == 204
    assert client.get(f"/v1/calls/{call_id}").status_code == 404
    assert all(c["id"] != call_id for c in client.get("/v1/calls").json())
    assert not object_exists(settings, key)  # the recording is gone from storage, not just the row
    assert client.delete(f"/v1/calls/{call_id}").status_code == 404
    assert any(r["action"] == "call.delete" and r["target"] == call_id for r in client.get("/v1/audit").json())


def test_another_org_cannot_delete(settings, client):
    from fastapi.testclient import TestClient

    from callsight import auth
    from callsight.api import app

    call_id = client.post("/v1/analyze", json={"transcript": TRANSCRIPT}).json()["id"]
    with store.connect(DB_URL) as conn:
        other_key = auth.create_key(conn, f"pytest-other-{uuid.uuid4().hex[:8]}", "intruder")
    other = TestClient(app, headers={"authorization": f"Bearer {other_key}"})
    assert other.delete(f"/v1/calls/{call_id}").status_code == 404
    assert client.get(f"/v1/calls/{call_id}").status_code == 200


def test_retention_purges_only_old_finished_calls(settings, client):
    old = client.post("/v1/analyze", json={"call_id": "old", "transcript": TRANSCRIPT}).json()["id"]
    busy = client.post("/v1/analyze", json={"call_id": "old-busy", "transcript": TRANSCRIPT}).json()["id"]
    new = client.post("/v1/analyze", json={"call_id": "new", "transcript": TRANSCRIPT}).json()["id"]
    with store.connect(DB_URL) as conn, conn.transaction():
        conn.execute("UPDATE calls SET created_at = now() - interval '40 days' WHERE id IN (%s, %s)", (old, busy))
        conn.execute("UPDATE calls SET status = 'processing' WHERE id = %s", (busy,))

    assert retention.purge_expired(Settings.from_env()) == 0  # retention off by default
    from dataclasses import replace

    assert retention.purge_expired(replace(settings, retention_days=30)) >= 1
    assert client.get(f"/v1/calls/{old}").status_code == 404
    assert client.get(f"/v1/calls/{busy}").status_code == 200   # a worker owns it; next round
    assert client.get(f"/v1/calls/{new}").status_code == 200
    assert any(r["actor"] == "retention" and r["target"] == old for r in client.get("/v1/audit").json())
    with store.connect(DB_URL) as conn, conn.transaction():  # don't leave a fake 'processing' job behind
        conn.execute("DELETE FROM calls WHERE id = %s", (busy,))
