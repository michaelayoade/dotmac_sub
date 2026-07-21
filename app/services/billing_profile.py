from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.catalog import BillingMode, CatalogOffer, Subscription
from app.models.subscriber import Subscriber
from app.services.billing_settings import COLLECTIBLE_SERVICE_STATUSES
from app.services.domain_errors import DomainError


class BillingProfileSource(StrEnum):
    """Authoritative record set that supplied the effective billing mode."""

    MIXED_SUBSCRIPTIONS = "mixed_subscriptions"
    SUBSCRIPTION = "subscription"
    ACCOUNT = "account"


class BillingProfileReason(StrEnum):
    """Stable reason vocabulary for billing-profile decisions and failures."""

    MIXED_COLLECTIBLE_SUBSCRIPTION_BILLING_MODES = (
        "mixed_collectible_subscription_billing_modes"
    )
    ACCOUNT_BILLING_MODE_MISSING = "account_billing_mode_missing"
    ALREADY_ALIGNED = "already_aligned"
    ALIGN_ACCOUNT_TO_COLLECTIBLE_SUBSCRIPTIONS = (
        "align_account_to_collectible_subscriptions"
    )
    ACCOUNT_SUBSCRIPTION_BILLING_MODE_MISMATCH = (
        "account_subscription_billing_mode_mismatch"
    )
    COLLECTIBLE_SUBSCRIPTIONS_REQUIRE_ALIGNMENT = (
        "collectible_subscriptions_require_alignment"
    )
    ACCOUNT_OFFER_BILLING_MODE_MISMATCH = "account_offer_billing_mode_mismatch"
    BILLING_MODE_UNRESOLVED = "billing_mode_unresolved"
    REQUESTED_BILLING_MODE_MISMATCH = "requested_billing_mode_mismatch"
    SUBSCRIBER_NOT_FOUND = "subscriber_not_found"
    OFFER_NOT_FOUND = "offer_not_found"


@dataclass(frozen=True, slots=True)
class BillingProfile:
    """Resolved billing-mode authority for one account.

    The migration-safe rule is:
    - no collectible subscriptions: use the account billing flag
    - exactly one collectible subscription mode: use that mode, flag account drift
    - mixed collectible subscription modes: invalid; automation must not guess
    """

    account_id: UUID
    account_mode: BillingMode | None
    subscription_modes: frozenset[BillingMode]
    effective_mode: BillingMode | None
    source: BillingProfileSource
    account_subscription_mismatch: bool
    invalid_reason: BillingProfileReason | None = None

    @property
    def has_collectible_subscriptions(self) -> bool:
        return bool(self.subscription_modes)

    @property
    def has_mixed_subscription_modes(self) -> bool:
        return len(self.subscription_modes) > 1

    @property
    def is_valid(self) -> bool:
        return self.invalid_reason is None

    @property
    def automation_safe(self) -> bool:
        return (
            self.is_valid
            and self.effective_mode is not None
            and not self.account_subscription_mismatch
        )

    @property
    def has_prepaid_collectible_service(self) -> bool:
        return BillingMode.prepaid in self.subscription_modes


@dataclass(frozen=True, slots=True)
class BillingModeTransitionDecision:
    account_id: UUID
    current_mode: BillingMode | None
    target_mode: BillingMode
    allowed: bool
    reason: BillingProfileReason | None
    requires_subscription_alignment: bool
    profile: BillingProfile


@dataclass(frozen=True, slots=True)
class SubscriptionBillingModeWriteDecision:
    account_mode: BillingMode | None
    offer_mode: BillingMode | None
    requested_mode: BillingMode | None
    resolved_mode: BillingMode | None
    allowed: bool
    reason: BillingProfileReason | None = None


class BillingProfileError(DomainError):
    """Stable transport-neutral billing-profile failure."""

    def __init__(self, reason: BillingProfileReason):
        self.reason = reason
        super().__init__(
            code=f"financial.billing_profile.{reason.value}",
            message=reason.value.replace("_", " "),
        )


