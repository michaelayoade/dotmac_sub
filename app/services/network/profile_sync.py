"""Dry-run planning for tariff/offer driven OLT profile bundles."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, joinedload

from app.models.catalog import (
    AccessType,
    CatalogOffer,
    OfferStatus,
    PlanCategory,
    Subscription,
    SubscriptionStatus,
)
from app.models.network import OLTDevice, OltProfileBundle, OltProfileSyncTask
from app.services.network.olt_command_gen import (
    generate_dba_profile_commands,
    generate_line_profile_commands,
    generate_service_profile_commands,
    generate_traffic_table_commands,
)
from app.services.network.profile_apply_workflow import (
    ProfileApplyPlan,
    ProfileCommandGroup,
    build_profile_apply_plan,
)
from app.services.network.profile_id_allocator import (
    ProfileIdAllocation,
    allocate_dba_profile_id,
    allocate_line_profile_id,
    allocate_service_profile_id,
    allocate_traffic_table_id,
)


class OfferProfileSyncError(ValueError):
    """Raised when an offer cannot be represented as OLT profiles."""


class OfferProfileSyncTaskError(ValueError):
    """Raised when a pending profile sync task cannot be created or updated."""


PENDING_SYNC_TASK_STATUSES = ("pending", "approved", "scheduled")


@dataclass(frozen=True)
class OfferProfileBundle:
    """OLT profile IDs generated for one catalog offer."""

    offer_id: str
    offer_name: str
    vlan_id: int
    download_kbps: int
    upload_kbps: int
    dba_profile_id: int
    download_traffic_table_id: int
    upload_traffic_table_id: int
    line_profile_id: int
    service_profile_id: int
    gem_id: int
    tcont_id: int
    checksum: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "offer_id": self.offer_id,
            "offer_name": self.offer_name,
            "vlan_id": self.vlan_id,
            "download_kbps": self.download_kbps,
            "upload_kbps": self.upload_kbps,
            "dba_profile_id": self.dba_profile_id,
            "download_traffic_table_id": self.download_traffic_table_id,
            "upload_traffic_table_id": self.upload_traffic_table_id,
            "line_profile_id": self.line_profile_id,
            "service_profile_id": self.service_profile_id,
            "gem_id": self.gem_id,
            "tcont_id": self.tcont_id,
            "checksum": self.checksum,
        }


@dataclass(frozen=True)
class OfferProfileSyncPlan:
    """Dry-run plan for syncing one offer to one OLT profile bundle."""

    bundle: OfferProfileBundle
    apply_plan: ProfileApplyPlan
    allocations: tuple[ProfileIdAllocation, ...]


@dataclass(frozen=True)
class OfferProfileSyncTaskRequest:
    """Request to stage a reviewed profile sync for one OLT."""

    olt_id: str
    vlan_id: int


def _command_plan_to_dict(plan: ProfileApplyPlan) -> dict[str, Any]:
    return {
        "name": plan.name,
        "groups": [
            {
                "step": group.step,
                "commands": list(group.commands),
                "requires_config_mode": group.requires_config_mode,
            }
            for group in plan.groups
        ],
    }


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value) or "")


def _require_positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise OfferProfileSyncError(f"{field} must be a positive integer")
    return value


def _offer_id(offer: Any) -> str:
    value = getattr(offer, "id", None)
    if value is None:
        raise OfferProfileSyncError("offer.id is required")
    return str(value)


def _offer_name(offer: Any) -> str:
    value = str(getattr(offer, "name", "") or "").strip()
    if not value:
        raise OfferProfileSyncError("offer.name is required")
    return value


def _validate_offer_is_syncable(offer: Any) -> None:
    if getattr(offer, "is_active", True) is False:
        raise OfferProfileSyncError("Only active offers can be synced to OLT profiles")
    if _enum_value(getattr(offer, "status", OfferStatus.active)) != OfferStatus.active.value:
        raise OfferProfileSyncError("Only active offers can be synced to OLT profiles")
    if _enum_value(getattr(offer, "access_type", AccessType.fiber)) != AccessType.fiber.value:
        raise OfferProfileSyncError("Only fiber offers can be synced to OLT profiles")
    if _enum_value(getattr(offer, "plan_category", PlanCategory.internet)) != PlanCategory.internet.value:
        raise OfferProfileSyncError("Only internet offers can be synced to OLT profiles")


def _speed_kbps(offer: Any, field: str) -> int:
    mbps = _require_positive_int(getattr(offer, field, None), field)
    return mbps * 1000


def _profile_safe_token(value: str, *, fallback: str = "OFFER") -> str:
    token = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip().upper()).strip("_")
    return token or fallback


def _profile_names(offer: Any, download_kbps: int, upload_kbps: int) -> dict[str, str]:
    raw_base = str(getattr(offer, "code", "") or "").strip() or _offer_name(offer)
    base = _profile_safe_token(raw_base)
    speed = f"{download_kbps // 1000}D_{upload_kbps // 1000}U"
    raw_stem = f"{base}_{speed}".strip("_")
    stem = raw_stem[:36].strip("_")
    if len(raw_stem) > 36:
        suffix = hashlib.sha256(raw_stem.encode()).hexdigest()[:8].upper()
        stem = f"{raw_stem[:27].strip('_')}_{suffix}"
    return {
        "dba": f"DOTMAC_DBA_{stem}"[:64],
        "traffic_down": f"DOTMAC_TT_D_{stem}"[:64],
        "traffic_up": f"DOTMAC_TT_U_{stem}"[:64],
        "line": f"DOTMAC_LINE_{stem}"[:64],
        "service": f"DOTMAC_SRV_{stem}"[:64],
    }


def _checksum(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def build_offer_profile_sync_plan(
    offer: Any,
    *,
    vlan_id: int,
    live_dba_profiles: list[Any] | tuple[Any, ...] = (),
    live_traffic_tables: list[Any] | tuple[Any, ...] = (),
    live_line_profiles: list[Any] | tuple[Any, ...] = (),
    live_service_profiles: list[Any] | tuple[Any, ...] = (),
    gem_id: int = 1,
    tcont_id: int = 1,
    eth_ports: int = 4,
    pots_ports: int = 0,
) -> OfferProfileSyncPlan:
    """Build a dry-run profile bundle plan for a catalog offer.

    The returned apply plan can be fed into ``apply_profile_bundle``. This
    function does not write to the database or to the OLT.
    """
    _validate_offer_is_syncable(offer)
    vlan_id = _require_positive_int(vlan_id, "vlan_id")
    gem_id = _require_positive_int(gem_id, "gem_id")
    if not isinstance(tcont_id, int) or tcont_id < 0:
        raise OfferProfileSyncError("tcont_id must be a non-negative integer")

    offer_id = _offer_id(offer)
    offer_name = _offer_name(offer)
    download_kbps = _speed_kbps(offer, "speed_download_mbps")
    upload_kbps = _speed_kbps(offer, "speed_upload_mbps")
    names = _profile_names(offer, download_kbps, upload_kbps)

    dba_alloc = allocate_dba_profile_id(live_dba_profiles)
    traffic_down_alloc = allocate_traffic_table_id(live_traffic_tables)
    traffic_up_alloc = allocate_traffic_table_id(
        live_traffic_tables,
        reserved_ids=[traffic_down_alloc.allocated_id],
    )
    line_alloc = allocate_line_profile_id(live_line_profiles)
    service_alloc = allocate_service_profile_id(live_service_profiles)

    bundle_payload = {
        "offer_id": offer_id,
        "offer_name": offer_name,
        "vlan_id": vlan_id,
        "download_kbps": download_kbps,
        "upload_kbps": upload_kbps,
        "dba_profile_id": dba_alloc.allocated_id,
        "download_traffic_table_id": traffic_down_alloc.allocated_id,
        "upload_traffic_table_id": traffic_up_alloc.allocated_id,
        "line_profile_id": line_alloc.allocated_id,
        "service_profile_id": service_alloc.allocated_id,
        "gem_id": gem_id,
        "tcont_id": tcont_id,
    }
    bundle = OfferProfileBundle(checksum=_checksum(bundle_payload), **bundle_payload)

    command_groups = (
        ProfileCommandGroup(
            step="Create DBA profile",
            commands=tuple(
                generate_dba_profile_commands(
                    profile_id=bundle.dba_profile_id,
                    name=names["dba"],
                    profile_type="type3",
                    assured_bw=upload_kbps,
                    max_bw=upload_kbps,
                )
            ),
        ),
        ProfileCommandGroup(
            step="Create download traffic table",
            commands=tuple(
                generate_traffic_table_commands(
                    index=bundle.download_traffic_table_id,
                    name=names["traffic_down"],
                    cir=download_kbps,
                    pir=download_kbps,
                )
            ),
        ),
        ProfileCommandGroup(
            step="Create upload traffic table",
            commands=tuple(
                generate_traffic_table_commands(
                    index=bundle.upload_traffic_table_id,
                    name=names["traffic_up"],
                    cir=upload_kbps,
                    pir=upload_kbps,
                )
            ),
        ),
        ProfileCommandGroup(
            step="Create line profile",
            commands=tuple(
                generate_line_profile_commands(
                    profile_id=bundle.line_profile_id,
                    name=names["line"],
                    tcont_id=tcont_id,
                    dba_profile_id=bundle.dba_profile_id,
                    gem_id=gem_id,
                    vlan=vlan_id,
                )
            ),
        ),
        ProfileCommandGroup(
            step="Create service profile",
            commands=tuple(
                generate_service_profile_commands(
                    profile_id=bundle.service_profile_id,
                    name=names["service"],
                    eth_ports=eth_ports,
                    pots_ports=pots_ports,
                    vlan=vlan_id,
                )
            ),
        ),
    )
    return OfferProfileSyncPlan(
        bundle=bundle,
        apply_plan=build_profile_apply_plan(offer_name, command_groups),
        allocations=(
            dba_alloc,
            traffic_down_alloc,
            traffic_up_alloc,
            line_alloc,
            service_alloc,
        ),
    )


def list_syncable_catalog_offers(db: Session) -> list[CatalogOffer]:
    """Return active fiber internet offers eligible for profile sync planning."""
    stmt = (
        select(CatalogOffer)
        .where(
            CatalogOffer.is_active.is_(True),
            CatalogOffer.status == OfferStatus.active,
            CatalogOffer.access_type == AccessType.fiber,
            CatalogOffer.plan_category == PlanCategory.internet,
            CatalogOffer.speed_download_mbps.is_not(None),
            CatalogOffer.speed_upload_mbps.is_not(None),
        )
        .order_by(CatalogOffer.name.asc())
    )
    return list(db.scalars(stmt).all())


def resolve_profile_bundle_for_offer(
    db: Session,
    *,
    olt_id: Any,
    offer_id: Any,
) -> OltProfileBundle | None:
    """Return the active OLT profile bundle for one OLT+offer."""
    if olt_id is None or offer_id is None:
        return None
    return db.scalars(
        select(OltProfileBundle)
        .where(OltProfileBundle.olt_id == olt_id)
        .where(OltProfileBundle.offer_id == offer_id)
        .where(OltProfileBundle.is_active.is_(True))
        .limit(1)
    ).first()


def resolve_profile_bundle_for_active_subscription(
    db: Session,
    *,
    olt_id: Any,
    subscriber_id: Any,
) -> OltProfileBundle | None:
    """Return the active bundle for the subscriber's latest active offer on an OLT."""
    if olt_id is None or subscriber_id is None:
        return None
    subscription = db.scalars(
        select(Subscription)
        .where(Subscription.subscriber_id == subscriber_id)
        .where(Subscription.status == SubscriptionStatus.active)
        .order_by(Subscription.created_at.desc())
        .limit(1)
    ).first()
    if subscription is None:
        return None
    return resolve_profile_bundle_for_offer(
        db,
        olt_id=olt_id,
        offer_id=subscription.offer_id,
    )


