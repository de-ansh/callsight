"""Round-trip through Postgres. Runs only when CALLSIGHT_TEST_DATABASE_URL is set (conftest.py migrates it)."""

import os
import uuid

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("psycopg")

DB_URL = os.environ.get("CALLSIGHT_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DB_URL, reason="CALLSIGHT_TEST_DATABASE_URL not set")

TRANSCRIPT = (
    "[00:00] Alex: Hi Priya, thanks for joining.\n"
    "[00:10] Priya: Honestly the price is too expensive for us.\n"
    "[00:25] Alex: Understood. Can we book a follow-up next Tuesday?\n"
)


def test_analyze_persists_and_reads_back(client):
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


def test_unknown_call_is_404_and_bad_id_is_422(client):
    assert client.get(f"/v1/calls/{uuid.uuid4()}").status_code == 404
    assert client.get("/v1/calls/not-a-uuid").status_code == 422


def test_migrations_are_recorded_and_idempotent():
    import psycopg

    from callsight.migrate import migrate, pending_files

    assert migrate(DB_URL) == []  # conftest already applied everything
    with psycopg.connect(DB_URL) as conn:
        recorded = [r[0] for r in conn.execute("SELECT version FROM schema_migrations ORDER BY version")]
    assert recorded == [v for v, _ in pending_files()]


# --- auth and tenancy -------------------------------------------------------------------------


def _other_org_client(monkeypatch):
    from fastapi.testclient import TestClient

    from callsight import auth, store
    from callsight.api import app

    with store.connect(DB_URL) as conn:
        key = auth.create_key(conn, f"pytest-other-{uuid.uuid4().hex[:8]}", "intruder")
    return TestClient(app, headers={"authorization": f"Bearer {key}"}), key


def test_requests_without_a_valid_key_are_rejected(client):
    from fastapi.testclient import TestClient

    from callsight.api import app

    anonymous = TestClient(app)
    assert anonymous.get("/v1/calls").status_code == 401
    assert anonymous.post("/v1/analyze", json={"transcript": TRANSCRIPT}).status_code == 401
    assert anonymous.get("/v1/calls", headers={"authorization": "Bearer cs_nope"}).status_code == 401
    assert anonymous.get("/v1/calls", headers={"x-api-key": client.headers["authorization"][7:]}).status_code == 200


def test_one_org_cannot_see_another_orgs_calls(client, monkeypatch):
    mine = client.post("/v1/analyze", json={"call_id": "private", "transcript": TRANSCRIPT}).json()["id"]
    other, _ = _other_org_client(monkeypatch)
    assert other.get(f"/v1/calls/{mine}").status_code == 404           # not 403: existence is not revealed
    assert all(c["id"] != mine for c in other.get("/v1/calls").json())
    assert other.get(f"/v1/calls/{mine}/recording").status_code == 404
    assert other.get("/v1/audit").json() == [] or all(r["target"] != mine for r in other.get("/v1/audit").json())


def test_revoked_key_stops_working(monkeypatch):
    from callsight import auth, store

    other, key = _other_org_client(monkeypatch)
    monkeypatch.setenv("CALLSIGHT_DATABASE_URL", DB_URL)
    assert other.get("/v1/calls").status_code == 200
    with store.connect(DB_URL) as conn:
        org = conn.execute("SELECT o.name FROM orgs o JOIN api_keys k ON k.org_id = o.id WHERE k.key_hash = %s",
                           (auth.hash_key(key),)).fetchone()["name"]
        assert auth.revoke_key(conn, org, "intruder") == 1
    assert other.get("/v1/calls").status_code == 401


def test_audit_log_records_creation_and_reads(client, org):
    call_id = client.post("/v1/analyze", json={"call_id": "audited", "transcript": TRANSCRIPT}).json()["id"]
    client.get(f"/v1/calls/{call_id}")
    entries = [(r["actor"], r["action"]) for r in client.get("/v1/audit").json() if r["target"] == str(call_id)]
    assert ("key:pytest", "call.create") in entries and ("key:pytest", "call.read") in entries


def test_signed_recording_links_are_bound_to_call_org_and_time():
    from callsight import auth

    from urllib.parse import parse_qs, urlsplit

    def params(link):
        return {k: v[0] for k, v in parse_qs(urlsplit(link).query).items()}

    call, org, other = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    q = params(auth.sign_recording(call, org, "key:alice", "s3cret"))
    exp, sig = int(q["expires"]), q["sig"]
    assert q["by"] == "key:alice"
    assert auth.verify_recording(call, org, "key:alice", exp, sig, "s3cret")
    assert not auth.verify_recording(uuid.uuid4(), org, "key:alice", exp, sig, "s3cret")   # other call
    assert not auth.verify_recording(call, other, "key:alice", exp, sig, "s3cret")          # other org
    assert not auth.verify_recording(call, org, "key:mallory", exp, sig, "s3cret")         # forged actor
    assert not auth.verify_recording(call, org, "key:alice", exp + 1, sig, "s3cret")       # tampered expiry
    assert not auth.verify_recording(call, org, "key:alice", exp, sig, "other-secret")
    q = params(auth.sign_recording(call, org, "key:alice", "s3cret", now=0))
    assert not auth.verify_recording(call, org, "key:alice", int(q["expires"]), q["sig"], "s3cret")  # expired


# --- search, filters, demo ------------------------------------------------------------------


def test_search_by_name_and_words_said_with_severity_counts(client):
    tag = uuid.uuid4().hex[:8]
    a = client.post("/v1/analyze", json={"call_id": f"search-{tag}-a", "transcript": TRANSCRIPT}).json()["id"]
    b = client.post("/v1/analyze", json={"call_id": f"other-{tag}",
                                         "transcript": "[00:00] Alex: Hi\n[00:05] Priya: Let us talk about zebras.\n"}).json()["id"]
    ids = lambda **p: [c["id"] for c in client.get("/v1/calls", params={"limit": 200, **p}).json()]
    assert ids(q=f"search-{tag}") == [a]                  # by name
    assert ids(q="ZEBRAS") == [b]                          # by words said, case-insensitive
    assert ids(q="100%") == [] and ids(q="_") != ids()     # LIKE wildcards are literal
    assert a in ids(status="done") and a not in ids(status="queued")
    assert client.get("/v1/calls", params={"status": "bogus"}).status_code == 422
    row = next(c for c in client.get("/v1/calls", params={"q": f"search-{tag}"}).json())
    assert row["high_count"] + row["medium_count"] <= row["insight_count"] and row["medium_count"] >= 1


def test_search_never_crosses_orgs(client, monkeypatch):
    marker = f"secret-{uuid.uuid4().hex[:8]}"
    client.post("/v1/analyze", json={"call_id": marker, "transcript": TRANSCRIPT})
    other, _ = _other_org_client(monkeypatch)
    assert other.get("/v1/calls", params={"q": marker}).json() == []


def test_demo_loads_example_calls_once(monkeypatch, capsys):
    from callsight import admin, store

    monkeypatch.setenv("CALLSIGHT_DATABASE_URL", DB_URL)
    assert admin.main(["demo"]) == 0
    first = capsys.readouterr().out
    assert "Loaded 6 example call(s)" in first and "API key: cs_" in first
    assert admin.main(["demo"]) == 0
    assert "Loaded 0 example call(s)" in capsys.readouterr().out   # second run adds a key, not duplicates
    with store.connect(DB_URL) as conn:
        n = conn.execute("SELECT count(*) AS n FROM calls c JOIN orgs o ON o.id = c.org_id WHERE o.name = 'demo'").fetchone()["n"]
    assert n == 6
