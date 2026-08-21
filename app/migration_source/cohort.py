"""The Sub persistence surface that holds cohort-isp-01 source facts.

One declaration, read by three otherwise-independent things:

- the writer ratchet in `scripts/architecture/isp_cohort_writers.py`, which
  counts who mutates these tables;
- the export snapshot service, which reads them;
- the digest contract, which fingerprints what the export produced.

They are kept on one declaration on purpose. A ratchet that freezes a table
the exporter never reads, or an exporter that reads a table the ratchet never
watched, is the shape of every migration that discovered a forgotten writer
after the cutover.

## Why this module imports nothing

It has no SQLAlchemy import, no `app.db`, no models. That is what lets a
static architecture guard import it directly instead of re-encoding the table
list in a regex, so the guard and the contract cannot silently disagree. The
model class *names* are strings here for the same reason; the mapping from
name to class lives in the reader, which is allowed to import models.

## Scope, and what deliberately sits outside it

Governance names cohort 1 "Foundation party and customer" with components
`dotmac-party` (release), `dotmac-customers` (build) and
`dotmac-brand-profiles` (adopt). The tables below are Sub's side of exactly
that. Resellers, subscriptions, invoices, network and access state are later
cohorts and are absent: pulling a table forward because it is convenient to
export is how a cohort boundary stops meaning anything.

`resellers`, `reseller_users`, `customer_identity_index`,
`subscriber_channels`, `subscriber_nin_verifications`,
`subscriber_custom_fields`, `carried_source_identity_adjudications` and the
two `subscriber_contact_*_projections` tables are cohort-adjacent and
deliberately UNMAPPED — see `surfaces.UNMAPPED_ADJACENT_TABLES`, which records
each one and why, so their absence reads as a decision rather than an
oversight.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict, model_validator


class CohortEntityType(StrEnum):
    """The declared entity types cohort-isp-01 exports from Sub.

    These are *Sub's* names for its own facts. They are deliberately not the
    target module's vocabulary: an exporter that speaks the destination's
    status words has already started deciding what the destination should
    conclude, which is precisely what a source snapshot must not do.
    """

    PARTY = "party"
    PARTY_ROLE = "party_role"
    PARTY_RELATIONSHIP = "party_relationship"
    PARTY_MEMBERSHIP = "party_membership"
    PARTY_CONTACT_POINT = "party_contact_point"
    PARTY_EXTERNAL_REFERENCE = "party_external_reference"
    CUSTOMER_ACCOUNT = "customer_account"
    CUSTOMER_CONTACT = "customer_contact"
    CUSTOMER_ADDRESS = "customer_address"
    ORGANIZATION = "organization"
    ORGANIZATION_MEMBERSHIP = "organization_membership"
    BRAND_PROFILE = "brand_profile"


class CohortComponent(StrEnum):
    """The Governance component each entity type will eventually land in.

    Recorded as an expectation, never as a claim. The component identifiers
    come from the accepted matrix; the disposition (`release`, `build`,
    `adopt`) is Governance's and is not restated here, because a source
    repository restating a target's disposition is how two records start
    disagreeing.
    """

    PARTY = "dotmac-party"
    CUSTOMERS = "dotmac-customers"
    BRAND_PROFILES = "dotmac-brand-profiles"


class CohortTable(BaseModel):
    """One Sub table holding source state for one cohort entity type."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    entity_type: CohortEntityType
    #: The physical table. The ratchet matches raw SQL against this.
    table: str
    #: The mapped class name. The ratchet matches ORM construction against
    #: this; the reader resolves it to a real class.
    model_class: str
    #: Where that class is defined, so a reader can find it without grepping.
    model_module: str
    #: The Sub SOT service that owns writes to it today, or `None` when no
    #: single owner is declared. `None` is a finding, not a formatting choice.
    owning_service: str | None
    #: Where this fact is expected to live after a later, separately
    #: authorised cutover. An expectation, not a plan and not an approval.
    expected_target_component: CohortComponent

    @model_validator(mode="after")
    def _check(self) -> CohortTable:
        if not self.table.strip() or self.table != self.table.strip():
            raise ValueError(f"table {self.table!r} is blank or padded")
        if not self.model_class.isidentifier():
            raise ValueError(f"{self.model_class!r} is not a class name")
        if not self.model_module.startswith("app.models."):
            raise ValueError(
                f"{self.model_module!r} is not a Sub model module; the cohort "
                "surface may only name tables this application owns"
            )
        return self


