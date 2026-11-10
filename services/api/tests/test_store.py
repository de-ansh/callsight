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
    with store.connect(DB_URL) as conn:
        demo = conn.execute("SELECT id, competitors FROM orgs WHERE name = 'demo'").fetchone()
        created = conn.execute("SELECT count(*) AS n FROM audit_log WHERE org_id = %s AND action = 'call.create'",
                               (demo["id"],)).fetchone()["n"]
    assert demo["competitors"] == ["AcmeCall", "Gong", "Chorus"] and created == 6
    assert admin.main(["demo"]) == 0
    assert "Loaded 0 example call(s)" in capsys.readouterr().out   # second run adds a key, not duplicates
    with store.connect(DB_URL) as conn:
        n = conn.execute("SELECT count(*) AS n FROM calls c JOIN orgs o ON o.id = c.org_id WHERE o.name = 'demo'").fetchone()["n"]
    assert n == 6


def test_org_competitors_are_flagged_and_isolated(client, monkeypatch):
    assert client.get("/v1/settings").json() == {"competitors": []}
    res = client.put("/v1/settings", json={"competitors": [" Zorbcorp ", "Gong", "Gong"]})
    assert res.json() == {"competitors": ["Zorbcorp", "Gong"]}            # trimmed, de-duplicated
    assert client.put("/v1/settings", json={"competitors": [""]}).status_code == 422
    assert client.put("/v1/settings", json={"competitors": ["x"] * 51}).status_code == 422

    said = "[00:00] Alex: Hi\n[00:05] Priya: We are also talking to Zorbcorp.\n"
    mine = client.post("/v1/analyze", json={"transcript": said}).json()
    assert any(i["title"] == "Competitor mentioned: Zorbcorp" for i in mine["insights"])
    other, _ = _other_org_client(monkeypatch)
    theirs = other.post("/v1/analyze", json={"transcript": said}).json()
    assert not any(i["kind"] == "competitor_mention" for i in theirs["insights"])  # their list is empty
    assert other.get("/v1/settings").json() == {"competitors": []}
    assert any(r["action"] == "settings.update" for r in client.get("/v1/audit").json())
    client.put("/v1/settings", json={"competitors": []})


# --- re-analysis and speakers -------------------------------------------------------------

STEREO_LIKE = (
    "[00:00] Speaker 1: Hi, thanks for joining.\n"
    "[00:04] Speaker 2: Sure. We looked at Quuxcall and honestly the price is too high for our team right now, "
    "we would need a much better deal before we change anything at all this year.\n"
    "[00:20] Speaker 1: Understood.\n"
)


def test_reanalyze_applies_current_competitor_list(client):
    call = client.post("/v1/analyze", json={"call_id": "reanalyze-me", "transcript": STEREO_LIKE}).json()
    assert not any(i["kind"] == "competitor_mention" for i in call["insights"])
    client.put("/v1/settings", json={"competitors": ["Quuxcall"]})
    try:
        updated = client.post(f"/v1/calls/{call['id']}/reanalyze").json()
    finally:
        client.put("/v1/settings", json={"competitors": []})
    assert any(i["title"] == "Competitor mentioned: Quuxcall" for i in updated["insights"])
    assert client.get(f"/v1/calls/{call['id']}").json()["insights"] == updated["insights"]
    assert any(r["action"] == "call.reanalyze" and r["target"] == call["id"] for r in client.get("/v1/audit").json())