def enqueue_offer_profile_sync_tasks(
    db: Session,
    *,
    offer: CatalogOffer,
    requests: list[OfferProfileSyncTaskRequest] | tuple[OfferProfileSyncTaskRequest, ...],
    trigger: str = "offer_change",
    requested_by: str | None = None,
) -> list[OltProfileSyncTask]:
    """Create pending review tasks for an auto-sync-enabled offer.

    This intentionally does not read or mutate OLTs. Admin approval/scheduling
    must happen before any live preview or apply workflow is run.
    """
    if not getattr(offer, "olt_profile_auto_sync_enabled", False):
        return []
    _validate_offer_is_syncable(offer)

    created: list[OltProfileSyncTask] = []
    for request in requests:
        vlan_id = _require_positive_int(request.vlan_id, "vlan_id")
        olt = db.get(OLTDevice, request.olt_id)
        if olt is None:
            raise OfferProfileSyncTaskError(f"OLT {request.olt_id} not found")

        existing = _existing_open_sync_task(db, olt_id=olt.id, offer_id=offer.id)
        if existing is not None:
            continue

        task = OltProfileSyncTask(
            olt_id=olt.id,
            offer_id=offer.id,
            status="pending",
            trigger=str(trigger or "offer_change")[:80],
            requested_by=requested_by,
            preview_payload=_pending_task_payload(offer, olt=olt, vlan_id=vlan_id),
        )
        db.add(task)
        created.append(task)
    db.flush()
    return created


