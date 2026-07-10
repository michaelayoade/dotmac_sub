"""B2B organization party model ported from the CRM (Phase 3, doc 02 §3.3).

CRM shape carried verbatim (``dotmac_crm/app/models/subscriber.py`` Organization
+ ``organization_membership.py``) with the sub conventions applied:

* PG enums become String columns + app-level enums (Phase 1 convention).
* CRM ``people.id`` FKs (``primary_contact_id``, ``owner_id``,
  ``organization_memberships.person_id``) become plain UUIDs — sub has no
  ``people`` table. Customer-party persons resolve through the Phase 3 party
  backfill map (``subscribers.metadata->>'crm_person_id'``); staff persons
  resolve for display via the Phase 1 staff map.
* ``parent_id`` stays a real self-FK (hierarchy for enterprise accounts).
"""

import enum
import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
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
        Index("ix_organizations_parent", "parent_id"),
        Index("ix_organizations_account_type", "account_type"),
        Index("ix_organizations_status", "account_status"),
        Index("ix_organizations_owner", "owner_id"),
        Index("ix_organizations_erp", "erp_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

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
    # verbatim; display resolves via the Phase 1 staff map.
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
    """Explicit access link between a person and an Organization.

    Ported verbatim from CRM (Phase 3 §1.9). ``person_id`` is the CRM person
    UUID carried as a plain UUID — it resolves to a sub subscriber through the
    party backfill map. Enables one person (one login) to manage multiple
    Organizations (e.g. a reseller managing many child customer orgs).
    """

    __tablename__ = "organization_memberships"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "person_id",
            name="uq_organization_memberships_org_person",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
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
