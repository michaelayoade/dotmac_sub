"""Subscription management services.

Provides services for Subscriptions and SubscriptionAddOns.
"""

import logging
from calendar import monthrange
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy.orm import Session, selectinload

from app.models.catalog import (
    AccessCredential,
    BillingCycle,
    BillingMode,
    CatalogOffer,
    ContractTerm,
    OfferPrice,
    OfferRadiusProfile,
    OfferVersionPrice,
    PriceType,
    Subscription,
    SubscriptionAddOn,
    SubscriptionStatus,
)
from app.models.domain_settings import SettingDomain
from app.schemas.catalog import (
    SubscriptionAddOnCreate,
    SubscriptionAddOnUpdate,
    SubscriptionCreate,
    SubscriptionUpdate,
)
from app.services import settings_spec
from app.services.common import apply_ordering, apply_pagination, validate_enum
from app.services.crud import CRUDManager
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.query_builders import apply_optional_equals
from app.services.response import ListResponseMixin
from app.validators import catalog as catalog_validators

logger = logging.getLogger(__name__)


def _ensure_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _add_months(value: datetime, months: int) -> datetime:
    total = value.month - 1 + months
    year = value.year + total // 12
    month = total % 12 + 1
    day = min(value.day, monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def _resolve_billing_cycle(
    db: Session,
    offer_id: str,
    offer_version_id: str | None,
) -> BillingCycle:
    if offer_version_id:
        version_price: OfferVersionPrice | None = (
            db.query(OfferVersionPrice)
            .filter(OfferVersionPrice.offer_version_id == offer_version_id)
            .filter(OfferVersionPrice.price_type == PriceType.recurring)
            .filter(OfferVersionPrice.is_active.is_(True))
            .first()
        )
        if version_price and version_price.billing_cycle:
            return version_price.billing_cycle
    offer_price: OfferPrice | None = (
        db.query(OfferPrice)
        .filter(OfferPrice.offer_id == offer_id)
        .filter(OfferPrice.price_type == PriceType.recurring)
        .filter(OfferPrice.is_active.is_(True))
        .first()
    )
    if offer_price and offer_price.billing_cycle:
        return offer_price.billing_cycle
    offer = db.get(CatalogOffer, offer_id)
    return (
        offer.billing_cycle if offer and offer.billing_cycle else BillingCycle.monthly
    )


def _compute_next_billing_at(start_at: datetime, cycle: BillingCycle) -> datetime:
    """Compute the next billing date based on the billing cycle.

    Args:
        start_at: The reference date (subscription start or last billing date)
        cycle: The billing cycle (daily, weekly, monthly, annual)

    Returns:
        The next billing date
    """
    if cycle == BillingCycle.daily:
        return start_at + timedelta(days=1)
    if cycle == BillingCycle.weekly:
        return start_at + timedelta(weeks=1)
    if cycle == BillingCycle.annual:
        return _add_months(start_at, 12)
    # Default to monthly
    return _add_months(start_at, 1)


def _compute_contract_end_at(start_at: datetime, term: ContractTerm) -> datetime | None:
    if term == ContractTerm.twelve_month:
        return _add_months(start_at, 12)
    if term == ContractTerm.twentyfour_month:
        return _add_months(start_at, 24)
    return None


def _generate_proration_if_enabled(
    db: Session,
    subscription: Subscription,
    from_status: SubscriptionStatus | None,
) -> None:
    """Generate a prorated invoice if proration is enabled and subscription is newly activated."""
    from app.services.billing_automation import generate_prorated_invoice

    # Only prorate on activation (not resume from suspension)
    if from_status == SubscriptionStatus.suspended:
        return

    # Check if proration is enabled
    proration_enabled = settings_spec.resolve_value(
        db, SettingDomain.billing, "proration_enabled"
    )
    if proration_enabled is False:
        return

    # Generate prorated invoice
    try:
        generate_prorated_invoice(db, subscription)
    except Exception as exc:
        # Log but don't fail the activation
        logger.warning(
            "Failed to generate prorated invoice for subscription %s: %s",
            subscription.id,
            exc,
        )


def _sync_credentials_to_radius(db: Session, subscriber_id) -> None:
    """Reconcile internal/external RADIUS state for active subscriptions."""
    try:
        from app.services.radius import reconcile_subscription_connectivity

        active_subscriptions = (
            db.query(Subscription)
            .filter(Subscription.subscriber_id == subscriber_id)
            .filter(Subscription.status == SubscriptionStatus.active)
            .all()
        )
        for subscription in active_subscriptions:
            reconcile_subscription_connectivity(db, str(subscription.id))
    except Exception as exc:
        logger.warning(
            "Failed to reconcile RADIUS state for subscriber %s: %s",
            subscriber_id,
            exc,
        )


def _select_nas_for_subscriber(db: Session, subscriber_id: str):
    """Select an appropriate NAS device for a subscriber based on their POP site.

    Selection priority:
    1. Active NAS devices in subscriber's POP site
    2. Prefer NAS with lowest current_subscriber_count (load balancing)
    3. Fall back to None if no suitable NAS found

    Returns:
        NasDevice ID or None
    """
    from app.models.catalog import NasDevice, NasDeviceStatus
    from app.models.network_monitoring import PopSite
    from app.models.subscriber import Subscriber

    subscriber = db.get(Subscriber, subscriber_id)
    if not subscriber or not subscriber.pop_site_id:
        return None

    pop_site = db.get(PopSite, subscriber.pop_site_id)
    if not pop_site:
        return None

    # Find active NAS devices in this POP site, ordered by load
    nas_device = (
        db.query(NasDevice)
        .filter(NasDevice.pop_site_id == pop_site.id)
        .filter(NasDevice.is_active.is_(True))
        .filter(NasDevice.status == NasDeviceStatus.active)
        .order_by(
            NasDevice.current_subscriber_count.asc().nullsfirst(),
            NasDevice.created_at.asc(),
        )
        .first()
    )

    if nas_device:
        logger.debug(
            "Auto-selected NAS device %s for subscriber %s (POP: %s)",
            nas_device.id,
            subscriber_id,
            pop_site.name,
        )
        return nas_device.id

    logger.debug(
        "No active NAS device found in POP %s for subscriber %s",
        pop_site.name,
        subscriber_id,
    )
    return None


def _auto_generate_pppoe(
    db: Session,
    subscription: Subscription,
) -> None:
    """Auto-generate PPPoE credentials for newly activated subscriptions."""
    try:
        from app.services.pppoe_credentials import auto_generate_pppoe_credential

        profile_id = subscription.radius_profile_id
        auto_generate_pppoe_credential(
            db,
            str(subscription.subscriber_id),
            radius_profile_id=str(profile_id) if profile_id else None,
        )
    except Exception as exc:
        logger.warning(
            "PPPoE auto-generation failed for subscription %s: %s",
            subscription.id,
            exc,
        )


def _resolve_offer_radius_profile_id(db: Session, offer_id: str | None):
    if not offer_id:
        return None
    link = (
        db.query(OfferRadiusProfile)
        .filter(OfferRadiusProfile.offer_id == offer_id)
        .first()
    )
    return link.profile_id if link else None


def apply_offer_radius_profile(
    db: Session,
    subscription: Subscription,
    *,
    previous_offer_id=None,
    target_profile_id=None,
    force: bool = False,
    sync_credentials: bool = True,
):
    """Keep subscription and inherited credentials aligned to the offer profile."""
    previous_default = _resolve_offer_radius_profile_id(
        db, str(previous_offer_id) if previous_offer_id else None
    )
    resolved_target = (
        target_profile_id
        if target_profile_id is not None
        else _resolve_offer_radius_profile_id(db, str(subscription.offer_id))
    )

    inherited_subscription = (
        subscription.radius_profile_id is None
        or subscription.radius_profile_id == previous_default
    )
    if force or inherited_subscription:
        subscription.radius_profile_id = resolved_target

    if sync_credentials:
        credentials = (
            db.query(AccessCredential)
            .filter(AccessCredential.subscriber_id == subscription.subscriber_id)
            .filter(AccessCredential.is_active.is_(True))
            .all()
        )
        for credential in credentials:
            inherited_credential = (
                credential.radius_profile_id is None
                or credential.radius_profile_id == previous_default
            )
            if force or inherited_credential:
                credential.radius_profile_id = resolved_target

    return resolved_target


def _emit_subscription_status_event(
    db: Session,
    subscription: Subscription,
    from_status: SubscriptionStatus | None,
    to_status: SubscriptionStatus | None,
) -> None:
    """Emit the appropriate event based on subscription status transition.

    IMPORTANT: For activation, credentials are synced BEFORE events are emitted
    to ensure provisioning handlers have access to RadiusUser records.
    """
    if to_status is None:
        return

    from_str = from_status.value if from_status else None
    to_str = to_status.value if to_status else None
    offer_name = subscription.offer.name if subscription.offer else None

    payload = {
        "subscription_id": str(subscription.id),
        "offer_name": offer_name,
        "from_status": from_str,
        "to_status": to_str,
    }

    # Map status transitions to event types
    if to_status == SubscriptionStatus.active:
        # Generate PPPoE BEFORE events so provisioning handler sees credentials
        _auto_generate_pppoe(db, subscription)

        # CRITICAL: Sync credentials to RADIUS BEFORE emitting events
        # This ensures provisioning handlers can access RadiusUser records
        _sync_credentials_to_radius(db, subscription.subscriber_id)

        # If resuming from suspension, restore connectivity
        if from_status == SubscriptionStatus.suspended:
            try:
                from app.services.enforcement import restore_subscription_connectivity

                restore_subscription_connectivity(db, str(subscription.id))
            except Exception as exc:
                logger.warning(
                    "Failed to restore connectivity for subscription %s: %s",
                    subscription.id,
                    exc,
                )
            emit_event(
                db,
                EventType.subscription_resumed,
                payload,
                subscription_id=subscription.id,
                account_id=subscription.subscriber_id,
            )
        else:
            emit_event(
                db,
                EventType.subscription_activated,
                payload,
                subscription_id=subscription.id,
                account_id=subscription.subscriber_id,
            )
            # Generate prorated invoice for new activations
            _generate_proration_if_enabled(db, subscription, from_status)

    elif to_status == SubscriptionStatus.suspended:
        # Cleanup RADIUS connectivity on suspension
        try:
            from app.services.enforcement import cleanup_subscription_on_suspend

            cleanup_subscription_on_suspend(db, str(subscription.id))
        except Exception as exc:
            logger.warning(
                "Failed to cleanup on suspend for subscription %s: %s",
                subscription.id,
                exc,
            )
        emit_event(
            db,
            EventType.subscription_suspended,
            payload,
            subscription_id=subscription.id,
            account_id=subscription.subscriber_id,
        )
    elif to_status == SubscriptionStatus.canceled:
        emit_event(
            db,
            EventType.subscription_canceled,
            payload,
            subscription_id=subscription.id,
            account_id=subscription.subscriber_id,
        )
    elif to_status == SubscriptionStatus.expired:
        emit_event(
            db,
            EventType.subscription_expired,
            payload,
            subscription_id=subscription.id,
            account_id=subscription.subscriber_id,
        )


def _handle_status_transition_via_lifecycle(
    db: Session,
    subscription: Subscription,
    from_status: SubscriptionStatus | None,
    to_status: SubscriptionStatus,
) -> None:
    """Route status transitions through the lifecycle module.

    Called after the subscription status has already been committed.
    Manages enforcement locks, computes account status, and emits events.
    For transitions that also need PPPoE generation or RADIUS sync,
    delegates to the original ``_emit_subscription_status_event`` which
    handles those side effects.
    """
    from app.models.enforcement_lock import EnforcementReason
    from app.services.account_lifecycle import (
        SUSPENDED_EQUIVALENT,
        compute_account_status,
        suspend_subscription,
    )

    sub_id = str(subscription.id)
    subscriber_id = str(subscription.subscriber_id)

    if to_status == SubscriptionStatus.suspended:
        # Admin/catalog-initiated suspension — create enforcement lock
        try:
            suspend_subscription(
                db,
                sub_id,
                reason=EnforcementReason.admin,
                source="catalog_update",
                emit=False,
            )
        except ValueError as e:
            if "not found" in str(e):
                logger.error(
                    "Data integrity: subscription %s not found during suspend "
                    "despite being just committed: %s",
                    sub_id,
                    e,
                )
            else:
                logger.info(
                    "Skipped enforcement lock for subscription %s: %s", sub_id, e
                )
        _emit_subscription_status_event(db, subscription, from_status, to_status)

    elif to_status == SubscriptionStatus.active:
        from app.services.account_lifecycle import resolve_locks_for_trigger

        if from_status in SUSPENDED_EQUIVALENT:
            resolve_locks_for_trigger(
                db,
                subscription,
                trigger="admin",
                resolved_by="catalog_update",
                emit=False,
            )
        compute_account_status(db, subscriber_id)
        _emit_subscription_status_event(db, subscription, from_status, to_status)

    elif to_status == SubscriptionStatus.canceled:
        from app.services.account_lifecycle import resolve_all_locks

        resolve_all_locks(db, subscription, "canceled")
        if not subscription.canceled_at:
            subscription.canceled_at = datetime.now(UTC)
            db.flush()
        compute_account_status(db, subscriber_id)
        _emit_subscription_status_event(db, subscription, from_status, to_status)

    elif to_status == SubscriptionStatus.expired:
        from app.services.account_lifecycle import resolve_all_locks

        resolve_all_locks(db, subscription, "expired")
        compute_account_status(db, subscriber_id)
        _emit_subscription_status_event(db, subscription, from_status, to_status)

    else:
        compute_account_status(db, subscriber_id)
        _emit_subscription_status_event(db, subscription, from_status, to_status)

    # Ensure lifecycle state (locks, account status) is persisted
    # independently of event dispatcher's internal commits
    db.commit()


def _validate_plan_change(
    db: Session,
    subscription: Subscription,
    new_offer_id: str,
) -> None:
    """Validate that a plan change is allowed.

    Checks:
    - New offer exists and is active
    - Service type compatibility (residential ↔ residential only)
    - Regional availability (if offer has region_zone_id)
    - Billing mode compatibility (prepaid ↔ prepaid, postpaid ↔ postpaid)
    """
    new_offer = db.get(CatalogOffer, new_offer_id)
    if not new_offer:
        raise HTTPException(status_code=404, detail="Target offer not found")

    old_offer = db.get(CatalogOffer, subscription.offer_id)

    # Service type check: don't allow residential → business cross-change
    if old_offer and new_offer:
        old_type = getattr(old_offer, "service_type", None)
        new_type = getattr(new_offer, "service_type", None)
        if old_type and new_type and old_type != new_type:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot change from {old_type.value} to {new_type.value} plan. "
                f"Upgrade/downgrade must be within the same service class.",
            )

    # Billing mode check: prepaid stays prepaid, postpaid stays postpaid
    sub_mode = subscription.billing_mode
    if sub_mode and new_offer:
        new_mode = getattr(new_offer, "billing_mode", None)
        if new_mode and sub_mode != new_mode:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot change from {sub_mode.value} to {new_mode.value} billing. "
                f"Plan changes must stay within the same billing mode.",
            )

    # Region availability check
    if new_offer.region_zone_id:
        from app.models.subscriber import Subscriber

        subscriber = db.get(Subscriber, subscription.subscriber_id)
        if subscriber and hasattr(subscriber, "pop_site_id") and subscriber.pop_site_id:
            from app.models.network_monitoring import PopSite

            pop = db.get(PopSite, subscriber.pop_site_id)
            if (
                pop
                and pop.zone_id
                and str(pop.zone_id) != str(new_offer.region_zone_id)
            ):
                raise HTTPException(
                    status_code=400,
                    detail=f"Offer '{new_offer.name}' is not available in your service region.",
                )


