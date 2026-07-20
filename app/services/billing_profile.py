from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.catalog import BillingMode, CatalogOffer, Subscription
from app.models.subscriber import Subscriber
from app.services.billing_settings import COLLECTIBLE_SERVICE_STATUSES


@dataclass(frozen=True)
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
    source: str
    account_subscription_mismatch: bool
    invalid_reason: str | None = None

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


@dataclass(frozen=True)
class BillingModeTransitionDecision:
    account_id: UUID
    current_mode: BillingMode | None
    target_mode: BillingMode
    allowed: bool
    reason: str | None
    requires_subscription_alignment: bool
    profile: BillingProfile


@dataclass(frozen=True)
class SubscriptionBillingModeWriteDecision:
    account_mode: BillingMode | None
    offer_mode: BillingMode | None
    requested_mode: BillingMode | None
    resolved_mode: BillingMode | None
    allowed: bool
    reason: str | None = None


class BillingModeWriteRejected(ValueError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason.replace("_", " "))


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
            source="mixed_subscriptions",
            account_subscription_mismatch=True,
            invalid_reason="mixed_collectible_subscription_billing_modes",
        )

    if len(modes) == 1:
        effective = next(iter(modes))
        return BillingProfile(
            account_id=account.id,
            account_mode=account_mode,
            subscription_modes=modes,
            effective_mode=effective,
            source="subscription",
            account_subscription_mismatch=(
                account_mode is not None and account_mode != effective
            ),
            invalid_reason=(
                "account_billing_mode_missing" if account_mode is None else None
            ),
        )

    return BillingProfile(
        account_id=account.id,
        account_mode=account_mode,
        subscription_modes=modes,
        effective_mode=account_mode,
        source="account",
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
            reason="already_aligned",
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
            reason="align_account_to_collectible_subscriptions",
            requires_subscription_alignment=False,
            profile=profile,
        )

    if profile.has_mixed_subscription_modes and not allow_mixed_subscription_modes:
        return BillingModeTransitionDecision(
            account_id=profile.account_id,
            current_mode=profile.effective_mode,
            target_mode=target_mode,
            allowed=False,
            reason="mixed_collectible_subscription_billing_modes",
            requires_subscription_alignment=True,
            profile=profile,
        )

    if profile.account_subscription_mismatch:
        return BillingModeTransitionDecision(
            account_id=profile.account_id,
            current_mode=profile.effective_mode,
            target_mode=target_mode,
            allowed=False,
            reason="account_subscription_billing_mode_mismatch",
            requires_subscription_alignment=True,
            profile=profile,
        )

    return BillingModeTransitionDecision(
        account_id=profile.account_id,
        current_mode=profile.effective_mode,
        target_mode=target_mode,
        allowed=True,
        reason=None,
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
            reason="account_offer_billing_mode_mismatch",
        )

    resolved_mode = offer_mode or account_mode or requested_mode
    if resolved_mode is None:
        return SubscriptionBillingModeWriteDecision(
            account_mode=account_mode,
            offer_mode=offer_mode,
            requested_mode=requested_mode,
            resolved_mode=None,
            allowed=False,
            reason="billing_mode_unresolved",
        )
    if requested_mode is not None and requested_mode != resolved_mode:
        return SubscriptionBillingModeWriteDecision(
            account_mode=account_mode,
            offer_mode=offer_mode,
            requested_mode=requested_mode,
            resolved_mode=resolved_mode,
            allowed=False,
            reason="requested_billing_mode_mismatch",
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
        raise BillingModeWriteRejected("subscriber_not_found")
    offer = db.get(CatalogOffer, offer_id)
    if offer is None:
        raise BillingModeWriteRejected("offer_not_found")
    decision = plan_subscription_billing_mode_write(
        account_mode=account.billing_mode,
        offer_mode=offer.billing_mode,
        requested_mode=requested_mode,
    )
    if not decision.allowed or decision.resolved_mode is None:
        raise BillingModeWriteRejected(decision.reason or "billing_mode_unresolved")
    return decision.resolved_mode
