"""Read cohort-isp-01 source facts. Decide nothing, write nothing.

The owner of the export snapshot and comparison digest defined in
`app/migration_source/`. It is the only code that turns Sub rows into that
contract, so there is exactly one place where a field mapping, a minimisation
rule or a tenant check can be wrong.

## Read-only is layered, not asserted once

Both halves of the snapshot contract matter and must not be separated:
REPEATABLE READ because a cohort assembled from twelve statements has to see
one snapshot or its own counts can disagree, and READ ONLY because a path that
reads a production database on behalf of a migration must be incapable of
changing it.

Three independent things hold it up, because a single check would be a single
point of quiet failure:

1. both adapters open `app.db.read_only_snapshot_session`, the repository's
   one seam for a read-only operator report;
2. an export pins a not-yet-started session to that seam itself — see
   `_pin_read_only` for why it cannot pin one already in a transaction;
3. this module issues no `add`, `flush`, `commit`, `delete` or DML of any
   kind, which `tests/architecture/test_migration_export_boundary.py` asserts
   statically rather than trusting the reading.

## Tenant scope is resolved, never accepted

Sub is a single-operator deployment: the ISP operator *is* the tenant, and
`tenancy.operator_tenant` owns that identity. A caller may state which tenant
it believes it is exporting, and a mismatch is **refused**. It is deliberately
not answered with an empty page — an empty page and a refusal are
indistinguishable to an importer, and only one of them is safe to retry.

## What this service must never grow into

It does not write to a destination, does not dual-write, does not decide a
row's disposition, and does not speak the target's status vocabulary. Sub is
`asm-dotmac-sub-legacy` and remains the sole production writer of every fact
below until a separately authorised sealed switch. A snapshot describes what
Sub holds; what the destination concludes is the destination's resolver's job,
and moving that judgement here would give the cohort two authorities before it
has one.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, TypeVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import inspect, select, text
from sqlalchemy.engine import Connection
from sqlalchemy.orm import InstrumentedAttribute

from app.db import Base, begin_read_only_snapshot
from app.migration_source.cohort import CohortEntityType
from app.migration_source.digest import (
    CohortDigest,
    EntityDigest,
    EntityTypeDigest,
    build_cohort_digest,
    build_entity_type_digest,
    digest_page,
)
from app.migration_source.snapshot import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    BrandProfileRecord,
    CohortRecord,
    Completeness,
    ContractVersion,
    CustomerAccountRecord,
    CustomerAddressRecord,
    CustomerContactRecord,
    ExportCursor,
    ExternalCorrelation,
    OrganizationMembershipRecord,
    OrganizationRecord,
    PartyContactPointRecord,
    PartyExternalReferenceRecord,
    PartyMembershipRecord,
    PartyRecord,
    PartyRelationshipRecord,
    PartyRoleRecord,
    SnapshotPage,
    SourceRevision,
    TenantScope,
    opaque_blob,
    require_contract_version,
)
from app.models.branding import BrandProfile
from app.models.organization import Organization, OrganizationMembership
from app.models.party import (
    Party,
    PartyContactPoint,
    PartyExternalReference,
    PartyMembership,
    PartyRelationship,
    PartyRole,
)
from app.models.subscriber import Address, Subscriber, SubscriberContact
from app.services.operator_tenant import operator_tenant_id
from app.version import get_app_version

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

#: Bounded to Sub's own mapped classes: an export may only read tables this
#: application owns.
RowT = TypeVar("RowT", bound=Base)

#: How many pages one digest drain may read before it reports a partial
#: result. A digest over a partial drain is legal and says so; an unbounded
#: loop over a production table is not something a read path should offer.
DEFAULT_PAGE_BUDGET: Final[int] = 200


class CohortExportError(RuntimeError):
    """The cohort export refused to answer."""


class CrossTenantExportRefused(CohortExportError):
    """A caller asked for a tenant this deployment does not hold.

    Refused rather than answered with nothing. An importer cannot tell an
    empty page from a wrong-tenant page, and would record "the source has no
    parties" as a fact about the cohort.
    """


class CohortExportCommand(BaseModel):
    """One typed request for one page of one entity type."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: A string, validated against the closed set. Kept as transport-shaped
    #: input because that is what a route or CLI actually receives, and
    #: converting it here is what makes an unsupported version a refusal
    #: rather than a 500.
    contract_version: str
    entity_type: CohortEntityType
    #: `None` starts at the beginning of the entity type.
    after_source_id: UUID | None = None
    page_size: int = Field(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE)
    #: What the caller believes it is exporting. Checked, never trusted.
    tenant_id: UUID