def _billing_cycle_days(db: Session, offer_id) -> int:
    """Resolve the number of days in a billing cycle for an offer."""
    price = (
        db.query(OfferPrice)
        .filter(
            OfferPrice.offer_id == offer_id,
            OfferPrice.price_type == PriceType.recurring,
            OfferPrice.is_active.is_(True),
        )
        .first()
    )
    cycle = price.billing_cycle.value if price and price.billing_cycle else "monthly"
    return {"daily": 1, "weekly": 7, "monthly": 30, "quarterly": 90, "annual": 365}.get(
        cycle, 30
    )


def _offer_recurring_price_amount(db: Session, offer_id) -> Decimal:
    """Return the active recurring amount for an offer, or zero."""
    price = (
        db.query(OfferPrice.amount)
        .filter(
            OfferPrice.offer_id == offer_id,
            OfferPrice.price_type == PriceType.recurring,
            OfferPrice.is_active.is_(True),
        )
        .first()
    )
    return Decimal(str(price[0])) if price else Decimal("0")


def _plan_change_text_setting(db: Session, key: str, default: str) -> str:
    value = settings_spec.resolve_value(db, SettingDomain.billing, key)
    text = str(value or "").strip()
    return text if text else default


def _plan_change_decimal_setting(
    db: Session, key: str, default: str = "0.00"
) -> Decimal:
    try:
        return Decimal(_plan_change_text_setting(db, key, default))
    except Exception:
        return Decimal(default)


