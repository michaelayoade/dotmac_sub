"""Subscription management services.

Provides services for Subscriptions and SubscriptionAddOns.
"""

from calendar import monthrange
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy.orm import Session, selectinload

from app.models.catalog import (
    BillingCycle,
    CatalogOffer,
    ContractTerm,
    BillingMode,
    OfferPrice,
    OfferVersionPrice,
    PriceType,
    Subscription,
    SubscriptionAddOn,
    SubscriptionStatus,
)
from app.models.domain_settings import SettingDomain
from app.services.common import apply_ordering, apply_pagination, validate_enum
from app.services.response import ListResponseMixin
from app.services import settings_spec
from app.services.events import emit_event
from app.services.events.types import EventType
from app.validators import catalog as catalog_validators
from app.schemas.catalog import (
    SubscriptionAddOnCreate,
    SubscriptionAddOnUpdate,
    SubscriptionCreate,
    SubscriptionUpdate,
)


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
        price = (
            db.query(OfferVersionPrice)
            .filter(OfferVersionPrice.offer_version_id == offer_version_id)
            .filter(OfferVersionPrice.price_type == PriceType.recurring)
            .filter(OfferVersionPrice.is_active.is_(True))
            .first()
        )
        if price and price.billing_cycle:
            return price.billing_cycle
    price = (
        db.query(OfferPrice)
        .filter(OfferPrice.offer_id == offer_id)
        .filter(OfferPrice.price_type == PriceType.recurring)
        .filter(OfferPrice.is_active.is_(True))
        .first()
    )
    if price and price.billing_cycle:
        return price.billing_cycle
    offer = db.get(CatalogOffer, offer_id)
    return offer.billing_cycle if offer and offer.billing_cycle else BillingCycle.monthly


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
        import logging
        logging.getLogger(__name__).warning(
            f"Failed to generate prorated invoice for subscription {subscription.id}: {exc}"
        )


def _sync_credentials_to_radius(db: Session, subscriber_id) -> None:
    """Sync all subscriber credentials to RADIUS on subscription activation."""
    try:
        from app.services.radius import sync_account_credentials_to_radius
        sync_account_credentials_to_radius(db, subscriber_id)
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning(
            f"Failed to sync credentials to RADIUS for subscriber {subscriber_id}: {exc}"
        )


def _emit_subscription_status_event(
    db: Session,
    subscription: Subscription,
    from_status: SubscriptionStatus | None,
    to_status: SubscriptionStatus | None,
) -> None:
    """Emit the appropriate event based on subscription status transition."""
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
    context = {
        "subscription_id": subscription.id,
        "account_id": subscription.subscriber_id,
    }

    # Map status transitions to event types
    if to_status == SubscriptionStatus.active:
        if from_status == SubscriptionStatus.suspended:
            emit_event(db, EventType.subscription_resumed, payload, **context)
        else:
            emit_event(db, EventType.subscription_activated, payload, **context)
            # Generate prorated invoice for new activations
            _generate_proration_if_enabled(db, subscription, from_status)

        # Sync credentials to RADIUS immediately on activation/resume
        _sync_credentials_to_radius(db, subscription.subscriber_id)

    elif to_status == SubscriptionStatus.suspended:
        emit_event(db, EventType.subscription_suspended, payload, **context)
    elif to_status == SubscriptionStatus.canceled:
        emit_event(db, EventType.subscription_canceled, payload, **context)