def _enum_value(value: object) -> str | None:
    if value is None:
        return None
    inner = getattr(value, "value", value)
    return str(inner)


def _pin_read_only(db: Session) -> None:
    """Pin a not-yet-started session to the read-only snapshot seam.

    SQLAlchemy applies `postgresql_readonly` and the isolation level when a
    connection is first procured, and raises `InvalidRequestError` if they are
    re-requested once a transaction is open. A session — or the connection
    under it — whose transaction has already begun therefore keeps the
    isolation its owner chose, and this returns without pretending otherwise.

    The read-only guarantee is layered rather than asserted in one line: both
    adapters open `app.db.read_only_snapshot_session`, this module issues no
    persistence call at all (proved statically by
    `tests/architecture/test_migration_export_boundary.py`), and a PostgreSQL
    canary proves the seam really does refuse a write.
    """

    if db.in_transaction():
        return
    bind = db.get_bind()
    # The session may not have begun while the CONNECTION it is bound to
    # already has — that is exactly the shape of a test bound to an outer
    # transaction, and SQLAlchemy raises rather than silently ignoring the
    # options. Ask the connection too, so the seam degrades to "the owner
    # keeps its isolation" instead of failing the read.
    if isinstance(bind, Connection) and bind.in_transaction():
        return
    begin_read_only_snapshot(db)


def _resolve_tenant(requested: UUID) -> TenantScope:
    resolved = operator_tenant_id()
    if requested != resolved:
        raise CrossTenantExportRefused(
            f"this deployment holds one operator tenant and it is not "
            f"{requested}. The request is refused rather than answered with an "
            "empty page, because an importer cannot distinguish the two."
        )
    return TenantScope(tenant_id=resolved)


def _source_revision(db: Session) -> SourceRevision:
    """Describe the database this snapshot came from, precisely."""

    schema_revision = "unknown"
    bind = db.get_bind()
    # Asked rather than attempted. A failed statement aborts the surrounding
    # PostgreSQL transaction, so a `try/except` around the read would turn a
    # missing revision table into a poisoned snapshot; `has_table` answers
    # without issuing anything the transaction can choke on. The fast unit
    # lane builds its schema from metadata and genuinely has no
    # `alembic_version`, and "unknown" is the honest answer there.
    if inspect(bind).has_table("alembic_version"):
        row = db.execute(text("SELECT version_num FROM alembic_version")).first()
        if row is not None and row[0]:
            schema_revision = str(row[0])

    snapshot_transaction_id: str | None = None
    if bind.dialect.name == "postgresql":
        # `pg_current_snapshot()` observes the visibility snapshot without
        # assigning a transaction id. `txid_current()` would assign one, which
        # a READ ONLY transaction refuses — the watermark must not be the
        # thing that breaks the read it is describing.
        snapshot_row = db.execute(text("SELECT pg_current_snapshot()::text")).first()
        if snapshot_row is not None and snapshot_row[0]:
            snapshot_transaction_id = str(snapshot_row[0])

    return SourceRevision(
        schema_revision=schema_revision,
        application_version=get_app_version(),
        snapshot_transaction_id=snapshot_transaction_id,
        captured_at=datetime.now(UTC),
    )


