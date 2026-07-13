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
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models.catalog import BillingMode, Subscription, SubscriptionStatus
from app.models.domain_settings import SettingDomain
from app.models.enforcement_lock import EnforcementReason
from app.models.subscriber import Subscriber, SubscriberStatus
from app.services import control_registry, enforcement_window, settings_spec
from app.services.access_resolution import (
    PrepaidFundingDecision,
    resolve_prepaid_funding,
)
from app.services.billing_profile import resolve_billing_profile
from app.services.collections._core import (
    _clear_prepaid_dunning_flags,
    _effective_billing_mode_for_account,
    _get_account_email,
    _restore_prepaid_if_funded,
    _suspend_account,
)
from app.services.common import coerce_uuid

logger = logging.getLogger(__name__)

# Canonical control key (feature gate). Off by default (on_missing=False).
PREPAID_BALANCE_ENFORCEMENT_CONTROL = "collections.prepaid_balance_enforcement"

# Statuses that mean the account still has an operational relationship we care
# about: active/pending can be armed+suspended; suspended/blocked can be
# restored once funded again.
_RELEVANT_STATUSES = (
    SubscriptionStatus.active,
    SubscriptionStatus.pending,
    SubscriptionStatus.suspended,
    SubscriptionStatus.blocked,
)

_SOURCE = "prepaid_balance_sweep"


@dataclass(frozen=True)
class _SweepConfig:
    """Settings resolved once per run (all read from the ``collections`` domain)."""

    deactivation_days: int
    warning_subject: str
    warning_body: str
    deactivation_subject: str
    deactivation_body: str
    blocking_time: str | None
    skip_weekends: bool
    skip_holidays: list[str]


def prepaid_balance_enforcement_enabled(db: Session) -> bool:
    """Whether the prepaid balance sweep is armed (default OFF)."""
    return control_registry.is_enabled(db, PREPAID_BALANCE_ENFORCEMENT_CONTROL)


def _resolve_config(db: Session) -> _SweepConfig:
    def _s(key: str) -> str:
        value = settings_spec.resolve_value(db, SettingDomain.collections, key)
        return str(value) if value is not None else ""

    days_raw = settings_spec.resolve_value(
        db, SettingDomain.collections, "prepaid_deactivation_days"
    )
    try:
        deactivation_days = max(0, int(str(days_raw))) if days_raw is not None else 0
    except (TypeError, ValueError):
        deactivation_days = 0

    skip_weekends = bool(
        settings_spec.resolve_value(
            db, SettingDomain.collections, "prepaid_skip_weekends"
        )
    )
    skip_holidays_raw = settings_spec.resolve_value(
        db, SettingDomain.collections, "prepaid_skip_holidays"
    )
    skip_holidays = (
        [str(d) for d in skip_holidays_raw]
        if isinstance(skip_holidays_raw, list)
        else []
    )
    blocking_time = (
        settings_spec.resolve_value(
            db, SettingDomain.collections, "prepaid_blocking_time"
        )
        or None
    )
    return _SweepConfig(
        deactivation_days=deactivation_days,
        warning_subject=_s("prepaid_warning_subject"),
        warning_body=_s("prepaid_warning_body"),
        deactivation_subject=_s("prepaid_deactivation_subject"),
        deactivation_body=_s("prepaid_deactivation_body"),
        blocking_time=str(blocking_time) if blocking_time is not None else None,
        skip_weekends=skip_weekends,
        skip_holidays=skip_holidays,
    )


def _candidate_account_ids(db: Session) -> set:
    """Prepaid accounts to reconcile: those with an operationally-relevant
    subscription, plus any still carrying prepaid timers (to clear stale ones)."""
    ids: set = {
        row[0]
        for row in (
            db.query(Subscriber.id)
            .join(Subscription, Subscription.subscriber_id == Subscriber.id)
            .filter(
                or_(
                    Subscriber.billing_mode == BillingMode.prepaid,
                    Subscription.billing_mode == BillingMode.prepaid,
                )
            )
            .filter(Subscription.status.in_(_RELEVANT_STATUSES))
            .distinct()
            .all()
        )
    }
    ids.update(
        row[0]
        for row in (
            db.query(Subscriber.id)
            .filter(Subscriber.billing_mode == BillingMode.prepaid)
            .filter(
                or_(
                    Subscriber.prepaid_low_balance_at.is_not(None),
                    Subscriber.prepaid_deactivation_at.is_not(None),
                )
            )
            .all()
        )
    )
    return ids


def _send_notice(
    db: Session,
    account: Subscriber,
    subject: str,
    body: str,
    balance: Decimal,
    threshold: Decimal,
) -> None:
    """Queue a customer email using the operator-configured subject/body.

    Reuses the same simple ``Notification`` (channel=email, status=queued)
    mechanism the dunning throttle/suspension notices use — the notification
    queue runner owns delivery. ``{balance}`` / ``{threshold}`` placeholders in
    the body are filled; malformed templates fall back to the raw text.
    """
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


def _deactivation_deferred(db: Session, now: datetime, cfg: _SweepConfig) -> bool:
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
        skip_holidays=cfg.skip_holidays,
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
    cfg: _SweepConfig,
    balance: Decimal,
    threshold: Decimal,
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
        )
        logger.info("prepaid_balance_sweep suspended account %s", account.id)
        return "suspended"
    return result


def _process_account(
    db: Session, account: Subscriber, now: datetime, cfg: _SweepConfig
) -> str:
    # Serialize the funding decision and any restore/suspend mutation against
    # payment-driven access reconciliation, which locks the same account row.
    account = db.execute(
        select(Subscriber).where(Subscriber.id == account.id).with_for_update()
    ).scalar_one()
    profile = resolve_billing_profile(db, account)
    if not profile.automation_safe and profile.has_collectible_subscriptions:
        logger.warning(
            "prepaid_balance_sweep skipped account %s: billing profile source=%s "
            "invalid_reason=%s account=%s subscription_modes=%s",
            account.id,
            profile.source,
            profile.invalid_reason,
            profile.account_mode.value if profile.account_mode else None,
            sorted(mode.value for mode in profile.subscription_modes),
        )
        return "billing_profile_invalid"
    if _effective_billing_mode_for_account(db, account) != BillingMode.prepaid:
        if (
            account.prepaid_low_balance_at is not None
            or account.prepaid_deactivation_at is not None
        ):
            _clear_prepaid_dunning_flags(db, str(account.id))
            return "restored"
        return "ok"

    funding = resolve_prepaid_funding(db, account, now=now)
    if funding.funded:
        return _reconcile_funded(db, account, funding)
    return _reconcile_low(
        db,
        account,
        now,
        cfg,
        funding.available_balance,
        funding.required_balance,
    )


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
    cfg = _resolve_config(db)
    stats: dict[str, int | str] = {
        "accounts_scanned": 0,
        "warned": 0,
        "suspended": 0,
        "restored": 0,
        "deferred": 0,
        "ok": 0,
        "errors": 0,
    }
    account_ids = _candidate_account_ids(db)
    stats["accounts_scanned"] = len(account_ids)
    for account_id in account_ids:
        try:
            account = db.get(Subscriber, coerce_uuid(str(account_id)))
            if account is None or account.status == SubscriberStatus.canceled:
                continue
            outcome = _process_account(db, account, run_at, cfg)
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
