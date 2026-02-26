"""Service helpers for admin speed test result web routes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session, selectinload

from app.models.catalog import CatalogOffer, Subscription
from app.models.network_monitoring import (
    NetworkDevice,
    PopSite,
    SpeedTestResult,
    SpeedTestSource,
)
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


def create_customer_speedtest(
    db: Session,
    *,
    subscriber_id: str,
    subscription_id: str | None,
    download_mbps: float,
    upload_mbps: float,
    latency_ms: float | None,
    jitter_ms: float | None,
    server_name: str | None,
    user_agent: str | None,
) -> SpeedTestResult:
    values: dict[str, object] = {
        "subscriber_id": subscriber_id,
        "subscription_id": subscription_id,
        "source": SpeedTestSource.api.value,
        "provider": "LibreSpeed Web",
        "server_name": server_name or "Portal Browser Test",
        "download_mbps": max(0.0, float(download_mbps)),
        "upload_mbps": max(0.0, float(upload_mbps)),
        "latency_ms": max(0.0, float(latency_ms)) if latency_ms is not None else None,
        "jitter_ms": max(0.0, float(jitter_ms)) if jitter_ms is not None else None,
        "tested_at": datetime.now(UTC),
        "notes": "Portal speed test run",
    }
    if user_agent:
        values["user_agent"] = user_agent[:500]
    return create_speedtest(db, values)


def portal_page_data(
    db: Session,
    *,
    subscriber_id: str,
    subscription_id: str | None,
) -> dict[str, object]:
    sub_uuid = coerce_uuid(subscriber_id)
    query = (
        db.query(SpeedTestResult)
        .filter(SpeedTestResult.subscriber_id == sub_uuid)
        .order_by(SpeedTestResult.tested_at.desc())
    )
    history = query.limit(30).all()
    recent = history[:10]
    avg_download = (
        round(sum(float(item.download_mbps or 0) for item in recent) / len(recent), 2)
        if recent
        else 0
    )
    avg_upload = (
        round(sum(float(item.upload_mbps or 0) for item in recent) / len(recent), 2)
        if recent
        else 0
    )
    subscriptions = (
        db.query(Subscription)
        .filter(Subscription.subscriber_id == sub_uuid)
        .order_by(Subscription.created_at.desc())
        .limit(10)
        .all()
    )
    active_subscription_id = subscription_id
    if not active_subscription_id and subscriptions:
        active_subscription_id = str(subscriptions[0].id)
    return {
        "speedtest_history": history,
        "speedtest_stats": {
            "avg_download": avg_download,
            "avg_upload": avg_upload,
            "latest": history[0] if history else None,
        },
        "subscriptions": subscriptions,
        "active_subscription_id": active_subscription_id,
    }


def analytics_page_data(db: Session, *, days: int = 30) -> dict[str, object]:
    days = min(max(days, 1), 365)
    since = datetime.now(UTC) - timedelta(days=days)
    results = (
        db.query(SpeedTestResult)
        .options(
            selectinload(SpeedTestResult.subscriber),
            selectinload(SpeedTestResult.subscription).selectinload(Subscription.offer),
            selectinload(SpeedTestResult.network_device),
            selectinload(SpeedTestResult.pop_site),
        )
        .filter(SpeedTestResult.tested_at >= since)
        .order_by(SpeedTestResult.tested_at.desc())
        .limit(5000)
        .all()
    )

    by_plan: dict[str, dict[str, float]] = {}
    by_location: dict[str, dict[str, float]] = {}
    by_hour: dict[int, dict[str, float]] = {hour: {"download": 0.0, "upload": 0.0, "count": 0} for hour in range(24)}

    for item in results:
        plan_name = "Unknown Plan"
        if item.subscription and item.subscription.offer:
            plan_name = item.subscription.offer.name
        slot = by_plan.setdefault(plan_name, {"download": 0.0, "upload": 0.0, "count": 0})
        slot["download"] += float(item.download_mbps or 0)
        slot["upload"] += float(item.upload_mbps or 0)
        slot["count"] += 1

        location = "Unknown"
        if item.pop_site and item.pop_site.name:
            location = item.pop_site.name
        elif item.subscriber and item.subscriber.city:
            location = item.subscriber.city
        loc_slot = by_location.setdefault(location, {"download": 0.0, "upload": 0.0, "count": 0})
        loc_slot["download"] += float(item.download_mbps or 0)
        loc_slot["upload"] += float(item.upload_mbps or 0)
        loc_slot["count"] += 1

        tested_at = item.tested_at
        hour = tested_at.hour if tested_at else 0
        by_hour[hour]["download"] += float(item.download_mbps or 0)
        by_hour[hour]["upload"] += float(item.upload_mbps or 0)
        by_hour[hour]["count"] += 1

    plan_rows = [
        {
            "plan": name,
            "count": int(values["count"]),
            "avg_download": round(values["download"] / max(1, values["count"]), 2),
            "avg_upload": round(values["upload"] / max(1, values["count"]), 2),
        }
        for name, values in sorted(by_plan.items(), key=lambda entry: entry[1]["count"], reverse=True)
    ]
    location_rows = [
        {
            "location": name,
            "count": int(values["count"]),
            "avg_download": round(values["download"] / max(1, values["count"]), 2),
            "avg_upload": round(values["upload"] / max(1, values["count"]), 2),
        }
        for name, values in sorted(by_location.items(), key=lambda entry: entry[1]["count"], reverse=True)
    ]
    hourly_rows = [
        {
            "hour": hour,
            "count": int(by_hour[hour]["count"]),
            "avg_download": round(by_hour[hour]["download"] / max(1, by_hour[hour]["count"]), 2),
            "avg_upload": round(by_hour[hour]["upload"] / max(1, by_hour[hour]["count"]), 2),
        }
        for hour in range(24)
    ]

    return {
        "days": days,
        "total_results": len(results),
        "by_plan": plan_rows,
        "by_location": location_rows,
        "by_hour": hourly_rows,
    }


def clear_history(
    db: Session,
    *,
    confirm_text: str,
    older_than_days: int | None,
) -> int:
    if confirm_text.strip().upper() != "CLEAR":
        raise ValueError("Type CLEAR to confirm deletion.")

    query = db.query(SpeedTestResult)
    if older_than_days is not None and older_than_days > 0:
        cutoff = datetime.now(UTC) - timedelta(days=older_than_days)
        query = query.filter(SpeedTestResult.tested_at < cutoff)
    deleted = query.delete(synchronize_session=False)
    db.commit()
    return int(deleted)


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
    query = (
        db.query(SpeedTestResult)
        .options(
            selectinload(SpeedTestResult.subscriber),
            selectinload(SpeedTestResult.subscription),
            selectinload(SpeedTestResult.network_device),
            selectinload(SpeedTestResult.pop_site),
        )
        .order_by(SpeedTestResult.tested_at.desc())
    )

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
            "underperforming": count_underperforming_connections(items),
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


def count_underperforming_connections(items: list[SpeedTestResult]) -> int:
    underperforming = 0
    for item in items:
        if not item.subscription or not item.subscription.offer:
            continue
        offer: CatalogOffer = item.subscription.offer
        plan_down = float(offer.speed_download_mbps or 0)
        plan_up = float(offer.speed_upload_mbps or 0)
        if plan_down <= 0 and plan_up <= 0:
            continue
        down_ratio = (float(item.download_mbps or 0) / plan_down) if plan_down > 0 else 1.0
        up_ratio = (float(item.upload_mbps or 0) / plan_up) if plan_up > 0 else 1.0
        if min(down_ratio, up_ratio) < 0.8:
            underperforming += 1
    return underperforming
