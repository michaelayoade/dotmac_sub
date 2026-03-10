"""Batch geocoding tool helpers for admin system page."""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

from app.models.subscriber import Address, Subscriber, SubscriberStatus
from app.models.subscription_engine import SettingValueType
from app.schemas.settings import DomainSettingUpdate
from app.services import domain_settings as domain_settings_service
from app.services import geocoding as geocoding_service
from app.services import gis_sync as gis_sync_service
from app.services import job_log_store

GEOCODE_JOB_KEY = "batch_geocode_jobs_log"
GEOCODE_LOG_KEY = "batch_geocode_log_rows"


@dataclass
class GeocodeFilters:
    date_from: str | None
    date_to: str | None
    subscriber_status: str | None
    overwrite_existing: bool


def _now() -> datetime:
    return datetime.now(UTC)


def _parse_date(value: str | None, *, end: bool = False) -> datetime | None:
    from app.services.common import parse_date_filter
    return parse_date_filter(value, end_of_day=end)


def _setting_json(db: Session, key: str, default: Any) -> Any:
    try:
        setting = domain_settings_service.geocoding_settings.get_by_key(db, key)
    except Exception as exc:
        logger.warning("Failed to read geocoding setting %s: %s", key, exc)
        return default
    if isinstance(setting.value_json, (dict, list)):
        return setting.value_json
    if isinstance(setting.value_text, str) and setting.value_text.strip():
        try:
            return json.loads(setting.value_text)
        except json.JSONDecodeError:
            return default
    return default


def _upsert_json(db: Session, key: str, payload: dict[str, Any] | list[dict[str, Any]]) -> None:
    domain_settings_service.geocoding_settings.upsert_by_key(
        db,
        key,
        DomainSettingUpdate(
            value_type=SettingValueType.json,
            value_json=payload,
            value_text=None,
            is_secret=False,
            is_active=True,
        ),
    )


def _jobs(db: Session) -> list[dict[str, Any]]:
    return job_log_store.read_json_list(
        db, domain_settings_service.geocoding_settings, GEOCODE_JOB_KEY
    )


def _save_jobs(db: Session, jobs: list[dict[str, Any]]) -> None:
    job_log_store.save_json_list(
        db,
        domain_settings_service.geocoding_settings,
        GEOCODE_JOB_KEY,
        jobs,
        limit=100,
        is_secret=False,
        is_active=True,
    )


def _log_rows(db: Session) -> list[dict[str, Any]]:
    rows = _setting_json(db, GEOCODE_LOG_KEY, [])
    if not isinstance(rows, list):
        return []
    return [item for item in rows if isinstance(item, dict)]


def _save_log_rows(db: Session, rows: list[dict[str, Any]]) -> None:
    _upsert_json(db, GEOCODE_LOG_KEY, rows[:1000])


def list_log_rows(db: Session, *, limit: int = 150) -> list[dict[str, Any]]:
    return _log_rows(db)[: max(1, limit)]


def parse_filters(form: dict[str, Any]) -> GeocodeFilters:
    return GeocodeFilters(
        date_from=(str(form.get("date_from") or "").strip() or None),
        date_to=(str(form.get("date_to") or "").strip() or None),
        subscriber_status=(str(form.get("subscriber_status") or "").strip() or None),
        overwrite_existing=bool(form.get("overwrite_existing")),
    )


def _query_candidates(db: Session, filters: GeocodeFilters) -> list[Address]:
    stmt = select(Address).join(Subscriber, Subscriber.id == Address.subscriber_id)
    stmt = stmt.where(Subscriber.is_active.is_(True))

    if filters.subscriber_status:
        try:
            stmt = stmt.where(Subscriber.status == SubscriberStatus(filters.subscriber_status))
        except ValueError:
            return []

    date_from = _parse_date(filters.date_from)
    date_to = _parse_date(filters.date_to, end=True)
    if date_from:
        stmt = stmt.where(Address.created_at >= date_from)
    if date_to:
        stmt = stmt.where(Address.created_at < date_to)

    if not filters.overwrite_existing:
        stmt = stmt.where((Address.latitude.is_(None)) | (Address.longitude.is_(None)))

    stmt = stmt.where(Address.address_line1.isnot(None))
    return db.scalars(stmt.order_by(Address.created_at.desc())).all()


def create_job(db: Session, *, filters: GeocodeFilters, actor_id: str | None) -> dict[str, Any]:
    candidates = _query_candidates(db, filters)
    job = {
        "job_id": str(uuid.uuid4()),
        "status": "queued",
        "queued_at": _now().isoformat(),
        "started_at": None,
        "completed_at": None,
        "progress_percent": 0,
        "counts": {"success": 0, "failed": 0, "skipped": 0},
        "total": len(candidates),
        "filters": {
            "date_from": filters.date_from,
            "date_to": filters.date_to,
            "subscriber_status": filters.subscriber_status,
            "overwrite_existing": filters.overwrite_existing,
        },
        "actor_id": actor_id,
        "error": None,
    }
    jobs = _jobs(db)
    jobs.insert(0, job)
    _save_jobs(db, jobs)
    return job