def enqueue_offer_profile_sync_tasks_for_existing_bundles(
    db: Session,
    *,
    offer: CatalogOffer,
    trigger: str = "offer_change",
    requested_by: str | None = None,
) -> list[OltProfileSyncTask]:
    """Stage pending sync tasks for OLTs that already have this offer bundled."""
    if not getattr(offer, "olt_profile_auto_sync_enabled", False):
        return []
    bundles = db.scalars(
        select(OltProfileBundle)
        .where(OltProfileBundle.offer_id == offer.id)
        .where(OltProfileBundle.is_active.is_(True))
        .order_by(OltProfileBundle.created_at.asc())
    ).all()
    return enqueue_offer_profile_sync_tasks(
        db,
        offer=offer,
        requests=[
            OfferProfileSyncTaskRequest(
                olt_id=str(bundle.olt_id),
                vlan_id=int(bundle.vlan_id),
            )
            for bundle in bundles
        ],
        trigger=trigger,
        requested_by=requested_by,
    )


def approve_profile_sync_task(
    db: Session,
    *,
    task_id: str,
    approved_by: str,
    scheduled_for: datetime | None = None,
) -> OltProfileSyncTask:
    """Approve or schedule a pending profile sync task without applying it."""
    task = db.get(OltProfileSyncTask, task_id)
    if task is None:
        raise OfferProfileSyncTaskError("Profile sync task not found")
    if task.status != "pending":
        raise OfferProfileSyncTaskError(
            f"Only pending profile sync tasks can be approved, got {task.status}"
        )
    task.status = "scheduled" if scheduled_for is not None else "approved"
    task.approved_by = approved_by
    task.approved_at = datetime.now(UTC)
    task.scheduled_for = scheduled_for
    db.flush()
    return task


