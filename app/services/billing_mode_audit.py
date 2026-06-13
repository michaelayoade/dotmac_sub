"""Detect billing_mode drift across account / subscription / offer.

`billing_mode` (prepaid/postpaid) is denormalized onto Subscriber (account),
Subscription, and CatalogOffer with no enforced sync (see the 2026-06-13 review):
a subscription inherits the account mode at creation and is never re-derived, an
account-mode change is not propagated, and there is no subscribe-time guard that
the offer's mode matches. Only `Subscription.billing_mode` is load-bearing for
billing/enforcement, so any drift is silent.

This surfaces accounts where the three layers disagree (or an account holds
mixed-mode active subscriptions) so they can be reconciled — especially before
the local billing runner is enabled at Splynx cutover.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.catalog import CatalogOffer, Subscription, SubscriptionStatus
from app.models.subscriber import Subscriber

# Same "live" set as enforce_single_active_subscription.
_ACTIVE_STATUSES = (
    SubscriptionStatus.active,
    SubscriptionStatus.pending,
    SubscriptionStatus.suspended,
)


def _mode_value(mode: object) -> str | None:
    if mode is None:
        return None
    return getattr(mode, "value", str(mode))


def find_billing_mode_inconsistencies(db: Session) -> list[dict]:
    """Return one entry per detected billing_mode inconsistency.

    Issue types:
    - ``subscription_vs_account``: an active subscription's mode != its account's
    - ``subscription_vs_offer``: an active subscription's mode != its offer's
    - ``mixed_mode_account``: an account holds active subscriptions of >1 mode
    """
    rows = db.execute(
        select(
            Subscription.id,
            Subscription.subscriber_id,
            Subscription.billing_mode,
            Subscriber.billing_mode,
            CatalogOffer.billing_mode,
        )
        .join(Subscriber, Subscriber.id == Subscription.subscriber_id)
        .outerjoin(CatalogOffer, CatalogOffer.id == Subscription.offer_id)
        .where(Subscription.status.in_(_ACTIVE_STATUSES))
    ).all()

    issues: list[dict] = []
    modes_by_account: dict[str, set] = {}

    for sub_id, subscriber_id, sub_mode, account_mode, offer_mode in rows:
        acct = str(subscriber_id)
        modes_by_account.setdefault(acct, set()).add(sub_mode)

        if account_mode is not None and sub_mode != account_mode:
            issues.append(
                {
                    "issue": "subscription_vs_account",
                    "subscriber_id": acct,
                    "subscription_id": str(sub_id),
                    "subscription_mode": _mode_value(sub_mode),
                    "account_mode": _mode_value(account_mode),
                }
            )
        if offer_mode is not None and sub_mode != offer_mode:
            issues.append(
                {
                    "issue": "subscription_vs_offer",
                    "subscriber_id": acct,
                    "subscription_id": str(sub_id),
                    "subscription_mode": _mode_value(sub_mode),
                    "offer_mode": _mode_value(offer_mode),
                }
            )

    for acct, modes in modes_by_account.items():
        if len(modes) > 1:
            issues.append(
                {
                    "issue": "mixed_mode_account",
                    "subscriber_id": acct,
                    "modes": sorted(_mode_value(m) for m in modes),
                }
            )

    return issues
