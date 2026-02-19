"""Service helpers for admin catalog subscription web routes."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from app.models.billing import InvoiceStatus
from app.models.catalog import (
    BillingMode,
    ContractTerm,
    NasDevice,
    OfferStatus,
    RadiusProfile,
    Subscription,
    SubscriptionStatus,
)
from app.models.domain_settings import SettingDomain
from app.models.subscriber import Subscriber
from app.schemas.billing import InvoiceCreate, InvoiceLineCreate
from app.schemas.catalog import SubscriptionCreate, SubscriptionUpdate
from app.schemas.subscriber import SubscriberAccountCreate
from app.services import billing as billing_service
from app.services import catalog as catalog_service
from app.services import email as email_service
from app.services import settings_spec
from app.services import subscriber as subscriber_service
from app.services.audit_helpers import (
    build_changes_metadata,
    log_audit_event,
)

logger = logging.getLogger(__name__)


def _form_str(form: FormData, key: str, default: str = "") -> str:
    value = form.get(key, default)
    return value if isinstance(value, str) else default


def default_subscription_form(account_id: str, subscriber_id: str) -> dict[str, object]:
    """Return default values for subscription create form."""
    return {
        "account_id": account_id,
        "subscriber_id": subscriber_id,
        "offer_id": "",
        "status": SubscriptionStatus.pending.value,
        "billing_mode": "",
        "contract_term": ContractTerm.month_to_month.value,
        "start_at": "",
        "end_at": "",
        "next_billing_at": "",
        "canceled_at": "",
        "cancel_reason": "",
        "splynx_service_id": "",
        "router_id": "",
        "service_description": "",
        "quantity": "",
        "unit": "",
        "unit_price": "",
        "discount": False,
        "discount_value": "",
        "discount_type": "",
        "service_status_raw": "",
        "login": "",
        "ipv4_address": "",
        "ipv6_address": "",
        "mac_address": "",
        "provisioning_nas_device_id": "",
        "radius_profile_id": "",
    }


def parse_subscription_form(form: FormData, *, subscription_id: str | None = None) -> dict[str, object]:
    """Parse subscription form payload from request form."""
    data = {
        "account_id": _form_str(form, "account_id").strip(),
        "subscriber_id": _form_str(form, "subscriber_id").strip(),
        "offer_id": _form_str(form, "offer_id").strip(),
        "status": _form_str(form, "status").strip(),
        "billing_mode": _form_str(form, "billing_mode").strip(),
        "contract_term": _form_str(form, "contract_term").strip(),
        "start_at": _form_str(form, "start_at").strip(),
        "end_at": _form_str(form, "end_at").strip(),
        "next_billing_at": _form_str(form, "next_billing_at").strip(),
        "canceled_at": _form_str(form, "canceled_at").strip(),
        "cancel_reason": _form_str(form, "cancel_reason").strip(),
        "splynx_service_id": _form_str(form, "splynx_service_id").strip(),
        "router_id": _form_str(form, "router_id").strip(),
        "service_description": _form_str(form, "service_description").strip(),
        "quantity": _form_str(form, "quantity").strip(),
        "unit": _form_str(form, "unit").strip(),
        "unit_price": _form_str(form, "unit_price").strip(),
        "discount": form.get("discount") == "true",
        "discount_value": _form_str(form, "discount_value").strip(),
        "discount_type": _form_str(form, "discount_type").strip(),
        "service_status_raw": _form_str(form, "service_status_raw").strip(),
        "login": _form_str(form, "login").strip(),
        "ipv4_address": _form_str(form, "ipv4_address").strip(),
        "ipv6_address": _form_str(form, "ipv6_address").strip(),
        "mac_address": _form_str(form, "mac_address").strip(),
        "provisioning_nas_device_id": _form_str(form, "provisioning_nas_device_id").strip(),
        "radius_profile_id": _form_str(form, "radius_profile_id").strip(),
    }
    if subscription_id:
        data["id"] = subscription_id
    return data


def resolve_account_id(db: Session, subscription: dict[str, object]) -> str | None:
    """Resolve account from subscriber id when account is omitted."""
    account_id = str(subscription.get("account_id") or "")
    subscriber_id = str(subscription.get("subscriber_id") or "")
    if account_id:
        return None
    if not subscriber_id:
        return "Account or subscriber is required."
    try:
        subscriber_uuid = UUID(subscriber_id)
    except ValueError:
        return "Subscriber is invalid."

    accounts = subscriber_service.accounts.list(
        db=db,
        subscriber_id=str(subscriber_uuid),
        reseller_id=None,
        order_by="created_at",
        order_dir="desc",
        limit=1,
        offset=0,
    )
    if accounts:
        subscription["account_id"] = str(accounts[0].id)
        return None
    try:
        account = subscriber_service.accounts.create(
            db=db,
            payload=SubscriberAccountCreate(subscriber_id=subscriber_uuid),
        )
    except Exception as exc:
        return exc.detail if hasattr(exc, "detail") else str(exc)
    subscription["account_id"] = str(account.id)
    return None


def validate_subscription_form(subscription: dict[str, object], *, for_create: bool) -> str | None:
    """Validate required subscription form fields."""
    if for_create:
        if not subscription.get("account_id") and not subscription.get("subscriber_id"):
            return "Account or subscriber is required."
    else:
        if not subscription.get("account_id"):
            return "Account is required."
    if not subscription.get("offer_id"):
        return "Offer is required."
    return None


def build_payload_data(subscription: dict[str, object]) -> dict[str, object]:
    """Build Subscription create/update payload dict."""
    payload_data = {
        "account_id": subscription["account_id"],
        "offer_id": subscription["offer_id"],
        "discount": subscription["discount"],
    }
    optional_fields = [
        "status",
        "billing_mode",
        "contract_term",
        "start_at",
        "end_at",
        "next_billing_at",
        "canceled_at",
        "cancel_reason",
        "splynx_service_id",
        "router_id",
        "service_description",
        "quantity",
        "unit",
        "unit_price",
        "discount_value",
        "discount_type",
        "service_status_raw",
        "login",
        "ipv4_address",
        "ipv6_address",
        "mac_address",
        "provisioning_nas_device_id",
        "radius_profile_id",
    ]
    for field in optional_fields:
        value = subscription.get(field)
        if value:
            payload_data[field] = value
    return payload_data


def apply_create_quick_options(payload_data: dict[str, object], form: FormData) -> tuple[bool, bool, bool]:
    """Apply create quick options and return flags."""
    activate_immediately = form.get("activate_immediately") == "1"
    generate_invoice = form.get("generate_invoice") == "1"
    send_welcome_email = form.get("send_welcome_email") == "1"
    if activate_immediately:
        payload_data["status"] = "active"
        if not payload_data.get("start_at"):
            payload_data["start_at"] = datetime.now(UTC).isoformat()
    return activate_immediately, generate_invoice, send_welcome_email


def create_subscription(db: Session, payload_data: dict[str, object]):
    """Create subscription."""
    return catalog_service.subscriptions.create(
        db=db, payload=SubscriptionCreate.model_validate(payload_data)
    )


def update_subscription(db: Session, subscription_id: str, payload_data: dict[str, object]):
    """Update subscription."""
    return catalog_service.subscriptions.update(
        db=db,
        subscription_id=subscription_id,
        payload=SubscriptionUpdate.model_validate(payload_data),
    )


def create_invoice_for_subscription(db: Session, created: Subscription) -> None:
    """Generate initial invoice for subscription."""
    if not created.subscriber_id:
        return
    offer = catalog_service.offers.get(db=db, offer_id=str(created.offer_id))
    line_amount = Decimal("0.00")
    line_description = "Subscription"
    if offer:
        line_description = offer.name
        if offer.prices:
            line_amount = offer.prices[0].amount or Decimal("0.00")

    invoice_payload = InvoiceCreate(
        account_id=created.subscriber_id,
        status=InvoiceStatus.issued,
        issued_at=datetime.now(UTC),
    )
    invoice = billing_service.invoices.create(db=db, payload=invoice_payload)
    billing_service.invoice_lines.create(
        db,
        InvoiceLineCreate(
            invoice_id=invoice.id,
            description=line_description,
            quantity=Decimal("1"),
            unit_price=line_amount,
        ),
    )


def send_welcome_email_for_subscription(db: Session, created: Subscription) -> None:
    """Send welcome email when subscriber has email."""
    if not created.subscriber_id:
        return
    subscriber = db.get(Subscriber, created.subscriber_id)
    email_addr = subscriber.email if subscriber else None
    if not email_addr:
        return
    body_text = "Welcome! Your subscription is now set up."
    body_html = f"<p>{body_text}</p>"
    email_service.send_email(
        db=db,
        to_email=email_addr,
        subject="Welcome to your new subscription",
        body_html=body_html,
        body_text=body_text,
    )


def error_message(exc: Exception) -> str:
    """Normalize exception details for UI errors."""
    return exc.detail if hasattr(exc, "detail") else str(exc)


def edit_form_data(subscription_obj: Subscription) -> dict[str, object]:
    """Convert persisted subscription to form dict."""
    return {
        "id": str(subscription_obj.id),
        "account_id": str(subscription_obj.subscriber_id),
        "subscriber_id": str(subscription_obj.subscriber_id),
        "offer_id": str(subscription_obj.offer_id),
        "status": subscription_obj.status.value if subscription_obj.status else "",
        "billing_mode": subscription_obj.billing_mode.value if subscription_obj.billing_mode else "",
        "contract_term": subscription_obj.contract_term.value if subscription_obj.contract_term else "",
        "start_at": subscription_obj.start_at.strftime("%Y-%m-%dT%H:%M") if subscription_obj.start_at else "",
        "end_at": subscription_obj.end_at.strftime("%Y-%m-%dT%H:%M") if subscription_obj.end_at else "",
        "next_billing_at": subscription_obj.next_billing_at.strftime("%Y-%m-%dT%H:%M")
        if subscription_obj.next_billing_at
        else "",
        "canceled_at": subscription_obj.canceled_at.strftime("%Y-%m-%dT%H:%M") if subscription_obj.canceled_at else "",
        "cancel_reason": subscription_obj.cancel_reason or "",
        "splynx_service_id": subscription_obj.splynx_service_id or "",
        "router_id": subscription_obj.router_id or "",
        "service_description": subscription_obj.service_description or "",
        "quantity": subscription_obj.quantity or "",
        "unit": subscription_obj.unit or "",
        "unit_price": subscription_obj.unit_price or "",
        "discount": subscription_obj.discount,
        "discount_value": subscription_obj.discount_value or "",
        "discount_type": subscription_obj.discount_type or "",
        "service_status_raw": subscription_obj.service_status_raw or "",
        "login": subscription_obj.login or "",
        "ipv4_address": subscription_obj.ipv4_address or "",
        "ipv6_address": subscription_obj.ipv6_address or "",
        "mac_address": subscription_obj.mac_address or "",
        "provisioning_nas_device_id": str(subscription_obj.provisioning_nas_device_id)
        if subscription_obj.provisioning_nas_device_id
        else "",
        "radius_profile_id": str(subscription_obj.radius_profile_id) if subscription_obj.radius_profile_id else "",
    }


def _resolve_subscriber_label(db: Session, subscriber_id: str) -> str:
    """Look up a human-readable label for a subscriber."""
    if not subscriber_id:
        return ""
    try:
        subscriber = subscriber_service.subscribers.get(db=db, subscriber_id=subscriber_id)
        if subscriber.organization:
            label = str(subscriber.organization.name or "")
        else:
            label = (
                f"{subscriber.first_name} {subscriber.last_name}".strip()
                or subscriber.display_name
                or "Subscriber"
            )
        if subscriber.subscriber_number:
            label = f"{label} ({subscriber.subscriber_number})"
        return str(label)
    except Exception:
        return ""


def subscription_form_context(
    db: Session,
    subscription: dict[str, object],
    error: str | None = None,
) -> dict[str, object]:
    """Build context dict for the subscription create/edit form template.

    Returns all reference data (accounts, offers, NAS devices, RADIUS profiles,
    enum value lists, settings) needed by the form.
    """
    default_billing_mode = settings_spec.resolve_value(
        db, SettingDomain.catalog, "default_billing_mode"
    ) or BillingMode.prepaid.value
    if not subscription.get("billing_mode"):
        subscription["billing_mode"] = default_billing_mode

    accounts = subscriber_service.accounts.list(
        db=db,
        subscriber_id=None,
        reseller_id=None,
        order_by="created_at",
        order_dir="desc",
        limit=500,
        offset=0,
    )
    offers = catalog_service.offers.list(
        db=db,
        service_type=None,
        access_type=None,
        status=OfferStatus.active.value,
        is_active=True,
        order_by="created_at",
        order_dir="desc",
        limit=500,
        offset=0,
    )

    nas_stmt = (
        select(NasDevice)
        .where(NasDevice.is_active.is_(True))
        .order_by(NasDevice.name)
    )
    nas_devices = db.scalars(nas_stmt).all()

    rp_stmt = (
        select(RadiusProfile)
        .where(RadiusProfile.is_active.is_(True))
        .order_by(RadiusProfile.name)
    )
    radius_profiles = db.scalars(rp_stmt).all()

    subscriber_id = subscription.get("subscriber_id") if isinstance(subscription, dict) else None
    subscriber_label = _resolve_subscriber_label(db, str(subscriber_id or ""))

    context: dict[str, object] = {
        "subscription": subscription,
        "accounts": accounts,
        "offers": offers,
        "nas_devices": nas_devices,
        "radius_profiles": radius_profiles,
        "subscription_statuses": [item.value for item in SubscriptionStatus],
        "billing_modes": [item.value for item in BillingMode],
        "contract_terms": [item.value for item in ContractTerm],
        "action_url": "/admin/catalog/subscriptions",
        "subscriber_label": subscriber_label,
        "billing_mode_help_text": settings_spec.resolve_value(
            db, SettingDomain.catalog, "billing_mode_help_text"
        ) or "Overrides tariff default.",
        "billing_mode_prepaid_notice": settings_spec.resolve_value(
            db, SettingDomain.catalog, "billing_mode_prepaid_notice"
        ) or "Balance enforcement applies.",
        "billing_mode_postpaid_notice": settings_spec.resolve_value(
            db, SettingDomain.catalog, "billing_mode_postpaid_notice"
        ) or "This subscription follows dunning steps.",
    }
    if error:
        context["error"] = error
    return context


def subscriptions_list_page_data(
    db: Session,
    *,
    status: str | None = None,
    page: int = 1,
    per_page: int = 25,
) -> dict[str, object]:
    """Build page data for the subscriptions list route."""
    offset = (page - 1) * per_page
    subscriptions = catalog_service.subscriptions.list(
        db=db,
        subscriber_id=None,
        offer_id=None,
        status=status if status else None,
        order_by="created_at",
        order_dir="desc",
        limit=per_page,
        offset=offset,
    )

    count_stmt = select(func.count(Subscription.id))
    if status:
        count_stmt = count_stmt.where(Subscription.status == status)
    total: int = db.scalar(count_stmt) or 0
    total_pages = (total + per_page - 1) // per_page if total else 1

    return {
        "subscriptions": subscriptions,
        "status": status,
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
    }


def bulk_update_status(
    db: Session,
    subscription_ids_csv: str,
    target_status: SubscriptionStatus,
    allowed_from: list[SubscriptionStatus],
    request: object,
    actor_id: str | None,
) -> int:
    """Bulk-update subscription statuses, logging audit events.

    Only transitions subscriptions whose current status is in *allowed_from*.
    Returns the number of subscriptions successfully updated.
    """
    action_labels = {
        SubscriptionStatus.active: "activate",
        SubscriptionStatus.suspended: "suspend",
        SubscriptionStatus.canceled: "cancel",
    }
    action = action_labels.get(target_status, "update")
    count = 0

    for sub_id in subscription_ids_csv.split(","):
        sub_id = sub_id.strip()
        if not sub_id:
            continue
        try:
            sub = catalog_service.subscriptions.get(db, sub_id)
            if sub and sub.status in allowed_from:
                payload = SubscriptionUpdate(status=target_status)
                catalog_service.subscriptions.update(
                    db=db, subscription_id=sub_id, payload=payload
                )
                log_audit_event(
                    db=db,
                    request=request,
                    action=action,
                    entity_type="subscription",
                    entity_id=sub_id,
                    actor_id=actor_id,
                )
                count += 1
        except Exception:
            continue

    return count


def create_subscription_with_audit(
    db: Session,
    payload_data: dict[str, object],
    form: FormData,
    request: object,
    actor_id: str | None,
) -> object:
    """Create subscription with quick-options, invoice, welcome email, and audit.

    Returns the created subscription ORM object.
    """
    _, generate_invoice, send_welcome_email = apply_create_quick_options(
        payload_data, form
    )
    created = create_subscription(db, payload_data)

    log_audit_event(
        db=db,
        request=request,
        action="create",
        entity_type="subscription",
        entity_id=str(created.id),
        actor_id=actor_id,
        metadata={"offer_id": str(created.offer_id), "account_id": str(created.subscriber_id)},
    )

    if generate_invoice and created.subscriber_id:
        create_invoice_for_subscription(db, created)

    if send_welcome_email and created.subscriber_id:
        send_welcome_email_for_subscription(db, created)

    return created


def update_subscription_with_audit(
    db: Session,
    subscription_id: str,
    payload_data: dict[str, object],
    request: object,
    actor_id: str | None,
) -> object:
    """Update subscription, compute diff, and log audit.

    Returns the updated subscription ORM object.
    """
    before = catalog_service.subscriptions.get(db=db, subscription_id=subscription_id)
    update_subscription(db, subscription_id, payload_data)
    after = catalog_service.subscriptions.get(db=db, subscription_id=subscription_id)
    metadata_payload = build_changes_metadata(before, after)

    log_audit_event(
        db=db,
        request=request,
        action="update",
        entity_type="subscription",
        entity_id=str(subscription_id),
        actor_id=actor_id,
        metadata=metadata_payload,
    )

    return after
