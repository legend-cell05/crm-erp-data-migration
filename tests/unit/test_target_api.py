"""The simulated Atlas Cloud, tested as a client would meet it.

These tests are about the *target's* contract, not about keystone: they pin
the behaviour the migration is written against, so that changing the simulator
to be more forgiving cannot quietly weaken the tests above it.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

ACCOUNT = {
    "arcadia_id": "C-000001",
    "name": "Argos SA",
    "country_code": "FR",
    "status": "active",
    "source_system": "arcadia",
}


def batch(client: TestClient, entity_set: str, records: list[dict[str, object]]):  # type: ignore[no-untyped-def]
    return client.post(f"/api/v1/{entity_set}/$batch", json={"records": records})


class TestAuthentication:
    def test_a_missing_key_is_refused(self, target_app_client: TestClient) -> None:
        response = target_app_client.post(
            "/api/v1/accounts/$batch", json={"records": [ACCOUNT]}, headers={"X-API-Key": ""}
        )
        assert response.status_code == 401

    def test_a_wrong_key_is_refused(self, target_app_client: TestClient) -> None:
        response = target_app_client.post(
            "/api/v1/accounts/$batch", json={"records": [ACCOUNT]}, headers={"X-API-Key": "nope"}
        )
        assert response.status_code == 401
        assert response.json()["detail"]["code"] == "INVALID_API_KEY"

    def test_health_needs_no_key(self, target_app_client: TestClient) -> None:
        assert target_app_client.get("/health").status_code == 200


class TestUpsert:
    def test_the_same_record_twice_is_one_record(self, target_app_client: TestClient) -> None:
        first = batch(target_app_client, "accounts", [ACCOUNT])
        second = batch(target_app_client, "accounts", [ACCOUNT])
        assert first.json()["results"][0]["status"] == "created"
        assert second.json()["results"][0]["status"] == "unchanged"
        assert target_app_client.get("/api/v1/accounts/$count").json()["count"] == 1

    def test_a_changed_record_updates_in_place(self, target_app_client: TestClient) -> None:
        batch(target_app_client, "accounts", [ACCOUNT])
        changed = {**ACCOUNT, "name": "Argos SAS"}
        result = batch(target_app_client, "accounts", [changed]).json()["results"][0]
        assert result["status"] == "updated"
        assert target_app_client.get("/api/v1/accounts/$count").json()["count"] == 1

    def test_unchanged_is_distinct_from_updated(self, target_app_client: TestClient) -> None:
        """A re-run must be able to prove it changed nothing."""
        batch(target_app_client, "accounts", [ACCOUNT])
        stats_before = target_app_client.get("/admin/stats").json()["writes_applied"]
        batch(target_app_client, "accounts", [ACCOUNT])
        assert target_app_client.get("/admin/stats").json()["writes_applied"] == stats_before


class TestValidation:
    def test_one_bad_record_does_not_sink_the_batch(self, target_app_client: TestClient) -> None:
        records = [ACCOUNT, {**ACCOUNT, "arcadia_id": "C-2", "country_code": "XX"}]
        response = batch(target_app_client, "accounts", records)
        assert response.status_code == 207
        statuses = [r["status"] for r in response.json()["results"]]
        assert statuses == ["created", "failed"]

    def test_an_unknown_field_is_refused_rather_than_dropped(
        self, target_app_client: TestClient
    ) -> None:
        """Silently dropping it would lose a column and report success."""
        response = batch(target_app_client, "accounts", [{**ACCOUNT, "telephone2": "0123456789"}])
        error = response.json()["results"][0]["error"]
        assert error["code"] == "UNKNOWN_FIELD"
        assert error["field"] == "telephone2"

    @pytest.mark.parametrize(
        ("mutation", "code"),
        [
            ({"country_code": None}, "REQUIRED_FIELD"),
            ({"status": "actif"}, "INVALID_VALUE"),
            ({"name": "x" * 300}, "VALUE_TOO_LONG"),
            ({"arcadia_id": "  "}, "MISSING_ALTERNATE_KEY"),
        ],
    )
    def test_each_rule_reports_its_own_code(
        self, target_app_client: TestClient, mutation: dict[str, object], code: str
    ) -> None:
        response = batch(target_app_client, "accounts", [{**ACCOUNT, **mutation}])
        assert response.json()["results"][0]["error"]["code"] == code

    def test_referential_integrity_is_enforced(self, target_app_client: TestClient) -> None:
        contact = {
            "arcadia_id": "P-1",
            "account_id": "00000000-0000-0000-0000-000000000000",
            "last_name": "Martin",
            "source_system": "arcadia",
        }
        response = batch(target_app_client, "contacts", [contact])
        assert response.json()["results"][0]["error"]["code"] == "REFERENCE_NOT_FOUND"

    def test_a_resolvable_reference_is_accepted(self, target_app_client: TestClient) -> None:
        created = batch(target_app_client, "accounts", [ACCOUNT]).json()["results"][0]
        contact = {
            "arcadia_id": "P-1",
            "account_id": created["id"],
            "last_name": "Martin",
            "source_system": "arcadia",
        }
        assert (
            batch(target_app_client, "contacts", [contact]).json()["results"][0]["status"]
            == "created"
        )


class TestLimits:
    def test_an_oversized_batch_is_permanent_not_transient(
        self, target_app_client: TestClient
    ) -> None:
        """413, not 429: waiting will never make 300 records acceptable."""
        records = [{**ACCOUNT, "arcadia_id": f"C-{i}"} for i in range(300)]
        response = batch(target_app_client, "accounts", records)
        assert response.status_code == 413
        assert response.json()["detail"]["max_batch_size"] == 250

    def test_an_unknown_entity_set_is_a_404_that_names_the_known_ones(
        self, target_app_client: TestClient
    ) -> None:
        response = batch(target_app_client, "invoices", [ACCOUNT])
        assert response.status_code == 404
        assert "accounts" in response.json()["detail"]["known"]


class TestMetadata:
    def test_the_contract_is_published_not_documented(self, target_app_client: TestClient) -> None:
        metadata = target_app_client.get("/api/v1/$metadata").json()
        assert metadata["load_order"] == ["accounts", "contacts", "opportunities", "activities"]
        assert (
            metadata["entity_sets"]["contacts"]["fields"]["account_id"]["references"] == "accounts"
        )

    def test_the_published_limit_is_the_enforced_limit(self, target_app_client: TestClient) -> None:
        limit = target_app_client.get("/api/v1/$metadata").json()["max_batch_size"]
        records = [{**ACCOUNT, "arcadia_id": f"C-{i}"} for i in range(limit + 1)]
        assert batch(target_app_client, "accounts", records).status_code == 413


class TestChecksum:
    def test_totals_a_numeric_field(self, target_app_client: TestClient) -> None:
        account = batch(target_app_client, "accounts", [ACCOUNT]).json()["results"][0]
        deals = [
            {
                "arcadia_id": f"D-{i}",
                "account_id": account["id"],
                "name": "Deal",
                "amount": "100.50",
                "stage": "new",
                "source_system": "arcadia",
            }
            for i in range(3)
        ]
        batch(target_app_client, "opportunities", deals)
        total = target_app_client.get(
            "/api/v1/opportunities/$checksum", params={"field": "amount"}
        ).json()["total"]
        assert total == "301.50"
