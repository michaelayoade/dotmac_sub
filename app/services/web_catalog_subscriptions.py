"""Service helpers for admin catalog subscription web routes."""

from __future__ import annotations

import ipaddress
import logging
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from app.models.billing import InvoiceStatus
from app.models.catalog import (
    AccessCredential,
    BillingMode,
    ContractTerm,
    NasDevice,
    OfferStatus,
    RadiusProfile,
    Subscription,
    SubscriptionStatus,
)
from app.models.domain_settings import SettingDomain
from app.models.network import IPAssignment, IpBlock, IpPool, IPv4Address, IPVersion
from app.models.subscriber import ChannelType, Subscriber
from app.schemas.billing import InvoiceCreate, InvoiceLineCreate
from app.schemas.catalog import SubscriptionCreate, SubscriptionUpdate
from app.schemas.network import IPAssignmentCreate, IPAssignmentUpdate
from app.schemas.subscriber import SubscriberAccountCreate
from app.services import auth_flow as auth_flow_service
from app.services import billing as billing_service
from app.services import catalog as catalog_service
from app.services import email as email_service
from app.services import network as network_service
from app.services import settings_spec
from app.services import sms as sms_service
from app.services import subscriber as subscriber_service
from app.services.audit_helpers import (
    build_changes_metadata,
    log_audit_event,
)
from app.services.credential_crypto import decrypt_credential

logger = logging.getLogger(__name__)


def _form_str(form: FormData, key: str, default: str = "") -> str:
    value = form.get(key, default)
    return value if isinstance(value, str) else default


def _normalize_discount_type(value: object) -> str:
    raw = str(value or "").strip().lower()
    if raw == "percent":
        return "percentage"
    return raw


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
        "service_password": "",
        "ipv4_method": "permanent_static",
        "ipv4_block_ids": [],
        "ipv4_addresses": [],
    }


def parse_subscription_form(form: FormData, *, subscription_id: str | None = None) -> dict[str, object]:
    """Parse subscription form payload from request form."""
    ipv4_block_ids = [
        str(value).strip()
        for value in form.getlist("ipv4_block_ids")
        if str(value).strip()
    ]
    ipv4_addresses = [
        str(value).strip()
        for value in form.getlist("ipv4_addresses")
        if str(value).strip()
    ]
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
        "discount_type": _normalize_discount_type(_form_str(form, "discount_type").strip()),
        "service_status_raw": _form_str(form, "service_status_raw").strip(),
        "login": _form_str(form, "login").strip(),
        "ipv4_address": _form_str(form, "ipv4_address").strip(),
        "ipv6_address": _form_str(form, "ipv6_address").strip(),
        "mac_address": _form_str(form, "mac_address").strip(),
        "provisioning_nas_device_id": _form_str(form, "provisioning_nas_device_id").strip(),
        "radius_profile_id": _form_str(form, "radius_profile_id").strip(),
        "service_password": _form_str(form, "service_password").strip(),
        "ipv4_method": _form_str(form, "ipv4_method", "permanent_static").strip().lower() or "permanent_static",
        "ipv4_block_ids": ipv4_block_ids,
        "ipv4_addresses": ipv4_addresses,
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
    ipv4_method = str(subscription.get("ipv4_method") or "").strip().lower()
    if ipv4_method in {"permanent_static", "dynamic"}:
        subscription["service_status_raw"] = ipv4_method
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


def _subscriber_seq_from_number(subscriber_number: str | None) -> int:
    text = str(subscriber_number or "").strip()
    if "-" in text:
        text = text.rsplit("-", 1)[-1]
    digits = "".join(ch for ch in text if ch.isdigit())
    if not digits:
        return 0
    return int(digits)


def _generated_service_login(subscriber: Subscriber) -> str:
    seq = _subscriber_seq_from_number(subscriber.subscriber_number)
    return f"1{seq:07d}"


def _generated_service_password(subscriber: Subscriber) -> str:
    if subscriber.subscriber_number:
        return str(subscriber.subscriber_number)
    return str(subscriber.id)


