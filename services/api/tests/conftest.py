import os

import pytest


@pytest.fixture(scope="session", autouse=True)
def _migrated_test_database():
    """Bring the integration-test database up to the current schema once per run."""
    url = os.environ.get("CALLSIGHT_TEST_DATABASE_URL")
    if url:
        from callsight.migrate import migrate

        migrate(url)
