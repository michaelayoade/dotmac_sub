"""Service helpers for admin DNS threat monitoring routes."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.models.network_monitoring import (
    DnsThreatAction,
    DnsThreatEvent,
    DnsThreatSeverity,
    NetworkDevice,
    PopSite,
)
from app.models.subscriber import Subscriber
from app.services.common import coerce_uuid, validate_enum


def _parse_float(raw: str | None) -> float | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_event_form(form) -> dict[str, object]:
    occurred_raw = str(form.get("occurred_at") or "").strip()
    occurred_at = datetime.now(UTC)
    if occurred_raw:
        try:
            parsed = datetime.fromisoformat(occurred_raw)
            occurred_at = parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            occurred_at = datetime.now(UTC)

    return {
        "subscriber_id": str(form.get("subscriber_id") or "").strip() or None,
        "network_device_id": str(form.get("network_device_id") or "").strip() or None,
        "pop_site_id": str(form.get("pop_site_id") or "").strip() or None,
        "queried_domain": str(form.get("queried_domain") or "").strip(),
        "query_type": str(form.get("query_type") or "").strip() or None,
        "source_ip": str(form.get("source_ip") or "").strip() or None,
        "destination_ip": str(form.get("destination_ip") or "").strip() or None,
        "threat_category": str(form.get("threat_category") or "").strip() or None,
        "threat_feed": str(form.get("threat_feed") or "").strip() or None,
        "severity": str(form.get("severity") or DnsThreatSeverity.medium.value).strip(),
        "action": str(form.get("action") or DnsThreatAction.blocked.value).strip(),
        "confidence_score": _parse_float(form.get("confidence_score")),
        "occurred_at": occurred_at,
        "notes": str(form.get("notes") or "").strip() or None,
    }


def validate_event_values(values: dict[str, object]) -> str | None:
    domain = str(values.get("queried_domain") or "").strip()
    if not domain:
        return "Queried domain is required."
    score = values.get("confidence_score")
    if score is not None and (score < 0 or score > 100):
        return "Confidence score must be between 0 and 100."
    return None


def event_form_reference_data(db: Session) -> dict[str, object]:
    subscribers = db.query(Subscriber).order_by(Subscriber.first_name.asc(), Subscriber.last_name.asc()).limit(500).all()
    devices = db.query(NetworkDevice).order_by(NetworkDevice.name.asc()).limit(500).all()
    pop_sites = db.query(PopSite).order_by(PopSite.name.asc()).limit(500).all()
    return {
        "subscribers": subscribers,
        "devices": devices,
        "pop_sites": pop_sites,
        "severities": [item.value for item in DnsThreatSeverity],
        "actions": [item.value for item in DnsThreatAction],
    }


def create_event(db: Session, values: dict[str, object]) -> DnsThreatEvent:
    payload = dict(values)
    for field in ("subscriber_id", "network_device_id", "pop_site_id"):
        if payload.get(field):
            payload[field] = coerce_uuid(str(payload[field]))
    payload["severity"] = validate_enum(str(payload.get("severity") or "medium"), DnsThreatSeverity, "severity")
    payload["action"] = validate_enum(str(payload.get("action") or "blocked"), DnsThreatAction, "action")
    event = DnsThreatEvent(**payload)
    db.add(event)
    db.commit()
    db.refresh(event)
    return event


def list_page_data(
    db: Session,
    *,
    search: str | None = None,
    severity: str | None = None,
    action: str | None = None,
    subscriber_id: str | None = None,
    network_device_id: str | None = None,
) -> dict[str, object]:
    query = db.query(DnsThreatEvent).order_by(DnsThreatEvent.occurred_at.desc())

    severity_filter = str(severity or "").strip().lower()
    if severity_filter:
        query = query.filter(DnsThreatEvent.severity == validate_enum(severity_filter, DnsThreatSeverity, "severity"))

    action_filter = str(action or "").strip().lower()
    if action_filter:
        query = query.filter(DnsThreatEvent.action == validate_enum(action_filter, DnsThreatAction, "action"))

    subscriber_filter = str(subscriber_id or "").strip()
    if subscriber_filter:
        query = query.filter(DnsThreatEvent.subscriber_id == coerce_uuid(subscriber_filter))

    device_filter = str(network_device_id or "").strip()
    if device_filter:
        query = query.filter(DnsThreatEvent.network_device_id == coerce_uuid(device_filter))

    items = query.limit(1000).all()

    search_q = str(search or "").strip().lower()
    if search_q:
        items = [
            item
            for item in items
            if search_q in " ".join(
                [
                    str(item.queried_domain or ""),
                    str(item.threat_category or ""),
                    str(item.threat_feed or ""),
                    str(item.source_ip or ""),
                    str(item.destination_ip or ""),
                    str(item.subscriber.full_name if item.subscriber else ""),
                ]
            ).lower()
        ]

    total = len(items)
    blocked = sum(1 for item in items if item.action.value == DnsThreatAction.blocked.value)
    critical = sum(1 for item in items if item.severity.value == DnsThreatSeverity.critical.value)
    high = sum(1 for item in items if item.severity.value == DnsThreatSeverity.high.value)

    return {
        "events": items,
        "stats": {
            "total": total,
            "blocked": blocked,
            "critical": critical,
            "high": high,
        },
        "filters": {
            "search": str(search or "").strip(),
            "severity": severity_filter,
            "action": action_filter,
            "subscriber_id": subscriber_filter,
            "network_device_id": device_filter,
        },
        **event_form_reference_data(db),
    }
