"""Billing liveness / anomaly signals for monitoring (metrics + alerts).

These are MONITORING checks — surfaced as Prometheus gauges and operator
alerts. They are deliberately NOT enforcement gates: they never block a
suspension (that is ``billing_enforcement_guards``). They answer "is the billing
system producing correct, complete output?", not "is it safe to cut this
customer now?".

Signals:
* paid-with-balance — invoices marked ``paid`` that still carry a non-zero
  ``balance_due`` (AR-integrity defect; dashboards/restore logic trust ``paid``).
* invoice-scan coverage — the latest billing run's ``subscriptions_scanned`` vs
  the count of active subscriptions; a sharp drop means the cycle silently
  stopped scanning a cohort (the 4,041->108 incident).
* payment-volume — last-24h succeeded payments vs the trailing-7-day daily
  average; a collapse means intake broke (the Splynx-cutover recording gap),
  using a REAL baseline rather than a static floor.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import bindparam, func, select, text
from sqlalchemy.orm import Session

from app.models.billing import (
    BillingRun,
    Invoice,
    InvoiceStatus,
    Payment,
    PaymentStatus,
)
from app.models.catalog import (
    BillingMode,
    Subscription,
)
from app.models.domain_settings import SettingDomain
from app.models.scheduler import ScheduledTask
from app.models.subscriber import Subscriber
from app.services import settings_spec
from app.services.billing_settings import COLLECTIBLE_SERVICE_STATUSES
from app.services.billing_statuses import (
    BILLABLE_SUBSCRIBER_STATUS_VALUES,
    BILLABLE_SUBSCRIBER_STATUSES,
)
from app.services.customer_financial_position import prepaid_available_balance
from app.services.customer_service_state import (
    postpaid_invoice_eligible_filters,
    prepaid_enforcement_eligible_filters,
)
from app.services.job_heartbeat import get_last_result, get_last_success

# Alert thresholds. Conservative defaults; tune via ops experience.
SCAN_MIN_RATIO = 0.5  # alert if a run scanned < 50% of active subs
PAYMENT_VOLUME_MIN_RATIO = 0.4  # alert if last-24h volume < 40% of 7d daily avg
# Don't cry "collapse" on naturally low-traffic systems: require a real baseline.
PAYMENT_BASELINE_MIN_DAILY = 5.0

# A runner is "stale" if it has not succeeded within interval x this multiplier.
HEARTBEAT_STALE_MULTIPLIER = 3.0
# Critical billing/collections runners whose silence is a revenue/enforcement
# risk. Only the ENABLED ones are judged (a disabled runner is intentional).
_CRITICAL_RUNNERS = (
    "app.tasks.billing.run_invoice_cycle",
    "app.tasks.collections.run_billing_enforcement",
    "app.tasks.billing.mark_invoices_overdue",
    "app.tasks.billing.check_billing_switch",
)


@dataclass(frozen=True)
class RunnerHeartbeat:
    task_name: str
    enabled: bool
    interval_seconds: int | None
    last_success: datetime | None
    age_seconds: float | None
    stale: bool
    # Last-run result blob {status, at, detail} from job_heartbeat.get_last_result,
    # or None when unknown (never recorded / Redis down). Optional so older
    # positional call sites and tests stay valid.
    last_result: dict | None = None

    @property
    def last_result_status(self) -> str | None:
        """ "ok" / "error" / None (unknown)."""
        if not isinstance(self.last_result, dict):
            return None
        status = self.last_result.get("status")
        return status if isinstance(status, str) else None

    @property
    def last_result_at(self) -> datetime | None:
        """Timestamp of the last recorded result, or None. Never raises."""
        if not isinstance(self.last_result, dict):
            return None
        raw = self.last_result.get("at")
        if not isinstance(raw, str):
            return None
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return None
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed

    @property
    def last_result_detail(self) -> dict | None:
        """Returned counts (success) or {"error": msg} (failure), or None."""
        if not isinstance(self.last_result, dict):
            return None
        detail = self.last_result.get("detail")
        return detail if isinstance(detail, dict) else None

    @property
    def last_result_summary(self) -> str:
        """Compact one-line rendering of the last-run result for display."""
        status = self.last_result_status
        if status is None:
            return "No result yet"
        detail = self.last_result_detail or {}
        if status == "error":
            msg = detail.get("error")
            return f"errored: {msg}" if msg else "errored"
        if not detail:
            return "ok"
        counts = ", ".join(f"{k}={v}" for k, v in detail.items())
        return f"ok — {counts}" if counts else "ok"


@dataclass(frozen=True)
class BillingHealthSnapshot:
    paid_with_balance_count: int
    paid_with_balance_total: Decimal
    last_scanned: int | None
    eligible_active_subs: int
    scan_ratio: float | None
    payments_24h: int
    payments_7d_daily_avg: float
    payment_volume_ratio: float | None
    payment_volume_collapsed: bool
    # §6.3 runner heartbeats / §6.6 enforcement drift (defaults keep older
    # call sites and tests valid).
    runners: tuple[RunnerHeartbeat, ...] = ()
    covered_but_locked: int = 0
    # §6.1 billing-path coverage.
    unbilled_no_path: int = 0
    active_subs_on_terminal_account: int = 0
    negative_prepaid_balance_count: int = 0
    negative_prepaid_balance_total: Decimal = Decimal("0.00")
    prepaid_balance_sweep_enabled: bool = False
    negative_prepaid_with_sweep_disabled_count: int = 0
    billing_profile_mismatch_count: int = 0
    billing_profile_mixed_count: int = 0
    scan_min_ratio: float = SCAN_MIN_RATIO
    payment_volume_min_ratio: float = PAYMENT_VOLUME_MIN_RATIO
    payment_baseline_min_daily: float = PAYMENT_BASELINE_MIN_DAILY

    @property
    def stale_runners(self) -> list[str]:
        return [r.task_name for r in self.runners if r.stale]

    @property
    def anomalies(self) -> list[str]:
        out: list[str] = []
        if self.paid_with_balance_count > 0:
            out.append("paid_invoices_with_balance")
        if self.scan_ratio is not None and self.scan_ratio < self.scan_min_ratio:
            out.append("invoice_scan_count_low")
        if self.payment_volume_collapsed:
            out.append("payment_volume_collapse")
        if self.stale_runners:
            out.append("runner_heartbeat_stale")
        if self.covered_but_locked > 0:
            out.append("enforcement_covered_but_locked")
        if self.unbilled_no_path > 0:
            out.append("active_subs_without_billing_path")
        if self.negative_prepaid_balance_count > 0:
            out.append("negative_prepaid_balances")
        if self.negative_prepaid_with_sweep_disabled_count > 0:
            out.append("negative_prepaid_sweep_disabled")
        if self.billing_profile_mismatch_count > 0:
            out.append("billing_profile_mismatch")
        if self.billing_profile_mixed_count > 0:
            out.append("billing_profile_mixed_modes")
        return out


def paid_with_balance(db: Session) -> tuple[int, Decimal]:
    """Count + sum of invoices that are ``paid`` yet retain a balance_due."""
    count_, total = db.execute(
        select(
            func.count(Invoice.id),
            func.coalesce(func.sum(Invoice.balance_due), 0),
        )
        .where(Invoice.status == InvoiceStatus.paid)
        .where(Invoice.balance_due != 0)
    ).one()
    return int(count_ or 0), Decimal(str(total or 0))


def _resolve_float_setting(
    db: Session,
    key: str,
    default: float,
    *,
    minimum: float,
    maximum: float,
) -> float:
    value = settings_spec.resolve_value(db, SettingDomain.billing, key)
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def _health_thresholds(db: Session) -> tuple[float, float, float]:
    scan_min_ratio = _resolve_float_setting(
        db,
        "billing_health_scan_min_ratio",
        SCAN_MIN_RATIO,
        minimum=0.0,
        maximum=1.0,
    )
    payment_volume_min_ratio = _resolve_float_setting(
        db,
        "billing_health_payment_volume_min_ratio",
        PAYMENT_VOLUME_MIN_RATIO,
        minimum=0.0,
        maximum=1.0,
    )
    payment_baseline_min_daily = _resolve_float_setting(
        db,
        "billing_health_payment_baseline_min_daily",
        PAYMENT_BASELINE_MIN_DAILY,
        minimum=0.0,
        maximum=100000.0,
    )
    return scan_min_ratio, payment_volume_min_ratio, payment_baseline_min_daily


def invoice_scan_coverage(db: Session) -> tuple[int | None, int, float | None]:
    """(last_run_scanned, eligible_active_subs, ratio)."""
    last_scanned = db.execute(
        select(BillingRun.subscriptions_scanned)
        .order_by(BillingRun.created_at.desc())
        .limit(1)
    ).scalar()
    # Mirror run_invoice_cycle's eligibility: ACTIVE subscriptions whose account
    # is in a billable state and that are not prepaid. Counting only
    # SubscriptionStatus.active (with no subscriber/billing_mode filter) both
    # missed blocked/suspended/delinquent accounts that ARE billed and counted
    # prepaid subs that are NOT — skewing the coverage ratio.
    eligible = (
        db.execute(
            select(func.count(Subscription.id))
            .join(Subscriber, Subscriber.id == Subscription.subscriber_id)
            .where(*postpaid_invoice_eligible_filters(Subscription, Subscriber))
        ).scalar()
        or 0
    )
    ratio = (
        float(last_scanned) / float(eligible)
        if last_scanned is not None and eligible > 0
        else None
    )
    return (
        int(last_scanned) if last_scanned is not None else None,
        int(eligible),
        ratio,
    )


def payment_volume(
    db: Session, now: datetime | None = None
) -> tuple[int, float, float | None, bool]:
    """(count_24h, daily_avg_prev_7d, ratio, collapsed) for succeeded payments."""
    now = now or datetime.now(UTC)
    _, payment_volume_min_ratio, payment_baseline_min_daily = _health_thresholds(db)
    last_24h = now - timedelta(hours=24)
    baseline_start = now - timedelta(days=8)  # 7-day window ending 24h ago

    count_24h = (
        db.execute(
            select(func.count(Payment.id))
            .where(Payment.status == PaymentStatus.succeeded)
            .where(Payment.paid_at.is_not(None))
            .where(Payment.paid_at >= last_24h)
        ).scalar()
        or 0
    )
    count_prev_7d = (
        db.execute(
            select(func.count(Payment.id))
            .where(Payment.status == PaymentStatus.succeeded)
            .where(Payment.paid_at.is_not(None))
            .where(Payment.paid_at >= baseline_start)
            .where(Payment.paid_at < last_24h)
        ).scalar()
        or 0
    )
    daily_avg = float(count_prev_7d) / 7.0
    ratio = float(count_24h) / daily_avg if daily_avg > 0 else None
    collapsed = (
        daily_avg >= payment_baseline_min_daily
        and ratio is not None
        and ratio < payment_volume_min_ratio
    )
    return int(count_24h), daily_avg, ratio, collapsed


def runner_heartbeats(
    db: Session, now: datetime | None = None
) -> list[RunnerHeartbeat]:
    """Freshness of critical runners' last SUCCESS (Redis heartbeat).

    Only ENABLED runners are judged stale (a disabled runner is intentional).
    A runner that has never succeeded while enabled is stale (covers a freshly
    armed-but-dead consumer); it self-clears after the first success.
    """
    now = now or datetime.now(UTC)
    out: list[RunnerHeartbeat] = []
    for task_name in _CRITICAL_RUNNERS:
        row = db.execute(
            select(ScheduledTask.enabled, ScheduledTask.interval_seconds)
            .where(ScheduledTask.task_name == task_name)
            .limit(1)
        ).first()
        enabled = bool(row[0]) if row else False
        interval = int(row[1]) if row and row[1] else None
        last_result = get_last_result(task_name)
        if not enabled:
            out.append(
                RunnerHeartbeat(
                    task_name, False, interval, None, None, False, last_result
                )
            )
            continue
        last = get_last_success(task_name)
        if last is not None and last.tzinfo is None:
            last = last.replace(tzinfo=UTC)
        age = (now - last).total_seconds() if last else None
        if interval:
            stale = last is None or (
                age is not None and age > interval * HEARTBEAT_STALE_MULTIPLIER
            )
        else:
            stale = last is None
        out.append(
            RunnerHeartbeat(task_name, True, interval, last, age, stale, last_result)
        )
    return out


def _default_currency(db: Session) -> str:
    """Billing default currency setting (NGN when unset)."""
    value = settings_spec.resolve_value(db, SettingDomain.billing, "default_currency")
    code = str(value or "NGN").strip().upper()
    return code or "NGN"


def covered_but_locked(db: Session) -> int:
    """§6.6 drift: accounts still under a billing lock (overdue/prepaid) whose
    local ledger available balance is >= 0 — i.e. covered yet suspended
    (wrongful-suspension drift). Mirrors get_available_balance for the default
    currency: unallocated credit - unallocated debit - open invoice balance.
    """
    sql = text(
        """
        WITH locked AS (
            SELECT DISTINCT s.subscriber_id AS acct
            FROM enforcement_locks el
            JOIN subscriptions s ON s.id = el.subscription_id
            WHERE el.is_active AND el.reason IN ('overdue', 'prepaid')
        )
        SELECT count(*) FROM locked WHERE (
            COALESCE((SELECT sum(le.amount) FROM ledger_entries le
                WHERE le.account_id = acct AND le.invoice_id IS NULL
                  AND le.entry_type = 'credit' AND le.is_active
                  AND le.currency = :currency), 0)
          - COALESCE((SELECT sum(le.amount) FROM ledger_entries le
                WHERE le.account_id = acct AND le.invoice_id IS NULL
                  AND le.entry_type = 'debit' AND le.is_active
                  AND le.currency = :currency), 0)
          - COALESCE((SELECT sum(i.balance_due) FROM invoices i
                WHERE i.account_id = acct AND i.balance_due > 0
                  AND i.status IN ('issued', 'partially_paid', 'overdue')
                  AND i.currency = :currency), 0)
        ) >= 0
        """
    )
    return int(db.execute(sql, {"currency": _default_currency(db)}).scalar() or 0)


def _prepaid_monthly_enabled(db: Session) -> bool:
    """Same resolution as billing_automation's invoice cycle."""
    value = settings_spec.resolve_value(
        db, SettingDomain.billing, "prepaid_monthly_invoicing_enabled"
    )
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def billing_path_coverage(db: Session) -> tuple[int, int]:
    """§6.1: (unbilled_no_path, active_subs_on_terminal_account).

    Mirrors run_invoice_cycle's invoice-row selection. A billable-account active
    sub is covered iff it is postpaid, or (prepaid_monthly enabled AND its offer
    is a monthly cycle). Prepaid coverage means draft-until-funded accounting
    rows, not AR/dunning. ``unbilled_no_path`` is the scalable billing-visibility
    gap — a prepaid cohort that no enabled path records (flag off, or a
    non-monthly prepaid offer). ``active_subs_on_terminal_account`` is an active
    sub whose account is non-billable, so the cycle never touches it (lifecycle
    drift, low volume).
    """
    # Static SQL — the status set is a fixed constant from billing_statuses,
    # never user input, so this is not an injection surface.
    terminal = (
        db.execute(
            text(
                """
            SELECT count(*) FROM subscriptions sub
            JOIN subscribers s ON s.id = sub.subscriber_id
            WHERE sub.status = 'active'
              AND s.status NOT IN :billable_statuses
            """
            ).bindparams(bindparam("billable_statuses", expanding=True)),
            {"billable_statuses": BILLABLE_SUBSCRIBER_STATUS_VALUES},
        ).scalar()
        or 0
    )

    if _prepaid_monthly_enabled(db):
        no_path_sql = """
            SELECT count(*) FROM subscriptions sub
            JOIN subscribers s ON s.id = sub.subscriber_id
            JOIN catalog_offers o ON o.id = sub.offer_id
            WHERE sub.status = 'active'
              AND s.status IN :billable_statuses
              AND sub.billing_mode = 'prepaid' AND o.billing_cycle <> 'monthly'
        """
    else:
        no_path_sql = """
            SELECT count(*) FROM subscriptions sub
            JOIN subscribers s ON s.id = sub.subscriber_id
            WHERE sub.status = 'active'
              AND s.status IN :billable_statuses
              AND sub.billing_mode = 'prepaid'
        """
    no_path = (
        db.execute(
            text(no_path_sql).bindparams(
                bindparam("billable_statuses", expanding=True)
            ),
            {"billable_statuses": BILLABLE_SUBSCRIBER_STATUS_VALUES},
        ).scalar()
        or 0
    )
    return int(no_path), int(terminal)


