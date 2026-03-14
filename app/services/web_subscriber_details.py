"""Helpers for admin subscriber detail page."""

import logging
import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import String, cast, select
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session, joinedload

from app.models.billing import CreditNoteStatus, InvoiceStatus
from app.models.catalog import ContractTerm, OfferStatus, SubscriptionStatus
from app.models.network import (
    CPEDevice,
    FdhCabinet,
    FiberSpliceClosure,
    OntAssignment,
)
from app.models.network_monitoring import SpeedTestResult
from app.models.subscriber import Address, Subscriber, SubscriberChannel
from app.models.usage import AccountingStatus, RadiusAccountingSession
from app.services import audit as audit_service
from app.services import billing as billing_service
from app.services import catalog as catalog_service
from app.services import notification as notification_service
from app.services import subscriber as subscriber_service
from app.services import web_customer_user_access as web_customer_user_access_service
from app.services.audit_helpers import extract_changes, format_changes

logger = logging.getLogger(__name__)


def _format_attachment_size(size_bytes: object) -> str:
    try:
        size = int(str(size_bytes))
    except (TypeError, ValueError):
        return ""
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def _haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate distance between two lat/lon points in meters."""
    radius_m = 6371000
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    a = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    )
    return radius_m * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _resolve_subscriber_addresses(db: Session, subscriber_id):
    addresses = db.query(Address).filter(Address.subscriber_id == subscriber_id).all()
    primary_address = next(
        (addr for addr in addresses if addr.is_primary),
        addresses[0] if addresses else None,
    )
    return addresses, primary_address


def build_subscriber_map_data(db: Session, subscriber, primary_address):
    """Build mini-map GeoJSON payload for subscriber detail page."""
    if not (
        primary_address
        and primary_address.latitude is not None
        and primary_address.longitude is not None
    ):
        return None
    customer_lat = float(primary_address.latitude)
    customer_lon = float(primary_address.longitude)

    customer_name = "Customer"
    if getattr(subscriber, "organization", None):
        customer_name = subscriber.organization.name or "Customer"
    else:
        full_name = f"{getattr(subscriber, 'first_name', '')} {getattr(subscriber, 'last_name', '')}".strip()
        customer_name = full_name or getattr(subscriber, "display_name", None) or "Customer"

    features = [
        {
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": [primary_address.longitude, primary_address.latitude],
            },
            "properties": {
                "type": "customer",
                "name": customer_name,
                "address": primary_address.address_line1,
            },
        }
    ]

    fdh_cabinets = (
        db.query(FdhCabinet)
        .filter(FdhCabinet.is_active.is_(True))
        .filter(FdhCabinet.latitude.isnot(None))
        .filter(FdhCabinet.longitude.isnot(None))
        .all()
    )
    for fdh in fdh_cabinets:
        if fdh.latitude is None or fdh.longitude is None:
            continue
        distance = _haversine_distance(
            customer_lat,
            customer_lon,
            float(fdh.latitude),
            float(fdh.longitude),
        )
        if distance <= 2000:
            features.append(
                {
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [fdh.longitude, fdh.latitude]},
                    "properties": {
                        "type": "fdh_cabinet",
                        "name": fdh.name,
                        "code": fdh.code,
                        "distance_m": round(distance),
                    },
                }
            )

    closures = (
        db.query(FiberSpliceClosure)
        .filter(FiberSpliceClosure.is_active.is_(True))
        .filter(FiberSpliceClosure.latitude.isnot(None))
        .filter(FiberSpliceClosure.longitude.isnot(None))
        .all()
    )
    for closure in closures:
        if closure.latitude is None or closure.longitude is None:
            continue
        distance = _haversine_distance(
            customer_lat,
            customer_lon,
            float(closure.latitude),
            float(closure.longitude),
        )
        if distance <= 1000:
            features.append(
                {
                    "type": "Feature",
                    "geometry": {
                        "type": "Point",
                        "coordinates": [closure.longitude, closure.latitude],
                    },
                    "properties": {
                        "type": "splice_closure",
                        "name": closure.name,
                        "distance_m": round(distance),
                    },
                }
            )

    return {
        "center": [primary_address.latitude, primary_address.longitude],
        "geojson": {"type": "FeatureCollection", "features": features},
    }


def build_subscriber_geocode_target(primary_address):
    """Build geocode payload when address exists without coordinates."""
    if not primary_address or not (primary_address.address_line1 or "").strip():
        return None
    if (
        getattr(primary_address, "latitude", None) is not None
        and getattr(primary_address, "longitude", None) is not None
    ):
        return None
    target_id = getattr(primary_address, "id", None)
    return {
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


def _build_equipment_snapshot(db: Session, subscriber_id) -> dict[str, object]:
    """Collect ONT/CPE devices and direct management links for subscriber detail."""
    equipment: list[dict[str, object]] = []
    try:
        ont_assignments = (
            db.query(OntAssignment)
            .options(joinedload(OntAssignment.ont_unit))
            .filter(
                OntAssignment.subscriber_id == subscriber_id,
                OntAssignment.active.is_(True),
            )
            .order_by(OntAssignment.created_at.desc())
            .all()
        )
        for assignment in ont_assignments:
            ont = assignment.ont_unit
            if not ont:
                continue
            status_value = (
                ont.online_status.value
                if getattr(ont, "online_status", None) is not None
                and hasattr(ont.online_status, "value")
                else str(getattr(ont, "online_status", "") or "")
            ).strip().lower()
            equipment.append(
                {
                    "type": "ONT",
                    "model": ont.model or ont.name or "ONT",
                    "serial": ont.serial_number or "-",
                    "online": status_value == "online",
                    "detail_url": f"/admin/network/onts/{ont.id}",
                    "tr069_url": f"/admin/network/onts/{ont.id}?tab=tr069",
                }
            )
    except Exception:
        logger.exception(
            "Failed to load subscriber ONT equipment snapshot",
            extra={"subscriber_id": str(subscriber_id)},
        )
        db.rollback()

    try:
        cpe_rows = db.execute(
            select(
                CPEDevice.id,
                cast(CPEDevice.device_type, String).label("device_type"),
                cast(CPEDevice.status, String).label("status"),
                CPEDevice.model,
                CPEDevice.vendor,
                CPEDevice.serial_number,
                CPEDevice.mac_address,
            )
            .where(CPEDevice.subscriber_id == subscriber_id)
            .order_by(CPEDevice.created_at.desc())
        ).all()
        for cpe in cpe_rows:
            cpe_type = str(getattr(cpe, "device_type", "") or "CPE")
            status_value = str(getattr(cpe, "status", "") or "").strip().lower()
            equipment.append(
                {
                    "type": cpe_type.upper(),
                    "model": (cpe.model or cpe.vendor or cpe_type.upper()),
                    "serial": (cpe.serial_number or cpe.mac_address or "-"),
                    "online": status_value == "active",
                    "detail_url": f"/admin/network/cpes/{cpe.id}",
                }
            )
    except Exception:
        logger.exception(
            "Failed to load subscriber CPE equipment snapshot",
            extra={"subscriber_id": str(subscriber_id)},
        )
        db.rollback()

    primary_ont = next((item for item in equipment if item.get("type") == "ONT"), None)
    return {
        "equipment": equipment,
        "primary_ont_url": (primary_ont or {}).get("detail_url"),
        "primary_ont_tr069_url": (primary_ont or {}).get("tr069_url"),
    }


def build_subscriber_detail_snapshot(db: Session, subscriber, subscriber_id):
    """Collect subscriber detail page data from multiple services."""
    subscriptions = []
    online_status = {}
    try:
        subscriptions = catalog_service.subscriptions.list(
            db=db,
            subscriber_id=str(subscriber.id),
            offer_id=None,
            status=SubscriptionStatus.active.value,
            order_by="created_at",
            order_dir="desc",
            limit=10,
            offset=0,
        )
        subscriptions = sorted(
            subscriptions,
            key=lambda item: item.created_at or item.id,
            reverse=True,
        )[:10]
        for sub in subscriptions:
            latest_session = (
                db.query(RadiusAccountingSession)
                .filter(RadiusAccountingSession.subscription_id == sub.id)
                .order_by(RadiusAccountingSession.created_at.desc())
                .first()
            )
            if latest_session:
                online_status[str(sub.id)] = (
                    latest_session.status_type
                    in (AccountingStatus.start, AccountingStatus.interim)
                    and latest_session.session_end is None
                )
            else:
                online_status[str(sub.id)] = False
    except Exception:
        logger.exception(
            "Failed to load subscriber subscriptions/online status for detail snapshot",
            extra={"subscriber_id": str(subscriber_id)},
        )
        db.rollback()
        subscriptions = []
        online_status = {}

    accounts = []
    invoices = []
    balance_due = Decimal("0.00")
    available_credit = Decimal("0.00")
    current_balance = Decimal("0.00")
    try:
        accounts = subscriber_service.accounts.list(
            db=db,
            subscriber_id=str(subscriber_id),
            reseller_id=None,
            order_by="created_at",
            order_dir="desc",
            limit=25,
            offset=0,
        )
        if accounts:
            account = accounts[0]
            invoices = billing_service.invoices.list(
                db=db,
                account_id=account.id,
                status=None,
                is_active=None,
                order_by="created_at",
                order_dir="desc",
                limit=5,
                offset=0,
            )
            balance_due = sum(
                (
                    Decimal(str(getattr(inv, "balance_due", 0) or 0))
                    for inv in invoices
                    if inv.status
                    in (InvoiceStatus.issued, InvoiceStatus.partially_paid, InvoiceStatus.overdue)
                ),
                Decimal("0.00"),
            )
            credit_notes = billing_service.credit_notes.list(
                db=db,
                account_id=account.id,
                invoice_id=None,
                status=None,
                is_active=True,
                order_by="created_at",
                order_dir="desc",
                limit=100,
                offset=0,
            )
            available_credit = sum(
                (
                    Decimal(str(note.total or 0)) - Decimal(str(note.applied_total or 0))
                    for note in credit_notes
                    if note.status in (CreditNoteStatus.issued, CreditNoteStatus.partially_applied)
                ),
                Decimal("0.00"),
            )
            current_balance = balance_due + available_credit
    except Exception:
        logger.exception(
            "Failed to load subscriber billing snapshot",
            extra={"subscriber_id": str(subscriber_id)},
        )
        db.rollback()
        accounts = []
        invoices = []
        balance_due = Decimal("0.00")
        available_credit = Decimal("0.00")
        current_balance = Decimal("0.00")

    notifications = []
    try:
        recipients = []
        if getattr(subscriber, "email", None):
            recipients.append(subscriber.email)
        if getattr(subscriber, "phone", None):
            recipients.append(subscriber.phone)
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
            notifications = [
                item for item in all_notifications if item.recipient in recipients
            ][:5]
    except Exception:
        logger.exception(
            "Failed to load subscriber notifications snapshot",
            extra={"subscriber_id": str(subscriber_id)},
        )
        db.rollback()
        notifications = []

    monthly_bill = (
        sum(float(getattr(sub, "price", 0) or 0) for sub in subscriptions)
        if subscriptions
        else 0
    )
    stats = {
        "monthly_bill": monthly_bill,
        "balance_due": float(balance_due),
        "credit_issued": float(available_credit),
        "current_balance": float(current_balance),
        "has_credit_adjustment": available_credit > 0,
        "data_usage": "0",
    }

    addresses, primary_address = _resolve_subscriber_addresses(db, subscriber_id)
    channels = (
        db.query(SubscriberChannel)
        .filter(SubscriberChannel.subscriber_id == subscriber_id)
        .order_by(SubscriberChannel.is_primary.desc(), SubscriberChannel.created_at.asc())
        .all()
    )
    contacts: list[dict[str, object]] = []
    for channel in channels:
        contacts.append(
            {
                "id": str(channel.id),
                "type": channel.channel_type.value if channel.channel_type else "email",
                "label": channel.label or "",
                "value": channel.address,
                "is_primary": bool(channel.is_primary),
            }
        )

    organization_members = []
    if subscriber.organization_id:
        members = (
            db.query(Subscriber)
            .filter(Subscriber.organization_id == subscriber.organization_id)
            .order_by(Subscriber.is_active.desc(), Subscriber.created_at.asc())
            .limit(25)
            .all()
        )
        for member in members:
            organization_members.append(
                {
                    "id": str(member.id),
                    "name": member.display_name
                    or f"{member.first_name} {member.last_name}".strip()
                    or member.email,
                    "email": member.email,
                    "is_active": bool(member.is_active),
                    "is_current": str(member.id) == str(subscriber.id),
                }
            )

    map_data = build_subscriber_map_data(db, subscriber, primary_address)
    geocode_target = build_subscriber_geocode_target(primary_address)
    speedtest_snapshot = _build_speedtest_snapshot(db, subscriber_id, subscriptions)
    equipment_snapshot = _build_equipment_snapshot(db, subscriber_id)

    return {
        "accounts": accounts,
        "subscriptions": subscriptions,
        "online_status": online_status,
        "invoices": invoices,
        "notifications": notifications,
        "stats": stats,
        "addresses": addresses,
        "contacts": contacts,
        "primary_address": primary_address,
        "organization_members": organization_members,
        "map_data": map_data,
        "geocode_target": geocode_target,
        **speedtest_snapshot,
        **equipment_snapshot,
    }


def _build_speedtest_snapshot(db: Session, subscriber_id, subscriptions: list) -> dict[str, object]:
    plan_download = 0.0
    plan_upload = 0.0
    for subscription in subscriptions:
        offer = getattr(subscription, "offer", None)
        if not offer:
            continue
        plan_download = float(getattr(offer, "speed_download_mbps", 0) or 0)
        plan_upload = float(getattr(offer, "speed_upload_mbps", 0) or 0)
        if plan_download > 0 or plan_upload > 0:
            break

    try:
        tests = (
            db.query(SpeedTestResult)
            .filter(SpeedTestResult.subscriber_id == subscriber_id)
            .order_by(SpeedTestResult.tested_at.desc())
            .limit(30)
            .all()
        )
    except ProgrammingError as exc:
        # Some environments are behind on migrations and may not have speed_test_results yet.
        # Fail open so subscriber detail still renders.
        db.rollback()
        if "speed_test_results" in str(exc).lower():
            return {
                "speedtests": [],
                "speedtest_performance_rows": [],
                "speedtest_chart": {"labels": [], "download": [], "upload": []},
                "speedtest_plan": {
                    "download_mbps": plan_download,
                    "upload_mbps": plan_upload,
                },
                "speedtest_underperforming_count": 0,
            }
        raise

    performance_rows = []
    underperforming_count = 0
    for test in tests:
        down = float(test.download_mbps or 0)
        up = float(test.upload_mbps or 0)
        down_ratio = (down / plan_download) if plan_download > 0 else None
        up_ratio = (up / plan_upload) if plan_upload > 0 else None
        performance_ratio = min(
            [ratio for ratio in (down_ratio, up_ratio) if ratio is not None] or [1.0]
        )
        is_underperforming = performance_ratio < 0.8
        if is_underperforming:
            underperforming_count += 1
        performance_rows.append(
            {
                "test": test,
                "down_ratio_pct": round((down_ratio or 0) * 100, 1) if down_ratio is not None else None,
                "up_ratio_pct": round((up_ratio or 0) * 100, 1) if up_ratio is not None else None,
                "is_underperforming": is_underperforming,
            }
        )

    chart_source = list(reversed(tests[:12]))
    chart = {
        "labels": [
            item.tested_at.strftime("%m-%d %H:%M") if item.tested_at else ""
            for item in chart_source
        ],
        "download": [round(float(item.download_mbps or 0), 2) for item in chart_source],
        "upload": [round(float(item.upload_mbps or 0), 2) for item in chart_source],
    }
    return {
        "speedtests": tests,
        "speedtest_performance_rows": performance_rows,
        "speedtest_chart": chart,
        "speedtest_plan": {
            "download_mbps": plan_download,
            "upload_mbps": plan_upload,
        },
        "speedtest_underperforming_count": underperforming_count,
    }


def build_subscriber_timeline(db: Session, subscriber_id):
    """Build audit timeline for subscriber detail page."""
    audit_events = audit_service.audit_events.list(
        db=db,
        actor_id=None,
        actor_type=None,
        action=None,
        entity_type="subscriber",
        entity_id=str(subscriber_id),
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
            for person in db.query(Subscriber).filter(Subscriber.id.in_(actor_ids)).all()
        }
    timeline = []
    for event in audit_events:
        actor = people.get(str(event.actor_id)) if getattr(event, "actor_id", None) else None
        actor_name = f"{actor.first_name} {actor.last_name}".strip() if actor else "System"
        metadata = getattr(event, "metadata_", None) or {}
        comment_text = str(metadata.get("comment") or "").strip()
        is_todo = bool(metadata.get("is_todo"))
        is_completed = bool(metadata.get("is_completed"))
        attachments: list[dict[str, str]] = []
        raw_attachments = metadata.get("attachments")
        if isinstance(raw_attachments, list):
            for item in raw_attachments:
                if not isinstance(item, dict):
                    continue
                attachment_id = str(item.get("id") or "").strip()
                if not attachment_id:
                    continue
                filename = str(item.get("filename") or "Attachment").strip() or "Attachment"
                attachments.append(
                    {
                        "id": attachment_id,
                        "filename": filename,
                        "size_label": _format_attachment_size(item.get("size")),
                    }
                )
        changes = extract_changes(metadata, getattr(event, "action", None))
        change_summary = format_changes(changes, max_items=2)
        if comment_text:
            detail = f"{actor_name} · {comment_text}"
        else:
            detail = actor_name if not change_summary else f"{actor_name} · {change_summary}"
        timeline.append(
            {
                "id": str(event.id),
                "type": "audit",
                "title": (event.action or "Activity").replace("_", " ").title(),
                "detail": detail,
                "is_comment": bool(comment_text),
                "is_todo": is_todo,
                "is_completed": is_completed,
                "attachments": attachments,
                "time": (
                    event.occurred_at.strftime("%b %d, %Y %H:%M")
                    if event.occurred_at
                    else "Just now"
                ),
            }
        )
    return timeline


def build_subscriber_detail_page_context(db: Session, subscriber_id):
    subscriber = subscriber_service.subscribers.get(db=db, subscriber_id=subscriber_id)
    if not subscriber:
        raise ValueError("Subscriber not found")

    detail_snapshot = build_subscriber_detail_snapshot(
        db=db,
        subscriber=subscriber,
        subscriber_id=subscriber_id,
    )
    try:
        timeline = build_subscriber_timeline(db=db, subscriber_id=subscriber_id)
    except Exception:
        logger.exception(
            "Failed to load subscriber timeline for detail page",
            extra={"subscriber_id": str(subscriber_id)},
        )
        db.rollback()
        timeline = []
    try:
        offers = catalog_service.offers.list(
            db=db,
            service_type=None,
            access_type=None,
            status=OfferStatus.active.value,
            is_active=True,
            order_by="name",
            order_dir="asc",
            limit=500,
            offset=0,
        )
    except Exception:
        logger.exception(
            "Failed to load active offers for subscriber detail page",
            extra={"subscriber_id": str(subscriber_id)},
        )
        db.rollback()
        offers = []
    try:
        subscriber_user_access = (
            web_customer_user_access_service.build_subscriber_user_access_state(
                db,
                subscriber_id=str(subscriber_id),
            )
        )
    except Exception as exc:
        logger.exception(
            "Failed to load subscriber user access state for detail page",
            extra={"subscriber_id": str(subscriber_id)},
        )
        db.rollback()
        subscriber_user_access = {"error": str(exc)}

    return {
        "subscriber": subscriber,
        **detail_snapshot,
        "billing_config": _build_billing_config(subscriber, detail_snapshot.get("stats") or {}),
        "subscriber_user_access": subscriber_user_access,
        "timeline": timeline,
        "offers": offers,
        "subscription_statuses": [s.value for s in SubscriptionStatus],
        "contract_terms": [t.value for t in ContractTerm],
    }


def _build_billing_config(subscriber, stats: dict) -> dict[str, object]:
    metadata = dict(getattr(subscriber, "metadata_", None) or {})
    blocking_days = int(metadata.get("blocking_period_days") or 0)
    deactivation_days = int(metadata.get("deactivation_period_days") or 0)
    auto_create = bool(metadata.get("auto_create_invoices", True))
    send_notifications = bool(metadata.get("send_billing_notifications", True))

    next_block_at = None
    next_block_label = "No block scheduled"
    balance_due = float(stats.get("balance_due") or 0)
    if balance_due > 0:
        delay_days = max(blocking_days, int(getattr(subscriber, "grace_period_days", 0) or 0))
        next_block_at = datetime.now(UTC) + timedelta(days=delay_days)
        if delay_days <= 0:
            next_block_label = "Immediately"
        elif delay_days <= 30:
            next_block_label = f"In {delay_days} day(s)"
        else:
            next_block_label = next_block_at.strftime("%Y-%m-%d")

    return {
        "category": getattr(subscriber, "category", None),
        "billing_day": getattr(subscriber, "billing_day", None),
        "payment_due_days": getattr(subscriber, "payment_due_days", None),
        "grace_period_days": getattr(subscriber, "grace_period_days", None),
        "min_balance": getattr(subscriber, "min_balance", None),
        "billing_enabled": bool(getattr(subscriber, "billing_enabled", True)),
        "blocking_period_days": blocking_days,
        "deactivation_period_days": deactivation_days,
        "auto_create_invoices": auto_create,
        "send_billing_notifications": send_notifications,
        "next_block_at": next_block_at,
        "next_block_label": next_block_label,
    }