def _pool_allows_network_broadcast(pool: IpPool | None) -> bool:
    notes = str(getattr(pool, "notes", "") or "")
    for raw_line in notes.splitlines():
        line = raw_line.strip().lower()
        if line == "[allow_network_broadcast:true]":
            return True
    return False


def _iter_block_ipv4_hosts(block: IpBlock) -> list[str]:
    try:
        network = ipaddress.ip_network(str(block.cidr), strict=False)
    except ValueError:
        return []
    if network.version != 4:
        return []
    if _pool_allows_network_broadcast(getattr(block, "pool", None)) or network.prefixlen >= 31:
        return [str(ip) for ip in network]
    return [str(ip) for ip in network.hosts()]


def _available_ipv4_strings_for_block(db: Session, *, block: IpBlock) -> list[str]:
    address_rows = (
        db.query(IPv4Address, IPAssignment)
        .outerjoin(
            IPAssignment,
            and_(
                IPAssignment.ipv4_address_id == IPv4Address.id,
                IPAssignment.is_active.is_(True),
            ),
        )
        .filter(IPv4Address.pool_id == block.pool_id)
        .all()
    )
    address_state: dict[str, tuple[IPv4Address, IPAssignment | None]] = {
        str(address.address): (address, assignment)
        for address, assignment in address_rows
    }
    available: list[str] = []
    for ip_text in _iter_block_ipv4_hosts(block):
        row = address_state.get(ip_text)
        if not row:
            available.append(ip_text)
            continue
        address, assignment = row
        if assignment is None and not bool(address.is_reserved):
            available.append(ip_text)
    return available


def _validate_unique_selected_ipv4s(selected_ips: list[str] | None) -> None:
    seen: set[str] = set()
    for raw_ip in selected_ips or []:
        ip_text = str(raw_ip or "").strip()
        if not ip_text:
            continue
        if ip_text in seen:
            raise ValueError(f"IPv4 address {ip_text} was selected more than once.")
        seen.add(ip_text)


def _nas_device_label(device: NasDevice | None) -> str:
    if not device:
        return ""
    label = str(device.name or "")
    if device.management_ip:
        return f"{label} ({device.management_ip})"
    if device.ip_address:
        return f"{label} ({device.ip_address})"
    if device.nas_ip:
        return f"{label} ({device.nas_ip})"
    return label


def apply_generated_service_credentials(db: Session, subscription: dict[str, object]) -> None:
    subscriber_id = str(subscription.get("subscriber_id") or subscription.get("account_id") or "")
    if not subscriber_id:
        return
    try:
        subscriber = subscriber_service.subscribers.get(db=db, subscriber_id=subscriber_id)
    except Exception:
        logger.warning(
            "Subscriber lookup failed during credential generation for %s",
            subscriber_id,
            exc_info=True,
        )
        return
    if not str(subscription.get("login") or "").strip():
        subscription["login"] = _generated_service_login(subscriber)
    if not subscription.get("id") and not str(subscription.get("service_password") or "").strip():
        subscription["service_password"] = _generated_service_password(subscriber)


def _upsert_access_credential(
    db: Session,
    *,
    subscriber_id: UUID,
    username: str,
    plain_password: str | None = None,
    radius_profile_id: str | None = None,
) -> None:
    credential = (
        db.query(AccessCredential)
        .filter(AccessCredential.subscriber_id == subscriber_id)
        .order_by(AccessCredential.created_at.desc())
        .first()
    )
    secret_hash = (
        auth_flow_service.hash_service_secret(plain_password)
        if plain_password
        else None
    )
    radius_profile_uuid: UUID | None = None
    if radius_profile_id:
        try:
            radius_profile_uuid = UUID(str(radius_profile_id))
        except ValueError:
            radius_profile_uuid = None
    if credential:
        credential.username = username
        if secret_hash:
            credential.secret_hash = secret_hash
        credential.is_active = True
        if radius_profile_uuid:
            credential.radius_profile_id = radius_profile_uuid
        db.commit()
        return
    if not secret_hash:
        return
    db.add(
        AccessCredential(
            subscriber_id=subscriber_id,
            username=username,
            secret_hash=secret_hash,
            is_active=True,
            radius_profile_id=radius_profile_uuid,
        )
    )
    db.commit()