def _apply_plan_change_policy(
    db: Session,
    proration: dict,
    *,
    old_price: Decimal,
    new_price: Decimal,
) -> dict:
    """Apply billing-domain plan-change policy settings to proration output."""
    result = dict(proration)
    refund_policy = _plan_change_text_setting(db, "refund_policy", "none").lower()
    invoice_timing = _plan_change_text_setting(
        db, "invoice_timing", "immediate"
    ).lower()
    minimum_invoice_amount = _plan_change_decimal_setting(db, "minimum_invoice_amount")
    fee_tax_rate = _plan_change_decimal_setting(db, "fee_tax_rate")

    net_amount = Decimal(str(result.get("net_amount", "0")))
    credit_amount = Decimal(str(result.get("credit_amount", "0")))

    if net_amount < 0 and refund_policy == "none":
        result["credit_amount"] = Decimal("0.00")
        result["net_amount"] = Decimal("0.00")
        net_amount = Decimal("0.00")
        credit_amount = Decimal("0.00")

    fee_amount = Decimal("0.00")
    if new_price > old_price:
        fee_amount = _plan_change_decimal_setting(db, "upgrade_fee")
    elif new_price < old_price:
        fee_amount = _plan_change_decimal_setting(db, "downgrade_fee")

    if fee_amount > 0:
        tax_multiplier = Decimal("1.00") + (fee_tax_rate / Decimal("100"))
        fee_total = (fee_amount * tax_multiplier).quantize(Decimal("0.01"))
        result["fee_amount"] = fee_total
        net_amount = (Decimal(str(result.get("net_amount", "0"))) + fee_total).quantize(
            Decimal("0.01")
        )
        result["net_amount"] = net_amount

    result["invoice_timing"] = invoice_timing
    result["minimum_invoice_amount"] = minimum_invoice_amount
    result["generate_now"] = (
        invoice_timing == "immediate"
        and abs(Decimal(str(result.get("net_amount", "0")))) >= minimum_invoice_amount
    )
    result["credit_amount"] = credit_amount
    return result


