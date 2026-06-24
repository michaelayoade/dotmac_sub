from __future__ import annotations

import logging
from calendar import monthrange
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import ObjectDeletedError

from app.models.billing import (
    BillingRun,
    BillingRunStatus,
    Invoice,
    InvoiceLine,
    InvoiceStatus,
    TaxApplication,
    TaxRate,
)
from app.models.catalog import (
    AddOn,
    AddOnPrice,
    BillingCycle,
    BillingMode,
    CatalogOffer,
    DiscountType,
    OfferPrice,
    OfferVersionPrice,
    PriceType,
    Subscription,
    SubscriptionAddOn,
    SubscriptionStatus,
)
from app.models.domain_settings import SettingDomain
from app.models.subscriber import Address, Subscriber, SubscriberStatus
from app.services import enforcement_window, settings_spec
from app.services.billing import _recalculate_invoice_totals
from app.services.billing.invoices import next_invoice_number
from app.services.billing.reconcile_unposted import settle_open_invoices_from_credit
from app.services.billing_settings import (
    accounts_with_live_service,
    resolve_payment_due_days,
)
from app.services.common import coerce_uuid, round_money
from app.services.events import emit_event
from app.services.events.types import EventType

logger = logging.getLogger(__name__)


def _billing_run_extra(
    *,
    run_uuid: object | None,
    run_at: datetime,
    billing_cycle: BillingCycle | None,
    dry_run: bool,
    include_pending: bool,
    auto_activate_pending: bool,
    summary: dict[str, Any] | None = None,
    error: str | None = None,
    attempt: int | None = None,
    max_retries: int | None = None,
) -> dict[str, object]:
    extra: dict[str, object] = {
        "event": "billing_run",
        "billing_run_id": str(run_uuid) if run_uuid else None,
        "run_at": run_at.isoformat(),
        "billing_cycle": billing_cycle.value if billing_cycle else None,
        "dry_run": dry_run,
        "include_pending": include_pending,
        "auto_activate_pending": auto_activate_pending,
    }
    if summary is not None:
        for key in (
            "subscriptions_scanned",
            "subscriptions_billed",
            "invoices_created",
            "lines_created",
            "skipped",
            "currency_skipped",
            "pending_activated",
            "invoice_reminders_sent",
            "dunning_escalations_sent",
            "credit_applied",
            "credit_settled_invoices",
            "accounts_restored",
            "run_id",
        ):
            if key in summary:
                value = summary[key]
                extra[key] = str(value) if isinstance(value, Decimal) else value
    if error is not None:
        extra["error"] = error
    if attempt is not None:
        extra["attempt"] = attempt
    if max_retries is not None:
        extra["max_retries"] = max_retries
    return extra


def _setting_truthy(db: Session, key: str, *, default: bool) -> bool:
    value = settings_spec.resolve_value(db, SettingDomain.billing, key)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _coerce_int_setting(value: object) -> int | None:
    # settings_spec.resolve_value() returns object | None.
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            return int(s)
        except ValueError:
            return None
    return None


def _parse_day_offsets(value: object) -> list[int]:
    if value is None:
        return []
    if isinstance(value, int):
        return [value] if value >= 0 else []
    if isinstance(value, str):
        offsets: set[int] = set()
        for raw_part in value.split(","):
            part = raw_part.strip()
            if not part:
                continue
            try:
                parsed = int(part)
            except ValueError:
                continue
            if parsed >= 0:
                offsets.add(parsed)
        return sorted(offsets, reverse=True)
    return []


