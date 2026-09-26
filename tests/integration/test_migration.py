"""The whole migration, end to end, against a real PostgreSQL and the API.

Everything below is a property the README claims. A claim without a test that
would fail if it stopped being true is marketing.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from starlette.testclient import TestClient

from keystone import pipeline
from keystone.config import Settings
from keystone.db.engine import get_engine
from keystone.dryrun.gate import check_load_gate
from keystone.exceptions import MigrationBlocked
from keystone.load.client import AtlasClient
from keystone.load.crosswalk import CrosswalkStore
from keystone.mapping.loader import MappingSet
from tests.conftest import requires_database

pytestmark = [requires_database, pytest.mark.integration]


@pytest.fixture(scope="module")
def migrated(
    request: pytest.FixtureRequest,
) -> tuple[Settings, MappingSet, AtlasClient, TestClient]:
    """Seed, dry-run and load once; every test below inspects the result.

    Module-scoped because a full migration is the expensive part and each test
    is a question about the same outcome, not a different scenario.
    """
    settings: Settings = request.getfixturevalue("integration_settings")
    mapping_set: MappingSet = request.getfixturevalue("mapping_set")
    target: TestClient = request.getfixturevalue("module_target_client")
    client = AtlasClient(target, settings=settings)

    pipeline.seed(settings)
    pipeline.dry_run(settings, mapping_set=mapping_set)
    pipeline.load(client, settings, mapping_set=mapping_set)
    return settings, mapping_set, client, target


class TestTheGate:
    def test_a_load_without_a_dry_run_is_refused(
        self, integration_settings: Settings, mapping_set: MappingSet, atlas: AtlasClient
    ) -> None:
        """The whole point of the design: no dry-run, no load."""
        settings = integration_settings.model_copy(update={"migration_schema": "test_gate"})
        from keystone.db.schema import drop_schemas, initialise_database

        # Only the migration schema: the legacy data belongs to the other
        # tests in this module and must survive this one.
        drop_schemas(settings, include_legacy=False)
        initialise_database(settings)
        try:
            with pytest.raises(MigrationBlocked, match="no dry-run"):
                pipeline.load(atlas, settings, mapping_set=mapping_set)
        finally:
            drop_schemas(settings, include_legacy=False)

    def test_the_ceiling_blocks_a_migration_that_loses_too_much(
        self, migrated: tuple[Settings, MappingSet, AtlasClient, TestClient]
    ) -> None:
        settings, mapping_set, _, _ = migrated
        strict = settings.model_copy(update={"max_reject_rate_pct": 0.0})
        decision = check_load_gate(mapping_set.version, strict)
        assert not decision.allowed
        assert "rejection rate" in decision.reason

    def test_force_overrides_it_deliberately(
        self, migrated: tuple[Settings, MappingSet, AtlasClient, TestClient]
    ) -> None:
        settings, mapping_set, client, _ = migrated
        strict = settings.model_copy(update={"max_reject_rate_pct": 0.0})
        summary = pipeline.load(client, strict, mapping_set=mapping_set, force=True)
        assert summary.records_read > 0


class TestTheLoad:
    def test_every_entity_reached_the_target(
        self, migrated: tuple[Settings, MappingSet, AtlasClient, TestClient]
    ) -> None:
        _, mapping_set, client, _ = migrated
        for entity in mapping_set.order:
            entity_set = mapping_set[entity].target.entity_set
            assert client.count(entity_set) > 0, entity

    def test_the_crosswalk_matches_the_target(
        self, migrated: tuple[Settings, MappingSet, AtlasClient, TestClient]
    ) -> None:
        settings, mapping_set, client, _ = migrated
        counts = CrosswalkStore(settings).counts()
        for entity in mapping_set.order:
            assert counts.get(entity, 0) == client.count(mapping_set[entity].target.entity_set)

    def test_references_point_at_records_that_exist(
        self, migrated: tuple[Settings, MappingSet, AtlasClient, TestClient]
    ) -> None:
        """The target enforces this, so a contact in the target proves it."""
        _, _, _, target = migrated
        contacts = target.get("/api/v1/contacts", params={"$top": 20}).json()["value"]
        assert contacts
        accounts = target.get("/api/v1/accounts", params={"$top": 1000}).json()["value"]
        known_ids = {account["id"] for account in accounts}
        for contact in contacts:
            assert contact["account_id"] in known_ids
        assert all(contact["account_id"] for contact in contacts)

    def test_transformations_were_applied(
        self, migrated: tuple[Settings, MappingSet, AtlasClient, TestClient]
    ) -> None:
        """Spot-check the target for the shapes the source is full of."""
        _, _, _, target = migrated
        accounts = target.get("/api/v1/accounts", params={"$top": 1000}).json()["value"]
        assert accounts
        assert all(
            account["country_code"] in {"FR", "BE", "CH", "LU", "IT", "ES", "DE"}
            for account in accounts
        )
        assert not any("Ã" in (account["name"] or "") for account in accounts), "mojibake survived"
        assert not any(account["name"].startswith(" ") for account in accounts)
        phones = [a["telephone"] for a in accounts if a.get("telephone")]
        assert phones and all(phone.startswith("+") for phone in phones)


class TestIdempotency:
    def test_a_second_load_writes_nothing(
        self, migrated: tuple[Settings, MappingSet, AtlasClient, TestClient]
    ) -> None:
        settings, mapping_set, client, target = migrated
        before = target.get("/admin/stats").json()
        summary = pipeline.load(client, settings, mapping_set=mapping_set)
        after = target.get("/admin/stats").json()

        assert summary.created == 0
        assert summary.updated == 0
        assert summary.skipped > 0
        assert after["writes_applied"] == before["writes_applied"]
        assert after["counts"] == before["counts"]

    def test_a_changed_source_record_produces_exactly_one_update(
        self, migrated: tuple[Settings, MappingSet, AtlasClient, TestClient]
    ) -> None:
        settings, mapping_set, client, _ = migrated
        with get_engine(settings).begin() as conn:
            victim = conn.execute(
                text(
                    f"""SELECT custno FROM {settings.legacy_schema}.cust
                        WHERE COALESCE(del_flag,'N') <> 'Y' ORDER BY custno LIMIT 1"""
                )
            ).scalar_one()
            conn.execute(
                text(
                    f"""UPDATE {settings.legacy_schema}.cust
                        SET company = company || ' Holdings' WHERE custno = :id"""
                ),
                {"id": victim},
            )

        summary = pipeline.load(client, settings, mapping_set=mapping_set, entities=["account"])
        assert summary.updated == 1
        assert summary.created == 0

    def test_cosmetic_source_changes_produce_no_write_at_all(
        self, migrated: tuple[Settings, MappingSet, AtlasClient, TestClient]
    ) -> None:
        """The content hash is computed after mapping, not before.

        Re-typing a date in another format changes the source row and not the
        target payload, so nothing should be written.
        """
        settings, mapping_set, client, _ = migrated
        with get_engine(settings).begin() as conn:
            conn.execute(
                text(
                    f"""UPDATE {settings.legacy_schema}.cust
                        SET company = '  ' || company || '  '
                        WHERE COALESCE(del_flag,'N') <> 'Y'"""
                )
            )
        summary = pipeline.load(client, settings, mapping_set=mapping_set, entities=["account"])
        assert summary.created == 0
        assert summary.updated == 0


class TestRejections:
    def test_rejections_are_recorded_with_their_cause(
        self, migrated: tuple[Settings, MappingSet, AtlasClient, TestClient]
    ) -> None:
        settings, _, _, _ = migrated
        from keystone.load.rejects import RejectStore

        summary = RejectStore(settings).summary()
        assert summary, "the generator injects defects; zero rejections means nothing is checked"
        assert all(row["rule"] for row in summary), "a rejection without a rule is not actionable"
        assert all(row["stage"] in {"extract", "map", "validate", "load"} for row in summary)

    def test_repeated_failures_are_eventually_abandoned(
        self, migrated: tuple[Settings, MappingSet, AtlasClient, TestClient]
    ) -> None:
        settings, mapping_set, client, _ = migrated
        for _ in range(settings.max_retry_attempts + 1):
            pipeline.load(client, settings, mapping_set=mapping_set, entities=["account"])
        from keystone.load.rejects import RejectStore

        statuses = RejectStore(settings).counts_by_status()
        assert statuses.get("ABANDONED", 0) > 0


class TestReconciliation:
    def test_source_crosswalk_and_target_agree(
        self, migrated: tuple[Settings, MappingSet, AtlasClient, TestClient]
    ) -> None:
        settings, mapping_set, client, _ = migrated
        report = pipeline.reconcile_migration(client, settings, mapping_set=mapping_set)
        mismatches = [result.name for result in report.mismatches]
        assert not mismatches, mismatches

    def test_the_target_never_holds_more_money_than_the_source(
        self, migrated: tuple[Settings, MappingSet, AtlasClient, TestClient]
    ) -> None:
        settings, mapping_set, client, _ = migrated
        report = pipeline.reconcile_migration(client, settings, mapping_set=mapping_set)
        amount_check = next(r for r in report.results if r.name == "opportunity_amount_total")
        assert amount_check.status == "OK"