def get_job(db: Session, job_id: str) -> dict[str, Any] | None:
    return job_log_store.get_job(_jobs(db), job_id)


def _upsert_job(db: Session, payload: dict[str, Any]) -> dict[str, Any]:
    jobs, merged = job_log_store.upsert_job(_jobs(db), payload)
    _save_jobs(db, jobs)
    return merged


def _rps_limit(db: Session) -> int:
    value = geocoding_service._setting_int(db, "requests_per_second", 1)  # noqa: SLF001
    return max(1, int(value))


def _address_payload(address: Address) -> dict[str, Any]:
    return {
        "address_line1": address.address_line1,
        "address_line2": address.address_line2,
        "city": address.city,
        "region": address.region,
        "postal_code": address.postal_code,
        "country_code": address.country_code,
    }


def execute_job(db: Session, *, job_id: str) -> dict[str, Any]:
    job = get_job(db, job_id)
    if not job:
        raise ValueError("Geocode job not found")

    filters = GeocodeFilters(
        date_from=job.get("filters", {}).get("date_from"),
        date_to=job.get("filters", {}).get("date_to"),
        subscriber_status=job.get("filters", {}).get("subscriber_status"),
        overwrite_existing=bool(job.get("filters", {}).get("overwrite_existing")),
    )
    candidates = _query_candidates(db, filters)

    running = _upsert_job(
        db,
        {
            **job,
            "status": "running",
            "started_at": _now().isoformat(),
            "error": None,
            "total": len(candidates),
            "progress_percent": 0,
            "counts": {"success": 0, "failed": 0, "skipped": 0},
        },
    )

    delay_seconds = 1.0 / _rps_limit(db)
    rows = _log_rows(db)
    success = 0
    failed = 0
    skipped = 0

    for index, address in enumerate(candidates, start=1):
        subscriber_name = address.subscriber.full_name if address.subscriber else "Subscriber"
        line = {
            "job_id": job_id,
            "subscriber_id": str(address.subscriber_id),
            "subscriber_name": subscriber_name,
            "address": ", ".join([p for p in [address.address_line1, address.city, address.region] if p]),
            "latitude": address.latitude,
            "longitude": address.longitude,
            "status": "skipped",
            "message": "",
            "created_at": _now().isoformat(),
        }

        if (not filters.overwrite_existing) and address.latitude is not None and address.longitude is not None:
            skipped += 1
            line["status"] = "skipped"
            line["message"] = "Coordinates already exist"
        else:
            try:
                result = geocoding_service.geocode_address(db, _address_payload(address))
                lat = result.get("latitude")
                lon = result.get("longitude")
                if lat is None or lon is None:
                    skipped += 1
                    line["status"] = "skipped"
                    line["message"] = "No geocode result"
                else:
                    address.latitude = float(lat)
                    address.longitude = float(lon)
                    success += 1
                    line["status"] = "success"
                    line["latitude"] = address.latitude
                    line["longitude"] = address.longitude
                    line["message"] = "Coordinates updated"
                    db.commit()
            except Exception as exc:
                db.rollback()
                failed += 1
                line["status"] = "failed"
                line["message"] = str(exc)

        rows.insert(0, line)
        _save_log_rows(db, rows)

        progress = int(index * 100 / max(1, len(candidates)))
        running = _upsert_job(
            db,
            {
                **running,
                "counts": {"success": success, "failed": failed, "skipped": skipped},
                "progress_percent": progress,
            },
        )
        time.sleep(delay_seconds)

    # Keep GIS markers in sync with updated address coordinates.
    try:
        gis_sync_service.geo_sync.sync_addresses(db, deactivate_missing=False)
    except Exception as exc:
        logger.error("GIS sync after geocoding failed: %s", exc)
        db.rollback()

    return _upsert_job(
        db,
        {
            **running,
            "status": "completed",
            "completed_at": _now().isoformat(),
            "progress_percent": 100,
            "counts": {"success": success, "failed": failed, "skipped": skipped},
        },
    )


def build_page_state(db: Session) -> dict[str, Any]:
    jobs = _jobs(db)
    return {
        "jobs": jobs[:20],
        "log_rows": list_log_rows(db, limit=200),
        "subscriber_statuses": [status.value for status in SubscriberStatus],
        "providers": ["nominatim", "google", "mapbox"],
        "current_provider": geocoding_service._setting_value(db, "provider") or "nominatim",  # noqa: SLF001
    }
