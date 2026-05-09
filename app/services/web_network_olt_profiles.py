"""Web service for OLT profile display (line, service, TR-069, WAN profiles)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.models.catalog import CatalogOffer
from app.models.network import (
    OLTDevice,
    OltLineProfile,
    OltLineProfileGemMapping,
    OltOnuTypeProfileMapping,
    OltProfileBundle,
    OltProfileSyncTask,
    OltServicePort,
    OltServiceProfile,
    OntProvisioningProfile,
)
from app.services.network import olt as olt_service
from app.services.network import olt_ssh_profiles
from app.services.network.imported_service_ports import imported_service_port_summary
from app.services.network.olt_command_gen import (
    HuaweiCommandGenerator,
    OltCommandSet,
    OntProvisioningContext,
    build_spec_from_profile,
)
from app.services.network.olt_web_audit import log_olt_audit_event
from app.services.network.ont_provisioning.credentials import mask_credentials
from app.services.network.profile_apply_workflow import (
    BackupRunner,
    CommandExecutor,
    ProfileCommandGroup,
    apply_profile_bundle,
    build_profile_apply_plan,
)
from app.services.network.profile_inventory_preflight import (
    build_profile_inventory,
    validate_dotmac_profile_apply_plan,
    validate_offer_profile_sync_plan_inventory,
)
from app.services.network.profile_sync import (
    OfferProfileBundle,
    OfferProfileSyncError,
    OfferProfileSyncPlan,
    OfferProfileSyncTaskError,
    approve_profile_sync_task,
    build_offer_profile_sync_plan,
    list_profile_sync_tasks,
    list_syncable_catalog_offers,
    upsert_profile_bundle,
)
from app.services.olt_profile_adapter import olt_profile_adapter
from app.services.web_network_service_ports import _resolve_ont_olt_context

logger = logging.getLogger(__name__)

PROFILE_SYNC_TASK_STATUS_FILTERS = {
    "open": ("pending", "approved", "scheduled"),
    "pending": ("pending",),
    "approved": ("approved",),
    "scheduled": ("scheduled",),
    "done": ("completed", "failed", "cancelled"),
}


def line_profiles_context(db: Session, olt_id: str) -> dict[str, Any]:
    """Fetch OLT line and service profiles through the profile adapter."""
    return olt_profile_adapter.line_profiles_context(db, olt_id)


def tr069_profiles_context(db: Session, olt_id: str) -> dict[str, Any]:
    """Fetch OLT TR-069 server profiles through the profile adapter."""
    return olt_profile_adapter.tr069_profiles_context(db, olt_id)


def imported_profile_state_context(db: Session, olt_id: str) -> dict[str, Any]:
    """Return imported OLT profile state from DB source-of-truth tables."""
    olt = db.get(OLTDevice, olt_id)
    if olt is None:
        return {
            "olt": None,
            "line_profiles": [],
            "service_profiles": [],
            "profile_mappings": [],
            "error": "OLT not found",
        }

    line_profiles = list(
        db.scalars(
            select(OltLineProfile)
            .where(OltLineProfile.olt_id == olt.id)
            .order_by(OltLineProfile.profile_id)
        )
    )
    service_profiles = list(
        db.scalars(
            select(OltServiceProfile)
            .where(OltServiceProfile.olt_id == olt.id)
            .order_by(OltServiceProfile.profile_id)
        )
    )
    profile_mappings = list(
        db.scalars(
            select(OltOnuTypeProfileMapping)
            .where(OltOnuTypeProfileMapping.olt_id == olt.id)
            .order_by(OltOnuTypeProfileMapping.equipment_id)
        )
    )
    gem_mappings = list(
        db.scalars(
            select(OltLineProfileGemMapping)
            .where(OltLineProfileGemMapping.olt_id == olt.id)
            .order_by(
                OltLineProfileGemMapping.line_profile_id,
                OltLineProfileGemMapping.source,
                OltLineProfileGemMapping.vlan_id,
                OltLineProfileGemMapping.gem_index,
            )
        )
    )
    service_ports = list(
        db.scalars(
            select(OltServicePort)
            .where(OltServicePort.olt_device_id == olt.id)
            .order_by(OltServicePort.port_index)
            .limit(100)
        )
    )
    profile_bundles = list(
        db.scalars(
            select(OltProfileBundle)
            .where(OltProfileBundle.olt_id == olt.id)
            .order_by(OltProfileBundle.name)
        )
    )
    service_port_summary = imported_service_port_summary(db, olt_id=olt.id)
    return {
        "olt": olt,
        "line_profiles": line_profiles,
        "service_profiles": service_profiles,
        "profile_mappings": profile_mappings,
        "gem_mappings": gem_mappings,
        "service_ports": service_ports,
        "profile_bundles": profile_bundles,
        "service_port_summary": service_port_summary,
        "syncable_catalog_offers": list_syncable_catalog_offers(db),
        "error": None,
    }


def profile_sync_tasks_context(
    db: Session,
    *,
    status: str = "open",
    limit: int = 100,
) -> dict[str, Any]:
    """Build the admin review queue for tariff-driven OLT profile sync tasks."""
    selected_status = str(status or "open").strip().lower()
    statuses = PROFILE_SYNC_TASK_STATUS_FILTERS.get(selected_status)
    if selected_status == "all":
        statuses = None
    elif statuses is None:
        selected_status = "open"
        statuses = PROFILE_SYNC_TASK_STATUS_FILTERS[selected_status]

    tasks = list_profile_sync_tasks(db, statuses=statuses, limit=limit)
    status_rows = db.execute(
        select(OltProfileSyncTask.status, func.count(OltProfileSyncTask.id)).group_by(
            OltProfileSyncTask.status
        )
    ).all()
    status_counts = {str(status): int(count) for status, count in status_rows}
    return {
        "tasks": tasks,
        "selected_status": selected_status,
        "status_counts": status_counts,
        "open_count": sum(
            status_counts.get(status, 0)
            for status in PROFILE_SYNC_TASK_STATUS_FILTERS["open"]
        ),
        "pending_count": status_counts.get("pending", 0),
        "approved_count": status_counts.get("approved", 0),
        "scheduled_count": status_counts.get("scheduled", 0),
        "task_count": len(tasks),
    }


def approve_profile_sync_task_from_form(
    db: Session,
    *,
    task_id: str,
    approved_by: str | None,
    scheduled_for_raw: str | None = None,
) -> tuple[bool, str]:
    """Approve or schedule a pending sync task without executing OLT commands."""
    actor = str(approved_by or "").strip()
    if not actor:
        return False, "Cannot approve profile sync task without an actor"
    try:
        scheduled_for = _parse_scheduled_for(scheduled_for_raw)
    except ValueError:
        return False, "Scheduled time is invalid"
    try:
        task = approve_profile_sync_task(
            db,
            task_id=task_id,
            approved_by=actor,
            scheduled_for=scheduled_for,
        )
    except OfferProfileSyncTaskError as exc:
        return False, str(exc)
    db.commit()
    if task.status == "scheduled" and task.scheduled_for is not None:
        return True, f"Scheduled profile sync task for {task.scheduled_for.isoformat()}"
    return True, "Approved profile sync task"


def _parse_scheduled_for(value: str | None) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def offer_profile_sync_preview_context(
    db: Session,
    olt_id: str,
    *,
    offer_id: str,
    vlan_id: int,
) -> dict[str, Any]:
    """Build a dry-run command preview for syncing one offer to OLT profiles."""
    ok, message, olt, offer, plan = _build_offer_profile_sync_plan_from_live(
        db,
        olt_id,
        offer_id=offer_id,
        vlan_id=vlan_id,
    )
    if not ok or olt is None or offer is None or plan is None:
        return {"ok": False, "message": message}

    return {
        "ok": True,
        "message": f"Dry-run profile sync plan built for {offer.name}",
        "olt": olt,
        "offer": offer,
        "bundle": plan.bundle,
        "apply_plan": plan.apply_plan,
        "allocations": plan.allocations,
        "saved_bundle": None,
    }


def save_offer_profile_bundle(
    db: Session,
    olt_id: str,
    *,
    offer_id: str,
    vlan_id: int,
) -> dict[str, Any]:
    """Regenerate, validate, and persist a profile bundle without OLT writes."""
    ok, message, olt, offer, plan = _build_offer_profile_sync_plan_from_live(
        db,
        olt_id,
        offer_id=offer_id,
        vlan_id=vlan_id,
    )
    if not ok or olt is None or offer is None or plan is None:
        return {"ok": False, "message": message}

    record = upsert_profile_bundle(db, olt=olt, sync_plan=plan)
    db.commit()
    return {
        "ok": True,
        "message": f"Saved profile bundle for {offer.name}",
        "olt": olt,
        "offer": offer,
        "bundle": plan.bundle,
        "apply_plan": plan.apply_plan,
        "allocations": plan.allocations,
        "saved_bundle": record,
    }


def apply_saved_profile_bundle(
    db: Session,
    olt_id: str,
    bundle_id: str,
    *,
    actor_is_admin: bool,
    backup_runner: BackupRunner | None = None,
    command_executor: CommandExecutor | None = None,
) -> dict[str, Any]:
    """Apply a saved profile bundle to the OLT with backup and admin guardrails."""
    olt = db.get(OLTDevice, olt_id)
    if olt is None:
        return {"ok": False, "message": "OLT not found", "bundle": None}

    bundle = db.get(OltProfileBundle, bundle_id)
    if bundle is None or bundle.olt_id != olt.id:
        return {"ok": False, "message": "Profile bundle not found", "bundle": None}
    if not bundle.is_active:
        return {"ok": False, "message": "Profile bundle is inactive", "bundle": bundle}
    if not actor_is_admin:
        return {
            "ok": False,
            "message": "Only admin users can apply OLT profile bundles",
            "bundle": bundle,
        }

    try:
        plan = _build_apply_plan_from_saved_bundle(bundle)
    except ValueError as exc:
        return {"ok": False, "message": str(exc), "bundle": bundle}

    ownership_preflight = validate_dotmac_profile_apply_plan(plan)
    if not ownership_preflight.success:
        bundle.drift_status = "preflight_failed"
        bundle.drift_details = {
            "message": ownership_preflight.message,
            "errors": list(ownership_preflight.errors),
        }
        db.flush()
        return {"ok": False, "message": ownership_preflight.message, "bundle": bundle}

    live_preflight_ok, live_preflight_message = _validate_saved_bundle_against_live_inventory(
        olt,
        bundle,
        plan,
    )
    if not live_preflight_ok:
        bundle.drift_status = "preflight_failed"
        bundle.drift_details = {"message": live_preflight_message}
        db.flush()
        return {"ok": False, "message": live_preflight_message, "bundle": bundle}

    kwargs: dict[str, Any] = {}
    if backup_runner is not None:
        kwargs["backup_runner"] = backup_runner
    if command_executor is not None:
        kwargs["command_executor"] = command_executor
    result = apply_profile_bundle(
        db,
        olt,
        plan,
        actor_is_admin=actor_is_admin,
        dry_run=False,
        require_admin=True,
        require_backup=True,
        **kwargs,
    )
    if not result.success:
        bundle.drift_status = "apply_failed"
        bundle.drift_details = {
            "message": result.message,
            "errors": list(result.errors),
            "backup_id": result.backup_id,
        }
        db.flush()
        return {
            "ok": False,
            "message": result.message,
            "bundle": bundle,
            "apply_result": result,
        }

    now = datetime.now(UTC)
    bundle.last_applied_at = now
    bundle.drift_status = "applied"
    bundle.drift_details = {
        "message": result.message,
        "backup_id": result.backup_id,
        "commands": len(result.commands),
    }
    db.commit()
    return {
        "ok": True,
        "message": result.message,
        "bundle": bundle,
        "apply_result": result,
    }


def _build_apply_plan_from_saved_bundle(bundle: OltProfileBundle):
    command_plan = bundle.command_plan or {}
    groups = command_plan.get("groups")
    if not isinstance(groups, list) or not groups:
        raise ValueError("Saved profile bundle has no command plan")

    command_groups: list[ProfileCommandGroup] = []
    for raw_group in groups:
        if not isinstance(raw_group, dict):
            raise ValueError("Saved profile bundle command plan is malformed")
        commands = raw_group.get("commands")
        if not isinstance(commands, list):
            raise ValueError("Saved profile bundle command group has no commands")
        command_groups.append(
            ProfileCommandGroup(
                step=str(raw_group.get("step") or "Apply profile commands"),
                commands=tuple(str(command) for command in commands),
                requires_config_mode=bool(raw_group.get("requires_config_mode", True)),
            )
        )

    return build_profile_apply_plan(bundle.name, command_groups)


def _validate_saved_bundle_against_live_inventory(
    olt: OLTDevice,
    bundle: OltProfileBundle,
    plan,
) -> tuple[bool, str]:
    live_reads = {
        "DBA profiles": olt_ssh_profiles.get_dba_profiles(olt),
        "traffic tables": olt_ssh_profiles.get_traffic_tables(olt),
        "line profiles": olt_ssh_profiles.get_line_profiles(olt),
        "service profiles": olt_ssh_profiles.get_service_profiles(olt),
    }
    failures = [
        f"{label}: {message}"
        for label, (ok, message, _entries) in live_reads.items()
        if not ok
    ]
    if failures:
        return (
            False,
            "Cannot apply profile bundle because live OLT inventory failed: "
            + "; ".join(failures),
        )

    inventory = build_profile_inventory(
        dba_profiles=live_reads["DBA profiles"][2],
        traffic_tables=live_reads["traffic tables"][2],
        line_profiles=live_reads["line profiles"][2],
        service_profiles=live_reads["service profiles"][2],
    )
    sync_plan = OfferProfileSyncPlan(
        bundle=OfferProfileBundle(
            offer_id=str(bundle.offer_id),
            offer_name=bundle.name,
            vlan_id=bundle.vlan_id,
            download_kbps=bundle.download_kbps,
            upload_kbps=bundle.upload_kbps,
            dba_profile_id=bundle.dba_profile_id,
            download_traffic_table_id=bundle.download_traffic_table_id,
            upload_traffic_table_id=bundle.upload_traffic_table_id,
            line_profile_id=bundle.line_profile_id,
            service_profile_id=bundle.service_profile_id,
            gem_id=bundle.gem_id,
            tcont_id=bundle.tcont_id,
            checksum=bundle.checksum,
        ),
        apply_plan=plan,
        allocations=(),
    )
    preflight = validate_offer_profile_sync_plan_inventory(sync_plan, inventory)
    return preflight.success, preflight.message


def _build_offer_profile_sync_plan_from_live(
    db: Session,
    olt_id: str,
    *,
    offer_id: str,
    vlan_id: int,
):
    olt = db.get(OLTDevice, olt_id)
    if olt is None:
        return False, "OLT not found", None, None, None

    offer = db.get(CatalogOffer, offer_id)
    if offer is None:
        return False, "Catalog offer not found", olt, None, None

    live_reads = {
        "DBA profiles": olt_ssh_profiles.get_dba_profiles(olt),
        "traffic tables": olt_ssh_profiles.get_traffic_tables(olt),
        "line profiles": olt_ssh_profiles.get_line_profiles(olt),
        "service profiles": olt_ssh_profiles.get_service_profiles(olt),
    }
    failures = [
        f"{label}: {message}"
        for label, (ok, message, _entries) in live_reads.items()
        if not ok
    ]
    if failures:
        message = (
            "Cannot build profile sync preview because live OLT inventory failed: "
            + "; ".join(failures)
        )
        return False, message, olt, offer, None

    try:
        plan = build_offer_profile_sync_plan(
            offer,
            vlan_id=vlan_id,
            live_dba_profiles=live_reads["DBA profiles"][2],
            live_traffic_tables=live_reads["traffic tables"][2],
            live_line_profiles=live_reads["line profiles"][2],
            live_service_profiles=live_reads["service profiles"][2],
        )
    except (OfferProfileSyncError, ValueError) as exc:
        return False, str(exc), olt, offer, None

    try:
        inventory = build_profile_inventory(
            dba_profiles=live_reads["DBA profiles"][2],
            traffic_tables=live_reads["traffic tables"][2],
            line_profiles=live_reads["line profiles"][2],
            service_profiles=live_reads["service profiles"][2],
        )
        preflight = validate_offer_profile_sync_plan_inventory(plan, inventory)
    except ValueError as exc:
        return False, str(exc), olt, offer, None
    if not preflight.success:
        return False, preflight.message, olt, offer, None

    return True, "Profile sync plan built", olt, offer, plan


def save_imported_profile_mapping(
    db: Session,
    olt_id: str,
    *,
    equipment_id: str,
    line_profile_id: int,
    service_profile_id: int,
    wan_provisioning_mode: str | None = None,
    internet_config_ip_index: int | None = None,
    wan_config_profile_id: int | None = None,
    pppoe_wcd_index: int | None = None,
    mgmt_wcd_index: int | None = None,
    voip_wcd_index: int | None = None,
    primary_wan_service: str | None = None,
) -> tuple[bool, str]:
    """Create or update an OLT equipment mapping using imported profiles only."""
    olt = db.get(OLTDevice, olt_id)
    if olt is None:
        return False, "OLT not found"

    clean_equipment_id = equipment_id.strip()
    if not clean_equipment_id:
        return False, "Equipment ID is required"

    line_profile = db.scalars(
        select(OltLineProfile)
        .where(OltLineProfile.olt_id == olt.id)
        .where(OltLineProfile.profile_id == line_profile_id)
    ).first()
    if line_profile is None:
        return (
            False,
            f"Line profile {line_profile_id} has not been imported for {olt.name}",
        )

    service_profile = db.scalars(
        select(OltServiceProfile)
        .where(OltServiceProfile.olt_id == olt.id)
        .where(OltServiceProfile.profile_id == service_profile_id)
    ).first()
    if service_profile is None:
        return (
            False,
            f"Service profile {service_profile_id} has not been imported for {olt.name}",
        )

    mapping = db.scalars(
        select(OltOnuTypeProfileMapping)
        .where(OltOnuTypeProfileMapping.olt_id == olt.id)
        .where(OltOnuTypeProfileMapping.equipment_id == clean_equipment_id)
    ).first()
    created = mapping is None
    if mapping is None:
        mapping = OltOnuTypeProfileMapping(
            olt_id=olt.id,
            equipment_id=clean_equipment_id,
            line_profile_id=line_profile_id,
            service_profile_id=service_profile_id,
            wan_provisioning_mode=wan_provisioning_mode,
            internet_config_ip_index=internet_config_ip_index,
            wan_config_profile_id=wan_config_profile_id,
            pppoe_wcd_index=pppoe_wcd_index,
            mgmt_wcd_index=mgmt_wcd_index,
            voip_wcd_index=voip_wcd_index,
            primary_wan_service=primary_wan_service,
            source_registration_count=0,
        )
        db.add(mapping)
    else:
        mapping.line_profile_id = line_profile_id
        mapping.service_profile_id = service_profile_id
        mapping.wan_provisioning_mode = wan_provisioning_mode
        mapping.internet_config_ip_index = internet_config_ip_index
        mapping.wan_config_profile_id = wan_config_profile_id
        mapping.pppoe_wcd_index = pppoe_wcd_index
        mapping.mgmt_wcd_index = mgmt_wcd_index
        mapping.voip_wcd_index = voip_wcd_index
        mapping.primary_wan_service = primary_wan_service

    db.flush()
    action = "Created" if created else "Updated"
    return (
        True,
        (
            f"{action} mapping for {clean_equipment_id}: "
            f"line {line_profile_id}, service {service_profile_id}"
        ),
    )


def delete_imported_profile_mapping(
    db: Session,
    olt_id: str,
    mapping_id: str,
) -> tuple[bool, str]:
    """Delete an explicit imported equipment mapping."""
    olt = db.get(OLTDevice, olt_id)
    if olt is None:
        return False, "OLT not found"

    mapping = db.get(OltOnuTypeProfileMapping, mapping_id)
    if mapping is None or str(mapping.olt_id) != str(olt.id):
        return False, "Mapping not found"

    equipment_id = mapping.equipment_id
    db.delete(mapping)
    db.flush()
    return True, f"Deleted mapping for {equipment_id}"


def propagate_acs_to_onts(
    db: Session, olt_id: str, *, request: Request | None = None
) -> tuple[int, dict[str, Any]]:
    try:
        stats = olt_service.OLTDevices.propagate_acs_to_onts(db, olt_id)
    except HTTPException as exc:
        return exc.status_code, {"ok": False, "message": exc.detail}

    log_olt_audit_event(
        db,
        request=request,
        action="propagate_acs",
        entity_id=olt_id,
        metadata=dict(stats),
    )
    updated = stats["updated"]
    total = stats["total"]
    already = stats["already_bound"]
    if updated:
        message = (
            f"ACS binding propagated to {updated} ONTs "
            f"({already} already bound, {total} total)."
        )
    else:
        message = f"All {total} ONTs already bound to this ACS server."
    return 200, {"ok": True, "message": message, **stats}


def enforce_provisioning(
    db: Session, olt_id: str, *, request: Request | None = None
) -> tuple[int, dict[str, Any]]:
    from app.services.network.provisioning_enforcement import ProvisioningEnforcement

    stats = ProvisioningEnforcement.run_full_enforcement(db, olt_id=olt_id)
    log_olt_audit_event(
        db,
        request=request,
        action="enforce_provisioning",
        entity_id=olt_id,
        metadata=dict(stats),
    )

    gaps = stats.get("gaps_detected", {})
    total_gaps = sum(gaps.values()) if isinstance(gaps, dict) else 0
    if total_gaps == 0:
        message = "No provisioning gaps detected on this OLT."
    else:
        message = f"Provisioning gap scan complete: {total_gaps} gap(s) detected."
    return 200, {"ok": True, "message": message, **stats}


def backfill_pon_ports(
    db: Session, olt_id: str, *, request: Request | None = None
) -> tuple[int, dict[str, Any]]:
    try:
        stats = olt_service.OLTDevices.backfill_pon_ports(db, olt_id)
    except HTTPException as exc:
        return exc.status_code, {"ok": False, "message": exc.detail}

    log_olt_audit_event(
        db,
        request=request,
        action="backfill_pon_ports",
        entity_id=olt_id,
        metadata=dict(stats),
    )

    created = stats["ports_created"]
    linked = stats["assignments_linked"]
    total = stats["total_onts"]
    parts = []
    if created:
        parts.append(f"{created} PON ports created")
    if linked:
        parts.append(f"{linked} assignments linked")
    if not parts:
        message = f"All PON ports already exist for {total} ONTs."
    else:
        message = f"{', '.join(parts)} ({total} ONTs on this OLT)."
    return 200, {"ok": True, "message": message, **stats}


def command_preview_context(
    db: Session,
    ont_id: str,
    profile_id: str,
    *,
    tr069_olt_profile_id: int | None = None,
) -> dict[str, Any]:
    """Generate provisioning command preview for an ONT + profile combo.

    Args:
        db: Database session.
        ont_id: OntUnit ID.
        profile_id: OntProvisioningProfile ID.
        tr069_olt_profile_id: OLT-level TR-069 server profile ID.

    Returns:
        Context dict with command_sets, spec, error.
    """
    context: dict[str, Any] = {
        "command_sets": [],
        "error": None,
        "ont": None,
        "profile": None,
    }

    ont, olt, fsp, olt_ont_id = _resolve_ont_olt_context(db, ont_id)
    if not ont:
        context["error"] = "ONT not found"
        return context
    context["ont"] = ont

    if not olt or not fsp or olt_ont_id is None:
        context["error"] = (
            "Cannot resolve OLT context — check assignment and external ID"
        )
        return context

    profile = db.get(OntProvisioningProfile, profile_id)
    if not profile:
        context["error"] = "Provisioning profile not found"
        return context
    context["profile"] = profile

    # Build provisioning context
    parts = fsp.split("/")
    prov_context = OntProvisioningContext(
        frame=int(parts[0]) if len(parts) > 0 else 0,
        slot=int(parts[1]) if len(parts) > 1 else 0,
        port=int(parts[2]) if len(parts) > 2 else 0,
        ont_id=olt_ont_id,
        olt_name=olt.name,
    )

    # Get subscriber info if available

    for a in getattr(ont, "assignments", []):
        if a.active and a.subscriber_id:
            from app.models.subscriber import Subscriber

            sub = db.get(Subscriber, str(a.subscriber_id))
            if sub:
                prov_context.subscriber_code = getattr(sub, "account_number", "") or ""
                prov_context.subscriber_name = getattr(sub, "full_name", "") or ""
            break

    # Build spec and generate commands
    spec = build_spec_from_profile(
        profile, prov_context, tr069_profile_id=tr069_olt_profile_id, olt=olt
    )
    command_sets = [
        OltCommandSet(
            step=item.step,
            commands=[mask_credentials(command) for command in item.commands],
            description=item.description,
            requires_config_mode=item.requires_config_mode,
        )
        for item in HuaweiCommandGenerator.generate_full_provisioning(
            spec, prov_context
        )
    ]

    context["command_sets"] = command_sets
    context["spec"] = spec
    context["prov_context"] = prov_context

    return context
