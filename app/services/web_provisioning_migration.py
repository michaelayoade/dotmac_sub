"""Service migration helpers for provisioning tooling."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.models.audit import AuditActorType
from app.models.catalog import CatalogOffer, Subscription
from app.models.network import IPAssignment, IpPool, IPVersion, OntAssignment, PonPort
from app.models.network_monitoring import PopSite
from app.models.subscriber import Reseller, Subscriber, SubscriberStatus
from app.schemas.audit import AuditEventCreate
from app.services import audit as audit_service
from app.services import domain_settings as domain_settings_service
from app.services import job_log_store
from app.services.catalog.subscriptions import apply_offer_radius_profile
from app.services.radius import sync_account_credentials_to_radius

SERVICE_MIGRATION_JOBS_KEY = "service_migration_jobs_log"
SERVICE_MIGRATION_DEFAULT_LIMIT = 500


@dataclass
class MigrationFilters:
    reseller_id: str | None
    pop_site_id: str | None
    subscriber_status: str | None
    current_offer_id: str | None
    current_nas_device_id: str | None
    query: str | None


@dataclass
class MigrationTargets:
    offer_id: str | None
    nas_device_id: str | None
    ip_pool_id: str | None
    pon_port_id: str | None
    scheduled_at: str | None


def _normalize(value: str | None) -> str | None:
    text = (value or "").strip()
    return text or None


def parse_filters(form: dict[str, Any]) -> MigrationFilters:
    return MigrationFilters(
        reseller_id=_normalize(str(form.get("reseller_id") or "")),
        pop_site_id=_normalize(str(form.get("pop_site_id") or "")),
        subscriber_status=_normalize(str(form.get("subscriber_status") or "")),
        current_offer_id=_normalize(str(form.get("current_offer_id") or "")),
        current_nas_device_id=_normalize(str(form.get("current_nas_device_id") or "")),
        query=_normalize(str(form.get("query") or "")),
    )


def parse_targets(form: dict[str, Any]) -> MigrationTargets:
    return MigrationTargets(
        offer_id=_normalize(str(form.get("target_offer_id") or "")),
        nas_device_id=_normalize(str(form.get("target_nas_device_id") or "")),
        ip_pool_id=_normalize(str(form.get("target_ip_pool_id") or "")),
        pon_port_id=_normalize(str(form.get("target_pon_port_id") or "")),
        scheduled_at=_normalize(str(form.get("scheduled_at") or "")),
    )


def _parse_scheduled_at(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _selected_ids(form_data: Any) -> list[str]:
    raw_values = []
    if hasattr(form_data, "getlist"):
        raw_values.extend(form_data.getlist("selected_ids"))
    if hasattr(form_data, "get"):
        single = form_data.get("selected_ids")
        if single and single not in raw_values:
            raw_values.append(single)

    out: list[str] = []
    seen: set[str] = set()
    for item in raw_values:
        text = str(item or "").strip()
        if not text:
            continue
        try:
            normalized = str(uuid.UUID(text))
        except ValueError:
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        out.append(normalized)
    return out


def parse_selected_ids(form_data: Any) -> list[str]:
    return _selected_ids(form_data)


def _candidate_subscribers(db: Session, filters: MigrationFilters, *, limit: int) -> list[Subscriber]:
    stmt = (
        select(Subscriber)
        .options(
            joinedload(Subscriber.subscriptions).joinedload(Subscription.offer),
            joinedload(Subscriber.subscriptions).joinedload(Subscription.provisioning_nas_device),
            joinedload(Subscriber.access_credentials),
            joinedload(Subscriber.ip_assignments).joinedload(IPAssignment.ipv4_address),
            joinedload(Subscriber.ip_assignments).joinedload(IPAssignment.ipv6_address),
            joinedload(Subscriber.ont_assignments).joinedload(OntAssignment.pon_port),
        )
        .where(Subscriber.is_active.is_(True))
    )

    if filters.reseller_id:
        stmt = stmt.where(Subscriber.reseller_id == filters.reseller_id)
    if filters.subscriber_status:
        try:
            stmt = stmt.where(Subscriber.status == SubscriberStatus(filters.subscriber_status))
        except ValueError:
            return []

    subscribers = list(db.scalars(stmt.order_by(Subscriber.created_at.desc()).limit(max(1, limit))).unique().all())

    if filters.query:
        needle = filters.query.lower()

        def matches(sub: Subscriber) -> bool:
            values = [
                str(sub.id),
                sub.subscriber_number or "",
                sub.account_number or "",
                sub.display_name or "",
                sub.full_name,
                sub.email or "",
                sub.phone or "",
            ]
            values.extend(c.username or "" for c in sub.access_credentials)
            values.extend(s.login or "" for s in sub.subscriptions)
            return any(needle in value.lower() for value in values if value)

        subscribers = [sub for sub in subscribers if matches(sub)]

    if filters.current_offer_id:
        subscribers = [
            sub
            for sub in subscribers
            if any(str(s.offer_id) == filters.current_offer_id for s in sub.subscriptions)
        ]

    if filters.current_nas_device_id:
        subscribers = [
            sub
            for sub in subscribers
            if any(str(s.provisioning_nas_device_id or "") == filters.current_nas_device_id for s in sub.subscriptions)
        ]

    if filters.pop_site_id:
        subscribers = [
            sub
            for sub in subscribers
            if any(
                s.provisioning_nas_device is not None
                and str(s.provisioning_nas_device.pop_site_id or "") == filters.pop_site_id
                for s in sub.subscriptions
            )
        ]

    return subscribers


def _current_subscription(subscriber: Subscriber) -> Subscription | None:
    if not subscriber.subscriptions:
        return None
    active = [s for s in subscriber.subscriptions if str(getattr(s.status, "value", s.status)) == "active"]
    if active:
        active.sort(key=lambda row: row.start_at or row.created_at or datetime.min.replace(tzinfo=UTC), reverse=True)
        return active[0]
    rows = list(subscriber.subscriptions)
    rows.sort(key=lambda row: row.created_at or datetime.min.replace(tzinfo=UTC), reverse=True)
    return rows[0]


def _subscriber_login(subscriber: Subscriber, subscription: Subscription | None) -> str:
    if subscription and subscription.login:
        return subscription.login
    for cred in subscriber.access_credentials:
        if cred.is_active and cred.username:
            return cred.username
    return ""


def _subscriber_ips(subscriber: Subscriber) -> str:
    parts: list[str] = []
    for assignment in subscriber.ip_assignments:
        if not assignment.is_active:
            continue
        if assignment.ip_version == IPVersion.ipv4 and assignment.ipv4_address is not None:
            parts.append(str(assignment.ipv4_address.address))
        if assignment.ip_version == IPVersion.ipv6 and assignment.ipv6_address is not None:
            parts.append(str(assignment.ipv6_address.address))
    return ", ".join(parts)


def _subscriber_olt_port(subscriber: Subscriber) -> str:
    for assignment in subscriber.ont_assignments:
        if not assignment.active:
            continue
        if assignment.pon_port is not None:
            return assignment.pon_port.name
    return ""


def _table_row(subscriber: Subscriber) -> dict[str, Any]:
    current = _current_subscription(subscriber)
    return {
        "subscriber_id": str(subscriber.id),
        "status": subscriber.status.value if subscriber.status else "",
        "portal_login": _subscriber_login(subscriber, current),
        "full_name": subscriber.full_name,
        "phone": subscriber.phone or "",
        "current_plan": current.offer.name if current and current.offer else "",
        "current_offer_id": str(current.offer_id) if current else "",
        "assigned_ips": _subscriber_ips(subscriber),
        "router_nas": current.provisioning_nas_device.name if current and current.provisioning_nas_device else "",
        "router_nas_id": str(current.provisioning_nas_device_id) if current and current.provisioning_nas_device_id else "",
        "mac_address": current.mac_address if current and current.mac_address else "",
        "olt_port": _subscriber_olt_port(subscriber),
    }


def _require_targets(targets: MigrationTargets) -> None:
    if not any([targets.offer_id, targets.nas_device_id, targets.ip_pool_id, targets.pon_port_id]):
        raise HTTPException(status_code=400, detail="Select at least one migration target")


def build_selection_table(db: Session, *, filters: MigrationFilters, limit: int = SERVICE_MIGRATION_DEFAULT_LIMIT) -> dict[str, Any]:
    subscribers = _candidate_subscribers(db, filters, limit=limit)
    rows = [_table_row(subscriber) for subscriber in subscribers]
    return {
        "rows": rows,
        "total": len(rows),
    }


def _preview_changes(db: Session, row: dict[str, Any], targets: MigrationTargets) -> dict[str, Any]:
    out = {
        "subscriber_id": row["subscriber_id"],
        "full_name": row["full_name"],
        "changes": [],
    }

    if targets.offer_id:
        target_offer = db.get(CatalogOffer, targets.offer_id)
        out["changes"].append(
            {
                "field": "Plan",
                "from": row.get("current_plan") or "-",
                "to": target_offer.name if target_offer else targets.offer_id,
            }
        )
    if targets.nas_device_id:
        from app.models.catalog import NasDevice

        target_nas = db.get(NasDevice, targets.nas_device_id)
        out["changes"].append(
            {
                "field": "Router/NAS",
                "from": row.get("router_nas") or "-",
                "to": target_nas.name if target_nas else targets.nas_device_id,
            }
        )
    if targets.ip_pool_id:
        target_pool = db.get(IpPool, targets.ip_pool_id)
        out["changes"].append(
            {
                "field": "IP Pool",
                "from": "Current pools",
                "to": target_pool.name if target_pool else targets.ip_pool_id,
            }
        )
    if targets.pon_port_id:
        target_port = db.get(PonPort, targets.pon_port_id)
        out["changes"].append(
            {
                "field": "OLT Port",
                "from": row.get("olt_port") or "-",
                "to": target_port.name if target_port else targets.pon_port_id,
            }
        )

    return out


def build_preview(
    db: Session,
    *,
    filters: MigrationFilters,
    targets: MigrationTargets,
    selected_ids: list[str],
) -> dict[str, Any]:
    _require_targets(targets)
    table = build_selection_table(db, filters=filters, limit=SERVICE_MIGRATION_DEFAULT_LIMIT)
    row_map = {row["subscriber_id"]: row for row in table["rows"]}

    rows: list[dict[str, Any]] = []
    missing: list[str] = []
    for subscriber_id in selected_ids:
        row = row_map.get(subscriber_id)
        if not row:
            missing.append(subscriber_id)
            continue
        rows.append(_preview_changes(db, row, targets))

    return {
        "rows": rows,
        "selected_count": len(selected_ids),
        "available_count": len(rows),
        "missing": missing,
        "targets": targets.__dict__,
        "filters": filters.__dict__,
        "scheduled_at": _parse_scheduled_at(targets.scheduled_at),
    }


def _jobs(db: Session) -> list[dict[str, Any]]:
    return job_log_store.read_json_list(
        db, domain_settings_service.provisioning_settings, SERVICE_MIGRATION_JOBS_KEY
    )


def _save_jobs(db: Session, jobs: list[dict[str, Any]]) -> None:
    job_log_store.save_json_list(
        db,
        domain_settings_service.provisioning_settings,
        SERVICE_MIGRATION_JOBS_KEY,
        jobs,
        limit=200,
        is_secret=False,
        is_active=True,
    )


def list_jobs(db: Session, *, limit: int = 20) -> list[dict[str, Any]]:
    return _jobs(db)[: max(1, limit)]


def get_job(db: Session, job_id: str) -> dict[str, Any] | None:
    return job_log_store.get_job(_jobs(db), job_id)


def upsert_job(db: Session, payload: dict[str, Any]) -> dict[str, Any]:
    rows, merged = job_log_store.upsert_job(_jobs(db), payload)
    _save_jobs(db, rows)
    return merged


def create_job(
    db: Session,
    *,
    filters: MigrationFilters,
    targets: MigrationTargets,
    selected_ids: list[str],
    actor_id: str | None,
) -> dict[str, Any]:
    _require_targets(targets)
    if not selected_ids:
        raise ValueError("Select at least one subscriber")

    scheduled_at = _parse_scheduled_at(targets.scheduled_at)
    status = "scheduled" if scheduled_at and scheduled_at > datetime.now(UTC) else "queued"

    return upsert_job(
        db,
        {
            "job_id": str(uuid.uuid4()),
            "status": status,
            "progress_percent": 0,
            "queued_at": datetime.now(UTC).isoformat(),
            "started_at": None,
            "completed_at": None,
            "scheduled_at": scheduled_at.isoformat() if scheduled_at else None,
            "error": None,
            "result": None,
            "counts": {"migrated": 0, "failed": 0, "skipped": 0},
            "selected_ids": selected_ids,
            "filters": filters.__dict__,
            "targets": targets.__dict__,
            "actor_id": actor_id,
            "failed_items": [],
            "manual_intervention": [],
        },
    )


def _log_migration_audit(
    db: Session,
    *,
    actor_id: str | None,
    job_id: str,
    is_success: bool,
    summary: dict[str, Any],
) -> None:
    payload = AuditEventCreate(
        actor_type=AuditActorType.user if actor_id else AuditActorType.system,
        actor_id=actor_id,
        action="service_migration",
        entity_type="service_migration",
        entity_id=job_id,
        status_code=200 if is_success else 500,
        is_success=is_success,
        metadata_=summary,
    )
    audit_service.audit_events.create(db=db, payload=payload)


def _update_ip_pool_for_subscriber(db: Session, *, subscriber_id: str, pool_id: str) -> int:
    target_pool = db.get(IpPool, pool_id)
    if not target_pool:
        raise ValueError("Target IP pool not found")

    moved = 0
    assignments = db.scalars(
        select(IPAssignment)
        .where(IPAssignment.subscriber_id == subscriber_id)
        .where(IPAssignment.is_active.is_(True))
    ).all()
    for assignment in assignments:
        if assignment.ip_version == IPVersion.ipv4 and assignment.ipv4_address is not None:
            assignment.ipv4_address.pool_id = target_pool.id
            moved += 1
        if assignment.ip_version == IPVersion.ipv6 and assignment.ipv6_address is not None:
            assignment.ipv6_address.pool_id = target_pool.id
            moved += 1
    return moved


def _update_olt_port_for_subscriber(db: Session, *, subscriber_id: str, pon_port_id: str) -> int:
    target_port = db.get(PonPort, pon_port_id)
    if not target_port:
        raise ValueError("Target OLT port not found")

    moved = 0
    assignments = db.scalars(
        select(OntAssignment)
        .where(OntAssignment.subscriber_id == subscriber_id)
        .where(OntAssignment.active.is_(True))
    ).all()
    for assignment in assignments:
        assignment.pon_port_id = target_port.id
        moved += 1
    return moved


def execute_job(db: Session, *, job_id: str) -> dict[str, Any]:
    job = get_job(db, job_id)
    if not job:
        raise ValueError("Service migration job not found")

    filters = MigrationFilters(**(job.get("filters") or {}))
    targets = MigrationTargets(**(job.get("targets") or {}))
    selected_ids = [str(item) for item in (job.get("selected_ids") or [])]
    actor_id = str(job.get("actor_id") or "").strip() or None

    upsert_job(
        db,
        {
            "job_id": job_id,
            "status": "running",
            "started_at": datetime.now(UTC).isoformat(),
            "error": None,
        },
    )

    migrated = 0
    failed = 0
    skipped = 0
    failed_items: list[dict[str, Any]] = []

    total = len(selected_ids)

    for idx, subscriber_id in enumerate(selected_ids, start=1):
        subscriber_name = subscriber_id
        try:
            subscriber = db.get(Subscriber, subscriber_id)
            if not subscriber or not subscriber.is_active:
                skipped += 1
                continue
            subscriber_name = subscriber.full_name

            current = db.scalars(
                select(Subscription)
                .where(Subscription.subscriber_id == subscriber.id)
                .order_by(Subscription.created_at.desc())
            ).first()
            if not current:
                skipped += 1
                continue

            if targets.offer_id:
                previous_offer_id = current.offer_id
                current.offer_id = uuid.UUID(targets.offer_id)
                apply_offer_radius_profile(
                    db,
                    current,
                    previous_offer_id=previous_offer_id,
                )
            if targets.nas_device_id:
                current.provisioning_nas_device_id = uuid.UUID(targets.nas_device_id)

            moved_ip = 0
            moved_olt = 0
            if targets.ip_pool_id:
                moved_ip = _update_ip_pool_for_subscriber(db, subscriber_id=subscriber_id, pool_id=targets.ip_pool_id)
            if targets.pon_port_id:
                moved_olt = _update_olt_port_for_subscriber(db, subscriber_id=subscriber_id, pon_port_id=targets.pon_port_id)

            # ensure radius can be re-provisioned before committing this subscriber migration
            db.flush()
            sync_account_credentials_to_radius(db, subscriber.id)

            migrated += 1
            db.commit()

            # keep record of rows requiring manual checks (e.g., nothing changed in optional dimensions)
            if moved_ip == 0 and targets.ip_pool_id:
                failed_items.append(
                    {
                        "subscriber_id": subscriber_id,
                        "subscriber_name": subscriber.full_name,
                        "error": "No active IP assignment found for pool migration",
                    }
                )
        except Exception as exc:
            db.rollback()
            failed += 1
            failed_items.append(
                {
                    "subscriber_id": subscriber_id,
                    "subscriber_name": subscriber_name,
                    "error": str(exc),
                }
            )

        if total:
            upsert_job(
                db,
                {
                    "job_id": job_id,
                    "progress_percent": int((idx / total) * 100),
                    "counts": {"migrated": migrated, "failed": failed, "skipped": skipped},
                    "failed_items": failed_items,
                    "manual_intervention": failed_items,
                },
            )

    report = {
        "total_selected": total,
        "migrated": migrated,
        "failed": failed,
        "skipped": skipped,
        "failed_items": failed_items,
        "manual_intervention": failed_items,
        "targets": targets.__dict__,
        "filters": filters.__dict__,
    }

    final = upsert_job(
        db,
        {
            "job_id": job_id,
            "status": "completed" if failed == 0 else "partial",
            "completed_at": datetime.now(UTC).isoformat(),
            "progress_percent": 100,
            "counts": {"migrated": migrated, "failed": failed, "skipped": skipped},
            "result": report,
            "failed_items": failed_items,
            "manual_intervention": failed_items,
        },
    )

    _log_migration_audit(
        db,
        actor_id=actor_id,
        job_id=job_id,
        is_success=(failed == 0),
        summary=report,
    )
    return final


def page_options(db: Session) -> dict[str, Any]:
    from app.models.catalog import NasDevice

    offers = db.scalars(select(CatalogOffer).where(CatalogOffer.is_active.is_(True)).order_by(CatalogOffer.name.asc())).all()
    resellers = db.scalars(select(Reseller).where(Reseller.is_active.is_(True)).order_by(Reseller.name.asc())).all()
    pop_sites = db.scalars(select(PopSite).where(PopSite.is_active.is_(True)).order_by(PopSite.name.asc())).all()
    nas_devices = db.scalars(select(NasDevice).where(NasDevice.is_active.is_(True)).order_by(NasDevice.name.asc())).all()
    ip_pools = db.scalars(select(IpPool).where(IpPool.is_active.is_(True)).order_by(IpPool.name.asc())).all()
    pon_ports = db.scalars(select(PonPort).where(PonPort.is_active.is_(True)).order_by(PonPort.name.asc())).all()
    return {
        "offers": offers,
        "resellers": resellers,
        "pop_sites": pop_sites,
        "nas_devices": nas_devices,
        "ip_pools": ip_pools,
        "pon_ports": pon_ports,
        "subscriber_statuses": [status.value for status in SubscriberStatus],
        "jobs": list_jobs(db, limit=20),
    }
