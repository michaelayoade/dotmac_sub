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
from enum import StrEnum
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
UNWALL_OWNER = "financial.walled_account_healing"
UNWALL_EXCEPTION_PREFIX = "walled_account_healing:"


@dataclass
class UnwallResult:
    account_id: str
    available_balance: Decimal
    prior_status: str
    new_status: str | None = None
    restored: bool = False
    error: str | None = None
    disposition: UnwallDisposition | None = None
    decision: UnwallDecision | None = None
    restoration_outcomes: tuple[dict[str, object], ...] = ()


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


class UnwallDisposition(StrEnum):
    """Exactly why one healing attempt ended the way it did."""

    restored = "restored"
    not_walled = "not_walled"
    account_not_found = "account_not_found"
    blocked_overdue_receivable = "blocked_overdue_receivable"
    ambiguous_no_change = "ambiguous_no_change"
    failed = "failed"


@dataclass(frozen=True, slots=True)
class UnwallDecision:
    """Locked, recomputed evidence for one healing candidate."""

    account_id: str
    prior_status: str
    walled: bool
    overdue_receivable_total: Decimal
    overdue_receivable_invoice_ids: tuple[str, ...]
    available_balance: Decimal
    unambiguous: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "account_id": self.account_id,
            "prior_status": self.prior_status,
            "walled": self.walled,
            "overdue_receivable_total": str(self.overdue_receivable_total),
            "overdue_receivable_invoice_ids": list(self.overdue_receivable_invoice_ids),
            "available_balance": str(self.available_balance),
            "unambiguous": self.unambiguous,
        }


def decide_unwall(db: Session, account_id: str) -> UnwallDecision:
    """Recompute the exact healing decision for one account under an account lock.

    Exact arithmetic only. There is no tolerance, epsilon or de-minimis
    threshold: a fifty-kobo residue is a real overdue receivable and correctly
    blocks scheduled healing. What it does not do is disappear — it is reported
    on the decision and, when it blocks a scheduled pass, recorded as a durable
    operator exception.
    """
    from app.services.billing._common import lock_account
    from app.services.collections import (
        get_available_balance,
        overdue_receivable_snapshot,
    )

    lock_account(db, str(account_id))
    account = db.get(Subscriber, coerce_uuid(account_id))
    if account is None:
        return UnwallDecision(
            account_id=str(account_id),
            prior_status="unknown",
            walled=False,
            overdue_receivable_total=Decimal("0.00"),
            overdue_receivable_invoice_ids=(),
            available_balance=Decimal("0.00"),
            unambiguous=False,
        )
    rows = overdue_receivable_snapshot(db, str(account_id))
    total = sum(
        (Decimal(str(row.get("receivable", "0"))) for row in rows),
        Decimal("0.00"),
    )
    invoice_ids = tuple(
        str(row.get("invoice_id")) for row in rows if row.get("invoice_id") is not None
    )
    return UnwallDecision(
        account_id=str(account_id),
        prior_status=account.status.value if account.status else "unknown",
        walled=account.status in _WALLED_STATUSES,
        overdue_receivable_total=total,
        overdue_receivable_invoice_ids=invoice_ids,
        available_balance=get_available_balance(db, str(account_id)),
        unambiguous=not rows,
    )


def _stage_unwall_exception(
    db: Session,
    decision: UnwallDecision,
    *,
    disposition: UnwallDisposition,
    detail: str,
) -> None:
    """Record durable operator evidence for a row scheduled healing refused."""
    from app.models.network_monitoring import AlertSeverity
    from app.services.admin_alerts import AlertFinding, sync_alert

    sync_alert(
        db,
        AlertFinding(
            fingerprint=f"{UNWALL_EXCEPTION_PREFIX}{decision.account_id}",
            category="billing",
            source=UNWALL_OWNER,
            severity=AlertSeverity.warning,
            title=f"Walled account needs review: {disposition.value}",
            summary=detail,
            details={**decision.to_dict(), "disposition": disposition.value},
            target_url=f"/admin/customers/{decision.account_id}",
        ),
    )


def _clear_unwall_exception(db: Session, account_id: str) -> None:
    from app.services.admin_alerts import resolve_alert_by_fingerprint

    resolve_alert_by_fingerprint(
        db,
        f"{UNWALL_EXCEPTION_PREFIX}{account_id}",
        mark_notifications_read=True,
    )