def _current_access_credential(db: Session, subscriber_id: str | UUID | None) -> AccessCredential | None:
    if not subscriber_id:
        return None
    try:
        subscriber_uuid = UUID(str(subscriber_id))
    except ValueError:
        return None
    return (
        db.query(AccessCredential)
        .filter(AccessCredential.subscriber_id == subscriber_uuid)
        .order_by(AccessCredential.created_at.desc())
        .first()
    )


def _current_service_password(db: Session, subscriber_id: str | UUID | None) -> str | None:
    credential = _current_access_credential(db, subscriber_id)
    if not credential or not credential.secret_hash:
        return None
    try:
        return decrypt_credential(credential.secret_hash)
    except Exception:
        logger.warning(
            "Failed to decrypt service credential for subscriber %s",
            subscriber_id,
            exc_info=True,
        )
        return None


def _credential_contact_targets(subscriber: Subscriber) -> dict[str, list[str]]:
    emails: list[str] = []
    phones: list[str] = []

    def _push_unique(targets: list[str], value: str | None) -> None:
        text = str(value or "").strip()
        if text and text not in targets:
            targets.append(text)

    _push_unique(emails, subscriber.email)
    _push_unique(phones, subscriber.phone)
    for channel in getattr(subscriber, "channels", []) or []:
        channel_type = getattr(channel, "channel_type", None)
        if channel_type == ChannelType.email:
            _push_unique(emails, getattr(channel, "address", None))
        elif channel_type in {ChannelType.phone, ChannelType.sms}:
            _push_unique(phones, getattr(channel, "address", None))

    return {"email": emails, "sms": phones}


def send_subscription_credentials(
    db: Session,
    *,
    subscription_id: str,
) -> dict[str, object]:
    subscription = catalog_service.subscriptions.get(db=db, subscription_id=subscription_id)
    subscriber = db.get(Subscriber, subscription.subscriber_id)
    if not subscriber:
        raise ValueError("Subscriber not found for this subscription.")

    credential = _current_access_credential(db, subscription.subscriber_id)
    if not credential or not credential.username:
        raise ValueError("No service credential is stored for this subscriber.")

    password = _current_service_password(db, subscription.subscriber_id)
    if not password:
        raise ValueError("Current service password is not available for delivery.")

    targets = _credential_contact_targets(subscriber)
    if not targets["email"] and not targets["sms"]:
        raise ValueError("Subscriber has no email or SMS contact targets.")

    subject = "Your Internet service credentials"
    body_text = (
        f"Hello {subscriber.full_name},\n\n"
        f"Your service login is: {credential.username}\n"
        f"Your service password is: {password}\n\n"
        "Please keep these details secure."
    )
    body_html = (
        f"<p>Hello {subscriber.full_name},</p>"
        f"<p>Your service login is: <strong>{credential.username}</strong><br>"
        f"Your service password is: <strong>{password}</strong></p>"
        "<p>Please keep these details secure.</p>"
    )

    email_sent = 0
    sms_sent = 0
    for email in targets["email"]:
        email_service.send_email(
            db=db,
            to_email=email,
            subject=subject,
            body_html=body_html,
            body_text=body_text,
            activity="subscription_welcome",
        )
        email_sent += 1

    sms_body = (
        f"Service login: {credential.username} | "
        f"Password: {password}. Keep it secure."
    )
    for phone in targets["sms"]:
        if sms_service.send_sms(db, phone, sms_body, track=True):
            sms_sent += 1

    return {
        "email_sent": email_sent,
        "sms_sent": sms_sent,
        "email_targets": targets["email"],
        "sms_targets": targets["sms"],
    }


