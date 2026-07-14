"""Prepaid balance/expiry enforcement sweep (Item 2 of the prepaid alignment).

Deposit-is-truth prepaid customers are invoiced in advance; the read-side
(``service_status``) already *projects* low-balance / grace / deactivation
dates, but nothing armed the timers or acted on them. This periodic sweep is
the single writer that arms ``prepaid_low_balance_at`` /
``prepaid_deactivation_at``, warns the customer, and eventually suspends via the
lifecycle state machine — then clears/restores once the account is funded again.

SAFETY: this SUSPENDS customers. It is gated OFF by default behind the
``collections.prepaid_balance_enforcement`` control (legacy key
``collections.prepaid_balance_enforcement_enabled``). When the control is off the
sweep is a complete no-op — no timers armed, no notices, no suspensions. Every
account is processed in its own committed unit so one bad row cannot abort the
batch.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.enforcement_lock import EnforcementReason
from app.models.subscriber import Subscriber, SubscriberStatus
from app.services import enforcement_window
from app.services.access_resolution import PrepaidFundingDecision
from app.services.billing_enforcement_guards import (
    EnforcementHealth,
    billing_enforcement_health,
)
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
    plan_prepaid_account,
    prepaid_balance_enforcement_enabled,
    prepaid_notice_suppression_reasons,
    resolve_prepaid_enforcement_policy,
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
) -> None:
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
        return

    from app.models.notification import NotificationChannel
    from app.schemas.notification import NotificationCreate
    from app.services.notification import notifications as notifications_svc

    email = _get_account_email(db, str(account.id))
    if not email:
        logger.warning(
            "prepaid_balance_sweep notice skipped for %s: no email", account.id
        )
        return
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


def _deactivation_deferred(
    db: Session, now: datetime, cfg: PrepaidEnforcementPolicy
) -> bool:
    """Whether the DEACTIVATION step must wait (skip-day / outside window).

    Only gates the state-changing suspension — never the warning. Reuses the
    shared wall-clock decision helper: ``prepaid_blocking_time`` is the "act at/
    after" gate, plus weekend/holiday skips. ``prepaid_skip_holidays`` is the
    configured list of ISO dates (the calendar is the setting itself; no
    external holiday source is invented).
    """
    local_now = enforcement_window.to_local(db, now)
    reason = enforcement_window.window_block_reason(
        local_now,
        start_time=enforcement_window.parse_time(cfg.blocking_time),
        skip_weekends=cfg.skip_weekends,
        skip_holidays=list(cfg.skip_holidays),
    )
    if reason is not None:
        logger.info(
            "prepaid_balance_sweep deactivation deferred (%s)",
            reason,
        )
    return reason is not None


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
) -> str:
    """Balance below threshold: arm/warn, then (later) arm-deactivate/suspend."""
    result = "ok"
    just_armed = False
    if account.prepaid_low_balance_at is None:
        account.prepaid_low_balance_at = now
        db.flush()
        _send_notice(
            db,
            account,
            cfg.warning_subject,
            cfg.warning_body,
            balance,
            threshold,
            suppression_reason=notice_suppression_reason,
        )
        just_armed = True
        result = "warned"
        logger.info(
            "prepaid_balance_sweep armed low-balance for account %s", account.id
        )

    # Already deactivated, or only just armed this run → nothing more to do.
    if account.prepaid_deactivation_at is not None or just_armed:
        return result

    # The stored timestamp is tz-aware on Postgres but tz-naive when read back
    # on SQLite; normalise to UTC so the window comparison never crosses an
    # aware/naive boundary.
    low_at = account.prepaid_low_balance_at
    if low_at.tzinfo is None:
        low_at = low_at.replace(tzinfo=UTC)
    if cfg.activation_at is not None and low_at < cfg.activation_at:
        low_at = cfg.activation_at
    due_at = low_at + timedelta(days=cfg.deactivation_days)
    if now < due_at:
        return result

    if _deactivation_deferred(db, now, cfg):
        return "deferred"

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
        account.prepaid_deactivation_at = now
        db.flush()
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
    enforcement_health: EnforcementHealth,
    notice_suppression_reason: str | None,
) -> str:
    decision = plan_prepaid_account(
        db,
        account,
        now=now,
        policy=cfg,
        enforcement_health=enforcement_health,
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
            ),
        )
    if decision.action == PrepaidEnforcementAction.deferred:
        return "deferred"
    if decision.action == PrepaidEnforcementAction.shielded:
        return "shielded"
    if decision.action == PrepaidEnforcementAction.health_blocked:
        return "health_blocked"
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
        if cfg.activation_error is not None:
            logger.warning(
                "prepaid_balance_sweep adverse action blocked for account %s: %s",
                account.id,
                cfg.activation_error,
            )
            return "activation_blocked"
        if cfg.activation_at is not None and now < cfg.activation_at:
            logger.info(
                "prepaid_balance_sweep adverse action blocked for account %s: "
                "activation time not reached",
                account.id,
            )
            return "activation_blocked"
        return _reconcile_low(
            db,
            account,
            now,
            cfg,
            decision.available_balance,
            decision.required_balance,
            notice_suppression_reason=decision.notice_suppression_reason,
        )
    if decision.action == PrepaidEnforcementAction.not_applicable:
        return "ok"
    return "ok"


def run_prepaid_balance_sweep(
    db: Session, *, now: datetime | None = None
) -> dict[str, int | str]:
    """Reconcile every active prepaid account against its balance threshold.

    No-op (``{"skipped": "disabled"}``) unless the enforcement control is on.
    Commits per account so a single failure never aborts the batch.
    """
    if not prepaid_balance_enforcement_enabled(db):
        logger.info("prepaid_balance_sweep skipped: enforcement disabled")
        return {"skipped": "disabled"}

    run_at = now or datetime.now(UTC)
    cfg = resolve_prepaid_enforcement_policy(db)
    health = billing_enforcement_health(db)
    stats: dict[str, int | str] = {
        "accounts_scanned": 0,
        "warned": 0,
        "suspended": 0,
        "restored": 0,
        "deferred": 0,
        "shielded": 0,
        "health_blocked": 0,
        "activation_blocked": 0,
        "state_drift": 0,
        "ok": 0,
        "errors": 0,
    }
    account_ids = candidate_prepaid_account_ids(db)
    notice_reasons = prepaid_notice_suppression_reasons(db, account_ids)
    stats["accounts_scanned"] = len(account_ids)
    for account_id in account_ids:
        try:
            account = db.execute(
                select(Subscriber)
                .where(Subscriber.id == coerce_uuid(str(account_id)))
                .with_for_update()
            ).scalar_one_or_none()
            if account is None or account.status == SubscriberStatus.canceled:
                continue
            outcome = _process_account(
                db,
                account,
                run_at,
                cfg,
                enforcement_health=health,
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