def cancel_profile_sync_task(
    db: Session,
    *,
    task_id: str,
    cancelled_by: str,
    reason: str | None = None,
) -> OltProfileSyncTask:
    """Cancel an open profile sync task without applying it."""
    task = db.get(OltProfileSyncTask, task_id)
    if task is None:
        raise OfferProfileSyncTaskError("Profile sync task not found")
    if task.status not in PENDING_SYNC_TASK_STATUSES:
        raise OfferProfileSyncTaskError(
            f"Only open profile sync tasks can be cancelled, got {task.status}"
        )
    result_payload = dict(task.result_payload or {})
    result_payload.update(
        {
            "cancelled_by": cancelled_by,
            "cancelled_at": datetime.now(UTC).isoformat(),
        }
    )
    reason_text = str(reason or "").strip()
    if reason_text:
        result_payload["cancel_reason"] = reason_text[:500]
    task.status = "cancelled"
    task.result_payload = result_payload
    db.flush()
    return task


def retry_profile_sync_task(
    db: Session,
    *,
    task_id: str,
    retried_by: str,
    reason: str | None = None,
) -> OltProfileSyncTask:
    """Move a failed profile sync task back to pending review."""
    task = db.get(OltProfileSyncTask, task_id)
    if task is None:
        raise OfferProfileSyncTaskError("Profile sync task not found")
    if task.status != "failed":
        raise OfferProfileSyncTaskError(
            f"Only failed profile sync tasks can be retried, got {task.status}"
        )
    previous_error = task.error
    result_payload = dict(task.result_payload or {})
    retries = list(result_payload.get("retries") or [])
    retry_payload = {
        "retried_by": retried_by,
        "retried_at": datetime.now(UTC).isoformat(),
        "previous_error": previous_error,
    }
    reason_text = str(reason or "").strip()
    if reason_text:
        retry_payload["reason"] = reason_text[:500]
    retries.append({key: value for key, value in retry_payload.items() if value})
    result_payload["retries"] = retries
    task.status = "pending"
    task.error = None
    task.approved_by = None
    task.approved_at = None
    task.scheduled_for = None
    task.result_payload = result_payload
    db.flush()
    return task


def list_profile_sync_tasks(
    db: Session,
    *,
    statuses: tuple[str, ...] | list[str] | None = PENDING_SYNC_TASK_STATUSES,
    limit: int = 100,
) -> list[OltProfileSyncTask]:
    """Return profile sync review tasks with offer and OLT relationships loaded."""
    query = (
        select(OltProfileSyncTask)
        .options(
            joinedload(OltProfileSyncTask.olt),
            joinedload(OltProfileSyncTask.offer),
        )
        .order_by(OltProfileSyncTask.created_at.desc())
        .limit(max(1, min(int(limit or 100), 500)))
    )
    if statuses is not None:
        query = query.where(OltProfileSyncTask.status.in_(tuple(statuses)))
    return list(db.scalars(query))