def negative_prepaid_balance_exposure(db: Session) -> tuple[int, Decimal, bool, int]:
    """Negative prepaid wallet exposure using the same balance as enforcement.

    Returns ``(negative_count, negative_total_abs, sweep_enabled,
    negative_count_if_sweep_disabled)``. This is a monitoring signal only; the
    prepaid sweep owns any warning/suspension action after an operator enables
    it.
    """
    from app.services import control_registry

    account_ids = (
        db.execute(
            select(Subscriber.id)
            .join(Subscription, Subscription.subscriber_id == Subscriber.id)
            .where(*prepaid_enforcement_eligible_filters(Subscription, Subscriber))
            .distinct()
        )
        .scalars()
        .all()
    )
    count = 0
    total = Decimal("0.00")
    for account_id in account_ids:
        balance = prepaid_available_balance(db, account_id)
        if balance < Decimal("0.00"):
            count += 1
            total += abs(balance)
    sweep_enabled = control_registry.is_enabled(
        db, "collections.prepaid_balance_enforcement"
    )
    return count, total, sweep_enabled, count if not sweep_enabled else 0


def billing_profile_integrity(db: Session) -> tuple[int, int]:
    """Return (account/subscription mismatch count, mixed subscription count)."""
    rows = db.execute(
        select(
            Subscriber.id,
            Subscriber.billing_mode,
            Subscription.billing_mode,
        )
        .join(Subscription, Subscription.subscriber_id == Subscriber.id)
        .where(Subscription.status.in_(COLLECTIBLE_SERVICE_STATUSES))
        .where(Subscriber.status.in_(BILLABLE_SUBSCRIBER_STATUSES))
    ).all()
    account_modes: dict[object, BillingMode | None] = {}
    subscription_modes: dict[object, set[BillingMode]] = {}
    for account_id, account_mode, subscription_mode in rows:
        account_modes[account_id] = account_mode
        if subscription_mode is not None:
            subscription_modes.setdefault(account_id, set()).add(subscription_mode)

    mismatch = 0
    mixed = 0
    for account_id, modes in subscription_modes.items():
        if len(modes) > 1:
            mixed += 1
            continue
        only_mode = next(iter(modes))
        if (
            account_modes.get(account_id) is not None
            and account_modes[account_id] != only_mode
        ):
            mismatch += 1
    return mismatch, mixed


