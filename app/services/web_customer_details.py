"""Service helpers for web/admin customer detail pages."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, selectinload

from app.models.billing import Invoice, InvoiceStatus, Payment, PaymentStatus, TaxRate
from app.models.catalog import AccessCredential, Subscription, SubscriptionStatus
from app.models.collections import DunningCase, DunningCaseStatus
from app.models.communication_log import CommunicationLog
from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.network import CPEDevice, IPAssignment, OntAssignment
from app.models.provisioning import ServiceOrder, ServiceOrderStatus
from app.models.subscriber import (
    ChannelType,
    Reseller,
    Subscriber,
    SubscriberCategory,
    SubscriberChannel,
)
from app.models.support import Ticket, TicketStatus
from app.schemas.geocoding import GeocodePreviewRequest
from app.services import audit as audit_service
from app.services import catalog as catalog_service
from app.services import geocoding as geocoding_service
from app.services import notification as notification_service
from app.services import subscriber as subscriber_service
from app.services import web_customer_user_access as web_customer_user_access_service
from app.services.audit_helpers import extract_changes, format_changes
from app.services.billing_settings import resolve_payment_due_days

logger = logging.getLogger(__name__)

RESOLVED_TICKET_STATUSES = {
    TicketStatus.resolved,
    TicketStatus.closed,
    TicketStatus.canceled,
    TicketStatus.merged,
}
ACTIVE_SERVICE_ORDER_STATUSES = {
    ServiceOrderStatus.submitted,
    ServiceOrderStatus.scheduled,
    ServiceOrderStatus.provisioning,
}


def _coerce_setting_int(value: object | None) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return int(text)
        except ValueError:
            return None
    return None


def _coerce_setting_bool(
    value: object | None, default: bool | None = None
) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off"}:
            return False
    return default


def _coerce_setting_decimal(value: object | None) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int | float):
        return Decimal(str(value))
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return Decimal(text)
        except InvalidOperation:
            return None
    return None


def _format_billing_value(key: str, value: object | None) -> str:
    if value is None:
        return "Not set"
    if key == "billing_enabled":
        return "Enabled" if bool(value) else "Disabled"
    if key in {"billing_day", "payment_due_days", "grace_period_days"}:
        if key == "billing_day":
            return f"Day {value}"
        if key == "payment_due_days":
            return f"{value} day(s)"
        return f"{value} day(s)"
    if key == "min_balance":
        try:
            return f"NGN {Decimal(str(value)):,.2f}"
        except Exception:
            return str(value)
    return str(value)


def _billing_global_defaults(db: Session) -> dict[str, object | None]:
    keys = {"billing_enabled", "billing_day", "minimum_balance"}
    rows = (
        db.query(DomainSetting)
        .filter(DomainSetting.domain == SettingDomain.billing)
        .filter(DomainSetting.key.in_(keys))
        .all()
    )
    raw = {
        row.key: row.value_json if row.value_json is not None else row.value_text
        for row in rows
    }
    return {
        "billing_enabled": _coerce_setting_bool(raw.get("billing_enabled"), True),
        "billing_day": _coerce_setting_int(raw.get("billing_day")),
        "payment_due_days": resolve_payment_due_days(db),
        "min_balance": _coerce_setting_decimal(raw.get("minimum_balance")),
    }


def _resolve_tax_labels(db: Session, accounts: list[Subscriber]) -> dict[str, str]:
    tax_ids = {
        account.tax_rate_id
        for account in accounts
        if getattr(account, "tax_rate_id", None)
    }
    if not tax_ids:
        return {}
    rates = db.query(TaxRate).filter(TaxRate.id.in_(tax_ids)).all()
    return {str(rate.id): rate.name for rate in rates}


def _billing_policy_snapshot(
    db: Session, accounts: list[Subscriber]
) -> dict[str, object]:
    global_defaults = _billing_global_defaults(db)
    tax_labels = _resolve_tax_labels(db, accounts)
    fields = [
        ("billing_enabled", "Billing", True),
        ("billing_day", "Billing Day", True),
        ("payment_due_days", "Payment Due", True),
        ("grace_period_days", "Grace Period", False),
        ("min_balance", "Minimum Balance", True),
        ("tax_rate", "Tax Rate", False),
    ]
    rows: list[dict[str, object]] = []
    has_overrides = False
    has_mixed = False

    for key, label, uses_global in fields:
        override: object | None
        raw_values: list[object | None] = []
        effective_values: list[object | None] = []
        for account in accounts:
            if key == "tax_rate":
                raw = (
                    tax_labels.get(str(account.tax_rate_id))
                    if getattr(account, "tax_rate_id", None)
                    else None
                )
            else:
                raw = getattr(account, key, None)
            raw_values.append(raw)
            effective_values.append(
                raw
                if raw is not None
                else (global_defaults.get(key) if uses_global else None)
            )

        unique_effective = {
            str(value) for value in effective_values if value is not None
        }
        unique_raw = {str(value) for value in raw_values if value is not None}
        if not accounts:
            source = "Global default" if uses_global else "Not set"
            effective = global_defaults.get(key) if uses_global else None
            override = None
        elif len(unique_effective) > 1 or (len(unique_raw) > 1):
            source = "Mixed"
            effective = "Mixed"
            override = "Mixed"
            has_mixed = True
        else:
            resolved_raw: object | None = next(
                (value for value in raw_values if value is not None),
                None,
            )
            effective = effective_values[0] if effective_values else None
            override = resolved_raw
            if resolved_raw is None:
                source = "Global default" if uses_global else "Not set"
            else:
                source = "Customer override"
                has_overrides = True

        rows.append(
            {
                "key": key,
                "label": label,
                "effective": effective
                if effective == "Mixed"
                else _format_billing_value(key, effective),
                "source": source,
                "global": _format_billing_value(key, global_defaults.get(key))
                if uses_global
                else "Not set",
                "override": override
                if override == "Mixed"
                else _format_billing_value(key, override),
            }
        )

    return {
        "rows": rows,
        "has_overrides": has_overrides,
        "has_mixed": has_mixed,
        "account_count": len(accounts),
    }


def _dedupe_accounts(accounts):
    unique = {}
    for account in accounts:
        unique[str(account.id)] = account
    return list(unique.values())


def _list_subscriptions_for_accounts(db: Session, accounts):
    if not accounts:
        return []
    subscriptions = []
    for account in accounts:
        try:
            account_subs = catalog_service.subscriptions.list(
                db=db,
                subscriber_id=str(account.id),
                offer_id=None,
                status=None,
                order_by="created_at",
                order_dir="desc",
                limit=200,
                offset=0,
            )
            subscriptions.extend(account_subs)
        except Exception:
            logger.debug(
                "Skipping subscriptions for linked account %s",
                account.id,
                exc_info=True,
            )
            continue
    return subscriptions


def _format_contact_channel(
    subscriber: Subscriber, channel: SubscriberChannel
) -> dict[str, object]:
    return {
        "id": str(channel.id),
        "first_name": subscriber.first_name or "",
        "last_name": subscriber.last_name or "",
        "role": None,
        "title": channel.label,
        "is_primary": channel.is_primary,
        "email": channel.address if channel.channel_type == ChannelType.email else "",
        "phone": channel.address if channel.channel_type == ChannelType.phone else "",
    }


def _enum_label(value: object) -> str:
    raw_value = getattr(value, "value", value)
    if raw_value is None:
        return ""
    return str(raw_value).replace("_", " ").title()


def _event_timestamp(*values: datetime | None) -> datetime | None:
    for value in values:
        if value is not None:
            return value
    return None


def _timeline_sort_key(item: dict[str, object]) -> datetime:
    timestamp = item.get("timestamp")
    if isinstance(timestamp, datetime):
        return timestamp
    return datetime.min.replace(tzinfo=UTC)


def _build_activity_items(
    db: Session,
    entity_type: str,
    entity_id: str,
    account_ids: list[UUID],
    subscriptions: list[Subscription],
) -> list[dict[str, object]]:
    audit_events = audit_service.audit_events.list(
        db=db,
        actor_id=None,
        actor_type=None,
        action=None,
        entity_type=entity_type,
        entity_id=entity_id,
        request_id=None,
        is_success=None,
        status_code=None,
        is_active=None,
        order_by="occurred_at",
        order_dir="desc",
        limit=10,
        offset=0,
    )
    actor_ids = {
        str(event.actor_id)
        for event in audit_events
        if getattr(event, "actor_id", None)
    }
    people = {}
    if actor_ids:
        people = {
            str(person.id): person
            for person in db.query(Subscriber)
            .filter(Subscriber.id.in_(actor_ids))
            .all()
        }
    activity_items: list[dict[str, object]] = []

    if account_ids:
        invoices = (
            db.query(Invoice)
            .filter(Invoice.account_id.in_(account_ids))
            .order_by(func.coalesce(Invoice.issued_at, Invoice.created_at).desc())
            .limit(8)
            .all()
        )
        payments = (
            db.query(Payment)
            .filter(Payment.account_id.in_(account_ids))
            .order_by(func.coalesce(Payment.paid_at, Payment.created_at).desc())
            .limit(8)
            .all()
        )
        support_tickets = (
            db.query(Ticket)
            .filter(
                or_(
                    Ticket.subscriber_id.in_(account_ids),
                    Ticket.customer_account_id.in_(account_ids),
                    Ticket.customer_person_id.in_(account_ids),
                )
            )
            .order_by(Ticket.updated_at.desc())
            .limit(8)
            .all()
        )
        communication_logs = (
            db.query(CommunicationLog)
            .filter(CommunicationLog.subscriber_id.in_(account_ids))
            .order_by(
                func.coalesce(
                    CommunicationLog.sent_at, CommunicationLog.created_at
                ).desc()
            )
            .limit(8)
            .all()
        )
        service_orders = (
            db.query(ServiceOrder)
            .filter(ServiceOrder.subscriber_id.in_(account_ids))
            .order_by(ServiceOrder.updated_at.desc())
            .limit(8)
            .all()
        )
        dunning_cases = (
            db.query(DunningCase)
            .filter(DunningCase.account_id.in_(account_ids))
            .order_by(
                func.coalesce(
                    DunningCase.resolved_at,
                    DunningCase.updated_at,
                    DunningCase.started_at,
                ).desc()
            )
            .limit(8)
            .all()
        )
    else:
        invoices = []
        payments = []
        support_tickets = []
        communication_logs = []
        service_orders = []
        dunning_cases = []

    for invoice in invoices:
        amount = invoice.total if invoice.total is not None else 0
        activity_items.append(
            {
                "type": "invoice",
                "title": f"Invoice {invoice.invoice_number or 'created'}",
                "description": _enum_label(invoice.status),
                "timestamp": _event_timestamp(invoice.issued_at, invoice.created_at),
                "amount": float(amount),
                "link": f"/admin/billing/invoices/{invoice.id}",
            }
        )

    for payment in payments:
        activity_items.append(
            {
                "type": "payment",
                "title": "Payment received"
                if payment.status == PaymentStatus.succeeded
                else "Payment update",
                "description": _enum_label(payment.status),
                "timestamp": _event_timestamp(payment.paid_at, payment.created_at),
                "amount": float(payment.amount or 0),
            }
        )

    for subscription in subscriptions[:8]:
        account_label = (
            subscription.login or subscription.ipv4_address or subscription.ipv6_address
        )
        description = _enum_label(subscription.status)
        if account_label:
            description = (
                f"{description} · {account_label}" if description else account_label
            )
        activity_items.append(
            {
                "type": "subscription",
                "title": subscription.offer.name
                if subscription.offer
                else "Subscription updated",
                "description": description,
                "timestamp": _event_timestamp(
                    subscription.updated_at,
                    subscription.next_billing_at,
                    subscription.start_at,
                    subscription.created_at,
                ),
                "amount": float(subscription.unit_price or 0)
                if subscription.unit_price is not None
                else None,
                "link": f"/admin/catalog/subscriptions/{subscription.id}",
            }
        )

    for ticket in support_tickets:
        description = " · ".join(
            part
            for part in (_enum_label(ticket.status), _enum_label(ticket.priority))
            if part
        )
        activity_items.append(
            {
                "type": "ticket",
                "title": ticket.title or ticket.number or "Support ticket",
                "description": description,
                "timestamp": _event_timestamp(ticket.updated_at, ticket.created_at),
                "link": f"/admin/support/tickets/{ticket.id}",
            }
        )

    for log in communication_logs:
        subject = (
            log.subject
            or log.recipient
            or log.sender
            or _enum_label(log.channel)
            or "Communication"
        )
        description = " · ".join(
            part
            for part in (
                _enum_label(log.channel),
                _enum_label(log.direction),
                _enum_label(log.status),
            )
            if part
        )
        activity_items.append(
            {
                "type": "communication",
                "title": subject,
                "description": description,
                "timestamp": _event_timestamp(log.sent_at, log.created_at),
            }
        )

    for order in service_orders:
        title = f"{_enum_label(order.order_type) or 'Service'} order"
        description = _enum_label(order.status)
        activity_items.append(
            {
                "type": "service_order",
                "title": title,
                "description": description,
                "timestamp": _event_timestamp(order.updated_at, order.created_at),
                "link": f"/admin/provisioning/orders/{order.id}",
            }
        )

    for case in dunning_cases:
        description_parts = [_enum_label(case.status)]
        if case.current_step is not None:
            description_parts.append(f"Step {case.current_step}")
        activity_items.append(
            {
                "type": "dunning",
                "title": "Dunning case",
                "description": " · ".join(part for part in description_parts if part),
                "timestamp": _event_timestamp(
                    case.resolved_at, case.updated_at, case.started_at, case.created_at
                ),
            }
        )

    for event in audit_events:
        actor = (
            people.get(str(event.actor_id))
            if getattr(event, "actor_id", None)
            else None
        )
        actor_name = (
            f"{actor.first_name} {actor.last_name}".strip() if actor else "System"
        )
        metadata = getattr(event, "metadata_", None) or {}
        changes = extract_changes(metadata, getattr(event, "action", None))
        change_summary = format_changes(changes, max_items=2)
        description_parts = [actor_name]
        if change_summary:
            description_parts.append(change_summary)
        activity_items.append(
            {
                "type": "audit",
                "title": (event.action or "Activity").replace("_", " ").title(),
                "description": " · ".join(description_parts),
                "timestamp": event.occurred_at,
            }
        )

    activity_items.sort(key=_timeline_sort_key, reverse=True)
    return activity_items[:20]


def _build_common_financials(db: Session, account_ids):
    invoices = []
    payments = []
    if account_ids:
        invoices = (
            db.query(Invoice)
            .filter(Invoice.account_id.in_(account_ids))
            .order_by(Invoice.created_at.desc())
            .limit(10)
            .all()
        )
        payments = (
            db.query(Payment)
            .filter(Payment.account_id.in_(account_ids))
            .order_by(Payment.created_at.desc())
            .limit(10)
            .all()
        )

    balance_due = sum(
        float(getattr(inv, "balance_due", 0) or 0)
        for inv in invoices
        if inv.status
        in (InvoiceStatus.issued, InvoiceStatus.partially_paid, InvoiceStatus.overdue)
    )

    total_invoiced = 0
    total_paid = 0
    overdue_invoices = 0
    last_payment = None
    last_invoice = None
    if account_ids:
        total_invoiced = (
            db.query(func.coalesce(func.sum(Invoice.total), 0))
            .filter(Invoice.account_id.in_(account_ids))
            .scalar()
            or 0
        )
        total_paid = (
            db.query(func.coalesce(func.sum(Payment.amount), 0))
            .filter(Payment.account_id.in_(account_ids))
            .filter(Payment.status == PaymentStatus.succeeded)
            .scalar()
            or 0
        )
        overdue_invoices = (
            db.query(func.count(Invoice.id))
            .filter(Invoice.account_id.in_(account_ids))
            .filter(Invoice.status == InvoiceStatus.overdue)
            .scalar()
            or 0
        )
        last_payment = (
            db.query(Payment)
            .filter(Payment.account_id.in_(account_ids))
            .filter(Payment.status == PaymentStatus.succeeded)
            .order_by(func.coalesce(Payment.paid_at, Payment.created_at).desc())
            .first()
        )
        last_invoice = (
            db.query(Invoice)
            .filter(Invoice.account_id.in_(account_ids))
            .order_by(func.coalesce(Invoice.issued_at, Invoice.created_at).desc())
            .first()
        )

    return {
        "invoices": invoices,
        "payments": payments,
        "balance_due": balance_due,
        "financials": {
            "total_invoiced": total_invoiced,
            "total_paid": total_paid,
            "overdue_invoices": overdue_invoices,
            "last_payment": last_payment,
            "last_invoice": last_invoice,
        },
    }


def _build_relationship_data(db: Session, account_ids: list[UUID]) -> dict[str, object]:
    if not account_ids:
        empty_summary: dict[str, int] = {
            "open_tickets": 0,
            "recent_communications": 0,
            "active_service_orders": 0,
            "open_dunning_cases": 0,
            "active_credentials": 0,
            "active_cpes": 0,
            "active_onts": 0,
            "active_ip_assignments": 0,
            "linked_resellers": 0,
        }
        return {
            "support_tickets": [],
            "communication_logs": [],
            "service_orders": [],
            "dunning_cases": [],
            "access_credentials": [],
            "cpe_devices": [],
            "ip_assignments": [],
            "ont_assignments": [],
            "linked_resellers": [],
            "relationship_summary": empty_summary,
        }

    support_tickets = (
        db.query(Ticket)
        .filter(
            or_(
                Ticket.subscriber_id.in_(account_ids),
                Ticket.customer_account_id.in_(account_ids),
                Ticket.customer_person_id.in_(account_ids),
            )
        )
        .order_by(Ticket.updated_at.desc())
        .limit(10)
        .all()
    )
    communication_logs = (
        db.query(CommunicationLog)
        .filter(CommunicationLog.subscriber_id.in_(account_ids))
        .order_by(
            func.coalesce(CommunicationLog.sent_at, CommunicationLog.created_at).desc()
        )
        .limit(12)
        .all()
    )
    service_orders = (
        db.query(ServiceOrder)
        .filter(ServiceOrder.subscriber_id.in_(account_ids))
        .order_by(ServiceOrder.updated_at.desc())
        .limit(10)
        .all()
    )
    dunning_cases = (
        db.query(DunningCase)
        .filter(DunningCase.account_id.in_(account_ids))
        .order_by(DunningCase.updated_at.desc())
        .limit(10)
        .all()
    )
    access_credentials = (
        db.query(AccessCredential)
        .filter(AccessCredential.subscriber_id.in_(account_ids))
        .order_by(AccessCredential.updated_at.desc())
        .limit(10)
        .all()
    )
    cpe_devices = (
        db.query(CPEDevice)
        .filter(CPEDevice.subscriber_id.in_(account_ids))
        .order_by(CPEDevice.updated_at.desc())
        .limit(10)
        .all()
    )
    ip_assignments = (
        db.query(IPAssignment)
        .options(
            selectinload(IPAssignment.ipv4_address),
            selectinload(IPAssignment.ipv6_address),
        )
        .filter(IPAssignment.subscriber_id.in_(account_ids))
        .order_by(IPAssignment.updated_at.desc())
        .limit(10)
        .all()
    )
    ont_assignments = (
        db.query(OntAssignment)
        .options(
            selectinload(OntAssignment.ont_unit),
            selectinload(OntAssignment.pon_port),
        )
        .filter(OntAssignment.subscriber_id.in_(account_ids))
        .order_by(OntAssignment.updated_at.desc())
        .limit(10)
        .all()
    )

    reseller_ids = {
        account.reseller_id
        for account in db.query(Subscriber)
        .options(selectinload(Subscriber.reseller))
        .filter(Subscriber.id.in_(account_ids))
        .all()
        if getattr(account, "reseller_id", None)
    }
    linked_resellers = (
        db.query(Reseller)
        .filter(Reseller.id.in_(list(reseller_ids)))
        .order_by(Reseller.name.asc())
        .all()
        if reseller_ids
        else []
    )

    relationship_summary: dict[str, int] = {
        "open_tickets": sum(
            1
            for ticket in support_tickets
            if ticket.status not in RESOLVED_TICKET_STATUSES
        ),
        "recent_communications": len(communication_logs),
        "active_service_orders": sum(
            1
            for order in service_orders
            if order.status in ACTIVE_SERVICE_ORDER_STATUSES
        ),
        "open_dunning_cases": sum(
            1
            for case in dunning_cases
            if case.status in {DunningCaseStatus.open, DunningCaseStatus.paused}
        ),
        "active_credentials": sum(
            1 for credential in access_credentials if credential.is_active
        ),
        "active_cpes": sum(
            1
            for cpe in cpe_devices
            if str(getattr(cpe, "status", "")) == "DeviceStatus.active"
            or getattr(getattr(cpe, "status", None), "value", None) == "active"
        ),
        "active_onts": sum(1 for assignment in ont_assignments if assignment.active),
        "active_ip_assignments": sum(
            1 for assignment in ip_assignments if assignment.is_active
        ),
        "linked_resellers": len(linked_resellers),
    }

    return {
        "support_tickets": support_tickets,
        "communication_logs": communication_logs,
        "service_orders": service_orders,
        "dunning_cases": dunning_cases,
        "access_credentials": access_credentials,
        "cpe_devices": cpe_devices,
        "ip_assignments": ip_assignments,
        "ont_assignments": ont_assignments,
        "linked_resellers": linked_resellers,
        "relationship_summary": relationship_summary,
    }


def _build_person_fallback_address(db: Session, customer: Subscriber):
    if (customer.address_line1 or "").strip() == "":
        return []

    customer_meta = getattr(customer, "metadata_", None) or {}
    customer_lat = getattr(customer, "latitude", None)
    customer_lng = getattr(customer, "longitude", None)

    def _clean_value(value):
        if isinstance(value, str):
            trimmed = value.strip()
            return None if not trimmed or trimmed.lower() == "none" else trimmed
        return value

    address_line1 = (customer.address_line1 or "").strip()
    address_line2 = _clean_value(getattr(customer, "address_line2", None))
    city = _clean_value(getattr(customer, "city", None))
    region = _clean_value(getattr(customer, "region", None))
    postal_code = _clean_value(getattr(customer, "postal_code", None))
    country_code = _clean_value(getattr(customer, "country_code", None))
    if customer_lat is None:
        customer_lat = customer_meta.get("latitude")
    if customer_lng is None:
        customer_lng = customer_meta.get("longitude")

    if customer_lat is None or customer_lng is None:
        try:
            payload = GeocodePreviewRequest(
                address_line1=address_line1,
                address_line2=address_line2,
                city=city,
                region=region,
                postal_code=postal_code,
                country_code=country_code,
                limit=1,
            )
            results = geocoding_service.geocode_preview_from_request(db, payload)
            if results:
                first = results[0] or {}
                lat_value = first.get("latitude")
                lng_value = first.get("longitude")
                if lat_value is not None and lng_value is not None:
                    customer_lat = float(lat_value)
                    customer_lng = float(lng_value)
                    if getattr(customer, "metadata_", None) is None:
                        customer.metadata_ = {}
                    if isinstance(customer.metadata_, dict):
                        customer.metadata_["latitude"] = customer_lat
                        customer.metadata_["longitude"] = customer_lng
                        try:
                            db.add(customer)
                            db.commit()
                        except Exception:
                            db.rollback()
        except Exception:
            logger.debug(
                "Fallback geocoding preview failed for customer %s",
                customer.id,
                exc_info=True,
            )

    return [
        SimpleNamespace(
            id=None,
            is_primary=True,
            address_line1=address_line1,
            address_line2=address_line2,
            city=city,
            region=region,
            postal_code=postal_code,
            country_code=country_code,
            latitude=customer_lat,
            longitude=customer_lng,
            created_at=None,
        )
    ]


def _build_map_payload(primary_address, customer_name: str):
    map_data = None
    geocode_target = None
    if primary_address and (primary_address.address_line1 or "").strip():
        if (
            getattr(primary_address, "latitude", None) is not None
            and getattr(primary_address, "longitude", None) is not None
        ):
            map_data = {
                "center": [primary_address.latitude, primary_address.longitude],
                "geojson": {
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "geometry": {
                                "type": "Point",
                                "coordinates": [
                                    primary_address.longitude,
                                    primary_address.latitude,
                                ],
                            },
                            "properties": {
                                "type": "customer",
                                "name": customer_name,
                                "address": primary_address.address_line1,
                            },
                        }
                    ],
                },
            }
        else:
            target_id = getattr(primary_address, "id", None)
            geocode_target = {
                "id": str(target_id) if target_id is not None else None,
                "address_line1": primary_address.address_line1,
                "address_line2": primary_address.address_line2,
                "city": primary_address.city,
                "region": primary_address.region,
                "postal_code": primary_address.postal_code,
                "country_code": primary_address.country_code,
                "payload": {
                    "address_line1": primary_address.address_line1,
                    "address_line2": primary_address.address_line2 or "",
                    "city": primary_address.city or "",
                    "region": primary_address.region or "",
                    "postal_code": primary_address.postal_code or "",
                    "country_code": primary_address.country_code or "",
                },
            }
            geocode_target["payload_json"] = json.dumps(geocode_target["payload"])
    return map_data, geocode_target


def _build_network_access_cards(subscriptions: list) -> list[dict]:
    """Build network access info cards from active subscriptions."""
    cards = []
    for sub in subscriptions:
        if not sub.login and not sub.ipv4_address:
            continue
        nas = getattr(sub, "provisioning_nas_device", None)
        pop_site = getattr(nas, "pop_site", None) if nas else None
        cards.append(
            {
                "subscription_id": str(sub.id),
                "offer_name": sub.offer.name if sub.offer else "Subscription",
                "status": sub.status.value if sub.status else "unknown",
                "login": sub.login,
                "ipv4_address": sub.ipv4_address,
                "ipv6_address": getattr(sub, "ipv6_address", None),
                "mac_address": getattr(sub, "mac_address", None),
                "nas_name": nas.name if nas else None,
                "nas_id": str(nas.id) if nas else None,
                "pop_site_name": pop_site.name if pop_site else None,
            }
        )
    return cards


def build_customer_detail_snapshot(db: Session, customer_id: str) -> dict[str, Any]:
    """Build unified customer detail snapshot.

    Every customer is a subscriber. Business accounts store their
    company identity directly on the subscriber row.
    """
    customer = subscriber_service.subscribers.get(db=db, subscriber_id=customer_id)
    customer_name = (
        customer.company_name
        or customer.display_name
        or f"{customer.first_name or ''} {customer.last_name or ''}".strip()
    )
    organization = None
    if customer.category == SubscriberCategory.business:
        organization = SimpleNamespace(
            name=customer.company_name or customer_name,
            legal_name=customer.legal_name,
            tax_id=customer.tax_id,
            domain=customer.domain,
            website=customer.website,
            notes=customer.notes,
        )

    subscribers = [customer]

    # Load addresses and contacts for all subscriber accounts
    addresses: list[Any] = []
    contacts: list[dict[str, object]] = []
    accounts: list[Subscriber] = []
    for sub in subscribers:
        try:
            sub_addresses = subscriber_service.addresses.list(
                db=db,
                subscriber_id=str(sub.id),
                order_by="created_at",
                order_dir="desc",
                limit=50,
                offset=0,
            )
            addresses.extend(sub_addresses)
        except Exception:
            logger.debug(
                "Failed to load addresses for subscriber %s",
                sub.id,
                exc_info=True,
            )
        accounts.append(sub)
        channels = (
            db.query(SubscriberChannel)
            .filter(SubscriberChannel.subscriber_id == sub.id)
            .order_by(SubscriberChannel.created_at.desc())
            .limit(50)
            .all()
        )
        contacts.extend(_format_contact_channel(sub, channel) for channel in channels)

    accounts = _dedupe_accounts(accounts)
    subscriptions = _list_subscriptions_for_accounts(db, accounts)
    account_lookup = {str(account.id): account for account in accounts}
    account_ids = [account.id for account in accounts]

    # Financials & relationships
    finance_data = _build_common_financials(db, account_ids)
    invoices = finance_data["invoices"]
    payments = finance_data["payments"]
    balance_due = finance_data["balance_due"]
    financials = finance_data["financials"]
    active_subscriptions = sum(
        1 for sub in subscriptions if sub.status == SubscriptionStatus.active
    )
    monthly_recurring = sum(
        float(getattr(sub, "unit_price", 0) or 0)
        for sub in subscriptions
        if sub.status == SubscriptionStatus.active
    )
    financials["monthly_recurring"] = monthly_recurring
    relationship_data = _build_relationship_data(db, account_ids)

    # Address resolution with fallback
    if not addresses:
        addresses = _build_person_fallback_address(db, customer)

    primary_address = next(
        (
            a
            for a in addresses
            if getattr(a, "is_primary", False) and (a.address_line1 or "").strip()
        ),
        next(
            (a for a in addresses if getattr(a, "is_primary", False)),
            next(
                (a for a in addresses if (a.address_line1 or "").strip()),
                addresses[0] if addresses else None,
            ),
        ),
    )
    map_data, geocode_target = _build_map_payload(primary_address, customer_name)

    sub_filter = Subscriber.id == customer.id
    active_subscribers = (
        db.query(Subscriber).filter(sub_filter, Subscriber.is_active.is_(True)).count()
    )
    total_subscribers = db.query(Subscriber).filter(sub_filter).count()

    # Notifications — gather recipients from all accounts
    notifications: list[Any] = []
    try:
        recipients: list[str] = []
        for sub in subscribers:
            if sub.email:
                recipients.append(sub.email)
            if sub.phone:
                recipients.append(sub.phone)
        if recipients:
            all_notifications = notification_service.Notifications.list(
                db=db,
                channel=None,
                status=None,
                is_active=True,
                order_by="created_at",
                order_dir="desc",
                limit=100,
                offset=0,
            )
            notifications = [n for n in all_notifications if n.recipient in recipients][
                :5
            ]
    except Exception:
        notifications = []

    activity_items = _build_activity_items(
        db,
        "subscriber",
        str(customer_id),
        account_ids,
        subscriptions,
    )
    relationship_summary = cast(
        dict[str, int],
        relationship_data["relationship_summary"],
    )
    stats = {
        "total_subscribers": len(subscribers),
        "total_subscriptions": len(subscriptions),
        "active_subscriptions": active_subscriptions,
        "balance_due": balance_due,
        "total_addresses": len(addresses),
        "total_contacts": len(contacts),
        "open_tickets": relationship_summary["open_tickets"],
        "active_service_orders": relationship_summary["active_service_orders"],
        "service_orders": relationship_summary["active_service_orders"],
        "active_credentials": relationship_summary["active_credentials"],
        "active_network_assets": (
            relationship_summary["active_cpes"]
            + relationship_summary["active_onts"]
            + relationship_summary["active_ip_assignments"]
        ),
    }
    try:
        customer_user_access = (
            web_customer_user_access_service.build_customer_user_access_state(
                db,
                customer_type="person",
                customer_id=customer_id,
            )
        )
    except Exception as exc:
        customer_user_access = {"error": str(exc)}

    network_access_cards = _build_network_access_cards(subscriptions)

    return {
        "customer": customer,
        "customer_type": "person",
        "customer_name": customer_name,
        "organization": organization,
        "subscribers": subscribers,
        "accounts": accounts,
        "subscriptions": subscriptions,
        "account_lookup": account_lookup,
        "addresses": addresses,
        "primary_address": primary_address,
        "map_data": map_data,
        "geocode_target": geocode_target,
        "contacts": contacts,
        "invoices": invoices,
        "payments": payments,
        "notifications": notifications,
        "stats": stats,
        "financials": financials,
        "has_active_subscribers": active_subscribers > 0,
        "has_any_subscribers": total_subscribers > 0,
        "activity_items": activity_items,
        "customer_user_access": customer_user_access,
        "billing_policy": _billing_policy_snapshot(db, accounts),
        "network_access_cards": network_access_cards,
        **relationship_data,
    }


def build_person_detail_snapshot(db: Session, customer_id: str) -> dict[str, Any]:
    """Backwards-compatible wrapper — delegates to unified snapshot."""
    return build_customer_detail_snapshot(db, customer_id)


def build_business_detail_snapshot(db: Session, customer_id: str) -> dict[str, Any]:
    """Business-customer wrapper for business URLs."""
    subscriber = subscriber_service.subscribers.get(db=db, subscriber_id=customer_id)
    if subscriber.category != SubscriberCategory.business:
        raise HTTPException(status_code=404, detail="Business customer not found")
    return build_customer_detail_snapshot(db, str(subscriber.id))
