"""Applying a mapping to a record.

Every test here works on a hand-built mapping and a hand-built row, so a
failure points at the engine rather than at the data.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from keystone.exceptions import (
    MappingViolation,
    ReferenceNotResolved,
    ValidationViolation,
)
from keystone.mapping.engine import MappingEngine, content_hash
from keystone.mapping.models import EntityMapping


@pytest.fixture
def lookup_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "lookups"
    directory.mkdir()
    (directory / "country.csv").write_text(
        "source;target\nFR;FR\nFrance;FR\nFRANCE;FR\nBE;BE\n", encoding="utf-8"
    )
    return directory


def build(**overrides: object) -> EntityMapping:
    document: dict[str, object] = {
        "version": "1.0.0",
        "entity": "account",
        "source": {"kind": "sql", "schema": "legacy", "table": "cust", "key": "custno"},
        "target": {"entity_set": "accounts", "alternate_key": "arcadia_id"},
        "fields": [
            {
                "target": "arcadia_id",
                "source": "custno",
                "transform": ["trim", "upper"],
                "required": True,
            },
            {
                "target": "name",
                "source": "company",
                "transform": ["fix_mojibake", "collapse_spaces"],
                "required": True,
            },
            {
                "target": "country_code",
                "source": "ctry",
                "transform": ["trim"],
                "lookup": {"table": "country", "on_miss": "reject"},
            },
            {"target": "created_on", "source": "created", "transform": [{"fn": "parse_date"}]},
        ],
    }
    document.update(overrides)
    return EntityMapping.model_validate(document)


ROW = {
    "custno": " c-000001 ",
    "company": "  Argos   SociÃ©tÃ©  ",
    "ctry": "France",
    "created": "02/04/2021",
}


class Resolver:
    def __init__(self, known: dict[tuple[str, str], str] | None = None) -> None:
        self.known = known or {}

    def resolve(self, entity: str, source_id: str) -> str | None:
        return self.known.get((entity, source_id))


class TestHappyPath:
    def test_transforms_run_in_order_and_lookups_apply(self, lookup_dir: Path) -> None:
        record = MappingEngine(build(), lookup_dir=lookup_dir).apply(ROW)
        assert record.source_id == "c-000001"
        assert record.alternate_key == "C-000001"
        assert record.payload == {
            "arcadia_id": "C-000001",
            "name": "Argos Société",
            "country_code": "FR",
            "created_on": "2021-04-02",
        }


class TestContentHash:
    """The hash decides whether a re-run writes anything."""

    def test_cosmetic_differences_produce_the_same_hash(self, lookup_dir: Path) -> None:
        engine = MappingEngine(build(), lookup_dir=lookup_dir)
        messy = {
            "custno": "C-000001",
            "company": "Argos    Société",
            "ctry": "  FRANCE ",
            "created": "2021-04-02",
        }
        assert engine.apply(ROW).content_hash == engine.apply(messy).content_hash

    def test_a_real_difference_changes_it(self, lookup_dir: Path) -> None:
        engine = MappingEngine(build(), lookup_dir=lookup_dir)
        changed = {**ROW, "company": "Argos SAS"}
        assert engine.apply(ROW).content_hash != engine.apply(changed).content_hash

    def test_key_order_does_not_matter(self) -> None:
        assert content_hash({"a": 1, "b": 2}) == content_hash({"b": 2, "a": 1})


class TestRejections:
    def test_a_lookup_miss_names_the_field_and_the_table(self, lookup_dir: Path) -> None:
        with pytest.raises(MappingViolation) as caught:
            MappingEngine(build(), lookup_dir=lookup_dir).apply({**ROW, "ctry": "Frnace"})
        assert caught.value.field == "country_code"
        assert caught.value.rule == "lookup:country"
        assert caught.value.source_id == "c-000001"

    def test_on_miss_null_keeps_the_record(self, lookup_dir: Path) -> None:
        mapping = build(
            fields=[
                {"target": "arcadia_id", "source": "custno", "required": True},
                {
                    "target": "country_code",
                    "source": "ctry",
                    "lookup": {"table": "country", "on_miss": "null"},
                },
            ]
        )
        record = MappingEngine(mapping, lookup_dir=lookup_dir).apply({**ROW, "ctry": "Frnace"})
        assert record.payload["country_code"] is None

    def test_on_miss_default_substitutes(self, lookup_dir: Path) -> None:
        mapping = build(
            fields=[
                {"target": "arcadia_id", "source": "custno", "required": True},
                {
                    "target": "country_code",
                    "source": "ctry",
                    "lookup": {"table": "country", "on_miss": "default", "default": "XX"},
                },
            ]
        )
        record = MappingEngine(mapping, lookup_dir=lookup_dir).apply({**ROW, "ctry": "Frnace"})
        assert record.payload["country_code"] == "XX"

    def test_a_required_field_that_maps_to_nothing_is_rejected(self, lookup_dir: Path) -> None:
        with pytest.raises(ValidationViolation) as caught:
            MappingEngine(build(), lookup_dir=lookup_dir).apply({**ROW, "company": "   "})
        assert caught.value.rule == "required"
        assert caught.value.field == "name"

    def test_a_failing_transform_names_the_transform(self, lookup_dir: Path) -> None:
        with pytest.raises(MappingViolation) as caught:
            MappingEngine(build(), lookup_dir=lookup_dir).apply({**ROW, "created": "le 3 avril"})
        assert caught.value.rule == "parse_date"


class TestReferences:
    def _mapping(self) -> EntityMapping:
        return build(
            entity="contact",
            target={"entity_set": "contacts", "alternate_key": "arcadia_id"},
            fields=[
                {"target": "arcadia_id", "source": "persno", "required": True},
                {
                    "target": "account_id",
                    "source": "custno",
                    "reference": {"entity": "account", "on_missing": "reject"},
                    "required": True,
                },
            ],
        )

    def test_a_reference_resolves_through_the_crosswalk(self, lookup_dir: Path) -> None:
        resolver = Resolver({("account", "C-1"): "atlas-uuid-1"})
        engine = MappingEngine(self._mapping(), lookup_dir=lookup_dir, resolver=resolver)
        record = engine.apply({"persno": "P-1", "custno": "C-1"})
        assert record.payload["account_id"] == "atlas-uuid-1"

    def test_an_unresolved_reference_is_rejected_with_its_cause(self, lookup_dir: Path) -> None:
        engine = MappingEngine(self._mapping(), lookup_dir=lookup_dir, resolver=Resolver())
        with pytest.raises(ReferenceNotResolved) as caught:
            engine.apply({"persno": "P-1", "custno": "C-404"})
        assert caught.value.rule == "reference:account"
        assert "C-404" in str(caught.value)

    def test_a_mapping_with_a_reference_and_no_resolver_fails_at_load_time(
        self, lookup_dir: Path
    ) -> None:
        """Better to fail when the mapping is loaded than on record 40 000."""
        from keystone.exceptions import MappingError

        with pytest.raises(MappingError, match="no resolver"):
            MappingEngine(self._mapping(), lookup_dir=lookup_dir)


class TestValidations:
    def test_a_reject_severity_stops_the_record(self, lookup_dir: Path) -> None:
        mapping = build(
            validations=[{"rule": "not_after_today", "fields": ["created_on"]}],
        )
        with pytest.raises(ValidationViolation) as caught:
            MappingEngine(mapping, lookup_dir=lookup_dir).apply({**ROW, "created": "01/01/2099"})
        assert caught.value.rule == "not_after_today"

    def test_a_warn_severity_keeps_it_and_records_the_warning(self, lookup_dir: Path) -> None:
        mapping = build(
            validations=[{"rule": "not_after_today", "fields": ["created_on"], "severity": "warn"}],
        )
        record = MappingEngine(mapping, lookup_dir=lookup_dir).apply(
            {**ROW, "created": "01/01/2099"}
        )
        assert record.warnings and record.warnings[0].startswith("not_after_today")

    def test_an_unknown_rule_fails_when_the_mapping_loads(self, lookup_dir: Path) -> None:
        from keystone.exceptions import MappingError

        mapping = build(validations=[{"rule": "no_such_rule", "fields": ["name"]}])
        with pytest.raises(MappingError, match="no_such_rule"):
            MappingEngine(mapping, lookup_dir=lookup_dir)

    def test_an_unknown_transform_fails_when_the_mapping_loads(self, lookup_dir: Path) -> None:
        from keystone.exceptions import MappingError

        mapping = build(
            fields=[{"target": "arcadia_id", "source": "custno", "transform": ["shrink"]}]
        )
        with pytest.raises(MappingError, match="shrink"):
            MappingEngine(mapping, lookup_dir=lookup_dir)
