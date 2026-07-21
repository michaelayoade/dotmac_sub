"""Account-level service restore for funded-or-covered walled accounts.

The SAFE replacement for per-invoice credit settlement, which is unsound on the
migrated dataset (per-invoice ``balance_due``/allocations are not authoritative —
many invoices were paid from the account deposit with no invoice-linked
allocation, and recomputing locally manufactures phantom debt).

Instead of trusting per-invoice balances, prepaid selection consumes the same
funding and exact-coverage decision as live access restoration. A configured
reserve target never blocks restoration of an already covered service, and a
future billing anchor or paid invoice alone never authorizes restoration.
Postpaid legacy repair retains its non-negative account-net cohort, while the
restore owner separately refuses to clear an overdue lock until collectible
debt is gone.

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
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.catalog import BillingMode
from app.models.collections import FinancialAccessOrigin
from app.models.enforcement_lock import EnforcementLock, EnforcementReason
from app.models.subscriber import Subscriber, SubscriberStatus
from app.services.access_resolution import resolve_prepaid_funding
from app.services.billing_profile import resolve_billing_profile
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


def _funding_allows_restore(db: Session, account_id: str) -> bool:
    """Use the same prepaid funding decision as live access reconciliation."""
    from app.services.collections import get_available_balance

    account = db.get(Subscriber, coerce_uuid(account_id))
    if account is None:
        return False
    profile = resolve_billing_profile(db, account)
    if profile.effective_mode == BillingMode.prepaid:
        if not profile.automation_safe:
            return False
        funding = resolve_prepaid_funding(db, account)
        return funding.funded or bool(funding.covered_subscription_ids)
    return get_available_balance(db, account_id) >= 0


def find_walled_paid_account_ids(db: Session, *, limit: int | None = None) -> list[str]:
    """Walled subscribers with at least one canonical restoration path.

    Prepaid selection accepts sufficient account funding or exact current
    coverage. The restoration owner still chooses the exact eligible locks, so
    an unresolved sibling service cannot be restored by association. Postpaid
    selection remains account-net based, never per-invoice ``balance_due``.
    """
    candidate_ids = [
        str(r[0])
        for r in db.execute(
            select(Subscriber.id).where(Subscriber.status.in_(_WALLED_STATUSES))
        ).all()
    ]
    out: list[str] = []
    for account_id in candidate_ids:
        if _funding_allows_restore(db, account_id):
            out.append(account_id)
            if limit is not None and len(out) >= limit:
                break
    return out


def find_prepaid_restorable_lock_account_ids(
    db: Session, *, limit: int | None = None
) -> list[str]:
    """Accounts whose active prepaid locks have an owner-approved restore path.

    This is the production cleanup cohort. It starts from active prepaid locks,
    not subscriber status or ``next_billing_at``, and asks the canonical
    financial-access owner which exact lock IDs are restorable. Consequently a
    covered subscription can be restored while an unresolved sibling remains
    untouched, and postpaid/overdue-only accounts cannot enter this cohort.
    """
    from app.services.collections import preview_financial_access_restoration

    rows = db.execute(
        select(EnforcementLock.subscriber_id, EnforcementLock.id)
        .where(
            EnforcementLock.is_active.is_(True),
            EnforcementLock.reason == EnforcementReason.prepaid,
        )
        .order_by(EnforcementLock.subscriber_id, EnforcementLock.id)
    ).all()
    lock_ids_by_account: dict[str, set[UUID]] = {}
    for account_id, lock_id in rows:
        lock_ids_by_account.setdefault(str(account_id), set()).add(lock_id)

    out: list[str] = []
    for account_id, prepaid_lock_ids in lock_ids_by_account.items():
        preview = preview_financial_access_restoration(
            db,
            account_id,
            origin=FinancialAccessOrigin.prepaid_enforcement,
        )
        if prepaid_lock_ids.intersection(preview.target_lock_ids):
            out.append(account_id)
            if limit is not None and len(out) >= limit:
                break
    return out


def project_unwall(db: Session, account_id: str) -> UnwallResult:
    """Read-only: report an eligible walled account without mutating anything."""
    from app.services.collections import get_available_balance

    account = db.get(Subscriber, coerce_uuid(account_id))
    status = account.status.value if account and account.status else "unknown"
    return UnwallResult(
        account_id=str(account_id),
        available_balance=get_available_balance(db, str(account_id)),
        prior_status=status,
    )


def unwall_account(db: Session, account_id: str) -> UnwallResult:
    """Restore eligible service for one walled account (commits on success).

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
    prepaid_locks_only: bool = False,
) -> UnwallSummary:
    """Restore eligible walled service, then refresh RADIUS + CoA.

    Two modes:
      - **Targeted** (``account_ids`` given): restore ONLY those accounts that are
        paid up. Use this to safely un-wall a specific reported set first. It also
        covers active-but-stale-tag accounts (restore is a no-op for them; the
        RADIUS refresh + CoA below still drops the stale walled-garden tag and
        kicks the session).
      - **Cohort** (default): discover and restore every eligible walled account.

    ``notify`` defaults False (bulk catch-up — suppress the "service resumed"
    burst). ``extra_subscription_ids`` forces RADIUS + CoA onto extra subscriptions.
    """
    targeted = account_ids is not None
    if account_ids is not None:
        # The canonical funding gate still applies in targeted mode.
        targets = [a for a in account_ids if _funding_allows_restore(db, a)]
    elif prepaid_locks_only:
        targets = find_prepaid_restorable_lock_account_ids(db, limit=limit)
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
                    db, subscription_id, reason="funded-or-covered account un-wall"
                )
            except Exception:
                logger.warning(
                    "Un-wall: CoA kick failed for subscription %s",
                    subscription_id,
                    exc_info=True,
                )
        summary.sessions_kicked = kicked

    return summary