def _add_months(value: datetime, months: int) -> datetime:
    total = value.month - 1 + months
    year = value.year + total // 12
    month = total % 12 + 1
    day = min(value.day, monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _period_end(start: datetime, cycle: BillingCycle) -> datetime:
    """Calculate the end of a billing period based on the cycle.

    Args:
        start: The period start date
        cycle: The billing cycle (daily, weekly, monthly, annual)

    Returns:
        The period end date
    """
    if cycle == BillingCycle.daily:
        return start + timedelta(days=1)
    if cycle == BillingCycle.weekly:
        return start + timedelta(weeks=1)
    if cycle == BillingCycle.monthly:
        return _add_months(start, 1)
    if cycle == BillingCycle.annual:
        return _add_months(start, 12)
    # Default to monthly
    return _add_months(start, 1)


def _resolve_price(db: Session, subscription: Subscription):
    if subscription.offer_version_id:
        version_prices = (
            db.query(OfferVersionPrice)
            .filter(OfferVersionPrice.offer_version_id == subscription.offer_version_id)
            .filter(OfferVersionPrice.price_type == PriceType.recurring)
            .filter(OfferVersionPrice.is_active.is_(True))
            .order_by(OfferVersionPrice.created_at.desc(), OfferVersionPrice.id.desc())
            .limit(2)
            .all()
        )
        if len(version_prices) > 1:
            logger.warning(
                "Multiple active recurring offer-version prices for subscription %s "
                "(offer_version %s); using newest",
                subscription.id,
                subscription.offer_version_id,
            )
        if version_prices:
            version_price = version_prices[0]
            return (
                version_price.amount,
                version_price.currency,
                version_price.billing_cycle,
            )
    offer_prices = (
        db.query(OfferPrice)
        .filter(OfferPrice.offer_id == subscription.offer_id)
        .filter(OfferPrice.price_type == PriceType.recurring)
        .filter(OfferPrice.is_active.is_(True))
        .order_by(OfferPrice.created_at.desc(), OfferPrice.id.desc())
        .limit(2)
        .all()
    )
    if len(offer_prices) > 1:
        logger.warning(
            "Multiple active recurring offer prices for subscription %s (offer %s); "
            "using newest",
            subscription.id,
            subscription.offer_id,
        )
    if offer_prices:
        offer_price = offer_prices[0]
        return offer_price.amount, offer_price.currency, offer_price.billing_cycle
    return None, None, None


def _effective_unit_price(
    subscription: Subscription,
    catalog_amount: Decimal,
    now: datetime,
) -> Decimal:
    """Effective per-cycle price for a subscription.

    A positive subscription.unit_price (imported or admin-set
    negotiated price) overrides the catalog amount. Zero is treated as
    "no override" because the legacy importer stores 0 when the export
    carried no per-service price. An enabled discount inside its
    [discount_start_at, discount_end_at] window (open-ended where null,
    bounds inclusive) is then applied: percentage is percent-off, fixed
    is an absolute reduction. Never returns below 0.00.
    """
    base = catalog_amount
    if subscription.unit_price is not None and subscription.unit_price > 0:
        base = subscription.unit_price
    price = Decimal(str(base))
    if subscription.discount and subscription.discount_value is not None:
        now_utc = _as_utc(now) or datetime.now(UTC)
        start = _as_utc(subscription.discount_start_at)
        end = _as_utc(subscription.discount_end_at)
        in_window = (start is None or now_utc >= start) and (
            end is None or now_utc <= end
        )
        if in_window:
            value = Decimal(str(subscription.discount_value))
            if subscription.discount_type in (
                DiscountType.percentage,
                DiscountType.percent,
            ):
                price -= price * value / Decimal("100")
            elif subscription.discount_type == DiscountType.fixed:
                price -= value
    if price < Decimal("0.00"):
        price = Decimal("0.00")
    return round_money(price)


def _resolve_tax_rate_id(db: Session, subscription: Subscription):
    def _is_active(tax_rate_id) -> bool:
        rate = db.get(TaxRate, tax_rate_id)
        return rate is not None and bool(rate.is_active)

    if subscription.service_address_id:
        address = db.get(Address, subscription.service_address_id)
        if address and address.tax_rate_id and _is_active(address.tax_rate_id):
            return address.tax_rate_id
    subscriber = db.get(Subscriber, subscription.subscriber_id)
    if subscriber and subscriber.tax_rate_id and _is_active(subscriber.tax_rate_id):
        return subscriber.tax_rate_id
    return _default_tax_rate_id(db)


def _default_tax_rate_id(db: Session):
    """Configurable fallback VAT rate, applied when neither the service address
    nor the subscriber carries a tax_rate_id. Unset by default → returns None →
    no tax (current behaviour); set the ``billing.default_tax_rate_id`` setting
    to a TaxRate id to bill a default VAT."""
    raw = settings_spec.resolve_value(db, SettingDomain.billing, "default_tax_rate_id")
    value = str(raw or "").strip()
    if not value:
        return None
    try:
        rate = db.get(TaxRate, coerce_uuid(value))
    except (ValueError, TypeError):
        return None
    if rate is not None and bool(rate.is_active):
        return rate.id
    return None


def _default_tax_application(db: Session) -> TaxApplication:
    """Whether default-VAT billing treats catalog prices as tax-exclusive (tax
    added on top — the default) or tax-inclusive (tax extracted from the price).
    Controlled by ``billing.default_tax_application`` (exclusive|inclusive)."""
    raw = settings_spec.resolve_value(
        db, SettingDomain.billing, "default_tax_application"
    )
    value = str(raw or "").strip().lower()
    if value == "inclusive":
        return TaxApplication.inclusive
    if value == "exempt":
        return TaxApplication.exempt
    return TaxApplication.exclusive


def _prorated_amount(
    full_amount: Decimal,
    period_start: datetime,
    period_end: datetime,
    usage_start: datetime,
    usage_end: datetime,
) -> Decimal:
    period_seconds = (period_end - period_start).total_seconds()
    usage_seconds = (usage_end - usage_start).total_seconds()
    if period_seconds <= 0 or usage_seconds <= 0:
        return Decimal("0.00")
    ratio = min(
        Decimal(str(usage_seconds)) / Decimal(str(period_seconds)), Decimal("1.00")
    )
    return round_money(full_amount * ratio)


def _addon_recurring_price(db: Session, add_on_id) -> tuple[Decimal, str] | None:
    """Active recurring price for an add-on -> (amount, currency), or None when
    it has no recurring price (one-time/usage add-ons don't bill on the cycle)."""
    price = (
        db.query(AddOnPrice)
        .filter(AddOnPrice.add_on_id == add_on_id)
        .filter(AddOnPrice.is_active.is_(True))
        .filter(AddOnPrice.price_type == PriceType.recurring)
        .first()
    )
    if price is None:
        return None
    return round_money(price.amount or 0), str(price.currency or "NGN")


def _bill_recurring_addons(
    db: Session,
    invoice: Invoice,
    subscription: Subscription,
    period_start: datetime,
    period_end: datetime,
    tax_rate_id,
    run_at: datetime,
) -> int:
    """Add an invoice line per active recurring add-on on this subscription, so
    the monthly bill is base plan + recurring add-ons (e.g. extra IP blocks).
    Returns the number of lines added. One-time add-ons are skipped (no recurring
    price); add-ons in a different currency than the invoice are skipped."""
    rows = (
        db.query(SubscriptionAddOn, AddOn)
        .join(AddOn, AddOn.id == SubscriptionAddOn.add_on_id)
        .filter(SubscriptionAddOn.subscription_id == subscription.id)
        .filter(
            (SubscriptionAddOn.end_at.is_(None)) | (SubscriptionAddOn.end_at > run_at)
        )
        .all()
    )
    added = 0
    tax_application = _default_tax_application(db)
    for sub_addon, add_on in rows:
        priced = _addon_recurring_price(db, add_on.id)
        if priced is None:
            continue
        unit, currency = priced
        if currency != (invoice.currency or "NGN"):
            continue
        qty = Decimal(str(sub_addon.quantity or 1))
        amount = round_money(unit * qty)
        if amount <= Decimal("0.00"):
            continue
        db.add(
            InvoiceLine(
                invoice_id=invoice.id,
                subscription_id=subscription.id,
                description=(
                    f"{add_on.name} ({period_start.date()} - {period_end.date()})"
                ),
                quantity=qty,
                unit_price=unit,
                amount=amount,
                tax_rate_id=tax_rate_id,
                tax_application=tax_application,
            )
        )
        added += 1
    return added


def _activate_pending_subscription(
    db: Session,
    subscription: Subscription,
    run_at: datetime,
) -> None:
    """Activate a pending subscription when its first invoice is generated.

    Delegates to the lifecycle module for status change and account status
    derivation. Emits the activation event manually with the
    ``auto_activated=True`` flag so other handlers (like proration) skip
    since billing already creates the invoice.
    """
    from app.services.account_lifecycle import activate_subscription

    try:
        activate_subscription(
            db,
            str(subscription.id),
            start_at=run_at,
            emit=False,  # Emit manually below with auto_activated flag
        )
    except ValueError as e:
        logger.warning(
            "Could not auto-activate subscription %s: %s", subscription.id, e
        )
        return

    logger.info("Auto-activated subscription %s (pending → active)", subscription.id)

    emit_event(
        db,
        EventType.subscription_activated,
        {
            "subscription_id": str(subscription.id),
            "offer_name": subscription.offer.name if subscription.offer else None,
            "from_status": "pending",
            "to_status": "active",
            "auto_activated": True,
        },
        subscription_id=subscription.id,
        account_id=subscription.subscriber_id,
    )


def _emit_invoice_created_event(
    db: Session,
    invoice: Invoice,
    run_id: str | None,
) -> None:
    """Emit invoice.created event for webhook integrations."""
    emit_event(
        db,
        EventType.invoice_created,
        {
            "invoice_id": str(invoice.id),
            "account_id": str(invoice.account_id),
            "status": invoice.status.value if invoice.status else None,
            "currency": invoice.currency,
            "subtotal": str(invoice.subtotal) if invoice.subtotal else "0.00",
            "total": str(invoice.total) if invoice.total else "0.00",
            "billing_period_start": invoice.billing_period_start.isoformat()
            if invoice.billing_period_start
            else None,
            "billing_period_end": invoice.billing_period_end.isoformat()
            if invoice.billing_period_end
            else None,
            "due_at": invoice.due_at.isoformat() if invoice.due_at else None,
            "billing_run_id": run_id,
        },
        invoice_id=invoice.id,
        account_id=invoice.account_id,
    )


def _mark_invoice_metadata_flag(invoice: Invoice, key: str) -> None:
    metadata = dict(invoice.metadata_ or {})
    metadata[key] = datetime.now(UTC).isoformat()
    invoice.metadata_ = metadata


def _emit_invoice_reminders(
    db: Session,
    run_at: datetime,
) -> int:
    reminder_days = _parse_day_offsets(
        settings_spec.resolve_value(db, SettingDomain.billing, "invoice_reminder_days")
    )
    if not reminder_days:
        return 0

    sent = 0
    live_accounts = accounts_with_live_service(db)
    invoices = (
        db.query(Invoice)
        .filter(Invoice.is_active.is_(True))
        .filter(
            Invoice.status.in_([InvoiceStatus.issued, InvoiceStatus.partially_paid])
        )
        .all()
    )
    for invoice in invoices:
        # Don't remind on balances for accounts whose services are all
        # terminal (disabled/canceled/expired/…) — a dead service shouldn't
        # keep pinging the customer.
        if invoice.account_id not in live_accounts:
            continue
        if not invoice.due_at or (invoice.balance_due or Decimal("0.00")) <= Decimal(
            "0.00"
        ):
            continue
        due_at = _as_utc(invoice.due_at)
        if due_at is None:
            continue
        days_until_due = (due_at.date() - run_at.date()).days
        if days_until_due not in reminder_days:
            continue
        metadata = dict(invoice.metadata_ or {})
        marker = f"invoice_reminder_sent_{days_until_due}"
        if metadata.get(marker):
            continue
        emit_event(
            db,
            EventType.invoice_sent,
            {
                "invoice_id": str(invoice.id),
                "invoice_number": invoice.invoice_number or "",
                "amount": str(invoice.balance_due or invoice.total or Decimal("0.00")),
                "due_date": due_at.date().isoformat(),
                "days_until_due": str(days_until_due),
            },
            invoice_id=invoice.id,
            account_id=invoice.account_id,
        )
        _mark_invoice_metadata_flag(invoice, marker)
        sent += 1
    return sent


def _emit_dunning_escalations(
    db: Session,
    run_at: datetime,
) -> int:
    escalation_days = _parse_day_offsets(
        settings_spec.resolve_value(
            db, SettingDomain.billing, "dunning_escalation_days"
        )
    )
    if not escalation_days:
        return 0

    sent = 0
    live_accounts = accounts_with_live_service(db)
    invoices = (
        db.query(Invoice)
        .filter(Invoice.is_active.is_(True))
        .filter(
            Invoice.status.in_(
                [
                    InvoiceStatus.issued,
                    InvoiceStatus.partially_paid,
                    InvoiceStatus.overdue,
                ]
            )
        )
        .all()
    )
    for invoice in invoices:
        # Skip escalations for accounts whose services are all terminal
        # (disabled/canceled/expired/…): the real dunning workflow already
        # excludes them, and a dead service shouldn't keep escalating.
        if invoice.account_id not in live_accounts:
            continue
        if not invoice.due_at or (invoice.balance_due or Decimal("0.00")) <= Decimal(
            "0.00"
        ):
            continue
        due_at = _as_utc(invoice.due_at)
        if due_at is None:
            continue
        days_overdue = (run_at.date() - due_at.date()).days
        if days_overdue not in escalation_days:
            continue
        metadata = dict(invoice.metadata_ or {})
        marker = f"dunning_escalation_sent_{days_overdue}"
        if metadata.get(marker):
            continue
        emit_event(
            db,
            EventType.invoice_overdue,
            {
                "invoice_id": str(invoice.id),
                "invoice_number": invoice.invoice_number or "",
                "amount": str(invoice.balance_due or invoice.total or Decimal("0.00")),
                "due_date": due_at.date().isoformat(),
                "days_overdue": str(days_overdue),
            },
            invoice_id=invoice.id,
            account_id=invoice.account_id,
        )
        _mark_invoice_metadata_flag(invoice, marker)
        sent += 1
    return sent


def _log_billing_run_audit(
    db: Session,
    run: BillingRun | None,
    summary: dict[str, Any],
    status: str,
    error: str | None = None,
) -> None:
    """Log billing run results to audit log."""
    from app.models.audit import AuditActorType, AuditEvent

    run_id = None
    if run:
        try:
            run_id = str(run.id)
        except ObjectDeletedError:
            run_id = None

    run_at_value = summary.get("run_at")
    run_at_iso = (
        run_at_value.isoformat() if isinstance(run_at_value, datetime) else None
    )
    metadata = {
        "run_id": run_id,
        "run_at": run_at_iso,
        "subscriptions_scanned": summary.get("subscriptions_scanned", 0),
        "subscriptions_billed": summary.get("subscriptions_billed", 0),
        "invoices_created": summary.get("invoices_created", 0),
        "lines_created": summary.get("lines_created", 0),
        "skipped": summary.get("skipped", 0),
        "currency_skipped": summary.get("currency_skipped", 0),
        "pending_activated": summary.get("pending_activated", 0),
        "invoice_reminders_sent": summary.get("invoice_reminders_sent", 0),
        "dunning_escalations_sent": summary.get("dunning_escalations_sent", 0),
        "status": status,
    }
    if error:
        metadata["error"] = error

    audit_event = AuditEvent(
        actor_type=AuditActorType.system,
        actor_id="billing_automation",
        action="billing_run",
        entity_type="billing_run",
        entity_id=run_id,
        is_success=status == "success",
        metadata_=metadata,
    )
    db.add(audit_event)


_ABANDONED_RUN_MAX_AGE_HOURS = 12


def _fail_abandoned_runs(db: Session) -> int:
    """Mark billing runs stuck in `running` for hours as failed.

    A run left `running` means the worker died mid-run (deploy restart, OOM,
    kill) before the failure handler could write; without this sweep those
    rows look in-flight forever.
    """
    cutoff = datetime.now(UTC) - timedelta(hours=_ABANDONED_RUN_MAX_AGE_HOURS)
    stale = (
        db.query(BillingRun)
        .filter(BillingRun.status == BillingRunStatus.running)
        .filter(BillingRun.started_at < cutoff)
        .all()
    )
    for run in stale:
        run.status = BillingRunStatus.failed
        run.finished_at = datetime.now(UTC)
        run.error = "abandoned: worker died mid-run (swept by next run)"
    if stale:
        db.commit()
        logger.warning(
            "billing_runs_abandoned_swept",
            extra={"event": "billing_runs_abandoned_swept", "count": len(stale)},
        )
    return len(stale)


def subscription_invoice_eligible(
    subscription: Subscription, *, allow_prepaid: bool = False
) -> bool:
    """Whether a subscription may enter invoice generation.

    Prepaid subscriptions draw down a deposit balance (see
    ``app/services/prepaid_billing.py``) and must NOT receive balance-due
    invoices — doing so double-bills them. Only postpaid subscriptions are
    invoice-eligible, unless a caller passes an explicit credit/admin override.
    """
    if allow_prepaid:
        return True
    return subscription.billing_mode != BillingMode.prepaid


def _hourly_notifications_enabled(db: Session) -> bool:
    """Whether the dedicated hourly billing-notifications runner owns the emits."""
    return bool(
        settings_spec.resolve_value(
            db, SettingDomain.collections, "billing_notifications_hourly_enabled"
        )
    )


def run_billing_notifications(
    db: Session,
    run_at: datetime | None = None,
) -> dict[str, int | bool]:
    """Emit invoice reminders + dunning escalations, gated by the send window.

    Intended to run hourly: the send-window gate (``billing_notif_send_hour``)
    restricts the actual sends to the configured local hour, while the existing
    per-invoice metadata markers keep them idempotent across the hourly fires.
    """
    run_at = run_at or datetime.now(UTC)
    if not enforcement_window.within_send_window(db, run_at):
        return {
            "invoice_reminders_sent": 0,
            "dunning_escalations_sent": 0,
            "skipped_outside_window": True,
        }
    reminders = _emit_invoice_reminders(db, run_at)
    escalations = _emit_dunning_escalations(db, run_at)
    db.commit()
    return {
        "invoice_reminders_sent": reminders,
        "dunning_escalations_sent": escalations,
        "skipped_outside_window": False,
    }


def run_invoice_cycle(
    db: Session,
    run_at: datetime | None = None,
    billing_cycle: BillingCycle | None = None,
    dry_run: bool = False,
    include_pending: bool = True,
    auto_activate_pending: bool = True,
    suppress_restore_notifications: bool = False,
) -> dict[str, Any]:
    """Run the billing cycle to generate invoices for subscriptions.

    Args:
        db: Database session
        run_at: The reference time for the billing run (defaults to now)
        billing_cycle: Optional filter to only process subscriptions with this cycle
        dry_run: If True, don't create any records, just return what would be done
        include_pending: If True, also bill pending subscriptions ready for activation
        auto_activate_pending: If True, auto-activate pending subscriptions when billed
        suppress_restore_notifications: If True, mute customer notifications from any
            service restore triggered when credit settles a suspended account's debt.
            Off by default (steady-state restores are a legitimate "service resumed"
            notice); set True for a bulk catch-up run to avoid a notification burst.
    """
    run_at = _as_utc(run_at) or datetime.now(UTC)

    # Global kill-switch. Before local billing became the system of record,
    # invoice generation had to stay off to avoid duplicate bills. dry_run is
    # exempt so the shadow reconciler can still compute would-be invoices for
    # validation without writing anything.
    if not dry_run and not _setting_truthy(db, "billing_enabled", default=True):
        logger.info("billing_disabled_skip", extra={"run_at": run_at.isoformat()})
        return {
            "run_at": run_at,
            "run_id": None,
            "billing_disabled": True,
            "subscriptions_scanned": 0,
            "subscriptions_billed": 0,
            "invoices_created": 0,
            "lines_created": 0,
            "skipped": 0,
        }

    global_due_days = resolve_payment_due_days(db)

    # Read auto-activate setting if not explicitly specified
    if auto_activate_pending is True:
        auto_activate_setting = settings_spec.resolve_value(
            db, SettingDomain.billing, "auto_activate_pending_on_billing"
        )
        if auto_activate_setting is False:
            auto_activate_pending = False

    run = BillingRun(
        run_at=run_at,
        billing_cycle=billing_cycle.value if billing_cycle else None,
        status=BillingRunStatus.running,
        started_at=datetime.now(UTC),
    )
    run_uuid = None
    if not dry_run:
        _fail_abandoned_runs(db)
        db.add(run)
        db.commit()
        db.refresh(run)
        run_uuid = run.id
    logger.info(
        "billing_run_start",
        extra=_billing_run_extra(
            run_uuid=run_uuid,
            run_at=run_at,
            billing_cycle=billing_cycle,
            dry_run=dry_run,
            include_pending=include_pending,
            auto_activate_pending=auto_activate_pending,
        ),
    )

    # Query billable active subscriptions. Network/account enforcement states
    # like blocked/suspended must not suppress invoicing: those accounts still
    # owe for active service periods and may need the invoice to clear the block.
    billable_account_statuses = (
        SubscriberStatus.active,
        SubscriberStatus.blocked,
        SubscriberStatus.suspended,
        SubscriberStatus.delinquent,
    )
    # Postpaid subscriptions are always invoiced. ``prepaid_monthly`` accounts
    # (prepaid billing_mode on a MONTHLY-cycle offer) are invoiced too — billed
    # in advance, due on issue — but ONLY when the cutover flag is enabled;
    # default OFF keeps the scheduled cycle postpaid-only (no behaviour change),
    # so this is safe to deploy before the prepaid-monthly migration runs.
    # Genuine daily/balance prepaid stays off-invoice regardless.
    include_prepaid_monthly = _setting_truthy(
        db, "prepaid_monthly_invoicing_enabled", default=False
    )
    mode_filter = Subscription.billing_mode != BillingMode.prepaid
    if include_prepaid_monthly:
        monthly_offer_ids = select(CatalogOffer.id).where(
            CatalogOffer.billing_cycle == BillingCycle.monthly
        )
        mode_filter = or_(
            Subscription.billing_mode != BillingMode.prepaid,
            Subscription.offer_id.in_(monthly_offer_ids),
        )

    active_subscriptions = (
        db.query(Subscription)
        .join(Subscriber, Subscriber.id == Subscription.subscriber_id)
        .filter(Subscription.status == SubscriptionStatus.active)
        .filter(Subscriber.status.in_(billable_account_statuses))
        .filter(mode_filter)
        .all()
    )

    # Optionally include pending subscriptions ready for billing
    pending_subscriptions = []
    if include_pending:
        pending_subscriptions = (
            db.query(Subscription)
            .join(Subscriber, Subscriber.id == Subscription.subscriber_id)
            .filter(Subscription.status == SubscriptionStatus.pending)
            .filter(Subscriber.status == SubscriberStatus.active)
            .filter(mode_filter)
            .all()
        )

    prepaid_skipped = (
        db.query(Subscription)
        .join(Subscriber, Subscriber.id == Subscription.subscriber_id)
        .filter(Subscription.status == SubscriptionStatus.active)
        .filter(Subscriber.status.in_(billable_account_statuses))
        .filter(Subscription.billing_mode == BillingMode.prepaid)
        .count()
    )
    if prepaid_skipped:
        logger.info(
            "Invoice cycle skipped %d prepaid subscription(s) (drawdown-billed)",
            prepaid_skipped,
        )

    subscriptions = active_subscriptions + pending_subscriptions

    invoices: dict[str, Invoice] = {}
    newly_created_invoices: list[Invoice] = []
    summary: dict[str, Any] = {
        "run_at": run_at,
        "subscriptions_scanned": len(subscriptions),
        "subscriptions_billed": 0,
        "invoices_created": 0,
        "lines_created": 0,
        "skipped": 0,
        "prepaid_skipped": prepaid_skipped,
        "currency_skipped": 0,
        "pending_activated": 0,
        "invoice_reminders_sent": 0,
        "dunning_escalations_sent": 0,
        "credit_applied": Decimal("0.00"),
        "credit_settled_invoices": 0,
        "accounts_restored": 0,
    }

    for subscription in subscriptions:
        is_pending = subscription.status == SubscriptionStatus.pending
        amount, currency, cycle = _resolve_price(db, subscription)
        if amount is None:
            summary["skipped"] += 1
            continue
        amount = _effective_unit_price(subscription, amount, run_at)
        effective_cycle = cycle or BillingCycle.monthly
        if billing_cycle and effective_cycle != billing_cycle:
            continue

        # For pending subscriptions, use run_at as the period start if no start_at
        if is_pending:
            period_start = _as_utc(subscription.start_at) or run_at
        else:
            period_start = (
                _as_utc(subscription.next_billing_at or subscription.start_at or run_at)
                or run_at
            )

        if period_start > run_at:
            continue
        period_end = _period_end(period_start, effective_cycle)

        # Skip wholly-past billing periods instead of invoicing every missed
        # month. Migrated subscribers carry next_billing_at/start_at at
        # their original signup date — without this, the runner generated a
        # backdated invoice per missed period per run, double-billing periods
        # already settled before local billing cutover.
        # We fast-forward next_billing_at to the current period and bill only
        # that. Set billing.bill_backdated_periods=true to restore arrears
        # billing if an operator genuinely needs it.
        if not is_pending and period_end <= run_at:
            if not _setting_truthy(db, "bill_backdated_periods", default=False):
                skipped_periods = 0
                while period_end <= run_at:
                    period_start = period_end
                    period_end = _period_end(period_start, effective_cycle)
                    skipped_periods += 1
                subscription.next_billing_at = period_start
                logger.info(
                    "billing_fast_forward",
                    extra={
                        "run_id": str(run_uuid) if run_uuid else None,
                        "subscription_id": str(subscription.id),
                        "skipped_periods": skipped_periods,
                        "new_period_start": period_start.isoformat(),
                    },
                )
                if period_start > run_at:
                    summary["skipped"] += 1
                    continue
        end_at = _as_utc(subscription.end_at)
        start_at = _as_utc(subscription.start_at) or period_start
        if end_at and end_at <= period_start:
            continue
        usage_start = max(period_start, start_at)
        usage_end = min(period_end, end_at) if end_at else period_end
        line_amount = _prorated_amount(
            amount, period_start, period_end, usage_start, usage_end
        )
        if line_amount <= Decimal("0.00"):
            summary["skipped"] += 1
            continue

        # Idempotency check 1: verify no invoice line exists for this subscription+period
        # This catches cases where the invoice was created but next_billing_at wasn't updated
        existing_line_for_period = (
            db.query(InvoiceLine)
            .join(Invoice, Invoice.id == InvoiceLine.invoice_id)
            .filter(InvoiceLine.subscription_id == subscription.id)
            .filter(Invoice.billing_period_start == period_start)
            .filter(Invoice.billing_period_end == period_end)
            .filter(InvoiceLine.is_active.is_(True))
            .filter(Invoice.is_active.is_(True))
            .first()
        )
        if existing_line_for_period:
            # Ensure next_billing_at is consistent with existing invoice
            if (
                subscription.next_billing_at is None
                or subscription.next_billing_at < period_end
            ):
                subscription.next_billing_at = period_end
            logger.debug(
                "Skipping subscription %s: already billed for period %s - %s",
                subscription.id,
                period_start.date(),
                period_end.date(),
            )
            summary["skipped"] += 1
            continue

        if dry_run:
            summary["subscriptions_billed"] += 1
            summary["lines_created"] += 1
            if is_pending:
                summary["pending_activated"] += 1
            continue

        # Auto-activate pending subscription
        if is_pending and auto_activate_pending:
            _activate_pending_subscription(db, subscription, run_at)
            summary["pending_activated"] += 1

        account_id = str(subscription.subscriber_id)
        invoice = invoices.get(account_id)
        if not invoice:
            invoice = (
                db.query(Invoice)
                .filter(Invoice.account_id == subscription.subscriber_id)
                .filter(Invoice.billing_period_start == period_start)
                .filter(Invoice.billing_period_end == period_end)
                .filter(Invoice.is_active.is_(True))
                .first()
            )
        if not invoice:
            # Use subscriber-level payment_due_days if set, else global
            account = db.get(Subscriber, subscription.subscriber_id)
            due_days = (
                resolve_payment_due_days(db, subscriber=account)
                if account
                else global_due_days
            )
            # prepaid_monthly is billed in advance: the invoice is due on issue
            # (a short grace lives in dunning) so non-payment enforces promptly.
            if subscription.billing_mode == BillingMode.prepaid:
                due_days = 0
            invoice = Invoice(
                account_id=subscription.subscriber_id,
                invoice_number=next_invoice_number(db),
                status=InvoiceStatus.issued,
                currency=currency or "NGN",
                billing_period_start=period_start,
                billing_period_end=period_end,
                issued_at=run_at,
                due_at=run_at + timedelta(days=due_days),
            )
            db.add(invoice)
            db.flush()
            invoices[account_id] = invoice
            newly_created_invoices.append(invoice)
            summary["invoices_created"] += 1
        elif currency and invoice.currency != currency:
            logger.warning(
                "billing_currency_mismatch_skip",
                extra={
                    "event": "billing_currency_mismatch_skip",
                    "subscription_id": str(subscription.id),
                    "subscriber_id": str(subscription.subscriber_id),
                    "subscription_currency": currency,
                    "invoice_currency": invoice.currency,
                },
            )
            summary["skipped"] += 1
            summary["currency_skipped"] += 1
            continue

        # Double-check for existing line on this specific invoice (belt and suspenders)
        existing_line = (
            db.query(InvoiceLine)
            .filter(InvoiceLine.invoice_id == invoice.id)
            .filter(InvoiceLine.subscription_id == subscription.id)
            .filter(InvoiceLine.is_active.is_(True))
            .first()
        )
        if existing_line:
            summary["skipped"] += 1
            continue

        tax_rate_id = _resolve_tax_rate_id(db, subscription)
        offer_name = (
            subscription.offer.name
            if subscription.offer
            else f"Subscription {subscription.id}"
        )
        description = f"{offer_name} ({period_start.date()} - {period_end.date()})"
        line = InvoiceLine(
            invoice_id=invoice.id,
            subscription_id=subscription.id,
            description=description,
            quantity=Decimal("1.000"),
            unit_price=round_money(line_amount),
            amount=round_money(line_amount),
            tax_rate_id=tax_rate_id,
            tax_application=_default_tax_application(db),
        )
        db.add(line)
        summary["subscriptions_billed"] += 1
        summary["lines_created"] += 1
        # Bill active recurring add-ons (e.g. extra IP blocks) on the same invoice.
        summary["lines_created"] += _bill_recurring_addons(
            db, invoice, subscription, period_start, period_end, tax_rate_id, run_at
        )
        subscription.next_billing_at = period_end

    if dry_run:
        summary["run_id"] = None
        logger.info(
            "billing_run_dry_run_complete",
            extra=_billing_run_extra(
                run_uuid=None,
                run_at=run_at,
                billing_cycle=billing_cycle,
                dry_run=dry_run,
                include_pending=include_pending,
                auto_activate_pending=auto_activate_pending,
                summary=summary,
            ),
        )
        return summary

    try:
        # Persist the invoice lines just added in the loop before recalculating
        # totals and settling credit. _recalculate_invoice_totals and
        # settle_open_invoices_from_credit read rows back via queries, which only
        # see flushed rows when the session has autoflush disabled (the test
        # harness does); these flushes are harmless no-ops under default autoflush.
        db.flush()
        # Recalculate totals for all invoices
        for invoice in invoices.values():
            _recalculate_invoice_totals(db, invoice)
        # Recalc writes balance_due/status onto the invoice objects; flush so the
        # credit settlement below sees the open balance via its query.
        db.flush()

        # Inline credit settlement is DISABLED by default. It was found to be
        # unsafe on the migrated dataset: per-invoice balance_due/allocations are
        # not authoritative (many invoices were paid from the account deposit
        # with no invoice-linked allocation, and some allocations were synced
        # without recomputing balance_due), so settling against local "open"
        # invoices destroyed real credit on already-paid invoices. "Paid but
        # walled" must be solved at the account level, not
        # by per-invoice settlement here. Re-enable only after that redesign.
        if newly_created_invoices and _setting_truthy(
            db, "settle_credit_on_invoice_enabled", default=False
        ):
            from contextlib import nullcontext

            from app.services import collections as collections_service
            from app.services.notification_suppression import suppress_notifications

            touched_account_ids = {
                str(invoice.account_id) for invoice in newly_created_invoices
            }
            # Restore can emit "service resumed" notifications; suppress them for
            # a bulk catch-up run so we don't burst-mail a large suspended cohort.
            restore_notify_ctx = (
                suppress_notifications()
                if suppress_restore_notifications
                else nullcontext()
            )
            with restore_notify_ctx:
                for account_id in touched_account_ids:
                    try:
                        settle_result = settle_open_invoices_from_credit(db, account_id)
                    except Exception:
                        logger.exception(
                            "invoice_credit_settlement_failed",
                            extra={
                                "event": "invoice_credit_settlement_failed",
                                "run_id": str(run_uuid) if run_uuid else None,
                                "account_id": account_id,
                            },
                        )
                        continue
                    if settle_result.changed:
                        summary["credit_applied"] = round_money(
                            summary["credit_applied"] + settle_result.applied
                        )
                        summary["credit_settled_invoices"] += len(
                            settle_result.invoices_settled
                        )
                    # Re-couple access state to debt: settling credit clears debt
                    # WITHOUT a payment event, so the payment_received restore never
                    # fires and the account would settle-but-stay-walled. When credit
                    # applied and no overdue debt remains, re-evaluate enforcement:
                    #   - restore_account_services lifts payment-suspended SUBSCRIPTIONS
                    #     (reason-scoped: admin/abuse blocks untouched);
                    #   - compute_account_status re-derives the SUBSCRIBER status, which
                    #     is what the runner's widened (active-subscription) population
                    #     needs — restore alone won't clear a stale account-level block.
                    # Both are idempotent and never override a genuine subscription-level
                    # suspension (the derived status stays suspended in that case).
                    if (
                        settle_result.changed
                        and not collections_service.has_overdue_balance(db, account_id)
                    ):
                        from app.services.account_lifecycle import (
                            compute_account_status,
                        )

                        account = db.get(Subscriber, coerce_uuid(account_id))
                        was_walled = account is not None and account.status in (
                            SubscriberStatus.suspended,
                            SubscriberStatus.blocked,
                        )
                        try:
                            collections_service.restore_account_services(db, account_id)
                            new_status = compute_account_status(db, account_id)
                        except Exception:
                            logger.exception(
                                "invoice_credit_restore_failed",
                                extra={
                                    "event": "invoice_credit_restore_failed",
                                    "run_id": str(run_uuid) if run_uuid else None,
                                    "account_id": account_id,
                                },
                            )
                            new_status = None
                        if was_walled and new_status == SubscriberStatus.active:
                            summary["accounts_restored"] += 1

        db.commit()

        # Emit invoice.created events for newly created invoices
        run_id_str = str(run_uuid) if run_uuid else None
        for invoice in newly_created_invoices:
            try:
                _emit_invoice_created_event(db, invoice, run_id_str)
            except Exception as event_exc:
                logger.warning(
                    "Failed to emit invoice.created event for %s: %s",
                    invoice.id,
                    event_exc,
                )

        # When the dedicated hourly notifications runner is enabled it owns the
        # reminder/escalation emits (so they honour the configured send window);
        # the daily cycle then skips them to avoid an off-hours duplicate path.
        if _hourly_notifications_enabled(db):
            summary["invoice_reminders_sent"] = 0
            summary["dunning_escalations_sent"] = 0
        else:
            summary["invoice_reminders_sent"] = _emit_invoice_reminders(db, run_at)
            summary["dunning_escalations_sent"] = _emit_dunning_escalations(db, run_at)
        db.commit()

        summary["run_id"] = run_id_str
        run_db = db.get(BillingRun, run_uuid) if run_uuid else None
        if run_db:
            run_db.status = BillingRunStatus.success
            run_db.finished_at = datetime.now(UTC)
            run_db.subscriptions_scanned = summary["subscriptions_scanned"]
            run_db.subscriptions_billed = summary["subscriptions_billed"]
            run_db.invoices_created = summary["invoices_created"]
            run_db.lines_created = summary["lines_created"]
            run_db.skipped = summary["skipped"]
        db.commit()

        # Log the run to audit AFTER the success status is committed, and
        # never let it fail the run: this exact write failing (a NOT NULL
        # violation on audit_events) marked every 2026 billing run "failed"
        # while the invoices it had created survived.
        try:
            _log_billing_run_audit(db, run_db, summary, "success")
            db.commit()
        except Exception:
            db.rollback()
            logger.exception(
                "billing_run_audit_log_failed", extra={"run_id": run_id_str}
            )

        logger.info(
            "Billing run completed: %d invoices, %d lines, %d activated",
            summary["invoices_created"],
            summary["lines_created"],
            summary["pending_activated"],
            extra=_billing_run_extra(
                run_uuid=run_uuid,
                run_at=run_at,
                billing_cycle=billing_cycle,
                dry_run=dry_run,
                include_pending=include_pending,
                auto_activate_pending=auto_activate_pending,
                summary=summary,
            ),
        )
        return summary

    except Exception as exc:
        db.rollback()
        error_msg = str(exc)
        logger.error(
            "Billing run failed: %s",
            error_msg,
            extra=_billing_run_extra(
                run_uuid=run_uuid,
                run_at=run_at,
                billing_cycle=billing_cycle,
                dry_run=dry_run,
                include_pending=include_pending,
                auto_activate_pending=auto_activate_pending,
                summary=summary,
                error=error_msg,
            ),
        )

        run_db = db.get(BillingRun, run_uuid) if run_uuid else None
        if run_db:
            run_db.status = BillingRunStatus.failed
            run_db.finished_at = datetime.now(UTC)
            run_db.error = error_msg
            db.commit()

        # Log failed billing run to audit
        try:
            _log_billing_run_audit(db, run_db, summary, "failed", error_msg)
            db.commit()
        except Exception as audit_exc:
            logger.warning("Failed to log billing run audit: %s", audit_exc)

        raise


def generate_prorated_invoice(
    db: Session,
    subscription: Subscription,
    activation_date: datetime | None = None,
) -> Invoice | None:
    """Generate a prorated invoice for a subscription that starts mid-cycle.

    This should be called when a subscription is activated mid-billing-cycle
    to charge for the partial period until the next regular billing date.

    Args:
        db: Database session
        subscription: The subscription to generate prorated invoice for
        activation_date: The activation date (defaults to now)

    Returns:
        The created invoice or None if no proration is needed
    """
    # Prepaid subscriptions draw down a deposit and are never invoiced; a
    # mid-cycle activation must not mint a balance-due proration invoice.
    if not subscription_invoice_eligible(subscription):
        logger.info(
            "Skipping prorated invoice for prepaid subscription %s",
            subscription.id,
        )
        return None
    activation_date = _as_utc(activation_date) or datetime.now(UTC)

    # Get price info
    amount, currency, cycle = _resolve_price(db, subscription)
    if amount is None:
        logger.warning(
            "No price found for subscription %s, skipping proration", subscription.id
        )
        return None
    amount = _effective_unit_price(subscription, amount, activation_date)

    effective_cycle = cycle or BillingCycle.monthly

    # Calculate billing period start based on activation date
    # Use the activation date as the period start for proration
    # The period end will be calculated based on the billing cycle
    period_start = activation_date.replace(hour=0, minute=0, second=0, microsecond=0)
    period_end = _period_end(period_start, effective_cycle)

    # For monthly billing, check if we should align to month boundaries
    # If activation is on the 1st, no proration needed (full month)
    if effective_cycle == BillingCycle.monthly and period_start.day == 1:
        return None

    # For annual billing, if activation is on Jan 1st, no proration needed
    if (
        effective_cycle == BillingCycle.annual
        and period_start.month == 1
        and period_start.day == 1
    ):
        return None

    # Calculate prorated amount
    line_amount = _prorated_amount(
        amount, period_start, period_end, activation_date, period_end
    )

    if line_amount <= Decimal("0.00"):
        return None

    # Check for existing prorated invoice for this period
    existing = (
        db.query(InvoiceLine)
        .join(Invoice, Invoice.id == InvoiceLine.invoice_id)
        .filter(InvoiceLine.subscription_id == subscription.id)
        .filter(Invoice.billing_period_start == activation_date)
        .filter(Invoice.billing_period_end == period_end)
        .filter(InvoiceLine.is_active.is_(True))
        .filter(Invoice.is_active.is_(True))
        .first()
    )
    if existing:
        logger.debug(
            "Prorated invoice already exists for subscription %s", subscription.id
        )
        return None

    # Get due days — subscriber override > global setting
    account = db.get(Subscriber, subscription.subscriber_id)
    due_days = resolve_payment_due_days(db, subscriber=account)

    # Create prorated invoice
    invoice = Invoice(
        account_id=subscription.subscriber_id,
        invoice_number=next_invoice_number(db),
        status=InvoiceStatus.issued,
        currency=currency or "NGN",
        billing_period_start=activation_date,
        billing_period_end=period_end,
        issued_at=activation_date,
        due_at=activation_date + timedelta(days=due_days),
    )
    db.add(invoice)
    db.flush()

    tax_rate_id = _resolve_tax_rate_id(db, subscription)
    offer_name = (
        subscription.offer.name
        if subscription.offer
        else f"Subscription {subscription.id}"
    )
    description = (
        f"{offer_name} (Prorated: {activation_date.date()} - {period_end.date()})"
    )

    line = InvoiceLine(
        invoice_id=invoice.id,
        subscription_id=subscription.id,
        description=description,
        quantity=Decimal("1.000"),
        unit_price=round_money(line_amount),
        amount=round_money(line_amount),
        tax_rate_id=tax_rate_id,
        tax_application=_default_tax_application(db),
    )
    db.add(line)

    # Set next billing date to the end of this prorated period
    subscription.next_billing_at = period_end

    _recalculate_invoice_totals(db, invoice)
    db.commit()
    db.refresh(invoice)

    # Emit event
    _emit_invoice_created_event(db, invoice, None)

    logger.info(
        "Generated prorated invoice %s for subscription %s: %s %s",
        invoice.id,
        subscription.id,
        line_amount,
        currency,
    )

    return invoice


def run_invoice_cycle_with_retry(
    db: Session,
    run_at: datetime | None = None,
    billing_cycle: BillingCycle | None = None,
    dry_run: bool = False,
    include_pending: bool = True,
    auto_activate_pending: bool = True,
    max_retries: int = 3,
    retry_delay_seconds: int = 5,
) -> dict[str, Any]:
    """Run the billing cycle with automatic retry on transient failures.

    Args:
        db: Database session
        run_at: The reference time for the billing run
        billing_cycle: Optional filter for billing cycle
        dry_run: If True, don't create records
        include_pending: If True, include pending subscriptions
        auto_activate_pending: If True, auto-activate pending subscriptions
        max_retries: Maximum number of retry attempts
        retry_delay_seconds: Delay between retries

    Returns:
        Summary dict of the billing run results
    """
    import time

    from sqlalchemy.exc import IntegrityError, OperationalError

    last_error: BaseException | None = None
    for attempt in range(max_retries):
        try:
            logger.info(
                "billing_run_attempt_start",
                extra=_billing_run_extra(
                    run_uuid=None,
                    run_at=_as_utc(run_at) or datetime.now(UTC),
                    billing_cycle=billing_cycle,
                    dry_run=dry_run,
                    include_pending=include_pending,
                    auto_activate_pending=auto_activate_pending,
                    attempt=attempt + 1,
                    max_retries=max_retries,
                ),
            )
            return run_invoice_cycle(
                db=db,
                run_at=run_at,
                billing_cycle=billing_cycle,
                dry_run=dry_run,
                include_pending=include_pending,
                auto_activate_pending=auto_activate_pending,
            )
        except (OperationalError, IntegrityError) as exc:
            last_error = exc
            logger.warning(
                "Billing run attempt %d/%d failed: %s",
                attempt + 1,
                max_retries,
                exc,
                extra=_billing_run_extra(
                    run_uuid=None,
                    run_at=_as_utc(run_at) or datetime.now(UTC),
                    billing_cycle=billing_cycle,
                    dry_run=dry_run,
                    include_pending=include_pending,
                    auto_activate_pending=auto_activate_pending,
                    error=str(exc),
                    attempt=attempt + 1,
                    max_retries=max_retries,
                ),
            )
            if attempt < max_retries - 1:
                db.rollback()
                time.sleep(retry_delay_seconds)
                continue
            raise
        except Exception:
            # Don't retry non-transient errors
            raise

    if last_error is not None:
        raise last_error
    raise RuntimeError("Billing run failed but no exception was captured")


def _resolve_suspension_grace_hours(db: Session) -> int:
    """Resolve billing.suspension_grace_hours (default 48), same as the
    enforcement handler."""
    grace_setting = settings_spec.resolve_value(
        db, SettingDomain.billing, "suspension_grace_hours"
    )
    try:
        return int(str(grace_setting or 48))
    except (TypeError, ValueError):
        return 48


def _emit_post_grace_suspension_escalation(
    db: Session,
    invoice: Invoice,
    now: datetime,
    grace_hours: int,
) -> bool:
    """Re-emit ``invoice_overdue`` once the suspension grace has elapsed.

    The first overdue emit (within grace) only produces a "suspension in N
    hours" warning — actual suspension requires a second ``invoice_overdue``
    event after the grace. Re-emitting here makes suspension land within
    ~1 hour of grace expiry, independent of the daily billing run's
    day-3/7/14/30 dunning cadence.

    Emits at most once per invoice: the ``suspension_escalation_sent``
    metadata flag is written at emit time (regardless of handler outcome),
    so an hourly runner never spams. Only fires when the warning was
    actually sent (``suspension_warning_sent_at``) — if grace was zero or
    auto-suspend is disabled, there is nothing to escalate — and only while
    the subscriber is still active.

    Returns True when an escalation event was emitted.
    """
    metadata = dict(invoice.metadata_ or {})
    if metadata.get("suspension_escalation_sent"):
        return False
    if not metadata.get("suspension_warning_sent_at"):
        return False
    if grace_hours <= 0:
        return False
    due_at = _as_utc(invoice.due_at)
    if due_at is None:
        return False
    hours_overdue = (now - due_at).total_seconds() / 3600
    if hours_overdue < grace_hours:
        return False
    subscriber = db.get(Subscriber, invoice.account_id)
    if not subscriber or subscriber.status != SubscriberStatus.active:
        return False
    days_overdue = (now.date() - due_at.date()).days
    emit_event(
        db,
        EventType.invoice_overdue,
        {
            "invoice_id": str(invoice.id),
            "invoice_number": invoice.invoice_number or "",
            "amount": str(invoice.balance_due or invoice.total or Decimal("0.00")),
            "due_date": due_at.date().isoformat(),
            "days_overdue": str(days_overdue),
            "escalation": "post_grace_suspension",
        },
        invoice_id=invoice.id,
        account_id=invoice.account_id,
    )
    _mark_invoice_metadata_flag(invoice, "suspension_escalation_sent")
    return True


def mark_overdue_invoices(db: Session) -> dict[str, int]:
    """Mark past-due invoices as overdue and emit events.

    Runs independently of the billing cycle. Finds invoices where
    ``due_at <= now``, ``balance_due > 0``, and status is ``issued``
    or ``partially_paid``, then transitions them to ``overdue`` and
    emits ``invoice_overdue`` events (which trigger the enforcement
    handler for suspension).

    Already-overdue invoices are re-scanned for a one-time post-grace
    suspension escalation (see ``_emit_post_grace_suspension_escalation``),
    so suspension does not depend on the daily billing run being healthy.
    """
    now = datetime.now(UTC)
    invoices = (
        db.query(Invoice)
        .filter(Invoice.is_active.is_(True))
        .filter(
            Invoice.status.in_(
                [
                    InvoiceStatus.issued,
                    InvoiceStatus.partially_paid,
                    InvoiceStatus.overdue,
                ]
            )
        )
        .filter(Invoice.due_at.is_not(None))
        .filter(Invoice.due_at <= now)
        .filter(Invoice.balance_due > Decimal("0.00"))
        .all()
    )

    grace_hours = _resolve_suspension_grace_hours(db)

    marked = 0
    escalated = 0
    errors = 0
    skipped_on_hold = 0
    for invoice in invoices:
        # Check idempotency flag before changing status
        metadata = dict(invoice.metadata_ or {})
        # Reconciliation hold: invoices flagged as under reconciliation (e.g. the
        # phantom duplicate-billing cleanup) must not be marked overdue or
        # drive suspension/dunning. Setting this flag immediately stops dunning
        # before the invoices are voided.
        if metadata.get("reconciliation_hold"):
            skipped_on_hold += 1
            continue
        if metadata.get("overdue_event_sent"):
            # Already announced in a prior run — just ensure status is overdue
            if invoice.status != InvoiceStatus.overdue:
                invoice.status = InvoiceStatus.overdue
                marked += 1
            # Post-grace escalation: re-emit once so suspension happens
            # within ~1 hour of grace expiry (idempotent, never hourly spam).
            try:
                if _emit_post_grace_suspension_escalation(
                    db, invoice, now, grace_hours
                ):
                    escalated += 1
            except Exception as exc:
                logger.error(
                    "Failed post-grace escalation for invoice %s: %s",
                    invoice.id,
                    exc,
                )
                errors += 1
            continue

        try:
            invoice.status = InvoiceStatus.overdue
            due_at = _as_utc(invoice.due_at)
            days_overdue = (now.date() - due_at.date()).days if due_at else 0

            emit_event(
                db,
                EventType.invoice_overdue,
                {
                    "invoice_id": str(invoice.id),
                    "invoice_number": invoice.invoice_number or "",
                    "amount": str(
                        invoice.balance_due or invoice.total or Decimal("0.00")
                    ),
                    "due_date": due_at.date().isoformat() if due_at else "",
                    "days_overdue": str(days_overdue),
                },
                invoice_id=invoice.id,
                account_id=invoice.account_id,
            )
            _mark_invoice_metadata_flag(invoice, "overdue_event_sent")
            marked += 1
        except Exception as exc:
            logger.error("Failed to process overdue invoice %s: %s", invoice.id, exc)
            errors += 1

    if marked or escalated:
        db.commit()

    logger.info(
        "Overdue detection: %d marked, %d escalated, %d errors, %d scanned",
        marked,
        escalated,
        errors,
        len(invoices),
    )
    return {
        "marked_overdue": marked,
        "escalated": escalated,
        "errors": errors,
        "scanned": len(invoices),
        "skipped_on_hold": skipped_on_hold,
    }


def generate_cancellation_credit(
    db: Session,
    subscription: Subscription,
) -> None:
    """Generate a credit note for unused days when a subscription is canceled mid-cycle.

    Only generates if proration is enabled and the subscription has been billed
    (has at least one invoice line). The credit covers the unused portion from
    cancellation date to next_billing_at.
    """
    from app.models.billing import CreditNote, CreditNoteLine, CreditNoteStatus
    from app.services import numbering

    # Check if proration is enabled
    proration_enabled = settings_spec.resolve_value(
        db, SettingDomain.billing, "proration_enabled"
    )
    if proration_enabled is False:
        return

    if not subscription.next_billing_at:
        return

    now = datetime.now(UTC)
    next_billing = _as_utc(subscription.next_billing_at)
    if not next_billing or next_billing <= now:
        return  # No unused future period

    # Find the most recent invoice line for this subscription
    last_line = (
        db.query(InvoiceLine)
        .filter(InvoiceLine.subscription_id == subscription.id)
        .filter(InvoiceLine.is_active.is_(True))
        .order_by(InvoiceLine.created_at.desc())
        .first()
    )
    if not last_line or not last_line.amount:
        return  # Never billed

    # Calculate unused portion
    _as_utc(subscription.start_at) or now
    cycle = BillingCycle.monthly  # fallback
    if subscription.offer and subscription.offer.billing_cycle:
        cycle = subscription.offer.billing_cycle

    period_start = _as_utc(subscription.next_billing_at)
    if period_start:
        # Work backwards: period_start = next_billing_at - cycle
        if cycle == BillingCycle.daily:
            period_start = period_start - timedelta(days=1)
        elif cycle == BillingCycle.weekly:
            period_start = period_start - timedelta(weeks=1)
        elif cycle == BillingCycle.annual:
            period_start = period_start.replace(year=period_start.year - 1)
        else:  # monthly
            month = period_start.month - 1 or 12
            year = (
                period_start.year if period_start.month > 1 else period_start.year - 1
            )
            day = min(period_start.day, monthrange(year, month)[1])
            period_start = period_start.replace(year=year, month=month, day=day)

    if not period_start:
        return

    total_seconds = max((next_billing - period_start).total_seconds(), 1)
    unused_seconds = max((next_billing - now).total_seconds(), 0)
    ratio = Decimal(str(unused_seconds)) / Decimal(str(total_seconds))
    credit_amount = (last_line.amount * ratio).quantize(Decimal("0.01"))

    if credit_amount <= Decimal("0.00"):
        return

    credit_number = numbering.generate_number(
        db,
        SettingDomain.billing,
        "credit_note_number",
        "credit_note_number_enabled",
        "credit_note_number_prefix",
        "credit_note_number_padding",
        "credit_note_number_start",
    )

    offer_name = subscription.offer.name if subscription.offer else "Subscription"
    credit = CreditNote(
        account_id=subscription.subscriber_id,
        credit_number=credit_number,
        currency="NGN",
        subtotal=credit_amount,
        tax_total=Decimal("0"),
        total=credit_amount,
        status=CreditNoteStatus.issued,
        memo=f"Cancellation credit: {offer_name} (unused {int(unused_seconds / 86400)} days)",
    )
    db.add(credit)
    db.flush()

    credit_line = CreditNoteLine(
        credit_note_id=credit.id,
        description=f"Prorated credit for {offer_name}",
        amount=credit_amount,
        quantity=Decimal("1"),
    )
    db.add(credit_line)
    db.flush()

    logger.info(
        "Generated cancellation credit %s for subscription %s: %s",
        credit_number or credit.id,
        subscription.id,
        credit_amount,
    )