def _calculate_proration(
    db: Session,
    subscription: Subscription,
    new_offer_id: str,
) -> dict:
    """Calculate proration amounts for a mid-cycle plan change.

    Supports daily, weekly, monthly, and annual billing cycles.

    Returns dict with:
    - credit_amount: unused portion of current plan to refund
    - charge_amount: prorated charge for new plan
    - net_amount: charge - credit (positive = customer owes, negative = credit)
    - days_remaining: days left in current billing cycle
    - days_in_cycle: total days in billing cycle
    """
    from decimal import Decimal

    now = datetime.now(UTC)
    next_billing = subscription.next_billing_at
    if not next_billing:
        return {
            "credit_amount": Decimal("0"),
            "charge_amount": Decimal("0"),
            "net_amount": Decimal("0"),
            "days_remaining": 0,
            "days_in_cycle": 30,
        }

    # Resolve cycle length from the current offer's billing cycle
    cycle_days = _billing_cycle_days(db, subscription.offer_id)
    if next_billing.tzinfo is None:
        next_billing = next_billing.replace(tzinfo=UTC)
    cycle_start = next_billing - timedelta(days=cycle_days)
    days_elapsed = max(0, (now - cycle_start).days)
    days_remaining = max(0, cycle_days - days_elapsed)

    if days_remaining == 0:
        return {
            "credit_amount": Decimal("0"),
            "charge_amount": Decimal("0"),
            "net_amount": Decimal("0"),
            "days_remaining": 0,
            "days_in_cycle": cycle_days,
        }

    # Get old and new recurring prices
    old_price_row = (
        db.query(OfferPrice.amount)
        .filter(
            OfferPrice.offer_id == subscription.offer_id,
            OfferPrice.price_type == PriceType.recurring,
            OfferPrice.is_active.is_(True),
        )
        .first()
    )
    new_price_row = (
        db.query(OfferPrice.amount)
        .filter(
            OfferPrice.offer_id == new_offer_id,
            OfferPrice.price_type == PriceType.recurring,
            OfferPrice.is_active.is_(True),
        )
        .first()
    )

    old_price = Decimal(str(old_price_row[0])) if old_price_row else Decimal("0")
    new_price = Decimal(str(new_price_row[0])) if new_price_row else Decimal("0")

    daily_old = old_price / Decimal(str(cycle_days))
    daily_new = new_price / Decimal(str(cycle_days))
    remaining = Decimal(str(days_remaining))

    credit_amount = (daily_old * remaining).quantize(Decimal("0.01"))
    charge_amount = (daily_new * remaining).quantize(Decimal("0.01"))
    net_amount = (charge_amount - credit_amount).quantize(Decimal("0.01"))

    return {
        "credit_amount": credit_amount,
        "charge_amount": charge_amount,
        "net_amount": net_amount,
        "days_remaining": days_remaining,
        "days_in_cycle": cycle_days,
        "old_daily_rate": daily_old.quantize(Decimal("0.01")),
        "new_daily_rate": daily_new.quantize(Decimal("0.01")),
    }


