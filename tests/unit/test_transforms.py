"""The transformation registry.

These are the tests that matter most in the whole suite: every one of them
corresponds to a shape that actually appears in the legacy data, and two of
them correspond to bugs that were in this code and were found by writing the
test rather than by reading it.
"""

from __future__ import annotations

import pytest

from keystone.mapping.functions import get_transform, registered_transforms


def t(name: str, value: object, **kwargs: object) -> object:
    return get_transform(name)(value, **kwargs)


class TestText:
    def test_trim_turns_blank_into_none(self) -> None:
        assert t("trim", "   ") is None
        assert t("trim", "  Argos  ") == "Argos"
        assert t("trim", None) is None

    def test_collapse_spaces(self) -> None:
        assert t("collapse_spaces", "Oriflamme   Partners") == "Oriflamme Partners"

    def test_title_case_keeps_known_acronyms(self) -> None:
        assert t("title_case", "SARL DUPONT") == "SARL Dupont"
        assert t("title_case", "argos sa") == "Argos SA"

    def test_truncate_can_reject_instead_of_cutting(self) -> None:
        assert t("truncate", "abcdef", length=3) == "abc"
        with pytest.raises(ValueError, match="target allows 3"):
            t("truncate", "abcdef", length=3, on_overflow="reject")


class TestMojibake:
    def test_repairs_latin1_read_as_utf8(self) -> None:
        assert t("fix_mojibake", "SociÃ©tÃ© GÃ©nÃ©rale") == "Société Générale"

    def test_leaves_correct_text_alone(self) -> None:
        """Re-encoding correct text is how accents are destroyed a second time."""
        assert t("fix_mojibake", "Société Générale") == "Société Générale"
        assert t("fix_mojibake", "Argos SA") == "Argos SA"


class TestAmounts:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("12500", "12500.00"),
            ("12 500,00", "12500.00"),  # French, with a normal space
            ("12\u00a0500,00", "12500.00"),  # French, with the NBSP Excel emits
            ("12,500.00", "12500.00"),
            ("€12 500,00", "12500.00"),
            ("$ 12,500.00", "12500.00"),
            ("12 500,00 EUR", "12500.00"),  # regression: the code used to survive
            ("-1 200,50", "-1200.50"),
            ("(1 200,50)", "-1200.50"),
        ],
    )
    def test_parses_every_shape_in_the_source(self, raw: str, expected: str) -> None:
        assert t("parse_amount", raw) == expected

    @pytest.mark.parametrize("raw", ["", "n/c", "à définir", "-"])
    def test_refuses_what_is_not_a_number(self, raw: str) -> None:
        if raw == "":
            assert t("parse_amount", raw) is None
        else:
            with pytest.raises(ValueError):
                t("parse_amount", raw)


class TestPercentages:
    @pytest.mark.parametrize(
        ("raw", "expected"), [("70", 70), ("70%", 70), ("0.70", 70), ("0", 0), ("100", 100)]
    )
    def test_three_notations_one_meaning(self, raw: str, expected: int) -> None:
        assert t("parse_percentage", raw) == expected

    def test_refuses_out_of_range(self) -> None:
        with pytest.raises(ValueError, match="outside 0-100"):
            t("parse_percentage", "180")


class TestDates:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("2021-04-02", "2021-04-02"),
            ("02/04/2021", "2021-04-02"),
            ("24-07-19", "2019-07-24"),
            ("02.04.2021", "2021-04-02"),
            ("20180303", "2018-03-03"),
        ],
    )
    def test_every_format_the_legacy_system_allowed(self, raw: str, expected: str) -> None:
        assert t("parse_date", raw) == expected

    def test_day_first_is_a_decision_not_a_guess(self) -> None:
        """03/04 is April 3rd in the source and March 4th to a US reader."""
        assert t("parse_date", "03/04/2021", day_first=True) == "2021-04-03"
        assert t("parse_date", "03/04/2021", day_first=False) == "2021-03-04"

    @pytest.mark.parametrize("raw", ["", "n/a", "00/00/0000", "-"])
    def test_placeholders_become_none(self, raw: str) -> None:
        assert t("parse_date", raw) is None

    def test_refuses_an_unknown_format(self) -> None:
        with pytest.raises(ValueError, match="no known date format"):
            t("parse_date", "le 3 avril")


class TestPhones:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("0123456789", "+33123456789"),
            ("01.23.45.67.89", "+33123456789"),
            ("0033123456789", "+33123456789"),
            ("+33123456789", "+33123456789"),
            ("0123456789 poste 42", "+33123456789"),
            # Regression: the trunk prefix inside +33 (0)X used to survive,
            # producing a number one digit too long -- that still dials.
            ("+33 (0)1 23 45 67 89", "+33123456789"),
        ],
    )
    def test_normalises_to_e164(self, raw: str, expected: str) -> None:
        assert t("normalise_phone", raw) == expected

    def test_unusable_becomes_none_by_default(self) -> None:
        assert t("normalise_phone", "12") is None
        with pytest.raises(ValueError):
            t("normalise_phone", "12", on_invalid="reject")


class TestEmails:
    def test_normalises_and_keeps_the_first_address(self) -> None:
        assert (
            t("normalise_email", " JEAN.MARTIN@EXAMPLE.INVALID ") == "jean.martin@example.invalid"
        )
        assert t("normalise_email", "a@b.invalid;c@d.invalid") == "a@b.invalid"

    @pytest.mark.parametrize("raw", ["n/a", "", "  "])
    def test_placeholders_become_none(self, raw: str) -> None:
        assert t("normalise_email", raw) is None

    def test_invalid_rejects_or_nulls_as_declared(self) -> None:
        with pytest.raises(ValueError):
            t("normalise_email", "jean.martin(at)example.invalid")
        assert t("normalise_email", "jean.martin(at)example.invalid", on_invalid="null") is None


class TestBooleansAndCodes:
    @pytest.mark.parametrize(
        ("raw", "expected"), [("Y", True), ("O", True), ("1", True), ("N", False), ("non", False)]
    )
    def test_maps_the_flags_the_source_uses(self, raw: str, expected: bool) -> None:
        assert t("map_boolean", raw) is expected

    def test_null_takes_the_declared_default(self) -> None:
        assert t("map_boolean", None, default=False) is False
        assert t("map_boolean", "", default=True) is True

    def test_refuses_something_that_is_neither(self) -> None:
        with pytest.raises(ValueError, match="neither true nor false"):
            t("map_boolean", "peut-être")

    def test_postal_code_restores_the_leading_zero(self) -> None:
        assert t("postal_code", "7500") == "07500"
        assert t("postal_code", "75012") == "75012"


def test_every_transform_tolerates_none() -> None:
    """A mapping can chain transforms, and any of them may receive None.

    A transform that crashes on None turns an empty optional field into a
    rejected record, which is a bug that only shows up on the rows nobody
    thought about.
    """
    required_args: dict[str, dict[str, object]] = {
        "truncate": {"length": 10},
        "concat": {"prefix": "x"},
    }
    for name in registered_transforms():
        if name in {"constant", "default"}:
            continue  # these two exist precisely to replace a missing value
        assert get_transform(name)(None, **required_args.get(name, {})) is None, name