def test_naming_speakers_and_choosing_the_rep(client):
    call_id = client.post("/v1/analyze", json={"transcript": STEREO_LIKE}).json()["id"]
    before = client.get(f"/v1/calls/{call_id}").json()
    # First speaker is assumed to be the rep, so the long monologue by Speaker 2 is the "prospect".
    assert not any(i["kind"] == "talk_ratio" for i in before["insights"])

    res = client.put(f"/v1/calls/{call_id}/speakers",
                     json={"names": {"Speaker 1": "Priya", "Speaker 2": "Alex"}, "rep": "Alex"})
    assert res.status_code == 200
    after = res.json()
    assert after["speakers"] == ["Priya", "Alex"] and after["rep_speaker"] == "Alex"
    assert {s["speaker"] for s in after["segments"]} == {"Priya", "Alex"}
    assert any(i["kind"] == "talk_ratio" and "Alex" in i["detail"] for i in after["insights"])  # rep talked most
    assert not any(i["kind"] == "pricing_objection" for i in after["insights"])  # the rep's words aren't objections

    # Swapping names back works, and the rep choice follows the rename.
    swapped = client.put(f"/v1/calls/{call_id}/speakers", json={"names": {"Priya": "Alex", "Alex": "Priya"}}).json()
    assert swapped["speakers"] == ["Alex", "Priya"] and swapped["rep_speaker"] == "Priya"
    # A plain re-analysis keeps the chosen rep.
    assert client.post(f"/v1/calls/{call_id}/reanalyze").json()["rep_speaker"] == "Priya"


def test_speaker_update_validation(client, monkeypatch):
    call_id = client.post("/v1/analyze", json={"transcript": STEREO_LIKE}).json()["id"]
    put = lambda body: client.put(f"/v1/calls/{call_id}/speakers", json=body).status_code
    assert put({"names": {"Nobody": "X"}}) == 422                                   # not a speaker
    assert put({"names": {"Speaker 1": "Speaker 2"}}) == 422                        # would merge two speakers
    assert put({"names": {"Speaker 1": "   "}}) == 422                              # blank
    assert put({"rep": "Somebody else"}) == 422                                     # rep must be a speaker
    other, _ = _other_org_client(monkeypatch)
    assert other.put(f"/v1/calls/{call_id}/speakers", json={"rep": "Speaker 1"}).status_code == 404
    assert other.post(f"/v1/calls/{call_id}/reanalyze").status_code == 404
    assert client.post(f"/v1/calls/{uuid.uuid4()}/reanalyze").status_code == 404


# --- trends -------------------------------------------------------------------------------


def test_trends_count_calls_per_insight_within_the_window(monkeypatch):
    from fastapi.testclient import TestClient

    from callsight import auth, store
    from callsight.api import app

    monkeypatch.setenv("CALLSIGHT_DATABASE_URL", DB_URL)
    name = f"pytest-trends-{uuid.uuid4().hex[:8]}"
    with store.connect(DB_URL) as conn:
        key = auth.create_key(conn, name, "t")
    c = TestClient(app, headers={"authorization": f"Bearer {key}"})
    c.put("/v1/settings", json={"competitors": ["Gong"]})
    pricey = "[00:00] Alex: Hi\n[00:05] Priya: The price is too expensive. Too expensive!\n[00:09] Priya: We use Gong.\n"
    booked = "[00:00] Alex: Hi\n[00:05] Priya: The budget is tight.\n[00:09] Alex: Let's meet Monday at 4.\n"
    old = "[00:00] Alex: Hi\n[00:05] Priya: The price is way too high.\n"
    ids = [c.post("/v1/analyze", json={"transcript": t}).json()["id"] for t in (pricey, booked, old)]
    with store.connect(DB_URL) as conn, conn.transaction():
        conn.execute("UPDATE calls SET created_at = now() - interval '60 days' WHERE id = %s", (ids[2],))

    t = c.get("/v1/trends", params={"days": 30}).json()
    assert t["totals"]["calls"] == 2                       # the 60-day-old call is outside the window
    kinds = {k["kind"]: k["calls"] for k in t["kinds"]}
    assert kinds["pricing_objection"] == 2                 # counted per call, not per line
    assert t["competitors"] == [{"name": "Gong", "calls": 1}]
    assert t["totals"]["calls_without_next_step"] == 1    # only 'booked' agreed a time
    assert sum(w["calls"] for w in t["weekly"]) == 2
    assert {r["id"] for r in t["needs_attention"]} <= set(ids[:2])
    assert c.get("/v1/trends", params={"days": 90}).json()["totals"]["calls"] == 3
    assert c.get("/v1/trends", params={"days": 0}).status_code == 422


