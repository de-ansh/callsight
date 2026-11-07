import os
import uuid

import pytest

DB_URL = os.environ.get("CALLSIGHT_TEST_DATABASE_URL")


@pytest.fixture(scope="session", autouse=True)
def _migrated_test_database():
    """Bring the integration-test database up to the current schema once per run."""
    if DB_URL:
        from callsight.migrate import migrate

        migrate(DB_URL)


@pytest.fixture(scope="session")
def org():
    """A fresh organization with one API key, so each test run sees only its own calls."""
    if not DB_URL:
        pytest.skip("CALLSIGHT_TEST_DATABASE_URL not set")
    from callsight import auth, store

    name = f"pytest-{uuid.uuid4().hex[:8]}"
    with store.connect(DB_URL) as conn:
        key = auth.create_key(conn, name, "pytest")
        row = conn.execute("SELECT id, webhook_secret FROM orgs WHERE name = %s", (name,)).fetchone()
    return {"name": name, "id": row["id"], "key": key, "webhook_secret": row["webhook_secret"]}


@pytest.fixture
def client(org, monkeypatch):
    """A TestClient authenticated as `org`, with the test database configured."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from callsight.api import app

    monkeypatch.setenv("CALLSIGHT_DATABASE_URL", DB_URL)
    return TestClient(app, headers={"authorization": f"Bearer {org['key']}"})


@pytest.fixture
def fake_principal():
    """Skip API-key lookup for tests that only exercise request validation (no database)."""
    pytest.importorskip("fastapi")
    from callsight.api import app, require_key
    from callsight.auth import Principal

    app.dependency_overrides[require_key] = lambda: Principal(uuid.uuid4(), "test")
    yield
    app.dependency_overrides.pop(require_key, None)
