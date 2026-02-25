"""Service helpers for admin speed test result web routes."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.models.catalog import Subscription
from app.models.network_monitoring import NetworkDevice, PopSite, SpeedTestResult, SpeedTestSource
from app.models.subscriber import Subscriber
from app.services.common import coerce_uuid, validate_enum


def _parse_float(raw: str | None) -> float | None:
    value = str(raw or "").strip()
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def parse_speedtest_form(form) -> dict[str, object]:
    tested_at_raw = str(form.get("tested_at") or "").strip()
    tested_at = datetime.now(UTC)
    if tested_at_raw:
        try:
            parsed = datetime.fromisoformat(tested_at_raw)
            tested_at = parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            tested_at = datetime.now(UTC)

    return {
        "subscriber_id": str(form.get("subscriber_id") or "").strip() or None,
        "subscription_id": str(form.get("subscription_id") or "").strip() or None,
        "network_device_id": str(form.get("network_device_id") or "").strip() or None,
        "pop_site_id": str(form.get("pop_site_id") or "").strip() or None,
        "source": str(form.get("source") or SpeedTestSource.manual.value).strip() or SpeedTestSource.manual.value,
        "target_label": str(form.get("target_label") or "").strip() or None,
        "provider": str(form.get("provider") or "").strip() or None,
        "server_name": str(form.get("server_name") or "").strip() or None,
        "external_ip": str(form.get("external_ip") or "").strip() or None,
        "download_mbps": _parse_float(form.get("download_mbps")),
        "upload_mbps": _parse_float(form.get("upload_mbps")),
        "latency_ms": _parse_float(form.get("latency_ms")),
        "jitter_ms": _parse_float(form.get("jitter_ms")),
        "packet_loss_pct": _parse_float(form.get("packet_loss_pct")),
        "tested_at": tested_at,
        "notes": str(form.get("notes") or "").strip() or None,
    }


def validate_speedtest_values(values: dict[str, object]) -> str | None:
    download = values.get("download_mbps")
    upload = values.get("upload_mbps")
    if download is None or download < 0:
        return "Download speed is required and must be >= 0."
    if upload is None or upload < 0:
        return "Upload speed is required and must be >= 0."
    latency = values.get("latency_ms")
    if latency is not None and latency < 0:
        return "Latency must be >= 0."
    return None


def speedtest_form_snapshot(values: dict[str, object]) -> dict[str, object]:
    return dict(values)


def speedtest_form_reference_data(db: Session) -> dict[str, object]:
    subscribers = db.query(Subscriber).order_by(Subscriber.first_name.asc(), Subscriber.last_name.asc()).limit(500).all()
    subscriptions = db.query(Subscription).order_by(Subscription.created_at.desc()).limit(500).all()
    devices = db.query(NetworkDevice).order_by(NetworkDevice.name.asc()).limit(500).all()
    pop_sites = db.query(PopSite).order_by(PopSite.name.asc()).limit(500).all()
    return {
        "subscribers": subscribers,
        "subscriptions": subscriptions,
        "devices": devices,
        "pop_sites": pop_sites,
        "sources": [item.value for item in SpeedTestSource],
    }


def create_speedtest(db: Session, values: dict[str, object]) -> SpeedTestResult:
    payload = dict(values)
    for field in ("subscriber_id", "subscription_id", "network_device_id", "pop_site_id"):
        if payload.get(field):
            payload[field] = coerce_uuid(str(payload[field]))
    payload["source"] = validate_enum(str(payload.get("source") or SpeedTestSource.manual.value), SpeedTestSource, "source")
    payload["download_mbps"] = float(payload.get("download_mbps") or 0)
    payload["upload_mbps"] = float(payload.get("upload_mbps") or 0)
    item = SpeedTestResult(**payload)
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def list_page_data(
    db: Session,
    *,
    search: str | None = None,
    subscriber_id: str | None = None,
    network_device_id: str | None = None,
    pop_site_id: str | None = None,
    source: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict[str, object]:
    query = db.query(SpeedTestResult).order_by(SpeedTestResult.tested_at.desc())

    subscriber_filter = str(subscriber_id or "").strip()
    if subscriber_filter:
        query = query.filter(SpeedTestResult.subscriber_id == coerce_uuid(subscriber_filter))

    device_filter = str(network_device_id or "").strip()
    if device_filter:
        query = query.filter(SpeedTestResult.network_device_id == coerce_uuid(device_filter))

    pop_filter = str(pop_site_id or "").strip()
    if pop_filter:
        query = query.filter(SpeedTestResult.pop_site_id == coerce_uuid(pop_filter))

    source_filter = str(source or "").strip().lower()
    if source_filter:
        query = query.filter(SpeedTestResult.source == validate_enum(source_filter, SpeedTestSource, "source"))

    if date_from:
        try:
            start = datetime.fromisoformat(date_from).replace(tzinfo=UTC)
            query = query.filter(SpeedTestResult.tested_at >= start)
        except ValueError:
            pass
    if date_to:
        try:
            end = datetime.fromisoformat(date_to).replace(tzinfo=UTC)
            query = query.filter(SpeedTestResult.tested_at <= end)
        except ValueError:
            pass

    items = query.limit(1000).all()

    search_q = str(search or "").strip().lower()
    if search_q:
        items = [
            item for item in items
            if search_q in " ".join(
                [
                    str(item.target_label or ""),
                    str(item.provider or ""),
                    str(item.server_name or ""),
                    str(item.external_ip or ""),
                    str(item.notes or ""),
                    str(item.subscriber.full_name if item.subscriber else ""),
                    str(item.network_device.name if item.network_device else ""),
                ]
            ).lower()
        ]

    total = len(items)
    avg_download = round(sum(float(item.download_mbps or 0) for item in items) / total, 2) if total else 0
    avg_upload = round(sum(float(item.upload_mbps or 0) for item in items) / total, 2) if total else 0
    avg_latency = round(
        sum(float(item.latency_ms or 0) for item in items if item.latency_ms is not None)
        / max(1, sum(1 for item in items if item.latency_ms is not None)),
        2,
    ) if total else 0

    return {
        "results": items,
        "stats": {
            "total": total,
            "avg_download": avg_download,
            "avg_upload": avg_upload,
            "avg_latency": avg_latency,
        },
        "filters": {
            "search": str(search or "").strip(),
            "subscriber_id": subscriber_filter,
            "network_device_id": device_filter,
            "pop_site_id": pop_filter,
            "source": source_filter,
            "date_from": str(date_from or "").strip(),
            "date_to": str(date_to or "").strip(),
        },
        **speedtest_form_reference_data(db),
    }