def _party_record(row: Party) -> PartyRecord:
    return PartyRecord(
        source_id=row.id,
        created_at=row.created_at,
        updated_at=row.updated_at,
        party_type=row.party_type,
        display_name=row.display_name,
        status=row.status,
        data_classification=row.data_classification,
        merged_into_party_id=row.merged_into_party_id,
        merge_reason=row.merge_reason,
        metadata_blob=opaque_blob(row.metadata_),
    )


def _party_role_record(row: PartyRole) -> PartyRoleRecord:
    return PartyRoleRecord(
        source_id=row.id,
        created_at=row.created_at,
        updated_at=row.updated_at,
        party_id=row.party_id,
        role_type=row.role_type,
        role_key=row.role_key,
        status=row.status,
        valid_from=row.valid_from,
        valid_until=row.valid_until,
        source=row.source,
        metadata_blob=opaque_blob(row.metadata_),
    )


def _party_relationship_record(row: PartyRelationship) -> PartyRelationshipRecord:
    return PartyRelationshipRecord(
        source_id=row.id,
        created_at=row.created_at,
        updated_at=row.updated_at,
        subject_party_id=row.subject_party_id,
        object_party_id=row.object_party_id,
        relationship_type=row.relationship_type,
        relationship_key=row.relationship_key,
        status=row.status,
        valid_from=row.valid_from,
        valid_until=row.valid_until,
        source=row.source,
        metadata_blob=opaque_blob(row.metadata_),
    )


def _party_membership_record(row: PartyMembership) -> PartyMembershipRecord:
    return PartyMembershipRecord(
        source_id=row.id,
        created_at=row.created_at,
        updated_at=row.updated_at,
        person_party_id=row.person_party_id,
        organization_party_id=row.organization_party_id,
        membership_type=row.membership_type,
        membership_key=row.membership_key,
        status=row.status,
        access_scope=opaque_blob(row.access_scope),
        valid_from=row.valid_from,
        valid_until=row.valid_until,
        source=row.source,
        metadata_blob=opaque_blob(row.metadata_),
    )


def _party_contact_point_record(row: PartyContactPoint) -> PartyContactPointRecord:
    return PartyContactPointRecord(
        source_id=row.id,
        created_at=row.created_at,
        updated_at=row.updated_at,
        party_id=row.party_id,
        channel_type=row.channel_type,
        normalized_value=row.normalized_value,
        scope_key=row.scope_key,
        provider=row.provider,
        provider_account_id=row.provider_account_id,
        external_subject_id=row.external_subject_id,
        is_primary=bool(row.is_primary),
        is_active=bool(row.is_active),
        verification_status=row.verification_status,
        verified_at=row.verified_at,
        verification_source=row.verification_source,
        consent_status=row.consent_status,
        consent_captured_at=row.consent_captured_at,
        metadata_blob=opaque_blob(row.metadata_),
    )


def _party_external_reference_record(
    row: PartyExternalReference,
) -> PartyExternalReferenceRecord:
    return PartyExternalReferenceRecord(
        source_id=row.id,
        created_at=row.created_at,
        updated_at=row.updated_at,
        party_id=row.party_id,
        source_system=row.source_system,
        referenced_entity_type=row.entity_type,
        external_id=row.external_id,
        is_active=bool(row.is_active),
        metadata_blob=opaque_blob(row.metadata_),
    )


def _account_correlations(row: Subscriber) -> tuple[ExternalCorrelation, ...]:
    """Foreign identifiers, carried opaquely and never resolved."""

    correlations: list[ExternalCorrelation] = []
    if row.splynx_customer_id is not None:
        correlations.append(
            ExternalCorrelation(system="splynx", reference=str(row.splynx_customer_id))
        )
    if row.crm_subscriber_id is not None:
        correlations.append(
            ExternalCorrelation(system="crm", reference=str(row.crm_subscriber_id))
        )
    return tuple(correlations)