def _reconcile_active_subscription_after_credential_sync(
    db: Session, subscription_id: str | None
) -> None:
    if not subscription_id:
        return
    try:
        subscription = catalog_service.subscriptions.get(db=db, subscription_id=str(subscription_id))
    except Exception:
        logger.warning(
            "Subscription lookup failed during RADIUS reconcile for %s",
            subscription_id,
            exc_info=True,
        )
        return
    if subscription.status != SubscriptionStatus.active:
        return
    try:
        from app.services.radius import reconcile_subscription_connectivity

        reconcile_subscription_connectivity(db, str(subscription.id))
    except Exception:
        logger.warning(
            "RADIUS reconcile failed during subscription credential sync for %s",
            subscription.id,
            exc_info=True,
        )


def _resolve_ipv4_for_block(
    db: Session,
    *,
    block: IpBlock,
    requested_ip: str | None = None,
) -> IPv4Address | None:
    available_ips = _available_ipv4_strings_for_block(db, block=block)
    if not available_ips:
        return None
    selected_ip = str(requested_ip or "").strip() or available_ips[0]
    if selected_ip not in available_ips:
        raise ValueError(f"Selected IPv4 address {selected_ip} is not available in block {block.cidr}.")
    address = (
        db.query(IPv4Address)
        .filter(IPv4Address.address == selected_ip)
        .first()
    )
    if address:
        return address
    address = IPv4Address(address=selected_ip, pool_id=block.pool_id, is_reserved=False)
    db.add(address)
    db.commit()
    db.refresh(address)
    return address


def _append_block_usage_note(
    db: Session,
    *,
    block: IpBlock,
    subscriber: Subscriber,
    allocated_ip: str,
) -> None:
    display_name = (
        subscriber.display_name
        or f"{subscriber.first_name or ''} {subscriber.last_name or ''}".strip()
        or subscriber.subscriber_number
        or str(subscriber.id)
    )
    entry = f"{datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}: allocated {allocated_ip} to {display_name}"
    existing = str(block.notes or "").strip()
    block.notes = f"{existing}\n{entry}".strip() if existing else entry
    db.commit()


def _allocate_ipv4_assignments_for_subscription(
    db: Session,
    *,
    subscription_obj: Subscription,
    block_ids: list[str],
    selected_ips: list[str] | None = None,
) -> list[str]:
    if not block_ids:
        return []
    _validate_unique_selected_ipv4s(selected_ips)
    subscriber = db.get(Subscriber, subscription_obj.subscriber_id)
    if not subscriber:
        return []
    allocated: list[str] = []
    for index, block_id in enumerate(block_ids):
        try:
            block_uuid = UUID(str(block_id))
        except ValueError as exc:
            raise ValueError("Invalid IPv4 block selected.") from exc
        block = db.get(IpBlock, block_uuid)
        if not block or not block.is_active:
            raise ValueError("Selected IPv4 block is not active.")
        requested_ip = ""
        if selected_ips and index < len(selected_ips):
            requested_ip = str(selected_ips[index] or "").strip()
        address = _resolve_ipv4_for_block(
            db,
            block=block,
            requested_ip=requested_ip or None,
        )
        if not address:
            raise ValueError(f"No available IPv4 address in block {block.cidr}.")
        assignment_payload = {
            "account_id": subscription_obj.subscriber_id,
            "subscription_id": subscription_obj.id,
            "ip_version": IPVersion.ipv4,
            "ipv4_address_id": address.id,
            "is_active": True,
        }
        existing_assignment = getattr(address, "assignment", None)
        if existing_assignment:
            network_service.ip_assignments.update(
                db=db,
                assignment_id=str(existing_assignment.id),
                payload=IPAssignmentUpdate.model_validate(assignment_payload),
            )
        else:
            network_service.ip_assignments.create(
                db=db,
                payload=IPAssignmentCreate.model_validate(assignment_payload),
            )
        allocated_ip = str(address.address)
        allocated.append(allocated_ip)
        _append_block_usage_note(
            db,
            block=block,
            subscriber=subscriber,
            allocated_ip=allocated_ip,
        )
    return allocated


