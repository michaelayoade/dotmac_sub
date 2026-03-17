"""Offer availability controls — restrict offers by partner, location, category, billing type."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.catalog import BillingMode
from app.models.subscriber import SubscriberCategory


class OfferResellerAvailability(Base):
    """Which resellers can sell this offer."""

    __tablename__ = "offer_reseller_availability"
    __table_args__ = (
        UniqueConstraint(
            "offer_id", "reseller_id",
            name="uq_offer_reseller",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    offer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("catalog_offers.id", ondelete="CASCADE"),
        nullable=False,
    )
    reseller_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("resellers.id", ondelete="CASCADE"),
        nullable=False,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )

    offer = relationship("CatalogOffer", back_populates="reseller_availability")
    reseller = relationship("Reseller")


class OfferLocationAvailability(Base):
    """Which POP sites / locations this offer is available in."""

    __tablename__ = "offer_location_availability"
    __table_args__ = (
        UniqueConstraint(
            "offer_id", "pop_site_id",
            name="uq_offer_location",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    offer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("catalog_offers.id", ondelete="CASCADE"),
        nullable=False,
    )
    pop_site_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("pop_sites.id", ondelete="CASCADE"),
        nullable=False,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )

    offer = relationship("CatalogOffer", back_populates="location_availability")
    pop_site = relationship("PopSite")


class OfferCategoryAvailability(Base):
    """Which subscriber categories (residential, business, etc.) can use this offer."""

    __tablename__ = "offer_category_availability"
    __table_args__ = (
        UniqueConstraint(
            "offer_id", "subscriber_category",
            name="uq_offer_category",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    offer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("catalog_offers.id", ondelete="CASCADE"),
        nullable=False,
    )
    subscriber_category: Mapped[SubscriberCategory] = mapped_column(
        Enum(
            SubscriberCategory,
            name="subscribercategory",
            create_constraint=False,
        ),
        nullable=False,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )

    offer = relationship("CatalogOffer", back_populates="category_availability")


class OfferBillingModeAvailability(Base):
    """Which billing modes (prepaid, postpaid) this offer supports."""

    __tablename__ = "offer_billing_mode_availability"
    __table_args__ = (
        UniqueConstraint(
            "offer_id", "billing_mode",
            name="uq_offer_billing_mode",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    offer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("catalog_offers.id", ondelete="CASCADE"),
        nullable=False,
    )
    billing_mode: Mapped[BillingMode] = mapped_column(
        Enum(
            BillingMode,
            name="billingmode",
            create_constraint=False,
        ),
        nullable=False,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )

    offer = relationship("CatalogOffer", back_populates="billing_mode_availability")
