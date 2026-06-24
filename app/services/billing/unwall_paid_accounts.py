"""Account-level service restore for paid-up-but-walled accounts.

The SAFE replacement for per-invoice credit settlement, which is unsound on the
migrated dataset (per-invoice ``balance_due``/allocations are not authoritative —
many invoices were paid from the account deposit with no invoice-linked
allocation, and recomputing locally manufactures phantom debt).

Instead of trusting per-invoice balances, this keys on the authoritative
account-level net: ``get_available_balance`` (the imported deposit for migrated
accounts, the local ledger for native ones). An account that is walled
(``suspended``/``blocked``) but whose available balance is ``>= 0`` is paid up
and should not be walled for non-payment — so we re-evaluate enforcement and
restore service.

NO ledger / money writes. Pure service-state correction:
  - ``restore_account_services`` — reason-scoped, lifts only payment/collections
    enforcement locks (never admin/fraud/FUP);
  - ``compute_account_status`` — re-derive subscriber status from its
    subscriptions (clears a stale account-level block).
The caller then refreshes RADIUS + CoA. Idempotent; a not-walled or genuinely
owing account is left untouched.
"""

from __future__ import annotations

import logging
from contextlib import nullcontext
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.subscriber import Subscriber, SubscriberStatus
from app.services.common import coerce_uuid
from app.services.notification_suppression import suppress_notifications

logger = logging.getLogger(__name__)

_WALLED_STATUSES = (SubscriberStatus.suspended, SubscriberStatus.blocked)


@dataclass
class UnwallResult:
    account_id: str
    available_balance: Decimal
    prior_status: str
    new_status: str | None = None
    restored: bool = False
    error: str | None = None


def find_walled_paid_account_ids(db: Session, *, limit: int | None = None) -> list[str]:
    """Walled (suspended/blocked) subscribers whose available balance is >= 0.

    Keyed on the account-level net (deposit/ledger via ``get_available_balance``),
    never per-invoice balance_due. These are paid up yet walled — the cohort to
    restore.
    """
    from app.services.collections import get_available_balance

    candidate_ids = [
        str(r[0])
        for r in db.execute(
            select(Subscriber.id).where(Subscriber.status.in_(_WALLED_STATUSES))
        ).all()
    ]
    out: list[str] = []
    for account_id in candidate_ids:
        if get_available_balance(db, account_id) >= 0:
            out.append(account_id)
            if limit is not None and len(out) >= limit:
                break
    return out


def project_unwall(db: Session, account_id: str) -> UnwallResult:
    """Read-only: report a walled+paid-up account without mutating anything."""
    from app.services.collections import get_available_balance

    account = db.get(Subscriber, coerce_uuid(account_id))
    status = account.status.value if account and account.status else "unknown"
    return UnwallResult(
        account_id=str(account_id),
        available_balance=get_available_balance(db, str(account_id)),
        prior_status=status,
    )


def unwall_account(db: Session, account_id: str) -> UnwallResult:
    """Restore service for one paid-up walled account (commits on success).

    Service-only: reason-scoped restore + status re-derivation. No ledger writes.
    """
    from app.services import collections as collections_service
    from app.services.account_lifecycle import compute_account_status
    from app.services.collections import get_available_balance

    account = db.get(Subscriber, coerce_uuid(account_id))
    result = UnwallResult(
        account_id=str(account_id),
        available_balance=get_available_balance(db, str(account_id)),
        prior_status=account.status.value if account and account.status else "unknown",
    )
    was_walled = account is not None and account.status in _WALLED_STATUSES
    try:
        collections_service.restore_account_services(db, str(account_id))
        new_status = compute_account_status(db, str(account_id))
        result.new_status = new_status.value
        result.restored = was_walled and new_status == SubscriberStatus.active
        db.commit()
    except Exception as exc:  # noqa: BLE001 — isolate one bad account from the batch
        db.rollback()
        result.error = str(exc)
        logger.exception("Un-wall failed for account %s", account_id)
    return result


def _account_subscription_ids(db: Session, account_id: str) -> list[str]:
    from app.models.catalog import Subscription

    return [
        str(r[0])
        for r in db.execute(
            select(Subscription.id).where(
                Subscription.subscriber_id == coerce_uuid(account_id)
            )
        ).all()
    ]


@dataclass
class UnwallSummary:
    candidates: int = 0
    restored: int = 0
    errors: int = 0
    dry_run: bool = True
    radius_refreshed: bool = False
    sessions_kicked: int = 0
    results: list[UnwallResult] = field(default_factory=list)


def unwall_cohort(
    db: Session,
    *,
    account_ids: list[str] | None = None,
    limit: int | None = None,
    dry_run: bool = True,
    refresh_radius: bool = True,
    send_coa: bool = True,
    notify: bool = False,
    extra_subscription_ids: list[str] | None = None,
) -> UnwallSummary:
    """Restore service for paid-up-but-walled accounts, then refresh RADIUS + CoA.

    Two modes:
      - **Targeted** (``account_ids`` given): restore ONLY those accounts that are
        paid up. Use this to safely un-wall a specific reported set first. It also
        covers active-but-stale-tag accounts (restore is a no-op for them; the
        RADIUS refresh + CoA below still drops the stale walled-garden tag and
        kicks the session).
      - **Cohort** (default): discover and restore all walled + paid-up accounts.

    ``notify`` defaults False (bulk catch-up — suppress the "service resumed"
    burst). ``extra_subscription_ids`` forces RADIUS + CoA onto extra subscriptions.
    """
    from app.services.collections import get_available_balance

    targeted = account_ids is not None
    if account_ids is not None:
        # Paid-up gate still applies — never restore an account that genuinely owes.
        targets = [a for a in account_ids if get_available_balance(db, a) >= 0]
    else:
        targets = find_walled_paid_account_ids(db, limit=limit)
    summary = UnwallSummary(candidates=len(targets), dry_run=dry_run)

    if dry_run:
        summary.results = [project_unwall(db, aid) for aid in targets]
        return summary

    suppress_ctx = nullcontext() if notify else suppress_notifications()
    coa_subscription_ids: set = set(extra_subscription_ids or [])
    with suppress_ctx:
        for account_id in targets:
            result = unwall_account(db, account_id)
            summary.results.append(result)
            if result.error:
                summary.errors += 1
                continue
            if result.restored:
                summary.restored += 1
            # CoA the account's sessions when we restored it, OR always in targeted
            # mode (so a named active-but-stale-tag account still gets kicked).
            if result.restored or targeted:
                coa_subscription_ids.update(_account_subscription_ids(db, account_id))

    if refresh_radius:
        from app.services.radius_population import populate

        populate(dry_run=False)
        summary.radius_refreshed = True

    if send_coa and coa_subscription_ids:
        from app.services.enforcement import disconnect_subscription_sessions

        kicked = 0
        for subscription_id in coa_subscription_ids:
            try:
                kicked += disconnect_subscription_sessions(
                    db, subscription_id, reason="paid-up account un-wall"
                )
            except Exception:
                logger.warning(
                    "Un-wall: CoA kick failed for subscription %s",
                    subscription_id,
                    exc_info=True,
                )
        summary.sessions_kicked = kicked

    return summary