def ensure_ipv4_blocks_allocatable(
    db: Session,
    block_ids: list[str],
    selected_ips: list[str] | None = None,
) -> None:
    """Validate selected IPv4 blocks before subscription creation."""
    _validate_unique_selected_ipv4s(selected_ips)
    for index, block_id in enumerate(block_ids):
        try:
            block_uuid = UUID(str(block_id))
        except ValueError as exc:
            raise ValueError("Invalid IPv4 block selected.") from exc
        block = db.get(IpBlock, block_uuid)
        if not block or not block.is_active:
            raise ValueError("Selected IPv4 block is not active.")
        requested_ip = ""
        if selected_ips and index < len(selected_ips):
            requested_ip = str(selected_ips[index] or "").strip()
        address = _resolve_ipv4_for_block(
            db,
            block=block,
            requested_ip=requested_ip or None,
        )
        if not address:
            raise ValueError(f"No available IPv4 address in block {block.cidr}.")


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
        activity="subscription_welcome",
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
        "discount_type": _normalize_discount_type(
            subscription_obj.discount_type.value if subscription_obj.discount_type else ""
        ),
        "service_status_raw": subscription_obj.service_status_raw or "",
        "login": subscription_obj.login or "",
        "ipv4_address": subscription_obj.ipv4_address or "",
        "ipv6_address": subscription_obj.ipv6_address or "",
        "mac_address": subscription_obj.mac_address or "",
        "provisioning_nas_device_id": str(subscription_obj.provisioning_nas_device_id)
        if subscription_obj.provisioning_nas_device_id
        else "",
        "radius_profile_id": str(subscription_obj.radius_profile_id) if subscription_obj.radius_profile_id else "",
        "service_password": "",
        "ipv4_method": (
            "permanent_static"
            if (subscription_obj.service_status_raw or "").strip().lower() == "permanent_static"
            else "dynamic"
        ),
        "ipv4_block_ids": [],
        "ipv4_addresses": [subscription_obj.ipv4_address] if subscription_obj.ipv4_address else [],
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
        logger.warning(
            "Failed to resolve subscriber label for %s",
            subscriber_id,
            exc_info=True,
        )
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
    if not subscription.get("subscriber_id") and subscription.get("account_id"):
        subscription["subscriber_id"] = subscription.get("account_id")
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
    ipv4_pools = (
        db.query(IpPool)
        .filter(IpPool.ip_version == IPVersion.ipv4)
        .filter(IpPool.is_active.is_(True))
        .order_by(IpPool.name.asc())
        .all()
    )
    ipv4_blocks = (
        db.query(IpBlock)
        .join(IpPool, IpPool.id == IpBlock.pool_id)
        .filter(IpPool.ip_version == IPVersion.ipv4)
        .filter(IpPool.is_active.is_(True))
        .filter(IpBlock.is_active.is_(True))
        .order_by(IpPool.name.asc(), IpBlock.cidr.asc())
        .all()
    )
    block_options: list[dict[str, object]] = []
    for block in ipv4_blocks:
        in_block = _available_ipv4_strings_for_block(db, block=block)
        pool_name = getattr(block.pool, "name", "Pool")
        block_options.append(
            {
                "id": str(block.id),
                "pool_id": str(block.pool_id),
                "pool_name": pool_name,
                "cidr": str(block.cidr),
                "available_count": len(in_block),
                "available_ips": in_block,
                "display": f"{pool_name} - {block.cidr} ({len(in_block)} free)",
            }
        )

    rp_stmt = (
        select(RadiusProfile)
        .where(RadiusProfile.is_active.is_(True))
        .order_by(RadiusProfile.name)
    )
    radius_profiles = db.scalars(rp_stmt).all()

    subscriber_id = subscription.get("subscriber_id") if isinstance(subscription, dict) else None
    subscriber_label = _resolve_subscriber_label(db, str(subscriber_id or ""))
    current_password = _current_service_password(db, str(subscriber_id or ""))
    current_credential = _current_access_credential(db, str(subscriber_id or ""))
    credential_targets = None
    if subscriber_id:
        try:
            subscriber = subscriber_service.subscribers.get(db=db, subscriber_id=str(subscriber_id))
        except Exception:
            logger.warning("Subscriber lookup failed for form context: %s", subscriber_id, exc_info=True)
            subscriber = None
        if subscriber:
            credential_targets = _credential_contact_targets(subscriber)
    selected_router_label = ""
    provisioning_nas_device_id = str(subscription.get("provisioning_nas_device_id") or "").strip()
    if provisioning_nas_device_id:
        try:
            selected_router_label = _nas_device_label(
                catalog_service.nas_devices.get(db, provisioning_nas_device_id)
            )
        except Exception:
            logger.warning("NAS device lookup failed for %s", provisioning_nas_device_id, exc_info=True)
            selected_router_label = ""
    apply_generated_service_credentials(db, subscription)
    if not subscription.get("ipv4_method"):
        subscription["ipv4_method"] = "permanent_static"
    if not isinstance(subscription.get("ipv4_block_ids"), list):
        subscription["ipv4_block_ids"] = []
    if not isinstance(subscription.get("ipv4_addresses"), list):
        subscription["ipv4_addresses"] = []

    context: dict[str, object] = {
        "subscription": subscription,
        "accounts": accounts,
        "offers": offers,
        "nas_devices": nas_devices,
        "router_devices": nas_devices,
        "ipv4_pools": ipv4_pools,
        "ipv4_blocks": block_options,
        "radius_profiles": radius_profiles,
        "subscription_statuses": [item.value for item in SubscriptionStatus],
        "billing_modes": [item.value for item in BillingMode],
        "contract_terms": [item.value for item in ContractTerm],
        "action_url": "/admin/catalog/subscriptions",
        "subscriber_label": subscriber_label,
        "selected_router_label": selected_router_label,
        "billing_mode_help_text": settings_spec.resolve_value(
            db, SettingDomain.catalog, "billing_mode_help_text"
        ) or "Overrides tariff default.",
        "billing_mode_prepaid_notice": settings_spec.resolve_value(
            db, SettingDomain.catalog, "billing_mode_prepaid_notice"
        ) or "Balance enforcement applies.",
        "billing_mode_postpaid_notice": settings_spec.resolve_value(
            db, SettingDomain.catalog, "billing_mode_postpaid_notice"
        ) or "This subscription follows dunning steps.",
        "current_service_login": getattr(current_credential, "username", "") if current_credential else "",
        "current_service_password": current_password or "",
        "credential_targets": credential_targets or {"email": [], "sms": []},
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

    # Load active offers for bulk plan change modal
    offers = catalog_service.offers.list(
        db=db,
        service_type=None,
        access_type=None,
        status="active",
        is_active=True,
        order_by="name",
        order_dir="asc",
        limit=200,
        offset=0,
    )

    return {
        "subscriptions": subscriptions,
        "offers": offers,
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
        except Exception as exc:
            logger.error("Bulk status update failed for subscription %s: %s", sub_id, exc)
            continue

    return count


def bulk_change_plan(
    db: Session,
    subscription_ids_csv: str,
    target_offer_id: str,
    request: object,
    actor_id: str | None,
) -> int:
    """Bulk-change plan/offer for subscriptions, logging audit events.

    Only changes active subscriptions. Returns count of updated subscriptions.
    """
    from app.models.catalog import CatalogOffer

    target_offer = db.get(CatalogOffer, target_offer_id)
    if not target_offer:
        raise ValueError("Target offer not found")

    count = 0
    for sub_id in subscription_ids_csv.split(","):
        sub_id = sub_id.strip()
        if not sub_id:
            continue
        try:
            sub = catalog_service.subscriptions.get(db, sub_id)
            if sub and sub.status == SubscriptionStatus.active:
                payload = SubscriptionUpdate(offer_id=UUID(target_offer_id))
                catalog_service.subscriptions.update(
                    db=db, subscription_id=sub_id, payload=payload
                )
                log_audit_event(
                    db=db,
                    request=request,
                    action="change_plan",
                    entity_type="subscription",
                    entity_id=sub_id,
                    actor_id=actor_id,
                    metadata={"new_offer_id": target_offer_id, "offer_name": target_offer.name},
                )
                count += 1
        except Exception as exc:
            logger.error("Bulk plan change failed for subscription %s: %s", sub_id, exc)
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

    subscriber = db.get(Subscriber, created.subscriber_id)
    if subscriber:
        generated_login = _generated_service_login(subscriber)
        generated_password = _generated_service_password(subscriber)
        selected_login = str(form.get("login") or "").strip() or str(created.login or "").strip() or generated_login
        selected_password = str(form.get("service_password") or "").strip() or generated_password
        created = update_subscription(
            db,
            str(created.id),
            {
                "login": selected_login,
                "service_status_raw": "permanent_static"
                if str(form.get("ipv4_method") or "").strip().lower() == "permanent_static"
                else "dynamic",
            },
        )

    selected_block_ids = [
        str(value).strip()
        for value in form.getlist("ipv4_block_ids")
        if str(value).strip()
    ]
    selected_ips = [
        str(value).strip()
        for value in form.getlist("ipv4_addresses")
        if str(value).strip()
    ]
    allocated_ips = _allocate_ipv4_assignments_for_subscription(
        db,
        subscription_obj=created,
        block_ids=selected_block_ids,
        selected_ips=selected_ips,
    )
    if allocated_ips:
        created = update_subscription(
            db,
            str(created.id),
            {
                "ipv4_address": allocated_ips[0],
            },
        )

    if subscriber:
        try:
            _upsert_access_credential(
                db,
                subscriber_id=created.subscriber_id,
                username=selected_login,
                plain_password=selected_password,
                radius_profile_id=str(created.radius_profile_id) if created.radius_profile_id else None,
            )
        except Exception:
            logger.warning(
                "Access credential sync failed during subscription create for %s",
                created.id,
                exc_info=True,
            )
        else:
            _reconcile_active_subscription_after_credential_sync(db, str(created.id))

    log_audit_event(
        db=db,
        request=request,
        action="create",
        entity_type="subscription",
        entity_id=str(created.id),
        actor_id=actor_id,
        metadata={
            "offer_id": str(created.offer_id),
            "account_id": str(created.subscriber_id),
            "generated_login": created.login,
            "service_password_changed": bool(str(form.get("service_password") or "").strip()),
            "ipv4_method": str(form.get("ipv4_method") or "permanent_static"),
            "allocated_ipv4_count": len(allocated_ips),
            "allocated_ipv4_addresses": allocated_ips,
        },
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
    service_password: str | None,
    request: object,
    actor_id: str | None,
) -> object:
    """Update subscription, compute diff, and log audit.

    Returns the updated subscription ORM object.
    """
    before = catalog_service.subscriptions.get(db=db, subscription_id=subscription_id)
    update_subscription(db, subscription_id, payload_data)
    after = catalog_service.subscriptions.get(db=db, subscription_id=subscription_id)
    entered_password = str(service_password or "").strip()
    try:
        _upsert_access_credential(
            db,
            subscriber_id=after.subscriber_id,
            username=str(after.login or ""),
            plain_password=entered_password or None,
            radius_profile_id=str(after.radius_profile_id) if after.radius_profile_id else None,
        )
    except Exception:
        logger.warning(
            "Access credential sync failed during subscription update for %s",
            subscription_id,
            exc_info=True,
        )
    else:
        _reconcile_active_subscription_after_credential_sync(db, subscription_id)
    metadata_payload = build_changes_metadata(before, after) or {}
    metadata_payload["service_password_changed"] = bool(entered_password)

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