COHORT_TABLES: Final[tuple[CohortTable, ...]] = (
    CohortTable(
        entity_type=CohortEntityType.PARTY,
        table="parties",
        model_class="Party",
        model_module="app.models.party",
        owning_service="party.registry",
        expected_target_component=CohortComponent.PARTY,
    ),
    CohortTable(
        entity_type=CohortEntityType.PARTY_ROLE,
        table="party_roles",
        model_class="PartyRole",
        model_module="app.models.party",
        owning_service="party.registry",
        expected_target_component=CohortComponent.PARTY,
    ),
    CohortTable(
        entity_type=CohortEntityType.PARTY_RELATIONSHIP,
        table="party_relationships",
        model_class="PartyRelationship",
        model_module="app.models.party",
        owning_service="party.registry",
        expected_target_component=CohortComponent.PARTY,
    ),
    CohortTable(
        entity_type=CohortEntityType.PARTY_MEMBERSHIP,
        table="party_memberships",
        model_class="PartyMembership",
        model_module="app.models.party",
        owning_service="party.registry",
        expected_target_component=CohortComponent.PARTY,
    ),
    CohortTable(
        entity_type=CohortEntityType.PARTY_CONTACT_POINT,
        table="party_contact_points",
        model_class="PartyContactPoint",
        model_module="app.models.party",
        owning_service="party.registry",
        expected_target_component=CohortComponent.PARTY,
    ),
    CohortTable(
        entity_type=CohortEntityType.PARTY_EXTERNAL_REFERENCE,
        table="party_external_references",
        model_class="PartyExternalReference",
        model_module="app.models.party",
        owning_service="party.registry",
        expected_target_component=CohortComponent.PARTY,
    ),
    CohortTable(
        entity_type=CohortEntityType.CUSTOMER_ACCOUNT,
        table="subscribers",
        model_class="Subscriber",
        model_module="app.models.subscriber",
        owning_service="customer.accounts",
        expected_target_component=CohortComponent.CUSTOMERS,
    ),
    CohortTable(
        entity_type=CohortEntityType.CUSTOMER_CONTACT,
        table="subscriber_contacts",
        model_class="SubscriberContact",
        model_module="app.models.subscriber",
        # `party.registry` owns the canonical Person binding on this row, but
        # no declared service owns the legacy contact columns themselves.
        # Recorded as unowned rather than attributed to the nearest service.
        owning_service=None,
        expected_target_component=CohortComponent.CUSTOMERS,
    ),
    CohortTable(
        entity_type=CohortEntityType.CUSTOMER_ADDRESS,
        table="addresses",
        model_class="Address",
        model_module="app.models.subscriber",
        owning_service=None,
        expected_target_component=CohortComponent.CUSTOMERS,
    ),
    CohortTable(
        entity_type=CohortEntityType.ORGANIZATION,
        table="organizations",
        model_class="Organization",
        model_module="app.models.organization",
        owning_service=None,
        expected_target_component=CohortComponent.CUSTOMERS,
    ),
    CohortTable(
        entity_type=CohortEntityType.ORGANIZATION_MEMBERSHIP,
        table="organization_memberships",
        model_class="OrganizationMembership",
        model_module="app.models.organization",
        owning_service=None,
        expected_target_component=CohortComponent.CUSTOMERS,
    ),
    CohortTable(
        entity_type=CohortEntityType.BRAND_PROFILE,
        table="brand_profiles",
        model_class="BrandProfile",
        model_module="app.models.branding",
        owning_service="customer.branding",
        expected_target_component=CohortComponent.BRAND_PROFILES,
    ),
)


def cohort_tables_by_entity() -> dict[CohortEntityType, CohortTable]:
    """Return the cohort surface keyed by entity type.

    Raises when two declarations claim one entity type: a duplicated entity
    would give the exporter two field sets and the digest two answers.
    """

    mapping: dict[CohortEntityType, CohortTable] = {}
    for declared in COHORT_TABLES:
        if declared.entity_type in mapping:
            raise ValueError(
                f"entity type {declared.entity_type} is declared twice; one "
                "entity type maps to exactly one table"
            )
        mapping[declared.entity_type] = declared
    missing = sorted(set(CohortEntityType) - set(mapping))
    if missing:
        raise ValueError("declared entity types with no table: " + ", ".join(missing))
    return mapping


def cohort_table_names() -> frozenset[str]:
    """Physical table names in the cohort surface."""

    return frozenset(declared.table for declared in COHORT_TABLES)


def cohort_model_names() -> frozenset[str]:
    """Mapped class names in the cohort surface."""

    return frozenset(declared.model_class for declared in COHORT_TABLES)


def cohort_model_modules() -> frozenset[str]:
    """Modules defining the cohort's mapped classes."""

    return frozenset(declared.model_module for declared in COHORT_TABLES)


__all__ = [
    "COHORT_TABLES",
    "CohortComponent",
    "CohortEntityType",
    "CohortTable",
    "cohort_model_modules",
    "cohort_model_names",
    "cohort_table_names",
    "cohort_tables_by_entity",
]
