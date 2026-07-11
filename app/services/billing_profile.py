from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.catalog import BillingMode, Subscription
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


def resolve_billing_profile(db: Session, account: Subscriber) -> BillingProfile:
    rows = (
        db.query(Subscription.billing_mode)
        .filter(Subscription.subscriber_id == account.id)
        .filter(Subscription.status.in_(COLLECTIBLE_SERVICE_STATUSES))
        .distinct()
        .all()
    )
    modes = frozenset(row[0] for row in rows if row[0] is not None)
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
        )

    return BillingProfile(
        account_id=account.id,
        account_mode=account_mode,
        subscription_modes=modes,
        effective_mode=account_mode,
        source="account",
        account_subscription_mismatch=False,
    )


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
