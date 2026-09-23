"""The mapping schema, the loader, and the fingerprint.

A mapping file is configuration that decides what happens to a company's data.
It therefore gets the same treatment as code: a strict schema, a typo is an
error, and the exact content that was approved is identifiable afterwards.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from keystone.exceptions import MappingError
from keystone.mapping.loader import _topological_order, load_mapping_set
from keystone.mapping.models import EntityMapping

MINIMAL: dict[str, object] = {
    "version": "1.0.0",
    "entity": "account",
    "source": {"kind": "sql", "schema": "legacy", "table": "cust", "key": "custno"},
    "target": {"entity_set": "accounts", "alternate_key": "arcadia_id"},
    "fields": [{"target": "arcadia_id", "source": "custno", "required": True}],
}


def write(tmp_path: Path, name: str, document: dict[str, object]) -> Path:
    path = tmp_path / f"{name}.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return path


class TestSchema:
    def test_a_minimal_mapping_is_valid(self) -> None:
        mapping = EntityMapping.model_validate(MINIMAL)
        assert mapping.entity == "account"
        assert mapping.target_fields == ["arcadia_id"]

    def test_an_unknown_key_is_an_error_not_a_shrug(self) -> None:
        """`requierd: true` must fail loudly rather than be ignored."""
        document = dict(MINIMAL)
        document["fields"] = [{"target": "x", "source": "y", "requierd": True}]
        with pytest.raises(Exception, match="requierd"):
            EntityMapping.model_validate(document)

    def test_version_must_be_semantic(self) -> None:
        with pytest.raises(Exception, match=r"1\.2\.3"):
            EntityMapping.model_validate({**MINIMAL, "version": "v1"})

    def test_a_field_mapped_twice_is_refused(self) -> None:
        document = dict(MINIMAL)
        document["fields"] = [
            {"target": "arcadia_id", "source": "custno"},
            {"target": "arcadia_id", "source": "other"},
        ]
        with pytest.raises(Exception, match="mapped twice"):
            EntityMapping.model_validate(document)

    def test_the_alternate_key_must_be_produced(self) -> None:
        document = dict(MINIMAL)
        document["target"] = {"entity_set": "accounts", "alternate_key": "missing_field"}
        with pytest.raises(Exception, match="alternate key"):
            EntityMapping.model_validate(document)

    def test_a_field_with_no_source_and_no_constant_is_refused(self) -> None:
        document = dict(MINIMAL)
        document["fields"] = [
            {"target": "arcadia_id", "source": "custno"},
            {"target": "orphan"},
        ]
        with pytest.raises(Exception, match="no `source`"):
            EntityMapping.model_validate(document)

    def test_a_constant_field_needs_no_source(self) -> None:
        document = dict(MINIMAL)
        document["fields"] = [
            {"target": "arcadia_id", "source": "custno"},
            {
                "target": "source_system",
                "transform": [{"fn": "constant", "value_to_use": "arcadia"}],
            },
        ]
        assert len(EntityMapping.model_validate(document).fields) == 2


class TestFilterSafety:
    """The source filter is the one place SQL comes from a file."""

    @pytest.mark.parametrize(
        "malicious",
        [
            "1=1; DROP SCHEMA migration CASCADE",
            "1=1 -- comment",
            "1=1 UNION SELECT * FROM migration.crosswalk",
            "1=1 /* block */",
            "del_flag <> 'Y' DELETE FROM cust",
        ],
    )
    def test_dangerous_filters_are_refused_before_a_connection_is_opened(
        self, malicious: str
    ) -> None:
        document = dict(MINIMAL)
        document["source"] = {
            "kind": "sql",
            "schema": "legacy",
            "table": "cust",
            "key": "custno",
            "filter": malicious,
        }
        with pytest.raises(Exception, match=r"terminators|comment|modifying"):
            EntityMapping.model_validate(document)

    def test_an_ordinary_filter_is_allowed(self) -> None:
        document = dict(MINIMAL)
        document["source"] = {
            "kind": "sql",
            "schema": "legacy",
            "table": "cust",
            "key": "custno",
            "filter": "COALESCE(del_flag, 'N') <> 'Y'",
        }
        assert EntityMapping.model_validate(document).source.filter is not None


class TestLoader:
    def test_dependencies_come_first(self) -> None:
        entities = {
            name: EntityMapping.model_validate({**MINIMAL, "entity": name, "depends_on": deps})
            for name, deps in {
                "activity": ["account", "contact"],
                "contact": ["account"],
                "account": [],
            }.items()
        }
        order = _topological_order(entities)
        assert order.index("account") < order.index("contact") < order.index("activity")

    def test_a_cycle_is_an_error_not_a_hang(self) -> None:
        entities = {
            name: EntityMapping.model_validate({**MINIMAL, "entity": name, "depends_on": deps})
            for name, deps in {"a": ["b"], "b": ["a"]}.items()
        }
        with pytest.raises(MappingError, match="circular"):
            _topological_order(entities)

    def test_a_dependency_with_no_mapping_is_an_error(self) -> None:
        entities = {
            "contact": EntityMapping.model_validate(
                {**MINIMAL, "entity": "contact", "depends_on": ["account"]}
            )
        }
        with pytest.raises(MappingError, match="which has no mapping"):
            _topological_order(entities)

    def test_the_file_name_must_match_the_entity(self, tmp_path: Path) -> None:
        (tmp_path / "lookups").mkdir()
        write(tmp_path, "accounts", MINIMAL)  # file says accounts, entity says account
        with pytest.raises(MappingError, match="declares entity"):
            load_mapping_set(directory=tmp_path)

    def test_invalid_yaml_names_the_file(self, tmp_path: Path) -> None:
        (tmp_path / "lookups").mkdir()
        (tmp_path / "account.yaml").write_text("fields: [ unclosed", encoding="utf-8")
        with pytest.raises(MappingError, match=r"account\.yaml"):
            load_mapping_set(directory=tmp_path)


class TestFingerprint:
    """The fingerprint is what ties a dry-run to the exact content approved."""

    def _build(self, tmp_path: Path) -> Path:
        (tmp_path / "lookups").mkdir(exist_ok=True)
        write(tmp_path, "account", MINIMAL)
        (tmp_path / "lookups" / "country.csv").write_text(
            "source;target\nFR;FR\n", encoding="utf-8"
        )
        return tmp_path

    def test_identical_content_gives_an_identical_fingerprint(self, tmp_path: Path) -> None:
        directory = self._build(tmp_path)
        assert load_mapping_set(directory=directory).fingerprint == (
            load_mapping_set(directory=directory).fingerprint
        )

    def test_editing_a_mapping_changes_it(self, tmp_path: Path) -> None:
        directory = self._build(tmp_path)
        before = load_mapping_set(directory=directory).fingerprint
        document = dict(MINIMAL)
        document["fields"] = [{"target": "arcadia_id", "source": "custno", "transform": ["trim"]}]
        write(directory, "account", document)
        assert load_mapping_set(directory=directory).fingerprint != before

    def test_editing_a_lookup_changes_it_too(self, tmp_path: Path) -> None:
        """This is the case a file-version number cannot catch."""
        directory = self._build(tmp_path)
        before = load_mapping_set(directory=directory).fingerprint
        (directory / "lookups" / "country.csv").write_text(
            "source;target\nFR;FR\nBE;BE\n", encoding="utf-8"
        )
        after = load_mapping_set(directory=directory)
        assert after.fingerprint != before
        assert after.version.startswith("1.0.0+")


def test_the_shipped_mappings_load_and_order_correctly(mapping_set) -> None:  # type: ignore[no-untyped-def]
    assert mapping_set.order == ("account", "contact", "opportunity", "activity")
    assert len(mapping_set) == 4
    assert mapping_set.version.startswith("1.3.0+")