class BillingModeWriteRejected(BillingProfileError):
    """A subscription billing-mode write conflicts with canonical evidence."""


def require_effective_billing_mode(profile: BillingProfile) -> BillingMode:
    """Return a valid effective mode or fail closed with a stable domain error."""

    if profile.invalid_reason is not None:
        raise BillingProfileError(profile.invalid_reason)
    if profile.effective_mode is None:
        raise BillingProfileError(BillingProfileReason.BILLING_MODE_UNRESOLVED)
    return profile.effective_mode


def _profile_from_modes(
    account: Subscriber, modes: frozenset[BillingMode]
) -> BillingProfile:
    account_mode = account.billing_mode

    if len(modes) > 1:
        return BillingProfile(
            account_id=account.id,
            account_mode=account_mode,
            subscription_modes=modes,
            effective_mode=None,
            source=BillingProfileSource.MIXED_SUBSCRIPTIONS,
            account_subscription_mismatch=True,
            invalid_reason=(
                BillingProfileReason.MIXED_COLLECTIBLE_SUBSCRIPTION_BILLING_MODES
            ),
        )

    if len(modes) == 1:
        effective = next(iter(modes))
        return BillingProfile(
            account_id=account.id,
            account_mode=account_mode,
            subscription_modes=modes,
            effective_mode=effective,
            source=BillingProfileSource.SUBSCRIPTION,
            account_subscription_mismatch=(
                account_mode is not None and account_mode != effective
            ),
            invalid_reason=(
                BillingProfileReason.ACCOUNT_BILLING_MODE_MISSING
                if account_mode is None
                else None
            ),
        )

    return BillingProfile(
        account_id=account.id,
        account_mode=account_mode,
        subscription_modes=modes,
        effective_mode=account_mode,
        source=BillingProfileSource.ACCOUNT,
        account_subscription_mismatch=False,
    )


def resolve_billing_profiles(
    db: Session, accounts: Iterable[Subscriber]
) -> dict[UUID, BillingProfile]:
    """Resolve billing-mode authority for a cohort with one subscription query."""
    account_by_id = {account.id: account for account in accounts}
    if not account_by_id:
        return {}
    rows = (
        db.query(Subscription.subscriber_id, Subscription.billing_mode)
        .filter(Subscription.subscriber_id.in_(account_by_id))
        .filter(Subscription.status.in_(COLLECTIBLE_SERVICE_STATUSES))
        .all()
    )
    modes_by_account: dict[UUID, set[BillingMode]] = {}
    for account_id, mode in rows:
        if mode is not None:
            modes_by_account.setdefault(account_id, set()).add(mode)
    return {
        account_id: _profile_from_modes(
            account, frozenset(modes_by_account.get(account_id, set()))
        )
        for account_id, account in account_by_id.items()
    }


def resolve_billing_profile(db: Session, account: Subscriber) -> BillingProfile:
    return resolve_billing_profiles(db, [account])[account.id]