def billing_health_snapshot(
    db: Session, now: datetime | None = None
) -> BillingHealthSnapshot:
    scan_min_ratio, payment_volume_min_ratio, payment_baseline_min_daily = (
        _health_thresholds(db)
    )
    pwb_count, pwb_total = paid_with_balance(db)
    last_scanned, eligible, scan_ratio = invoice_scan_coverage(db)
    c24, avg7, ratio, collapsed = payment_volume(db, now=now)
    no_path, terminal = billing_path_coverage(db)
    (
        negative_prepaid_count,
        negative_prepaid_total,
        prepaid_sweep_enabled,
        negative_prepaid_sweep_disabled,
    ) = negative_prepaid_balance_exposure(db)
    profile_mismatch_count, profile_mixed_count = billing_profile_integrity(db)
    return BillingHealthSnapshot(
        paid_with_balance_count=pwb_count,
        paid_with_balance_total=pwb_total,
        last_scanned=last_scanned,
        eligible_active_subs=eligible,
        scan_ratio=scan_ratio,
        payments_24h=c24,
        payments_7d_daily_avg=avg7,
        payment_volume_ratio=ratio,
        payment_volume_collapsed=collapsed,
        runners=tuple(runner_heartbeats(db, now=now)),
        covered_but_locked=covered_but_locked(db),
        unbilled_no_path=no_path,
        active_subs_on_terminal_account=terminal,
        negative_prepaid_balance_count=negative_prepaid_count,
        negative_prepaid_balance_total=negative_prepaid_total,
        prepaid_balance_sweep_enabled=prepaid_sweep_enabled,
        negative_prepaid_with_sweep_disabled_count=negative_prepaid_sweep_disabled,
        billing_profile_mismatch_count=profile_mismatch_count,
        billing_profile_mixed_count=profile_mixed_count,
        scan_min_ratio=scan_min_ratio,
        payment_volume_min_ratio=payment_volume_min_ratio,
        payment_baseline_min_daily=payment_baseline_min_daily,
    )