def list_due_profile_sync_tasks(
    db: Session,
    *,
    now: datetime | None = None,
    limit: int = 25,
) -> list[OltProfileSyncTask]:
    """Return approved or due scheduled tasks ready for execution."""
    now_value = now or datetime.now(UTC)
    return list(
        db.scalars(
            select(OltProfileSyncTask)
            .where(
                or_(
                    OltProfileSyncTask.status == "approved",
                    (
                        (OltProfileSyncTask.status == "scheduled")
                        & (OltProfileSyncTask.scheduled_for <= now_value)
                    ),
                )
            )
            .order_by(
                OltProfileSyncTask.scheduled_for.asc().nullsfirst(),
                OltProfileSyncTask.created_at.asc(),
            )
            .limit(max(1, min(int(limit or 25), 100)))
        )
    )


def _existing_open_sync_task(
    db: Session,
    *,
    olt_id: Any,
    offer_id: Any,
) -> OltProfileSyncTask | None:
    return db.scalars(
        select(OltProfileSyncTask)
        .where(OltProfileSyncTask.olt_id == olt_id)
        .where(OltProfileSyncTask.offer_id == offer_id)
        .where(OltProfileSyncTask.status.in_(PENDING_SYNC_TASK_STATUSES))
        .order_by(OltProfileSyncTask.created_at.desc())
        .limit(1)
    ).first()


def _pending_task_payload(
    offer: CatalogOffer,
    *,
    olt: OLTDevice,
    vlan_id: int,
) -> dict[str, Any]:
    download_kbps = _speed_kbps(offer, "speed_download_mbps")
    upload_kbps = _speed_kbps(offer, "speed_upload_mbps")
    payload = {
        "offer_id": str(offer.id),
        "offer_name": offer.name,
        "offer_code": offer.code,
        "olt_id": str(olt.id),
        "olt_name": olt.name,
        "vlan_id": vlan_id,
        "download_kbps": download_kbps,
        "upload_kbps": upload_kbps,
        "requires_admin_preview": True,
        "mutates_olt": False,
    }
    return {**payload, "checksum": _checksum(payload)}


def upsert_profile_bundle(
    db: Session,
    *,
    olt: OLTDevice,
    sync_plan: OfferProfileSyncPlan,
    wan_profile_id: int | None = None,
    tr069_profile_id: int | None = None,
) -> OltProfileBundle:
    """Persist the generated bundle for one OLT+offer without applying commands."""
    bundle = sync_plan.bundle
    offer_uuid = UUID(bundle.offer_id)
    existing = db.scalars(
        select(OltProfileBundle)
        .where(OltProfileBundle.olt_id == olt.id)
        .where(OltProfileBundle.offer_id == offer_uuid)
    ).first()
    record = existing
    if record is None:
        record = OltProfileBundle(
            olt_id=olt.id,
            offer_id=offer_uuid,
            name=bundle.offer_name,
            checksum=bundle.checksum,
            vlan_id=bundle.vlan_id,
            download_kbps=bundle.download_kbps,
            upload_kbps=bundle.upload_kbps,
            dba_profile_id=bundle.dba_profile_id,
            download_traffic_table_id=bundle.download_traffic_table_id,
            upload_traffic_table_id=bundle.upload_traffic_table_id,
            line_profile_id=bundle.line_profile_id,
            service_profile_id=bundle.service_profile_id,
            wan_profile_id=wan_profile_id,
            tr069_profile_id=tr069_profile_id,
            gem_id=bundle.gem_id,
            tcont_id=bundle.tcont_id,
            command_plan=_command_plan_to_dict(sync_plan.apply_plan),
            drift_status="pending",
            drift_details=None,
            is_active=True,
        )
        db.add(record)
    else:
        record.name = bundle.offer_name
        record.checksum = bundle.checksum
        record.vlan_id = bundle.vlan_id
        record.download_kbps = bundle.download_kbps
        record.upload_kbps = bundle.upload_kbps
        record.dba_profile_id = bundle.dba_profile_id
        record.download_traffic_table_id = bundle.download_traffic_table_id
        record.upload_traffic_table_id = bundle.upload_traffic_table_id
        record.line_profile_id = bundle.line_profile_id
        record.service_profile_id = bundle.service_profile_id
        record.wan_profile_id = wan_profile_id
        record.tr069_profile_id = tr069_profile_id
        record.gem_id = bundle.gem_id
        record.tcont_id = bundle.tcont_id
        record.command_plan = _command_plan_to_dict(sync_plan.apply_plan)
        record.drift_status = "pending"
        record.drift_details = None
        record.is_active = True
    db.flush()
    return record