def _generate_proration_invoice(
    db: Session,
    subscription: Subscription,
    proration: dict,
    old_offer_name: str,
    new_offer_name: str,
) -> None:
    """Generate a proration invoice or credit note for a plan change.

    For **prepaid** subscribers (already paid for the cycle):
    - Upgrade: invoice for the price difference (remaining days)
    - Downgrade: credit note for the overpayment (remaining days)

    For **postpaid** subscribers (pay at end of cycle):
    - The next invoice will naturally reflect the new rate
    - Only generate an adjustment if changing mid-cycle with partial billing
    """
    from decimal import Decimal

    # Postpaid: next invoice naturally reflects new rate — skip mid-cycle proration
    billing_mode = getattr(subscription, "billing_mode", None)
    if billing_mode and hasattr(billing_mode, "value"):
        billing_mode = billing_mode.value
    if billing_mode == "postpaid":
        logger.info(
            "Skipping proration for postpaid subscription %s — next invoice will reflect new rate",
            subscription.id,
        )
        return

    from app.models.billing import (
        CreditNote,
        CreditNoteStatus,
        Invoice,
        InvoiceLine,
        InvoiceStatus,
        TaxApplication,
    )
    from app.services import numbering

    net = proration["net_amount"]
    if abs(net) < Decimal("1.00"):
        # Skip tiny amounts
        return

    subscriber_id = str(subscription.subscriber_id)
    days = proration["days_remaining"]

    if net > 0:
        # Customer owes more — generate invoice
        invoice_number = numbering.generate_number(
            db,
            SettingDomain.billing,
            "invoice_number",
            "invoice_number_enabled",
            "invoice_number_prefix",
            "invoice_number_padding",
            "invoice_number_start",
        )
        invoice = Invoice(
            account_id=subscriber_id,
            invoice_number=invoice_number,
            currency="NGN",
            subtotal=net,
            tax_total=Decimal("0"),
            total=net,
            balance_due=net,
            status=InvoiceStatus.issued,
            memo=f"Plan change proration: {old_offer_name} → {new_offer_name} ({days} days remaining)",
        )
        db.add(invoice)
        db.flush()
        line = InvoiceLine(
            invoice_id=invoice.id,
            subscription_id=subscription.id,
            description=f"Proration: upgrade to {new_offer_name} ({days} days)",
            quantity=Decimal("1"),
            unit_price=net,
            amount=net,
            tax_application=TaxApplication.exclusive,
            is_active=True,
        )
        db.add(line)
        logger.info(
            "Generated proration invoice %s for ₦%s (upgrade %s → %s)",
            invoice_number,
            net,
            old_offer_name,
            new_offer_name,
        )
    else:
        # Customer gets credit — generate credit note
        credit_amount = abs(net)
        credit_number = numbering.generate_number(
            db,
            SettingDomain.billing,
            "credit_note_number",
            "credit_note_number_enabled",
            "credit_note_number_prefix",
            "credit_note_number_padding",
            "credit_note_number_start",
        )
        credit = CreditNote(
            account_id=subscriber_id,
            credit_number=credit_number,
            currency="NGN",
            subtotal=credit_amount,
            tax_total=Decimal("0"),
            total=credit_amount,
            status=CreditNoteStatus.issued,
            memo=f"Plan change credit: {old_offer_name} → {new_offer_name} ({days} days remaining)",
        )
        db.add(credit)
        logger.info(
            "Generated proration credit %s for ₦%s (downgrade %s → %s)",
            credit_number,
            credit_amount,
            old_offer_name,
            new_offer_name,
        )


def _emit_offer_change_event(
    db: Session,
    subscription: Subscription,
    previous_offer_id: str,
) -> None:
    """Emit upgrade or downgrade event when the offer changes on an active subscription.

    Determines direction by comparing recurring price of old vs new offer.
    Also triggers RADIUS credential sync so speed changes take effect.
    """
    from app.models.catalog import OfferPrice, PriceType

    old_price_row = (
        db.query(OfferPrice.amount)
        .filter(
            OfferPrice.offer_id == previous_offer_id,
            OfferPrice.price_type == PriceType.recurring,
            OfferPrice.is_active.is_(True),
        )
        .first()
    )
    new_price_row = (
        db.query(OfferPrice.amount)
        .filter(
            OfferPrice.offer_id == str(subscription.offer_id),
            OfferPrice.price_type == PriceType.recurring,
            OfferPrice.is_active.is_(True),
        )
        .first()
    )
    old_price = old_price_row[0] if old_price_row else 0
    new_price = new_price_row[0] if new_price_row else 0
    is_upgrade = new_price > old_price

    old_offer = db.get(CatalogOffer, previous_offer_id)
    new_offer = subscription.offer

    payload = {
        "subscription_id": str(subscription.id),
        "previous_offer_id": previous_offer_id,
        "previous_offer_name": old_offer.name if old_offer else None,
        "new_offer_id": str(subscription.offer_id),
        "new_offer_name": new_offer.name if new_offer else None,
        "direction": "upgrade" if is_upgrade else "downgrade",
    }

    event_type = (
        EventType.subscription_upgraded
        if is_upgrade
        else EventType.subscription_downgraded
    )
    emit_event(
        db,
        event_type,
        payload,
        subscription_id=subscription.id,
        account_id=subscription.subscriber_id,
    )
    logger.info(
        "Subscription %s %s: %s -> %s",
        subscription.id,
        "upgraded" if is_upgrade else "downgraded",
        old_offer.name if old_offer else previous_offer_id,
        new_offer.name if new_offer else subscription.offer_id,
    )

    # Sync RADIUS credentials so the new speed profile takes effect
    _sync_credentials_to_radius(db, subscription.subscriber_id)
    try:
        from app.services.enforcement import update_subscription_sessions

        update_subscription_sessions(db, str(subscription.id), reason="plan_change")
    except Exception:
        logger.warning(
            "Failed to update sessions after plan change for %s",
            subscription.id,
        )


def _create_service_order_for_subscription(db: Session, subscription: Subscription):
    """Create a service order for a new subscription that needs provisioning."""
    from app.models.provisioning import ServiceOrderStatus
    from app.schemas.provisioning import ServiceOrderCreate
    from app.services import provisioning as provisioning_service

    # Account roles removed during consolidation; no contact linkage available here.
    requested_by_contact_id = None

    try:
        payload = ServiceOrderCreate(
            account_id=subscription.subscriber_id,
            subscription_id=subscription.id,
            requested_by_contact_id=requested_by_contact_id,
            status=ServiceOrderStatus.submitted,
            notes=f"Auto-created for subscription: {subscription.offer.name if subscription.offer else subscription.id}",
        )
        provisioning_service.service_orders.create(db, payload)
    except Exception:
        # Don't fail subscription creation if service order creation fails
        logger.warning(
            "Auto service-order creation failed for subscription %s",
            subscription.id,
            exc_info=True,
        )


