"""Composable captive access policy records.

Owner: ``access.captive_access_policy`` (participant writer, called only by
the ``access.captive_access_policy_change`` coordinator).

A rule grants (``allow``) or forbids (``deny``) the captive walled-garden tier
for the subscriptions it matches. Resolution is per subscription: the most
specific matching scope wins (``account`` > ``customer_set`` > ``plan_family``
> ``global``), deny beats allow at the same scope, and no matching rule means
hard reject. Fixed safety rails (non-customer principals, inactive accounts,
terminal or disabled services) are enforced in code and cannot be granted by a
rule.

Closed vocabularies are stored as short strings guarded by CHECK constraints;
the service converts them to the enums below at its boundary.
"""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class CaptiveAccessRuleScope(enum.StrEnum):
    """Specificity level of a rule, most specific first in resolution."""

    account = "account"
    customer_set = "customer_set"
    plan_family = "plan_family"
    global_ = "global"


class CaptiveAccessRuleEffect(enum.StrEnum):
    allow = "allow"
    deny = "deny"


class CaptiveResellerCondition(enum.StrEnum):
    """Which reseller relationships a rule applies to."""

    any = "any"
    house = "house"
    specific = "specific"


def _now() -> datetime:
    return datetime.now(UTC)


class CaptiveCustomerSet(Base):
    """A named, audited cohort of customer accounts."""

    __tablename__ = "captive_customer_sets"
    __table_args__ = (
        UniqueConstraint("name", name="uq_captive_customer_sets_name"),
        CheckConstraint("length(trim(name)) > 0", name="ck_captive_customer_sets_name"),
        CheckConstraint(
            "length(trim(reason)) > 0", name="ck_captive_customer_sets_reason"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, onupdate=_now
    )


class CaptiveCustomerSetMember(Base):
    """Membership history of one account in one customer set.

    A row is never deleted: removal stamps ``removed_at``/``removed_by`` so
    the cohort's history stays auditable. At most one open membership exists
    per (set, account).
    """

    __tablename__ = "captive_customer_set_members"
    __table_args__ = (
        Index(
            "uq_captive_customer_set_members_open",
            "customer_set_id",
            "subscriber_id",
            unique=True,
            postgresql_where=text("removed_at IS NULL"),
            sqlite_where=text("removed_at IS NULL"),
        ),
        Index(
            "ix_captive_customer_set_members_subscriber_open",
            "subscriber_id",
            postgresql_where=text("removed_at IS NULL"),
        ),
        CheckConstraint(
            "(removed_at IS NULL) = (removed_by IS NULL)",
            name="ck_captive_customer_set_members_removal_evidence",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    customer_set_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("captive_customer_sets.id", ondelete="RESTRICT"),
        nullable=False,
    )
    subscriber_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subscribers.id", ondelete="CASCADE"),
        nullable=False,
    )
    added_by: Mapped[str] = mapped_column(String(255), nullable=False)
    added_reason: Mapped[str] = mapped_column(Text, nullable=False)
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    removed_by: Mapped[str | None] = mapped_column(String(255))
    removed_reason: Mapped[str | None] = mapped_column(Text)


class CaptiveAccessRule(Base):
    """One typed captive access rule."""

    __tablename__ = "captive_access_rules"
    __table_args__ = (
        CheckConstraint(
            "scope IN ('account', 'customer_set', 'plan_family', 'global')",
            name="ck_captive_access_rules_scope",
        ),
        CheckConstraint(
            "effect IN ('allow', 'deny')", name="ck_captive_access_rules_effect"
        ),
        CheckConstraint(
            "reseller_condition IN ('any', 'house', 'specific')",
            name="ck_captive_access_rules_reseller_condition",
        ),
        # Exactly the target column(s) the scope names are populated.
        CheckConstraint(
            "(scope = 'account' AND subscriber_id IS NOT NULL "
            "AND customer_set_id IS NULL AND plan_family IS NULL) OR "
            "(scope = 'customer_set' AND customer_set_id IS NOT NULL "
            "AND subscriber_id IS NULL AND plan_family IS NULL) OR "
            "(scope = 'plan_family' AND plan_family IS NOT NULL "
            "AND subscriber_id IS NULL AND customer_set_id IS NULL) OR "
            "(scope = 'global' AND subscriber_id IS NULL "
            "AND customer_set_id IS NULL AND plan_family IS NULL)",
            name="ck_captive_access_rules_scope_target",
        ),
        CheckConstraint(
            "scope = 'plan_family' OR offer_ids IS NULL",
            name="ck_captive_access_rules_offer_ids_scope",
        ),
        CheckConstraint(
            "(reseller_condition = 'specific') = (reseller_ids IS NOT NULL)",
            name="ck_captive_access_rules_reseller_ids",
        ),
        CheckConstraint(
            "enabled OR (disabled_at IS NOT NULL AND disabled_by IS NOT NULL)",
            name="ck_captive_access_rules_disable_evidence",
        ),
        CheckConstraint(
            "length(trim(reason)) > 0", name="ck_captive_access_rules_reason"
        ),
        Index("ix_captive_access_rules_enabled_scope", "enabled", "scope"),
        Index("ix_captive_access_rules_subscriber", "subscriber_id"),
        Index("ix_captive_access_rules_customer_set", "customer_set_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    effect: Mapped[str] = mapped_column(String(8), nullable=False)
    subscriber_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id", ondelete="CASCADE")
    )
    customer_set_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("captive_customer_sets.id", ondelete="RESTRICT"),
    )
    plan_family: Mapped[str | None] = mapped_column(String(40))
    #: Optional narrowing of a plan_family rule to exact offers (UUID strings).
    offer_ids: Mapped[list[str] | None] = mapped_column(JSON(none_as_null=True))
    #: Optional subscriber-category condition (``SubscriberCategory`` values).
    #: ``NULL`` matches any category, including unclassified accounts.
    subscriber_categories: Mapped[list[str] | None] = mapped_column(
        JSON(none_as_null=True)
    )
    reseller_condition: Mapped[str] = mapped_column(
        String(16), nullable=False, default=CaptiveResellerCondition.any.value
    )
    #: Reseller UUID strings when ``reseller_condition = 'specific'``.
    reseller_ids: Mapped[list[str] | None] = mapped_column(JSON(none_as_null=True))
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, onupdate=_now
    )
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    disabled_by: Mapped[str | None] = mapped_column(String(255))
    disabled_reason: Mapped[str | None] = mapped_column(Text)


class CaptiveAccessPolicyChange(Base):
    """Idempotency and outcome evidence for one applied policy change batch."""

    __tablename__ = "captive_access_policy_changes"
    __table_args__ = (
        UniqueConstraint(
            "idempotency_key", name="uq_captive_access_policy_changes_idempotency"
        ),
        CheckConstraint(
            "change_kind IN ('reevaluate', 'add_rule', 'disable_rule', "
            "'create_customer_set', 'add_customer_set_members', "
            "'remove_customer_set_members')",
            name="ck_captive_access_policy_changes_kind",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    command_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    change_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    change_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    preview_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    actor: Mapped[str] = mapped_column(String(255), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    max_subscriptions: Mapped[int] = mapped_column(nullable=False)
    #: Serialized typed outcome, replayed verbatim for the same key.
    outcome: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )


__all__ = [
    "CaptiveAccessPolicyChange",
    "CaptiveAccessRule",
    "CaptiveAccessRuleEffect",
    "CaptiveAccessRuleScope",
    "CaptiveCustomerSet",
    "CaptiveCustomerSetMember",
    "CaptiveResellerCondition",
]