def _customer_account_record(row: Subscriber) -> CustomerAccountRecord:
    return CustomerAccountRecord(
        source_id=row.id,
        created_at=row.created_at,
        updated_at=row.updated_at,
        party_id=row.party_id,
        party_bound_at=row.party_bound_at,
        party_binding_source=row.party_binding_source,
        party_binding_reason=row.party_binding_reason,
        first_name=row.first_name,
        last_name=row.last_name,
        display_name=row.display_name,
        company_name=row.company_name,
        legal_name=row.legal_name,
        tax_id=row.tax_id,
        domain=row.domain,
        website=row.website,
        email=row.email,
        email_verified=bool(row.email_verified),
        phone=row.phone,
        nin_present=row.nin is not None,
        date_of_birth_present=row.date_of_birth is not None,
        gender=_enum_value(row.gender),
        preferred_contact_method=_enum_value(row.preferred_contact_method),
        locale=row.locale,
        timezone=row.timezone,
        address_line1=row.address_line1,
        address_line2=row.address_line2,
        city=row.city,
        region=row.region,
        lga=row.lga,
        postal_code=row.postal_code,
        country_code=row.country_code,
        pop_site_id=row.pop_site_id,
        subscriber_number=row.subscriber_number,
        account_number=row.account_number,
        account_start_date=row.account_start_date,
        status=_enum_value(row.status),
        legacy_party_status=row.party_status,
        lifecycle_override_status=_enum_value(row.lifecycle_override_status),
        lifecycle_override_reason=row.lifecycle_override_reason,
        lifecycle_override_source=row.lifecycle_override_source,
        lifecycle_override_at=row.lifecycle_override_at,
        user_type=_enum_value(row.user_type),
        is_active=bool(row.is_active),
        marketing_opt_in=bool(row.marketing_opt_in),
        reseller_id=row.reseller_id,
        tax_rate_id=row.tax_rate_id,
        policy_set_id=row.policy_set_id,
        organization_id=row.organization_id,
        sales_order_id=row.sales_order_id,
        billing_enabled=bool(row.billing_enabled),
        captive_redirect_enabled=bool(row.captive_redirect_enabled),
        billing_name=row.billing_name,
        billing_address_line1=row.billing_address_line1,
        billing_address_line2=row.billing_address_line2,
        billing_city=row.billing_city,
        billing_region=row.billing_region,
        billing_postal_code=row.billing_postal_code,
        billing_country_code=row.billing_country_code,
        payment_method=row.payment_method,
        deposit=row.deposit,
        billing_mode=_enum_value(row.billing_mode),
        billing_day=row.billing_day,
        payment_due_days=row.payment_due_days,
        grace_period_days=row.grace_period_days,
        min_balance=row.min_balance,
        prepaid_low_balance_at=row.prepaid_low_balance_at,
        prepaid_deactivation_at=row.prepaid_deactivation_at,
        mrr_total=row.mrr_total,
        external_correlations=_account_correlations(row),
        metadata_blob=opaque_blob(row.metadata_),
    )


def _customer_contact_record(row: SubscriberContact) -> CustomerContactRecord:
    return CustomerContactRecord(
        source_id=row.id,
        created_at=row.created_at,
        updated_at=row.updated_at,
        subscriber_id=row.subscriber_id,
        person_party_id=row.person_party_id,
        party_bound_at=row.party_bound_at,
        party_binding_source=row.party_binding_source,
        party_binding_reason=row.party_binding_reason,
        full_name=row.full_name,
        phone=row.phone,
        email=row.email,
        whatsapp=row.whatsapp,
        facebook=row.facebook,
        instagram=row.instagram,
        x_handle=row.x_handle,
        telegram=row.telegram,
        linkedin=row.linkedin,
        other_social_present=bool(row.other_social),
        contact_relationship=row.relationship,
        contact_type=row.contact_type,
        is_billing_contact=bool(row.is_billing_contact),
        is_authorized=bool(row.is_authorized),
        receives_notifications=bool(row.receives_notifications),
    )