def heal_walled_account(
    db: Session,
    account_id: str,
    *,
    require_zero_overdue_receivable: bool,
    actor: str,
    reason: str,
) -> UnwallResult:
    """Idempotent owner command: restore one walled account or explain why not.

    ``require_zero_overdue_receivable`` is the scheduled-application gate. A
    scheduled pass may only apply when the locked recomputation above proves
    there is no overdue receivable at all; every other row becomes an operator
    exception with durable evidence rather than an automated guess. Operator-
    driven runs may pass ``False`` and let the restoration owner apply its own
    reason-scoped gates.

    Idempotent: healing an already-active account is a no-op that reports
    ``not_walled`` and resolves any stale exception.
    """
    from app.services.account_lifecycle import compute_account_status
    from app.services.collections import restore_account_services_detailed

    decision = decide_unwall(db, account_id)
    result = UnwallResult(
        account_id=str(account_id),
        available_balance=decision.available_balance,
        prior_status=decision.prior_status,
    )
    result.decision = decision
    if decision.prior_status == "unknown":
        result.disposition = UnwallDisposition.account_not_found
        return result
    if not decision.walled:
        result.disposition = UnwallDisposition.not_walled
        result.new_status = decision.prior_status
        _clear_unwall_exception(db, str(account_id))
        db.commit()
        return result
    if require_zero_overdue_receivable and not decision.unambiguous:
        result.disposition = UnwallDisposition.blocked_overdue_receivable
        _stage_unwall_exception(
            db,
            decision,
            disposition=UnwallDisposition.blocked_overdue_receivable,
            detail=(
                f"Scheduled healing refused: exact overdue receivable "
                f"{decision.overdue_receivable_total} across "
                f"{len(decision.overdue_receivable_invoice_ids)} invoice(s). "
                "No tolerance is applied; clear or write off the residue, then "
                "the next pass heals the account automatically."
            ),
        )
        db.commit()
        return result

    try:
        consequence = restore_account_services_detailed(
            db,
            str(account_id),
            resolved_by=f"{actor}:{reason}",
        )
        new_status = compute_account_status(db, str(account_id))
        result.new_status = new_status.value
        result.restored = new_status == SubscriberStatus.active
        result.restoration_outcomes = tuple(
            consequence.restoration_outcomes if consequence is not None else ()
        )
        if result.restored:
            result.disposition = UnwallDisposition.restored
            _clear_unwall_exception(db, str(account_id))
        else:
            result.disposition = UnwallDisposition.ambiguous_no_change
            _stage_unwall_exception(
                db,
                decision,
                disposition=UnwallDisposition.ambiguous_no_change,
                detail=(
                    "Zero overdue receivable, but the restoration owner did not "
                    "return the account to active. Remaining blockers are on the "
                    "typed restoration outcomes for this account."
                ),
            )
        db.commit()
    except Exception as exc:  # noqa: BLE001 — isolate one bad account from the batch
        db.rollback()
        result.error = str(exc)
        result.disposition = UnwallDisposition.failed
        logger.exception("Un-wall failed for account %s", account_id)
    return result


def unwall_account(db: Session, account_id: str) -> UnwallResult:
    """Operator-driven facade over :func:`heal_walled_account`.

    Service-only: reason-scoped restore + status re-derivation. No ledger writes.
    """
    return heal_walled_account(
        db,
        account_id,
        require_zero_overdue_receivable=False,
        actor="operator:unwall",
        reason="funded-or-covered account un-wall",
    )


def run_scheduled_walled_account_healing(
    db: Session,
    *,
    limit: int = 100,
    apply: bool = False,
) -> dict[str, int]:
    """Bounded scheduled healing pass over the walled cohort.

    Unambiguous only. A candidate is healed automatically ONLY when the locked
    recomputation proves zero overdue receivable; everything else becomes a
    durable operator exception. ``apply=False`` still records those exceptions
    so the cohort is visible before automation is switched on, but changes no
    service state.
    """
    stats = {
        "candidates": 0,
        "restored": 0,
        "not_walled": 0,
        "blocked_overdue_receivable": 0,
        "ambiguous_no_change": 0,
        "exceptions": 0,
        "errors": 0,
        "applied": int(apply),
    }
    targets = find_walled_paid_account_ids(db, limit=limit)
    stats["candidates"] = len(targets)
    for account_id in targets:
        if not apply:
            decision = decide_unwall(db, account_id)
            if decision.walled and not decision.unambiguous:
                _stage_unwall_exception(
                    db,
                    decision,
                    disposition=UnwallDisposition.blocked_overdue_receivable,
                    detail=(
                        "Detected walled account with exact overdue receivable "
                        f"{decision.overdue_receivable_total}. Scheduled healing "
                        "is not applying; review or clear the receivable."
                    ),
                )
                stats["blocked_overdue_receivable"] += 1
                stats["exceptions"] += 1
            db.commit()
            continue
        result = heal_walled_account(
            db,
            account_id,
            require_zero_overdue_receivable=True,
            actor="service:walled_account_healing",
            reason="scheduled zero-overdue-receivable healing",
        )
        if result.error:
            stats["errors"] += 1
            continue
        disposition = result.disposition
        if disposition is UnwallDisposition.restored:
            stats["restored"] += 1
        elif disposition is UnwallDisposition.not_walled:
            stats["not_walled"] += 1
        elif disposition is UnwallDisposition.blocked_overdue_receivable:
            stats["blocked_overdue_receivable"] += 1
            stats["exceptions"] += 1
        elif disposition is UnwallDisposition.ambiguous_no_change:
            stats["ambiguous_no_change"] += 1
            stats["exceptions"] += 1
    logger.info(
        "walled_account_healing_pass",
        extra={"event": "walled_account_healing_pass", **stats},
    )
    return stats


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