class Subscriptions(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: SubscriptionCreate):
        catalog_validators.validate_subscription_links(
            db,
            str(payload.subscriber_id),
            str(payload.offer_id),
            str(payload.offer_version_id) if payload.offer_version_id else None,
            str(payload.service_address_id) if payload.service_address_id else None,
        )
        catalog_validators.validate_offer_active(db, str(payload.offer_id))
        reference_at = payload.start_at or datetime.now(UTC)
        if payload.offer_version_id:
            catalog_validators.validate_offer_version_active(
                db,
                str(payload.offer_version_id),
                str(payload.offer_id),
                reference_at,
            )
        catalog_validators.validate_subscription_dates(
            payload.status,
            payload.start_at,
            payload.end_at,
            payload.next_billing_at,
            payload.canceled_at,
        )
        catalog_validators.enforce_single_active_subscription(
            db, str(payload.subscriber_id), payload.status
        )
        data = payload.model_dump()
        fields_set = payload.model_fields_set
        if "status" not in fields_set:
            default_status = settings_spec.resolve_value(
                db, SettingDomain.catalog, "default_subscription_status"
            )
            if default_status:
                data["status"] = validate_enum(
                    default_status, SubscriptionStatus, "status"
                )
        if "contract_term" not in fields_set:
            default_contract_term = settings_spec.resolve_value(
                db, SettingDomain.catalog, "default_contract_term"
            )
            if default_contract_term:
                data["contract_term"] = validate_enum(
                    default_contract_term, ContractTerm, "contract_term"
                )
        if "billing_mode" not in fields_set:
            # Inherit from subscriber first, then fall back to offer
            from app.models.subscriber import Subscriber

            subscriber = db.get(Subscriber, str(payload.subscriber_id))
            if subscriber and subscriber.billing_mode:
                data["billing_mode"] = subscriber.billing_mode
            else:
                offer = db.get(CatalogOffer, str(payload.offer_id))
                data["billing_mode"] = (
                    offer.billing_mode
                    if offer and offer.billing_mode
                    else BillingMode.prepaid
                )
        if (
            "start_at" not in fields_set
            and data.get("status") == SubscriptionStatus.active
        ):
            data["start_at"] = datetime.now(UTC)
        start_at = data.get("start_at")
        if (
            "next_billing_at" not in fields_set
            and start_at
            and data.get("status") == SubscriptionStatus.active
        ):
            offer_version_id = data.get("offer_version_id")
            cycle = _resolve_billing_cycle(
                db,
                str(data["offer_id"]),
                str(offer_version_id) if offer_version_id else None,
            )
            data["next_billing_at"] = _compute_next_billing_at(start_at, cycle)
        if "end_at" not in fields_set and start_at and data.get("contract_term"):
            end_at = _compute_contract_end_at(start_at, data["contract_term"])
            if end_at:
                data["end_at"] = end_at

        # Auto-select NAS device from subscriber's POP site if not provided
        if "provisioning_nas_device_id" not in fields_set or not data.get(
            "provisioning_nas_device_id"
        ):
            nas_device_id = _select_nas_for_subscriber(db, str(payload.subscriber_id))
            if nas_device_id:
                data["provisioning_nas_device_id"] = nas_device_id

        subscription = Subscription(**data)
        apply_offer_radius_profile(
            db,
            subscription,
            target_profile_id=data.get("radius_profile_id"),
            force="radius_profile_id" in fields_set
            or not bool(data.get("radius_profile_id")),
            sync_credentials=False,
        )
        db.add(subscription)
        db.commit()
        db.refresh(subscription)

        # Auto-create Service Order for pending subscriptions that need provisioning
        if subscription.status == SubscriptionStatus.pending:
            _create_service_order_for_subscription(db, subscription)

        # Emit subscription.created event
        emit_event(
            db,
            EventType.subscription_created,
            {
                "subscription_id": str(subscription.id),
                "offer_id": str(subscription.offer_id),
                "offer_name": subscription.offer.name if subscription.offer else None,
                "status": subscription.status.value if subscription.status else None,
            },
            subscription_id=subscription.id,
            account_id=subscription.subscriber_id,
        )

        # If created as active, generate credentials and sync to RADIUS FIRST
        # so the provisioning handler (triggered by the activation event) sees them.
        if subscription.status == SubscriptionStatus.active:
            _auto_generate_pppoe(db, subscription)

            # CRITICAL: Sync credentials to RADIUS BEFORE emitting events
            # This ensures provisioning handlers can access RadiusUser records
            _sync_credentials_to_radius(db, subscription.subscriber_id)

            emit_event(
                db,
                EventType.subscription_activated,
                {
                    "subscription_id": str(subscription.id),
                    "offer_name": subscription.offer.name
                    if subscription.offer
                    else None,
                    "from_status": None,
                    "to_status": "active",
                },
                subscription_id=subscription.id,
                account_id=subscription.subscriber_id,
            )

        # SQLite drops tzinfo even when DateTime(timezone=True), and emit_event()
        # commits may expire the instance. Normalize to UTC right before returning
        # so callers/tests can do arithmetic with aware datetimes.
        def _ensure_utc(value: datetime | None) -> datetime | None:
            if value is None:
                return None
            if value.tzinfo is None:
                return value.replace(tzinfo=UTC)
            return value

        subscription.start_at = _ensure_utc(subscription.start_at)
        subscription.end_at = _ensure_utc(subscription.end_at)
        subscription.next_billing_at = _ensure_utc(subscription.next_billing_at)
        subscription.canceled_at = _ensure_utc(subscription.canceled_at)

        return subscription

    @staticmethod
    def get(db: Session, subscription_id: str):
        subscription = db.get(
            Subscription,
            subscription_id,
            options=[
                selectinload(Subscription.offer),
                selectinload(Subscription.add_ons).selectinload(
                    SubscriptionAddOn.add_on
                ),
            ],
        )
        if not subscription:
            raise HTTPException(status_code=404, detail="Subscription not found")
        return subscription

    @staticmethod
    def list(
        db: Session,
        subscriber_id: str | None,
        offer_id: str | None,
        status: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(Subscription).options(
            selectinload(Subscription.offer),
            selectinload(Subscription.add_ons).selectinload(SubscriptionAddOn.add_on),
        )
        query = apply_optional_equals(
            query,
            {
                Subscription.subscriber_id: subscriber_id,
                Subscription.offer_id: offer_id,
            },
        )
        if status:
            query = query.filter(
                Subscription.status
                == validate_enum(status, SubscriptionStatus, "status")
            )
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {
                "created_at": Subscription.created_at,
                "status": Subscription.status,
                "start_at": Subscription.start_at,
                "next_billing_at": Subscription.next_billing_at,
            },
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, subscription_id: str, payload: SubscriptionUpdate):
        subscription = db.get(Subscription, subscription_id)
        if not subscription:
            raise HTTPException(status_code=404, detail="Subscription not found")
        # Track status before update for event emission
        previous_status = subscription.status
        previous_offer_id = subscription.offer_id
        previous_profile_id = subscription.radius_profile_id
        data = payload.model_dump(exclude_unset=True)
        subscriber_id = str(data.get("subscriber_id", subscription.subscriber_id))
        offer_id = str(data.get("offer_id", subscription.offer_id))
        offer_version_id = data.get("offer_version_id", subscription.offer_version_id)
        service_address_id = data.get(
            "service_address_id", subscription.service_address_id
        )
        catalog_validators.validate_subscription_links(
            db,
            subscriber_id,
            offer_id,
            str(offer_version_id) if offer_version_id else None,
            str(service_address_id) if service_address_id else None,
        )
        catalog_validators.validate_offer_active(db, offer_id)

        # Plan change validation and proration
        offer_changing = (
            "offer_id" in data
            and str(data["offer_id"]) != str(subscription.offer_id)
            and subscription.status == SubscriptionStatus.active
        )
        proration_result = None
        if offer_changing:
            _validate_plan_change(db, subscription, str(data["offer_id"]))
            proration_result = _calculate_proration(
                db, subscription, str(data["offer_id"])
            )
            proration_result = _apply_plan_change_policy(
                db,
                proration_result,
                old_price=_offer_recurring_price_amount(db, subscription.offer_id),
                new_price=_offer_recurring_price_amount(db, str(data["offer_id"])),
            )

        status = data.get("status", subscription.status)
        start_at = _ensure_utc(data.get("start_at", subscription.start_at))
        end_at = _ensure_utc(data.get("end_at", subscription.end_at))
        next_billing_at = _ensure_utc(
            data.get("next_billing_at", subscription.next_billing_at)
        )
        canceled_at = _ensure_utc(data.get("canceled_at", subscription.canceled_at))
        reference_at = start_at or datetime.now(UTC)
        if offer_version_id:
            catalog_validators.validate_offer_version_active(
                db,
                str(offer_version_id),
                offer_id,
                reference_at,
            )
        catalog_validators.validate_subscription_dates(
            status,
            start_at,
            end_at,
            next_billing_at,
            canceled_at,
        )
        catalog_validators.enforce_single_active_subscription(
            db, subscriber_id, status, subscription_id
        )
        if status == SubscriptionStatus.active and not start_at:
            start_at = datetime.now(UTC)
            data["start_at"] = start_at
        elif "offer_id" in data and not start_at:
            # Preserve historical behavior where plan changes establish
            # subscription start time if it was previously unset.
            start_at = datetime.now(UTC)
            data["start_at"] = start_at
        if status == SubscriptionStatus.active and start_at:
            cycle = _resolve_billing_cycle(
                db, offer_id, str(offer_version_id) if offer_version_id else None
            )
            existing_next = _ensure_utc(data.get("next_billing_at") or next_billing_at)
            now = datetime.now(UTC)
            # Recompute next_billing_at when:
            # 1. Not provided in form data, OR
            # 2. Resuming from suspension, OR
            # 3. The existing value is more than 60 days in the past (stale migration data)
            stale = existing_next and (now - existing_next).days > 60
            resuming = previous_status == SubscriptionStatus.suspended
            if "next_billing_at" not in data or resuming or stale:
                billing_anchor = now if (resuming or stale) else start_at
                data["next_billing_at"] = _compute_next_billing_at(
                    billing_anchor, cycle
                )
        if start_at and "end_at" not in data:
            term = data.get("contract_term", subscription.contract_term)
            end_at = _compute_contract_end_at(start_at, term)
            if end_at:
                data["end_at"] = end_at

        # Auto-select NAS device when activating if not already set
        if (
            status == SubscriptionStatus.active
            and not subscription.provisioning_nas_device_id
            and "provisioning_nas_device_id" not in data
        ):
            nas_device_id = _select_nas_for_subscriber(db, subscriber_id)
            if nas_device_id:
                data["provisioning_nas_device_id"] = nas_device_id

        for key, value in data.items():
            setattr(subscription, key, value)
        if "radius_profile_id" in data:
            apply_offer_radius_profile(
                db,
                subscription,
                previous_offer_id=previous_offer_id,
                target_profile_id=data["radius_profile_id"],
                force=True,
            )
        elif "offer_id" in data or subscription.radius_profile_id is None:
            apply_offer_radius_profile(
                db,
                subscription,
                previous_offer_id=previous_offer_id,
            )
        db.commit()
        db.refresh(subscription)

        # Handle lifecycle events based on status transitions
        new_status = subscription.status
        if previous_status != new_status:
            _handle_status_transition_via_lifecycle(
                db, subscription, previous_status, new_status
            )
        elif (
            previous_status == SubscriptionStatus.active
            and previous_profile_id != subscription.radius_profile_id
        ):
            _sync_credentials_to_radius(db, subscription.subscriber_id)
            try:
                from app.services.enforcement import update_subscription_sessions

                update_subscription_sessions(
                    db, str(subscription.id), reason="profile_change"
                )
            except Exception:
                logger.warning(
                    "Failed to refresh active sessions for subscription %s after profile change",
                    subscription.id,
                    exc_info=True,
                )

        # Emit upgrade/downgrade events when offer changes on an active subscription
        if (
            previous_offer_id
            and str(previous_offer_id) != str(subscription.offer_id)
            and subscription.status == SubscriptionStatus.active
        ):
            _emit_offer_change_event(db, subscription, str(previous_offer_id))

            # Generate proration invoice/credit for the plan change
            if (
                proration_result
                and proration_result.get("net_amount")
                and proration_result.get("generate_now")
            ):
                old_offer = db.get(CatalogOffer, previous_offer_id)
                new_offer = db.get(CatalogOffer, subscription.offer_id)
                _generate_proration_invoice(
                    db,
                    subscription,
                    proration_result,
                    old_offer.name if old_offer else "Previous Plan",
                    new_offer.name if new_offer else "New Plan",
                )
                db.commit()

        return subscription

    @staticmethod
    def delete(db: Session, subscription_id: str):
        subscription = db.get(Subscription, subscription_id)
        if not subscription:
            raise HTTPException(status_code=404, detail="Subscription not found")
        subscriber_id = subscription.subscriber_id
        offer_id = subscription.offer_id
        db.delete(subscription)
        db.commit()
        emit_event(
            db,
            EventType.subscription_deleted,
            {
                "subscription_id": str(subscription_id),
                "subscriber_id": str(subscriber_id) if subscriber_id else None,
                "offer_id": str(offer_id) if offer_id else None,
            },
        )

    @staticmethod
    def expire_subscriptions(
        db: Session,
        run_at: datetime | None = None,
        dry_run: bool = False,
    ) -> dict:
        """Expire subscriptions that have passed their end_at date.

        This should be run periodically (e.g., daily) to ensure subscriptions
        that have reached their contract end date are properly transitioned
        to expired status.

        Args:
            db: Database session
            run_at: Reference time (defaults to now)
            dry_run: If True, don't actually make changes

        Returns:
            Summary dict with counts
        """
        run_at = run_at or datetime.now(UTC)

        # Find subscriptions that should be expired
        subscriptions_to_expire = (
            db.query(Subscription)
            .filter(Subscription.end_at.is_not(None))
            .filter(Subscription.end_at <= run_at)
            .filter(
                Subscription.status.in_(
                    [SubscriptionStatus.active, SubscriptionStatus.suspended]
                )
            )
            .all()
        )

        expired_count = 0
        skipped_count = 0
        for subscription in subscriptions_to_expire:
            if not dry_run:
                try:
                    from app.services.account_lifecycle import expire_subscription

                    expire_subscription(db, str(subscription.id))
                except ValueError as e:
                    logger.info(
                        "Skipped expiring subscription %s: %s",
                        subscription.id,
                        e,
                    )
                    skipped_count += 1
                    continue

            expired_count += 1

        if not dry_run:
            db.commit()

        return {
            "run_at": run_at,
            "subscriptions_matched": len(subscriptions_to_expire),
            "subscriptions_expired": expired_count,
            "subscriptions_skipped": skipped_count,
            "dry_run": dry_run,
        }