def _customer_address_record(row: Address) -> CustomerAddressRecord:
    return CustomerAddressRecord(
        source_id=row.id,
        created_at=row.created_at,
        updated_at=row.updated_at,
        subscriber_id=row.subscriber_id,
        tax_rate_id=row.tax_rate_id,
        address_type=_enum_value(row.address_type),
        label=row.label,
        address_line1=row.address_line1,
        address_line2=row.address_line2,
        city=row.city,
        region=row.region,
        lga=row.lga,
        postal_code=row.postal_code,
        country_code=row.country_code,
        latitude=row.latitude,
        longitude=row.longitude,
        is_primary=bool(row.is_primary),
    )


def _organization_correlations(row: Organization) -> tuple[ExternalCorrelation, ...]:
    correlations: list[ExternalCorrelation] = []
    if row.backoffice_system and row.backoffice_account_reference:
        correlations.append(
            ExternalCorrelation(
                system=row.backoffice_system,
                reference=row.backoffice_account_reference,
            )
        )
    if row.legacy_account_system and row.legacy_account_reference:
        correlations.append(
            ExternalCorrelation(
                system=row.legacy_account_system,
                reference=row.legacy_account_reference,
            )
        )
    return tuple(correlations)


def _organization_record(row: Organization) -> OrganizationRecord:
    tags = row.tags if isinstance(row.tags, list) else []
    return OrganizationRecord(
        source_id=row.id,
        created_at=row.created_at,
        updated_at=row.updated_at,
        party_id=row.party_id,
        party_bound_at=row.party_bound_at,
        party_binding_source=row.party_binding_source,
        party_binding_reason=row.party_binding_reason,
        name=row.name,
        legal_name=row.legal_name,
        tax_id=row.tax_id,
        domain=row.domain,
        website=row.website,
        phone=row.phone,
        email=row.email,
        account_type=_enum_value(row.account_type) or "",
        account_status=_enum_value(row.account_status) or "",
        parent_id=row.parent_id,
        primary_contact_id=row.primary_contact_id,
        owner_id=row.owner_id,
        industry=row.industry,
        employee_count=row.employee_count,
        annual_revenue=row.annual_revenue,
        source=row.source,
        address_line1=row.address_line1,
        address_line2=row.address_line2,
        city=row.city,
        region=row.region,
        postal_code=row.postal_code,
        country_code=row.country_code,
        tags=tuple(str(tag) for tag in tags),
        commission_rate=row.commission_rate,
        is_active=bool(row.is_active),
        external_correlations=_organization_correlations(row),
        metadata_blob=opaque_blob(row.metadata_),
    )


def _organization_membership_record(
    row: OrganizationMembership,
) -> OrganizationMembershipRecord:
    return OrganizationMembershipRecord(
        source_id=row.id,
        created_at=row.created_at,
        updated_at=row.updated_at,
        organization_id=row.organization_id,
        person_id=row.person_id,
        party_membership_id=row.party_membership_id,
        party_bound_at=row.party_bound_at,
        party_binding_source=row.party_binding_source,
        party_binding_reason=row.party_binding_reason,
        role=_enum_value(row.role) or "",
        is_active=bool(row.is_active),
    )


def _brand_profile_record(row: BrandProfile) -> BrandProfileRecord:
    return BrandProfileRecord(
        source_id=row.id,
        created_at=row.created_at,
        updated_at=row.updated_at,
        scope_type=row.scope_type,
        scope_id=row.scope_id,
        brand_name=row.brand_name,
        product_name=row.product_name,
        legal_name=row.legal_name,
        tagline=row.tagline,
        primary_color=row.primary_color,
        secondary_color=row.secondary_color,
        logo_url=row.logo_url,
        dark_logo_url=row.dark_logo_url,
        favicon_url=row.favicon_url,
        support_email=row.support_email,
        support_phone=row.support_phone,
        from_email=row.from_email,
        from_name=row.from_name,
        app_url=row.app_url,
        portal_domain=row.portal_domain,
        legal_address=opaque_blob(row.legal_address),
        is_active=bool(row.is_active),
        metadata_blob=opaque_blob(row.metadata_),
    )