def test_trends_are_per_org(client, monkeypatch):
    other, _ = _other_org_client(monkeypatch)
    assert other.get("/v1/trends").json()["totals"]["calls"] == 0


# --- bulk re-analysis and export ------------------------------------------------------------


def _fresh_org_client(monkeypatch, prefix):
    from fastapi.testclient import TestClient

    from callsight import auth, store
    from callsight.api import app

    monkeypatch.setenv("CALLSIGHT_DATABASE_URL", DB_URL)
    with store.connect(DB_URL) as conn:
        key = auth.create_key(conn, f"{prefix}-{uuid.uuid4().hex[:8]}", "t")
    return TestClient(app, headers={"authorization": f"Bearer {key}"})


def test_bulk_reanalysis_updates_every_finished_call(monkeypatch):
    c = _fresh_org_client(monkeypatch, "pytest-bulk")
    said = "[00:00] Alex: Hi\n[00:05] Priya: We also talk to Blorpcall.\n"
    ids = [c.post("/v1/analyze", json={"call_id": f"b{i}", "transcript": said}).json()["id"] for i in range(3)]
    c.put("/v1/settings", json={"competitors": ["Blorpcall"]})
    res = c.post("/v1/reanalysis")  # TestClient runs the background task before returning
    assert res.status_code == 202 and res.json()["marked"] == 3
    assert c.get("/v1/reanalysis").json() == {"pending": 0}
    for i in ids:
        assert any(x["title"] == "Competitor mentioned: Blorpcall" for x in c.get(f"/v1/calls/{i}").json()["insights"])
    assert any(r["action"] == "calls.reanalyze_all" and r["details"] == {"calls": 3} for r in c.get("/v1/audit").json())


def test_csv_export_rows_filters_and_formula_safety(monkeypatch):
    import csv
    import io

    c = _fresh_org_client(monkeypatch, "pytest-export")
    c.post("/v1/analyze", json={"call_id": "=HYPERLINK(\"http://evil\")", "transcript":
                                "[00:00] Alex: Hi\n[00:05] Priya: The price is too expensive.\n"})
    c.post("/v1/analyze", json={"call_id": "quiet-call", "transcript": "[00:00] Alex: Hi\n[00:04] Priya: Hello.\n"})
    res = c.get("/v1/export/calls.csv")
    assert res.status_code == 200 and res.headers["content-type"].startswith("text/csv")
    assert "attachment" in res.headers["content-disposition"]
    rows = list(csv.DictReader(io.StringIO(res.text)))
    evil = [r for r in rows if "HYPERLINK" in r["call_name"]]
    assert evil and all(r["call_name"].startswith("'=") for r in evil)          # defused for spreadsheets
    pricing = next(r for r in evil if r["kind"] == "pricing_objection")
    assert pricing["evidence_time"] == "00:05" and pricing["evidence_speaker"] == "Priya"
    assert "too expensive" in pricing["evidence_quote"]
    assert {r["call_name"] for r in csv.DictReader(io.StringIO(c.get("/v1/export/calls.csv", params={"q": "quiet"}).text))} == {"quiet-call"}
    assert any(r["action"] == "calls.export" for r in c.get("/v1/audit").json())


def test_export_and_reanalysis_are_per_org(client, monkeypatch):
    marker = f"export-secret-{uuid.uuid4().hex[:6]}"
    client.post("/v1/analyze", json={"call_id": marker, "transcript": TRANSCRIPT})
    other = _fresh_org_client(monkeypatch, "pytest-other-export")
    assert marker not in other.get("/v1/export/calls.csv").text
    assert other.post("/v1/reanalysis").json()["marked"] == 0
