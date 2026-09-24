"""What Atlas Cloud accepts.

The target's own contract, written from the target's point of view: required
fields, maximum lengths, allowed enum values, and which fields are references
to other entity sets. keystone's mappings must satisfy this, and the
simulator enforces it -- including the parts a naive client would rather it
did not, such as refusing unknown fields.

Rejecting unknown fields matters more than it looks. A target that silently
drops ``telephone2`` because nobody defined it is a target that reports a
successful migration and loses a column, and the discovery happens in a sales
meeting six weeks later.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class FieldRule:
    required: bool = False
    max_length: int | None = None
    allowed: frozenset[str] | None = None
    references: str | None = None  # the entity set this field points at


@dataclass(frozen=True)
class EntitySchema:
    """One entity set's contract."""

    name: str
    alternate_key: str
    fields: dict[str, FieldRule] = field(default_factory=dict)

    def unknown_fields(self, payload: dict[str, object]) -> list[str]:
        return sorted(set(payload) - set(self.fields))


_COMMON = {
    "source_system": FieldRule(required=True, max_length=40),
    "owner_code": FieldRule(max_length=10),
    "created_on": FieldRule(),
}

COUNTRIES = frozenset({"FR", "BE", "CH", "LU", "IT", "ES", "DE", "NL", "PT", "GB"})

SCHEMAS: dict[str, EntitySchema] = {
    "accounts": EntitySchema(
        name="accounts",
        alternate_key="arcadia_id",
        fields={
            "arcadia_id": FieldRule(required=True, max_length=12),
            "name": FieldRule(required=True, max_length=160),
            "street": FieldRule(max_length=250),
            "street_2": FieldRule(max_length=250),
            "city": FieldRule(max_length=80),
            "postal_code": FieldRule(max_length=20),
            "country_code": FieldRule(required=True, max_length=2, allowed=COUNTRIES),
            "telephone": FieldRule(max_length=20),
            "fax": FieldRule(max_length=20),
            "vat_number": FieldRule(max_length=30),
            "industry": FieldRule(
                max_length=40,
                allowed=frozenset(
                    {
                        "manufacturing",
                        "business_services",
                        "retail",
                        "public_sector",
                        "transport_logistics",
                        "healthcare",
                        "construction",
                        "food_beverage",
                        "unclassified",
                    }
                ),
            ),
            "status": FieldRule(
                required=True, allowed=frozenset({"active", "inactive", "prospect"})
            ),
            "description": FieldRule(max_length=2000),
            "modified_on": FieldRule(),
            **_COMMON,
        },
    ),
    "contacts": EntitySchema(
        name="contacts",
        alternate_key="arcadia_id",
        fields={
            "arcadia_id": FieldRule(required=True, max_length=12),
            "account_id": FieldRule(required=True, references="accounts"),
            "first_name": FieldRule(max_length=60),
            "last_name": FieldRule(required=True, max_length=60),
            "job_title": FieldRule(max_length=100),
            "email": FieldRule(max_length=120),
            "telephone": FieldRule(max_length=20),
            "mobile": FieldRule(max_length=20),
            "contact_role": FieldRule(
                allowed=frozenset({"decision_maker", "influencer", "end_user"})
            ),
            "do_not_email": FieldRule(),
            **_COMMON,
        },
    ),
    "opportunities": EntitySchema(
        name="opportunities",
        alternate_key="arcadia_id",
        fields={
            "arcadia_id": FieldRule(required=True, max_length=12),
            "account_id": FieldRule(required=True, references="accounts"),
            "contact_id": FieldRule(references="contacts"),
            "name": FieldRule(required=True, max_length=200),
            "amount": FieldRule(),
            "currency": FieldRule(allowed=frozenset({"EUR", "CHF", "USD", "GBP"})),
            "stage": FieldRule(
                required=True,
                allowed=frozenset(
                    {
                        "new",
                        "qualified",
                        "proposal",
                        "negotiation",
                        "closed_won",
                        "closed_lost",
                        "abandoned",
                    }
                ),
            ),
            "probability": FieldRule(),
            "close_date": FieldRule(),
            **_COMMON,
        },
    ),
    "activities": EntitySchema(
        name="activities",
        alternate_key="arcadia_id",
        fields={
            "arcadia_id": FieldRule(required=True, max_length=12),
            "account_id": FieldRule(required=True, references="accounts"),
            "contact_id": FieldRule(references="contacts"),
            "opportunity_id": FieldRule(references="opportunities"),
            "activity_type": FieldRule(
                required=True,
                allowed=frozenset({"phonecall", "appointment", "email", "note", "task"}),
            ),
            "subject": FieldRule(required=True, max_length=200),
            "body": FieldRule(max_length=4000),
            "activity_date": FieldRule(required=True),
            **_COMMON,
        },
    ),
}

# The order in which the target will accept references, and the order the
# loader must therefore respect. Published by the API so a client does not
# have to hard-code it.
LOAD_ORDER: tuple[str, ...] = ("accounts", "contacts", "opportunities", "activities")

# Deliberately lower than a client would guess: a batch limit that is never
# hit is a batch limit that has never been handled.
MAX_BATCH_SIZE = 250
