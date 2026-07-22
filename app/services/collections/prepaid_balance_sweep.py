"""Prepaid balance/expiry enforcement sweep (Item 2 of the prepaid alignment).

Deposit-is-truth prepaid customers are invoiced in advance; the read-side
(``service_status``) already *projects* low-balance / grace / deactivation
dates. This periodic sweep executes the resolved enforcement plan through the
timer-state and subscription-lifecycle owners, then requests restoration once
the account is funded again.
A resolved zero-day policy suspends on the first eligible sweep; a nonzero
configured policy arms the timer and warning first.

SAFETY: this can suspend customers. The owner always evaluates eligible accounts;
safety is account-scoped: canonical funding, coverage, quarantine, profile
validity, shields, grace, and transaction-local locking. Every account is
processed in its own committed unit so one bad row cannot abort the batch.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.enforcement_lock import EnforcementReason
from app.models.subscriber import Subscriber
from app.services.access_resolution import PrepaidFundingDecision
from app.services.collections._core import (
    _clear_prepaid_dunning_flags,
    _get_account_email,
    _restore_prepaid_if_funded,
    _suspend_account,
)
from app.services.common import coerce_uuid
from app.services.prepaid_enforcement_planner import (
    PrepaidEnforcementAction,
    PrepaidEnforcementPolicy,
    candidate_prepaid_account_ids,
    candidate_prepaid_funding_account_ids,
    plan_prepaid_account,
    prepaid_notice_suppression_reasons,
    resolve_prepaid_enforcement_policy,
)
from app.services.prepaid_enforcement_state import (
    arm_prepaid_low_balance_timer,
    mark_prepaid_deactivated,
)

logger = logging.getLogger(__name__)

_SOURCE = "prepaid_balance_sweep"


def _send_notice(
    db: Session,
    account: Subscriber,
    subject: str,
    body: str,
    balance: Decimal,
    threshold: Decimal,
    *,
    suppression_reason: str | None = None,
) -> bool:
    """Queue a customer email using the operator-configured subject/body.

    Reuses the same simple ``Notification`` (channel=email, status=queued)
    mechanism the dunning throttle/suspension notices use — the notification
    queue runner owns delivery. ``{balance}`` / ``{threshold}`` placeholders in
    the body are filled; malformed templates fall back to the raw text.
    """
    if suppression_reason:
        logger.info(
            "prepaid_balance_sweep notice suppressed for %s: %s",
            account.id,
            suppression_reason,
        )
        return False

    from app.models.notification import NotificationChannel
    from app.schemas.notification import NotificationCreate
    from app.services.notification import notifications as notifications_svc

    email = _get_account_email(db, str(account.id))
    if not email:
        logger.warning(
            "prepaid_balance_sweep notice skipped for %s: no email", account.id
        )
        return False
    try:
        rendered = str(body).format(balance=balance, threshold=threshold)
    except (KeyError, IndexError, ValueError):
        rendered = str(body)
    notifications_svc.queue_customer_notification(
        db,
        NotificationCreate(
            subscriber_id=account.id,
            channel=NotificationChannel.email,
            event_type="prepaid_balance_enforcement",
            category="billing",
            recipient=email,
            subject=str(subject),
            body=rendered,
        ),
    )
    return True


def _reconcile_funded(
    db: Session, account: Subscriber, funding: PrepaidFundingDecision
) -> str:
    """Balance is at/above threshold: undo any prepaid enforcement. Idempotent."""
    had_timers = (
        account.prepaid_low_balance_at is not None
        or account.prepaid_deactivation_at is not None
    )
    restored = _restore_prepaid_if_funded(
        db,
        account,
        funding,
        resolved_by=f"{_SOURCE}:{account.id}",
    )
    if had_timers:
        _clear_prepaid_dunning_flags(db, str(account.id))
    if had_timers or restored:
        logger.info(
            "prepaid_balance_sweep recovered account %s (restored=%d)",
            account.id,
            restored,
        )
        return "restored"
    return "ok"


def _reconcile_low(
    db: Session,
    account: Subscriber,
    now: datetime,
    cfg: PrepaidEnforcementPolicy,
    balance: Decimal,
    threshold: Decimal,
    *,
    notice_suppression_reason: str | None = None,
    suspend_now: bool = False,
) -> str:
    """Apply the low-balance action already resolved by the policy owner."""
    result = "ok"
    just_armed = False
    if account.prepaid_low_balance_at is None:
        if not suspend_now:
            queued = _send_notice(
                db,
                account,
                cfg.warning_subject,
                cfg.warning_body,
                balance,
                threshold,
                suppression_reason=notice_suppression_reason,
            )
            if not queued:
                return "notice_blocked"
        just_armed = arm_prepaid_low_balance_timer(
            db,
            account.id,
            armed_at=now,
        )
        result = "ok" if suspend_now else "warned"
        logger.info(
            "prepaid_balance_sweep armed low-balance for account %s", account.id
        )

    # Non-zero configured grace arms and warns on the first observation. A
    # resolved zero-grace suspension deliberately continues in this locked unit.
    if account.prepaid_deactivation_at is not None or (just_armed and not suspend_now):
        return result

    # Arm the deactivation timer ONLY on a successful suspend. _suspend_account
    # fails-closed (returns False) for shielded / dedicated-bundle / canceled
    # accounts; if we armed the timer first, _reconcile_low would short-circuit
    # on the next run and never retry — leaving an armed-but-active account
    # showing a bogus deactivation date. Leaving the timer None makes the next
    # sweep re-attempt the suspension once the block clears.
    suspended = _suspend_account(
        db,
        str(account.id),
        reason=EnforcementReason.prepaid,
        source=_SOURCE,
    )
    if suspended:
        mark_prepaid_deactivated(db, account.id, deactivated_at=now)
        _send_notice(
            db,
            account,
            cfg.deactivation_subject,
            cfg.deactivation_body,
            balance,
            threshold,
            suppression_reason=notice_suppression_reason,
        )
        logger.info("prepaid_balance_sweep suspended account %s", account.id)
        return "suspended"
    return result


def _process_account(
    db: Session,
    account: Subscriber,
    now: datetime,
    cfg: PrepaidEnforcementPolicy,
    *,
    notice_suppression_reason: str | None,
) -> str:
    decision = plan_prepaid_account(
        db,
        account,
        now=now,
        policy=cfg,
        notice_suppression_reason=notice_suppression_reason,
    )
    if decision.action == PrepaidEnforcementAction.billing_profile_invalid:
        logger.warning(
            "prepaid_balance_sweep skipped account %s: %s",
            account.id,
            decision.reason,
        )
        return "billing_profile_invalid"
    if decision.action == PrepaidEnforcementAction.clear_stale_timers:
        _clear_prepaid_dunning_flags(db, str(account.id))
        return "restored"
    if decision.action == PrepaidEnforcementAction.restore:
        return _reconcile_funded(
            db,
            account,
            PrepaidFundingDecision(
                account_id=str(account.id),
                available_balance=decision.available_balance,
                required_balance=decision.required_balance,
                currency=decision.currency,
                covered_subscription_ids=decision.covered_subscription_ids,
                non_billable_subscription_ids=(decision.non_billable_subscription_ids),
                actionable_uncovered_subscription_ids=(
                    decision.actionable_uncovered_subscription_ids
                ),
                unresolved_projection_subscription_ids=(
                    decision.unresolved_projection_subscription_ids
                ),
            ),
        )
    if decision.action == PrepaidEnforcementAction.coverage_unresolved:
        logger.error(
            "prepaid_balance_sweep blocked adverse action for account %s: %s (%s)",
            account.id,
            decision.reason,
            ",".join(
                str(value) for value in decision.unresolved_projection_subscription_ids
            ),
        )
        return "coverage_unresolved"
    if decision.action == PrepaidEnforcementAction.deferred:
        return "deferred"
    if decision.action == PrepaidEnforcementAction.shielded:
        return "shielded"
    if decision.action == PrepaidEnforcementAction.state_drift:
        logger.warning(
            "prepaid_balance_sweep skipped account %s: enforcement state drift (%s)",
            account.id,
            decision.reason,
        )
        return "state_drift"
    if decision.action in {
        PrepaidEnforcementAction.warn,
        PrepaidEnforcementAction.suspend,
    }:
        return _reconcile_low(
            db,
            account,
            now,
            cfg,
            decision.available_balance,
            decision.required_balance,
            notice_suppression_reason=decision.notice_suppression_reason,
            suspend_now=decision.action == PrepaidEnforcementAction.suspend,
        )
    if decision.action == PrepaidEnforcementAction.not_applicable:
        return "ok"
    return "ok"


def run_prepaid_balance_sweep(
    db: Session, *, now: datetime | None = None
) -> dict[str, int | str]:
    """Reconcile every active prepaid account against its balance threshold.

    The lifecycle is permanently active. Commits per account so a single
    failure never aborts the batch; quarantine and evidence failures remain
    account-scoped.
    """
    run_at = now or datetime.now(UTC)
    cfg = resolve_prepaid_enforcement_policy(db)
    stats: dict[str, int | str] = {
        "accounts_scanned": 0,
        "warned": 0,
        "suspended": 0,
        "restored": 0,
        "deferred": 0,
        "shielded": 0,
        "billing_profile_invalid": 0,
        "coverage_unresolved": 0,
        "funding_quarantined": 0,
        "notice_blocked": 0,
        "state_drift": 0,
        "ok": 0,
        "errors": 0,
    }
    account_ids = candidate_prepaid_account_ids(db)
    from app.services.prepaid_funding_reconstruction import (
        prepaid_funding_quarantined_account_ids,
    )

    funding_candidate_ids = candidate_prepaid_funding_account_ids(db)
    quarantined_ids = prepaid_funding_quarantined_account_ids(
        db, set(account_ids) & funding_candidate_ids
    )
    enforceable_ids = set(account_ids) - quarantined_ids
    notice_reasons = prepaid_notice_suppression_reasons(db, enforceable_ids)
    stats["accounts_scanned"] = len(account_ids)
    stats["funding_quarantined"] = len(quarantined_ids)
    for account_id in enforceable_ids:
        try:
            account = db.execute(
                select(Subscriber)
                .where(Subscriber.id == coerce_uuid(str(account_id)))
                .with_for_update()
            ).scalar_one_or_none()
            if account is None:
                continue
            outcome = _process_account(
                db,
                account,
                run_at,
                cfg,
                notice_suppression_reason=notice_reasons.get(account.id),
            )
            db.commit()
            stats[outcome] = int(stats.get(outcome, 0)) + 1
        except Exception:
            db.rollback()
            stats["errors"] = int(stats["errors"]) + 1
            logger.exception(
                "prepaid_balance_sweep_account_failed",
                extra={"account_id": str(account_id)},
            )
    logger.info("prepaid_balance_sweep completed: %s", stats)
    return stats