def _rows(
    db: Session,
    model: type[RowT],
    identity: InstrumentedAttribute[UUID],
    after: UUID | None,
    limit: int,
) -> list[RowT]:
    """Read one keyset page of one mapped class, ordered by its primary key.

    The identity column is passed explicitly rather than reached through the
    class, so the ordering and the keyset predicate provably use the same
    column. A generic `model.id` would type as `Any` and would silently accept
    a model whose primary key is named something else.
    """

    statement = select(model).order_by(identity)
    if after is not None:
        statement = statement.where(identity > after)
    return list(db.execute(statement.limit(limit)).scalars())


def _read_records(
    db: Session,
    entity_type: CohortEntityType,
    after: UUID | None,
    limit: int,
) -> tuple[CohortRecord, ...]:
    """Dispatch to the one reader for one entity type.

    Spelled out branch by branch rather than driven from a lookup table. A
    table keyed by entity type would have to hold the model class and its
    mapper as loosely typed values, and the pair being wrong — a Party read
    shaped by the account mapper — is exactly the mistake that would survive
    every test that only counts rows.
    """

    match entity_type:
        case CohortEntityType.PARTY:
            return tuple(
                _party_record(row) for row in _rows(db, Party, Party.id, after, limit)
            )
        case CohortEntityType.PARTY_ROLE:
            return tuple(
                _party_role_record(row)
                for row in _rows(db, PartyRole, PartyRole.id, after, limit)
            )
        case CohortEntityType.PARTY_RELATIONSHIP:
            return tuple(
                _party_relationship_record(row)
                for row in _rows(
                    db, PartyRelationship, PartyRelationship.id, after, limit
                )
            )
        case CohortEntityType.PARTY_MEMBERSHIP:
            return tuple(
                _party_membership_record(row)
                for row in _rows(db, PartyMembership, PartyMembership.id, after, limit)
            )
        case CohortEntityType.PARTY_CONTACT_POINT:
            return tuple(
                _party_contact_point_record(row)
                for row in _rows(
                    db, PartyContactPoint, PartyContactPoint.id, after, limit
                )
            )
        case CohortEntityType.PARTY_EXTERNAL_REFERENCE:
            return tuple(
                _party_external_reference_record(row)
                for row in _rows(
                    db, PartyExternalReference, PartyExternalReference.id, after, limit
                )
            )
        case CohortEntityType.CUSTOMER_ACCOUNT:
            return tuple(
                _customer_account_record(row)
                for row in _rows(db, Subscriber, Subscriber.id, after, limit)
            )
        case CohortEntityType.CUSTOMER_CONTACT:
            return tuple(
                _customer_contact_record(row)
                for row in _rows(
                    db, SubscriberContact, SubscriberContact.id, after, limit
                )
            )
        case CohortEntityType.CUSTOMER_ADDRESS:
            return tuple(
                _customer_address_record(row)
                for row in _rows(db, Address, Address.id, after, limit)
            )
        case CohortEntityType.ORGANIZATION:
            return tuple(
                _organization_record(row)
                for row in _rows(db, Organization, Organization.id, after, limit)
            )
        case CohortEntityType.ORGANIZATION_MEMBERSHIP:
            return tuple(
                _organization_membership_record(row)
                for row in _rows(
                    db, OrganizationMembership, OrganizationMembership.id, after, limit
                )
            )
        case CohortEntityType.BRAND_PROFILE:
            return tuple(
                _brand_profile_record(row)
                for row in _rows(db, BrandProfile, BrandProfile.id, after, limit)
            )