class SubscriptionAddOns(CRUDManager[SubscriptionAddOn]):
    model = SubscriptionAddOn
    not_found_detail = "Subscription add-on not found"

    @staticmethod
    def create(db: Session, payload: SubscriptionAddOnCreate):
        catalog_validators.validate_subscription_add_on(
            db, str(payload.subscription_id), str(payload.add_on_id), payload.quantity
        )
        data = payload.model_dump()
        fields_set = payload.model_fields_set
        if "quantity" not in fields_set:
            default_quantity = settings_spec.resolve_value(
                db, SettingDomain.catalog, "default_addon_quantity"
            )
            if default_quantity:
                data["quantity"] = default_quantity
        subscription_add_on = SubscriptionAddOn(**data)
        db.add(subscription_add_on)
        db.commit()
        db.refresh(subscription_add_on)
        return subscription_add_on

    @classmethod
    def get(cls, db: Session, subscription_add_on_id: str):
        return super().get(db, subscription_add_on_id)

    @staticmethod
    def list(
        db: Session,
        subscription_id: str | None,
        add_on_id: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(SubscriptionAddOn)
        query = apply_optional_equals(
            query,
            {
                SubscriptionAddOn.subscription_id: subscription_id,
                SubscriptionAddOn.add_on_id: add_on_id,
            },
        )
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {
                "start_at": SubscriptionAddOn.start_at,
                "end_at": SubscriptionAddOn.end_at,
            },
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(
        db: Session, subscription_add_on_id: str, payload: SubscriptionAddOnUpdate
    ):
        subscription_add_on = db.get(SubscriptionAddOn, subscription_add_on_id)
        if not subscription_add_on:
            raise HTTPException(status_code=404, detail="Subscription add-on not found")
        data = payload.model_dump(exclude_unset=True)
        subscription_id = data.get(
            "subscription_id", subscription_add_on.subscription_id
        )
        add_on_id = data.get("add_on_id", subscription_add_on.add_on_id)
        quantity = data.get("quantity", subscription_add_on.quantity)
        catalog_validators.validate_subscription_add_on(
            db, str(subscription_id), str(add_on_id), quantity
        )
        for key, value in data.items():
            setattr(subscription_add_on, key, value)
        db.commit()
        db.refresh(subscription_add_on)
        return subscription_add_on

    @classmethod
    def delete(cls, db: Session, subscription_add_on_id: str):
        return super().delete(db, subscription_add_on_id)