def plan_billing_mode_transition(
    profile: BillingProfile,
    target_mode: BillingMode,
    *,
    allow_mixed_subscription_modes: bool = False,
) -> BillingModeTransitionDecision:
    """Decide whether an account can move to ``target_mode`` safely.

    This is a policy decision only; callers still own the actual mutations and
    any financial reconciliation required before changing modes.
    """
    if (
        profile.effective_mode == target_mode
        and not profile.account_subscription_mismatch
    ):
        return BillingModeTransitionDecision(
            account_id=profile.account_id,
            current_mode=profile.effective_mode,
            target_mode=target_mode,
            allowed=True,
            reason=BillingProfileReason.ALREADY_ALIGNED,
            requires_subscription_alignment=False,
            profile=profile,
        )

    if (
        profile.effective_mode == target_mode
        and profile.account_subscription_mismatch
        and not profile.has_mixed_subscription_modes
    ):
        return BillingModeTransitionDecision(
            account_id=profile.account_id,
            current_mode=profile.account_mode,
            target_mode=target_mode,
            allowed=True,
            reason=BillingProfileReason.ALIGN_ACCOUNT_TO_COLLECTIBLE_SUBSCRIPTIONS,
            requires_subscription_alignment=False,
            profile=profile,
        )

    if profile.has_mixed_subscription_modes and not allow_mixed_subscription_modes:
        return BillingModeTransitionDecision(
            account_id=profile.account_id,
            current_mode=profile.effective_mode,
            target_mode=target_mode,
            allowed=False,
            reason=(BillingProfileReason.MIXED_COLLECTIBLE_SUBSCRIPTION_BILLING_MODES),
            requires_subscription_alignment=True,
            profile=profile,
        )

    if profile.account_subscription_mismatch:
        return BillingModeTransitionDecision(
            account_id=profile.account_id,
            current_mode=profile.effective_mode,
            target_mode=target_mode,
            allowed=False,
            reason=BillingProfileReason.ACCOUNT_SUBSCRIPTION_BILLING_MODE_MISMATCH,
            requires_subscription_alignment=True,
            profile=profile,
        )

    return BillingModeTransitionDecision(
        account_id=profile.account_id,
        current_mode=profile.effective_mode,
        target_mode=target_mode,
        allowed=True,
        reason=(
            BillingProfileReason.COLLECTIBLE_SUBSCRIPTIONS_REQUIRE_ALIGNMENT
            if profile.has_collectible_subscriptions
            else None
        ),
        requires_subscription_alignment=profile.has_collectible_subscriptions,
        profile=profile,
    )


def plan_subscription_billing_mode_write(
    *,
    account_mode: BillingMode | None,
    offer_mode: BillingMode | None,
    requested_mode: BillingMode | None,
) -> SubscriptionBillingModeWriteDecision:
    if (
        account_mode is not None
        and offer_mode is not None
        and account_mode != offer_mode
    ):
        return SubscriptionBillingModeWriteDecision(
            account_mode=account_mode,
            offer_mode=offer_mode,
            requested_mode=requested_mode,
            resolved_mode=None,
            allowed=False,
            reason=BillingProfileReason.ACCOUNT_OFFER_BILLING_MODE_MISMATCH,
        )

    resolved_mode = offer_mode or account_mode or requested_mode
    if resolved_mode is None:
        return SubscriptionBillingModeWriteDecision(
            account_mode=account_mode,
            offer_mode=offer_mode,
            requested_mode=requested_mode,
            resolved_mode=None,
            allowed=False,
            reason=BillingProfileReason.BILLING_MODE_UNRESOLVED,
        )
    if requested_mode is not None and requested_mode != resolved_mode:
        return SubscriptionBillingModeWriteDecision(
            account_mode=account_mode,
            offer_mode=offer_mode,
            requested_mode=requested_mode,
            resolved_mode=resolved_mode,
            allowed=False,
            reason=BillingProfileReason.REQUESTED_BILLING_MODE_MISMATCH,
        )
    return SubscriptionBillingModeWriteDecision(
        account_mode=account_mode,
        offer_mode=offer_mode,
        requested_mode=requested_mode,
        resolved_mode=resolved_mode,
        allowed=True,
    )


def resolve_subscription_billing_mode_for_write(
    db: Session,
    *,
    account_id: UUID | str,
    offer_id: UUID | str,
    requested_mode: BillingMode | None = None,
) -> BillingMode:
    account = db.get(Subscriber, account_id)
    if account is None:
        raise BillingModeWriteRejected(BillingProfileReason.SUBSCRIBER_NOT_FOUND)
    offer = db.get(CatalogOffer, offer_id)
    if offer is None:
        raise BillingModeWriteRejected(BillingProfileReason.OFFER_NOT_FOUND)
    decision = plan_subscription_billing_mode_write(
        account_mode=account.billing_mode,
        offer_mode=offer.billing_mode,
        requested_mode=requested_mode,
    )
    if not decision.allowed or decision.resolved_mode is None:
        raise BillingModeWriteRejected(
            decision.reason or BillingProfileReason.BILLING_MODE_UNRESOLVED
        )
    return decision.resolved_mode