def _build_page(
    db: Session,
    command: CohortExportCommand,
    version: ContractVersion,
    tenant: TenantScope,
    revision: SourceRevision,
) -> SnapshotPage:
    """Assemble one page from an already-resolved version, tenant and revision.

    Split out so a multi-page drain resolves those three once. Re-resolving
    them per page would cost two extra statements per page and, worse, would
    stamp each page with its own capture instant — the pages of one drain must
    agree about which snapshot they came from.
    """

    # One extra row answers "is there more" without a second count query,
    # which under REPEATABLE READ would be consistent but is still a round
    # trip nobody needs.
    read = _read_records(
        db, command.entity_type, command.after_source_id, command.page_size + 1
    )
    has_more = len(read) > command.page_size
    records = read[: command.page_size]
    next_cursor = None
    if has_more and records:
        next_cursor = ExportCursor(
            entity_type=command.entity_type,
            after_source_id=records[-1].source_id,
            page_size=command.page_size,
        )

    return SnapshotPage(
        contract_version=version,
        tenant=tenant,
        source_revision=revision,
        entity_type=command.entity_type,
        records=records,
        next_cursor=next_cursor,
        completeness=(Completeness.PARTIAL if next_cursor else Completeness.COMPLETE),
    )


def export_page(db: Session, command: CohortExportCommand) -> SnapshotPage:
    """Return one bounded, ordered page of one cohort entity type.

    The caller owns the session; this pins it to a read-only repeatable-read
    snapshot and never completes the transaction.
    """

    version = require_contract_version(command.contract_version)
    tenant = _resolve_tenant(command.tenant_id)
    _pin_read_only(db)
    return _build_page(db, command, version, tenant, _source_revision(db))


def export_cohort_digest(
    db: Session,
    *,
    contract_version: str,
    tenant_id: UUID,
    page_size: int = DEFAULT_PAGE_SIZE,
    page_budget: int = DEFAULT_PAGE_BUDGET,
) -> CohortDigest:
    """Drain every entity type into one comparison digest.

    A drain that exhausts `page_budget` returns a digest that says so — the
    entity type is `PARTIAL` and carries its resume point. A partial digest is
    a legitimate artifact; silently presenting one as complete is what would
    make a later comparison report unread rows as missing.
    """

    version = require_contract_version(contract_version)
    tenant = _resolve_tenant(tenant_id)
    _pin_read_only(db)
    revision = _source_revision(db)

    per_type: list[EntityTypeDigest] = []
    for entity_type in sorted(CohortEntityType, key=lambda value: value.value):
        entries: list[EntityDigest] = []
        cursor: ExportCursor | None = None
        pages_read = 0
        after: UUID | None = None
        while True:
            page = _build_page(
                db,
                CohortExportCommand(
                    contract_version=contract_version,
                    entity_type=entity_type,
                    after_source_id=after,
                    page_size=page_size,
                    tenant_id=tenant_id,
                ),
                version,
                tenant,
                revision,
            )
            entries.extend(digest_page(page))
            pages_read += 1
            if page.next_cursor is None:
                break
            if pages_read >= page_budget:
                cursor = page.next_cursor
                break
            after = page.next_cursor.after_source_id

        per_type.append(
            build_entity_type_digest(
                entity_type=entity_type,
                entries=tuple(entries),
                completeness=(
                    Completeness.PARTIAL if cursor else Completeness.COMPLETE
                ),
                resume_from=cursor,
                contract_version=version,
            )
        )

    return build_cohort_digest(
        tenant=tenant,
        source_revision=revision,
        entity_types=tuple(per_type),
        contract_version=version,
        generated_at=datetime.now(UTC),
    )


__all__ = [
    "DEFAULT_PAGE_BUDGET",
    "CohortExportCommand",
    "CohortExportError",
    "CrossTenantExportRefused",
    "export_cohort_digest",
    "export_page",
]