def _create_service_order_for_subscription(db: Session, subscription: Subscription):
    """Create a service order for a new subscription that needs provisioning."""
    from app.services import provisioning as provisioning_service
    from app.schemas.provisioning import ServiceOrderCreate
    from app.models.provisioning import ServiceOrderStatus

    # Account roles removed during consolidation; no contact linkage available here.
    requested_by_contact_id = None

    try:
        payload = ServiceOrderCreate(
            subscriber_id=subscription.subscriber_id,
            subscription_id=subscription.id,
            requested_by_contact_id=requested_by_contact_id,
            status=ServiceOrderStatus.submitted,
            notes=f"Auto-created for subscription: {subscription.offer.name if subscription.offer else subscription.id}",
        )
        provisioning_service.service_orders.create(db, payload)
    except Exception:
        # Don't fail subscription creation if service order creation fails
        pass


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
        reference_at = payload.start_at or datetime.now(timezone.utc)
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
            offer = db.get(CatalogOffer, str(payload.offer_id))
            data["billing_mode"] = (
                offer.billing_mode if offer and offer.billing_mode else BillingMode.prepaid
            )
        if "start_at" not in fields_set and data.get("status") == SubscriptionStatus.active:
            data["start_at"] = datetime.now(timezone.utc)
        start_at = data.get("start_at")
        if "next_billing_at" not in fields_set and start_at and data.get("status") == SubscriptionStatus.active:
            offer_version_id = data.get("offer_version_id")
            cycle = _resolve_billing_cycle(
                db, str(data["offer_id"]), str(offer_version_id) if offer_version_id else None
            )
            data["next_billing_at"] = _compute_next_billing_at(start_at, cycle)
        if "end_at" not in fields_set and start_at and data.get("contract_term"):
            end_at = _compute_contract_end_at(start_at, data["contract_term"])
            if end_at:
                data["end_at"] = end_at
        subscription = Subscription(**data)
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

        # If created as active, also emit activation event
        if subscription.status == SubscriptionStatus.active:
            emit_event(
                db,
                EventType.subscription_activated,
                {
                    "subscription_id": str(subscription.id),
                    "offer_name": subscription.offer.name if subscription.offer else None,
                    "from_status": None,
                    "to_status": "active",
                },
                subscription_id=subscription.id,
                account_id=subscription.subscriber_id,
            )

        return subscription

    @staticmethod
    def get(db: Session, subscription_id: str):
        subscription = db.get(
            Subscription,
            subscription_id,
            options=[
                selectinload(Subscription.offer),
                selectinload(Subscription.add_ons).selectinload(SubscriptionAddOn.add_on),
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
        if subscriber_id:
            query = query.filter(Subscription.subscriber_id == subscriber_id)
        if offer_id:
            query = query.filter(Subscription.offer_id == offer_id)
        if status:
            query = query.filter(
                Subscription.status == validate_enum(status, SubscriptionStatus, "status")
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
        status = data.get("status", subscription.status)
        start_at = data.get("start_at", subscription.start_at)
        end_at = data.get("end_at", subscription.end_at)
        next_billing_at = data.get("next_billing_at", subscription.next_billing_at)
        canceled_at = data.get("canceled_at", subscription.canceled_at)
        reference_at = start_at or datetime.now(timezone.utc)
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
            start_at = datetime.now(timezone.utc)
            data["start_at"] = start_at
        if status == SubscriptionStatus.active and start_at and "next_billing_at" not in data:
            cycle = _resolve_billing_cycle(
                db, offer_id, str(offer_version_id) if offer_version_id else None
            )
            data["next_billing_at"] = _compute_next_billing_at(start_at, cycle)
        if start_at and "end_at" not in data:
            term = data.get("contract_term", subscription.contract_term)
            end_at = _compute_contract_end_at(start_at, term)
            if end_at:
                data["end_at"] = end_at
        for key, value in data.items():
            setattr(subscription, key, value)
        db.commit()
        db.refresh(subscription)

        # Emit lifecycle events based on status transitions
        new_status = subscription.status
        if previous_status != new_status:
            _emit_subscription_status_event(
                db, subscription, previous_status, new_status
            )

        return subscription

    @staticmethod
    def delete(db: Session, subscription_id: str):
        subscription = db.get(Subscription, subscription_id)
        if not subscription:
            raise HTTPException(status_code=404, detail="Subscription not found")
        db.delete(subscription)
        db.commit()

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
        run_at = run_at or datetime.now(timezone.utc)

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
        for subscription in subscriptions_to_expire:
            if not dry_run:
                previous_status = subscription.status
                subscription.status = SubscriptionStatus.expired

                # Emit expiration event
                emit_event(
                    db,
                    EventType.subscription_expired,
                    {
                        "subscription_id": str(subscription.id),
                        "offer_name": subscription.offer.name if subscription.offer else None,
                        "from_status": previous_status.value if previous_status else None,
                        "to_status": "expired",
                        "reason": "contract_end",
                        "end_at": subscription.end_at.isoformat() if subscription.end_at else None,
                    },
                    subscription_id=subscription.id,
            account_id=subscription.subscriber_id,
        )

            expired_count += 1

        if not dry_run:
            db.commit()

        return {
            "run_at": run_at,
            "subscriptions_expired": expired_count,
            "dry_run": dry_run,
        }


class SubscriptionAddOns(ListResponseMixin):
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

    @staticmethod
    def get(db: Session, subscription_add_on_id: str):
        subscription_add_on = db.get(SubscriptionAddOn, subscription_add_on_id)
        if not subscription_add_on:
            raise HTTPException(status_code=404, detail="Subscription add-on not found")
        return subscription_add_on

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
        if subscription_id:
            query = query.filter(SubscriptionAddOn.subscription_id == subscription_id)
        if add_on_id:
            query = query.filter(SubscriptionAddOn.add_on_id == add_on_id)
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
        subscription_id = data.get("subscription_id", subscription_add_on.subscription_id)
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

    @staticmethod
    def delete(db: Session, subscription_add_on_id: str):
        subscription_add_on = db.get(SubscriptionAddOn, subscription_add_on_id)
        if not subscription_add_on:
            raise HTTPException(status_code=404, detail="Subscription add-on not found")
        db.delete(subscription_add_on)
        db.commit()
