from __future__ import annotations

import logging
from calendar import monthrange
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import ObjectDeletedError

from app.models.billing import (
    BillingRun,
    BillingRunStatus,
    Invoice,
    InvoiceLine,
    InvoiceStatus,
    TaxApplication,
)
from app.models.catalog import (
    BillingCycle,
    OfferPrice,
    OfferVersionPrice,
    PriceType,
    Subscription,
    SubscriptionStatus,
)
from app.models.domain_settings import SettingDomain
from app.models.subscriber import Address, Subscriber, SubscriberStatus
from app.services import settings_spec
from app.services.billing import _recalculate_invoice_totals
from app.services.billing_settings import resolve_payment_due_days
from app.services.common import round_money
from app.services.events import emit_event
from app.services.events.types import EventType

logger = logging.getLogger(__name__)


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
        version_price = (
            db.query(OfferVersionPrice)
            .filter(OfferVersionPrice.offer_version_id == subscription.offer_version_id)
            .filter(OfferVersionPrice.price_type == PriceType.recurring)
            .filter(OfferVersionPrice.is_active.is_(True))
            .first()
        )
        if version_price:
            return (
                version_price.amount,
                version_price.currency,
                version_price.billing_cycle,
            )
    offer_price = (
        db.query(OfferPrice)
        .filter(OfferPrice.offer_id == subscription.offer_id)
        .filter(OfferPrice.price_type == PriceType.recurring)
        .filter(OfferPrice.is_active.is_(True))
        .first()
    )
    if offer_price:
        return offer_price.amount, offer_price.currency, offer_price.billing_cycle
    return None, None, None


def _resolve_tax_rate_id(db: Session, subscription: Subscription):
    if subscription.service_address_id:
        address = db.get(Address, subscription.service_address_id)
        if address and address.tax_rate_id:
            return address.tax_rate_id
    subscriber = db.get(Subscriber, subscription.subscriber_id)
    if subscriber and subscriber.tax_rate_id:
        return subscriber.tax_rate_id
    return None


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
    invoices = (
        db.query(Invoice)
        .filter(Invoice.is_active.is_(True))
        .filter(
            Invoice.status.in_([InvoiceStatus.issued, InvoiceStatus.partially_paid])
        )
        .all()
    )
    for invoice in invoices:
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


def run_invoice_cycle(
    db: Session,
    run_at: datetime | None = None,
    billing_cycle: BillingCycle | None = None,
    dry_run: bool = False,
    include_pending: bool = True,
    auto_activate_pending: bool = True,
) -> dict[str, Any]:
    """Run the billing cycle to generate invoices for subscriptions.

    Args:
        db: Database session
        run_at: The reference time for the billing run (defaults to now)
        billing_cycle: Optional filter to only process subscriptions with this cycle
        dry_run: If True, don't create any records, just return what would be done
        include_pending: If True, also bill pending subscriptions ready for activation
        auto_activate_pending: If True, auto-activate pending subscriptions when billed
    """
    run_at = _as_utc(run_at) or datetime.now(UTC)
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
        db.add(run)
        db.commit()
        db.refresh(run)
        run_uuid = run.id

    # Query active subscriptions
    active_subscriptions = (
        db.query(Subscription)
        .join(Subscriber, Subscriber.id == Subscription.subscriber_id)
        .filter(Subscription.status == SubscriptionStatus.active)
        .filter(Subscriber.status == SubscriberStatus.active)
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
            .all()
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
        "pending_activated": 0,
        "invoice_reminders_sent": 0,
        "dunning_escalations_sent": 0,
    }

    for subscription in subscriptions:
        is_pending = subscription.status == SubscriptionStatus.pending
        amount, currency, cycle = _resolve_price(db, subscription)
        if amount is None:
            summary["skipped"] += 1
            continue
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
            due_days = resolve_payment_due_days(
                db, subscriber=account
            ) if account else global_due_days
            invoice = Invoice(
                account_id=subscription.subscriber_id,
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
            summary["skipped"] += 1
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
            tax_application=TaxApplication.exclusive,
        )
        db.add(line)
        summary["subscriptions_billed"] += 1
        summary["lines_created"] += 1
        subscription.next_billing_at = period_end

    if dry_run:
        summary["run_id"] = None
        return summary

    try:
        # Recalculate totals for all invoices
        for invoice in invoices.values():
            _recalculate_invoice_totals(db, invoice)
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

        # Log successful billing run to audit
        _log_billing_run_audit(db, run_db, summary, "success")
        db.commit()

        logger.info(
            "Billing run completed: %d invoices, %d lines, %d activated",
            summary["invoices_created"],
            summary["lines_created"],
            summary["pending_activated"],
        )
        return summary

    except Exception as exc:
        db.rollback()
        error_msg = str(exc)
        logger.error("Billing run failed: %s", error_msg)

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
    activation_date = _as_utc(activation_date) or datetime.now(UTC)

    # Get price info
    amount, currency, cycle = _resolve_price(db, subscription)
    if amount is None:
        logger.warning(
            "No price found for subscription %s, skipping proration", subscription.id
        )
        return None

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
        tax_application=TaxApplication.exclusive,
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


def mark_overdue_invoices(db: Session) -> dict[str, int]:
    """Mark past-due invoices as overdue and emit events.

    Runs independently of the billing cycle. Finds invoices where
    ``due_at <= now``, ``balance_due > 0``, and status is ``issued``
    or ``partially_paid``, then transitions them to ``overdue`` and
    emits ``invoice_overdue`` events (which trigger the enforcement
    handler for suspension).
    """
    now = datetime.now(UTC)
    invoices = (
        db.query(Invoice)
        .filter(Invoice.is_active.is_(True))
        .filter(
            Invoice.status.in_(
                [InvoiceStatus.issued, InvoiceStatus.partially_paid]
            )
        )
        .filter(Invoice.due_at.is_not(None))
        .filter(Invoice.due_at <= now)
        .filter(Invoice.balance_due > Decimal("0.00"))
        .all()
    )

    marked = 0
    errors = 0
    for invoice in invoices:
        # Check idempotency flag before changing status
        metadata = dict(invoice.metadata_ or {})
        if metadata.get("overdue_event_sent"):
            # Already processed in a prior run — just ensure status is overdue
            if invoice.status != InvoiceStatus.overdue:
                invoice.status = InvoiceStatus.overdue
            marked += 1
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
            logger.error(
                "Failed to process overdue invoice %s: %s", invoice.id, exc
            )
            errors += 1

    if marked:
        db.commit()

    logger.info(
        "Overdue detection: %d marked, %d errors, %d scanned",
        marked,
        errors,
        len(invoices),
    )
    return {"marked_overdue": marked, "errors": errors, "scanned": len(invoices)}


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
    start_at = _as_utc(subscription.start_at) or now
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
            year = period_start.year if period_start.month > 1 else period_start.year - 1
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
