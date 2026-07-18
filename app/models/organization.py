"""B2B organization party model ported from the CRM.

CRM shape carried verbatim (``dotmac_crm/app/models/subscriber.py`` Organization
+ ``organization_membership.py``) with the sub conventions applied:

* PG enums become String columns + app-level enums.
* CRM ``people.id`` FKs (``primary_contact_id``, ``owner_id``,
  ``organization_memberships.person_id``) remain plain provenance UUIDs.
  OrganizationMembership now also has a nullable, evidence-bound canonical
  PartyMembership projection; the legacy UUID is not native identity.
* ``parent_id`` stays a real self-FK (hierarchy for enterprise accounts).
* ``party_id`` is the additive canonical Organization Party binding. The
  single-valued ``account_type`` remains compatibility data until role cutover.
"""

import enum
import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class OrganizationAccountType(enum.Enum):
    """Organization account type for B2B CRM."""

    prospect = "prospect"  # Potential customer, not yet qualified
    customer = "customer"  # Active paying customer
    partner = "partner"  # Business partner (integration, referral)
    reseller = "reseller"  # Resells our services
    vendor = "vendor"  # Supplies goods/services to us
    competitor = "competitor"  # For tracking
    other = "other"


class OrganizationAccountStatus(enum.Enum):
    """Organization account lifecycle status."""

    active = "active"  # Active relationship
    inactive = "inactive"  # Dormant, no recent activity
    churned = "churned"  # Former customer
    suspended = "suspended"  # Temporarily suspended
    archived = "archived"  # Archived/closed


class OrganizationMembershipRole(enum.Enum):
    owner = "owner"
    admin = "admin"
    member = "member"


class Organization(Base):
    """B2B Account/Company model with hierarchy support."""

    __tablename__ = "organizations"
    __table_args__ = (
        CheckConstraint(
            "(party_id IS NULL AND party_bound_at IS NULL AND "
            "party_binding_source IS NULL AND party_binding_reason IS NULL) OR "
            "(party_id IS NOT NULL AND party_bound_at IS NOT NULL AND "
            "party_binding_source IS NOT NULL AND "
            "party_binding_reason IS NOT NULL AND "
            "length(trim(party_binding_source)) > 0 AND "
            "length(trim(party_binding_reason)) > 0)",
            name="ck_organizations_party_binding_evidence",
        ),
        UniqueConstraint("party_id", name="uq_organizations_party_id"),
        Index("ix_organizations_parent", "parent_id"),
        Index("ix_organizations_account_type", "account_type"),
        Index("ix_organizations_status", "account_status"),
        Index("ix_organizations_owner", "owner_id"),
        Index("ix_organizations_erp", "erp_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    party_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("parties.id", ondelete="RESTRICT")
    )
    party_bound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    party_binding_source: Mapped[str | None] = mapped_column(String(80))
    party_binding_reason: Mapped[str | None] = mapped_column(Text)

    # Basic info
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    legal_name: Mapped[str | None] = mapped_column(String(200))
    tax_id: Mapped[str | None] = mapped_column(String(80))
    domain: Mapped[str | None] = mapped_column(String(120))
    website: Mapped[str | None] = mapped_column(String(255))
    phone: Mapped[str | None] = mapped_column(String(40))
    email: Mapped[str | None] = mapped_column(String(255))

    # Account classification (String + app enum per sub convention)
    account_type: Mapped[str] = mapped_column(
        String(40), default=OrganizationAccountType.prospect.value, nullable=False
    )
    account_status: Mapped[str] = mapped_column(
        String(40), default=OrganizationAccountStatus.active.value, nullable=False
    )

    # Hierarchy - parent/child for enterprise accounts
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id")
    )

    # Primary contact at this organization. CRM person UUID carried verbatim;
    # resolves via the party backfill map (subscribers.metadata.crm_person_id).
    primary_contact_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))

    # Account owner (sales rep/account manager). Staff person UUID carried
    # verbatim; display resolves via the staff map.
    owner_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))

    # B2B CRM fields
    industry: Mapped[str | None] = mapped_column(String(100))
    employee_count: Mapped[str | None] = mapped_column(String(40))
    annual_revenue: Mapped[str | None] = mapped_column(String(60))
    source: Mapped[str | None] = mapped_column(String(100))  # Lead source

    # Address
    address_line1: Mapped[str | None] = mapped_column(String(120))
    address_line2: Mapped[str | None] = mapped_column(String(120))
    city: Mapped[str | None] = mapped_column(String(80))
    region: Mapped[str | None] = mapped_column(String(80))
    postal_code: Mapped[str | None] = mapped_column(String(20))
    country_code: Mapped[str | None] = mapped_column(String(2))

    # External integrations
    erp_id: Mapped[str | None] = mapped_column(String(100), unique=True)
    erpnext_id: Mapped[str | None] = mapped_column(String(100), unique=True, index=True)

    # Metadata
    notes: Mapped[str | None] = mapped_column(Text)
    tags: Mapped[list | None] = mapped_column(JSON)
    # Reseller channel: per-reseller commission rate (percent); falls back to
    # the global default setting when null. Only meaningful for
    # account_type=reseller.
    commission_rate: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    metadata_: Mapped[dict | None] = mapped_column(
        "metadata", MutableDict.as_mutable(JSON())
    )

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    # Relationships
    party = relationship("Party", back_populates="organization_profile")
    parent: Mapped["Organization | None"] = relationship(
        "Organization", remote_side=[id], back_populates="children"
    )
    children: Mapped[list["Organization"]] = relationship(
        "Organization", back_populates="parent"
    )
    memberships = relationship(
        "OrganizationMembership",
        back_populates="organization",
        cascade="all, delete-orphan",
    )


class OrganizationMembership(Base):
    """Legacy organization access row with canonical context projection.

    ``person_id`` is the CRM person UUID carried as provenance. When reviewed,
    ``party_membership_id`` resolves the native Person and Organization context.
    Runtime access still uses compatibility state until an explicit cutover.
    """

    __tablename__ = "organization_memberships"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "person_id",
            name="uq_organization_memberships_org_person",
        ),
        CheckConstraint(
            "(party_membership_id IS NULL AND party_bound_at IS NULL AND "
            "party_binding_source IS NULL AND party_binding_reason IS NULL) OR "
            "(party_membership_id IS NOT NULL AND party_bound_at IS NOT NULL AND "
            "party_binding_source IS NOT NULL AND "
            "party_binding_reason IS NOT NULL AND "
            "length(trim(party_binding_source)) > 0 AND "
            "length(trim(party_binding_reason)) > 0)",
            name="ck_organization_memberships_party_binding_evidence",
        ),
        UniqueConstraint(
            "party_membership_id",
            name="uq_organization_memberships_party_membership_id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    party_membership_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("party_memberships.id", ondelete="RESTRICT"),
    )
    party_bound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    party_binding_source: Mapped[str | None] = mapped_column(String(80))
    party_binding_reason: Mapped[str | None] = mapped_column(Text)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False
    )
    # CRM person UUID (no people table in sub; see module docstring).
    person_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    role: Mapped[str] = mapped_column(
        String(20), default=OrganizationMembershipRole.member.value, nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    organization = relationship("Organization", back_populates="memberships")
    party_membership = relationship("PartyMembership")
