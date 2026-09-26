"""Shared fixtures.

Two decisions worth stating.

**Integration tests use a real PostgreSQL.** The thing under test is largely
SQL and the behaviour of a real driver -- upserts, JSONB, ``ON CONFLICT``,
array parameters. A mocked database would prove that the mock works.

**The target runs in-process.** ``TestClient`` subclasses ``httpx.Client``, so
the same ``AtlasClient`` the migration uses in production can be pointed at
the app object directly. The client's own code path is therefore the one under
test, rather than a fake that resembles it.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from keystone.config import Settings, get_settings
from keystone.db.engine import check_connection, dispose_engines
from keystone.db.schema import drop_schemas, initialise_database
from keystone.load.client import AtlasClient
from keystone.mapping.loader import MappingSet, load_mapping_set
from keystone.target.app import create_app
from keystone.target.dependencies import get_store

REPO_ROOT = Path(__file__).resolve().parents[1]

requires_database = pytest.mark.skipif(
    os.environ.get("KEYSTONE_RUN_INTEGRATION") != "1",
    reason="set KEYSTONE_RUN_INTEGRATION=1 and provide a PostgreSQL to run these",
)


@pytest.fixture(scope="session")
def mapping_set() -> MappingSet:
    """The repository's own mappings -- the ones that ship."""
    return load_mapping_set(directory=REPO_ROOT / "mappings")


def _fault_free_target() -> Iterator[TestClient]:
    """A fresh target with fault injection switched off.

    Faults are exercised deliberately in the tests that are about retrying. A
    suite that fails one run in twenty for reasons unrelated to the change
    under review is a suite people learn to re-run instead of read.
    """
    previous = {
        key: os.environ.get(key)
        for key in ("KEYSTONE_TARGET_FAULT_RATE", "KEYSTONE_TARGET_RATE_LIMIT_RATE")
    }
    os.environ["KEYSTONE_TARGET_FAULT_RATE"] = "0"
    os.environ["KEYSTONE_TARGET_RATE_LIMIT_RATE"] = "0"
    # `get_settings` is cached for the life of the process, so changing the
    # environment is not enough on its own -- the app would keep reading the
    # settings object built before this fixture ran, faults and all.
    get_settings.cache_clear()

    get_store().reset()
    with TestClient(create_app()) as client:
        client.headers.update({"X-API-Key": Settings().target_api_key.get_secret_value()})
        yield client
    get_store().reset()

    for key, value in previous.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    get_settings.cache_clear()


@pytest.fixture
def target_app_client() -> Iterator[TestClient]:
    yield from _fault_free_target()


@pytest.fixture(scope="module")
def module_target_client() -> Iterator[TestClient]:
    """The same target, kept for a whole module.

    A full migration is expensive; the tests that inspect its result all ask
    questions about the same outcome and must share it.
    """
    yield from _fault_free_target()


@pytest.fixture
def atlas(target_app_client: TestClient) -> AtlasClient:
    return AtlasClient(target_app_client, settings=Settings())


@pytest.fixture(scope="session")
def integration_settings(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Settings]:
    """A small, isolated migration: its own schemas, its own data directory."""
    settings = Settings(
        legacy_schema="test_legacy",
        migration_schema="test_migration",
        data_dir=tmp_path_factory.mktemp("keystone-data"),
        mapping_dir=REPO_ROOT / "mappings",
        n_accounts=60,
        n_contacts=150,
        n_opportunities=90,
        n_activities=200,
        batch_size=25,
        max_reject_rate_pct=100.0,
        target_fault_rate=0.0,
        target_rate_limit_rate=0.0,
        log_level="WARNING",
    )
    if not check_connection(settings, retries=2):
        pytest.skip("no PostgreSQL available")
    drop_schemas(settings)
    initialise_database(settings)
    yield settings
    drop_schemas(settings)
    dispose_engines()
