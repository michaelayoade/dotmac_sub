"""Service helpers for admin NAS routes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import quote_plus

from fastapi import Request
from sqlalchemy.orm import Session

from app.csrf import get_csrf_token
from app.schemas.catalog import (
    NasDeviceCreate,
    NasDeviceUpdate,
    ProvisioningTemplateCreate,
    ProvisioningTemplateUpdate,
)
from app.services import nas as nas_service
from app.services.audit_helpers import (
    build_audit_activities,
    diff_dicts,
    log_audit_event,
    model_to_dict,
)


def parse_form_data_sync(request: Request):
    """Lazy wrapper to avoid importing app.web at module import time."""
    from app.web.request_parsing import parse_form_data_sync as _parse_form_data_sync

    return _parse_form_data_sync(request)


DEVICE_AUDIT_EXCLUDE_FIELDS = {
    "ssh_password",
    "api_password",
    "radius_secret",
    "api_key",
    "ssh_key",
    "snmp_community",
}


@dataclass
class NasActionResult:
    """Simple carrier for redirect-or-render decisions."""

    redirect_url: str | None = None
    context: dict[str, Any] | None = None
    errors: list[str] | None = None


def _base_context(
    request: Request,
    db: Session,
    *,
    active_page: str,
    active_menu: str = "network",
) -> dict[str, Any]:
    from app.web.admin import get_current_user, get_sidebar_stats

    return {
        "request": request,
        "active_page": active_page,
        "active_menu": active_menu,
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
        "csrf_token": get_csrf_token(request),
    }


def _form_options(db: Session) -> dict[str, Any]:
    return nas_service.get_nas_form_options(db)


def _actor_id(request: Request) -> str | None:
    from app.web.admin import get_current_user

    current_user = get_current_user(request)
    if not current_user:
        return None
    subscriber_id = current_user.get("subscriber_id")
    return str(subscriber_id) if subscriber_id else None


def _device_form_context(
    request: Request,
    db: Session,
    *,
    device,
    errors: list[str],
    form_data,
    pop_site_label: str | None,
    selected_radius_pool_ids: list[str],
    selected_partner_org_ids: list[str],
    enhanced_fields: dict[str, Any],
) -> dict[str, Any]:
    return {
        **_base_context(request, db, active_page="nas"),
        **_form_options(db),
        "device": device,
        "errors": errors,
        "form_data": form_data,
        "pop_site_label": pop_site_label,
        "selected_radius_pool_ids": selected_radius_pool_ids,
        "selected_partner_org_ids": selected_partner_org_ids,
        "enhanced_fields": enhanced_fields,
    }


def dashboard_context(
    request: Request,
    db: Session,
    *,
    vendor: str | None = None,
    nas_type: str | None = None,
    status: str | None = None,
    pop_site_id: str | None = None,
    partner_org_id: str | None = None,
    online_status: str | None = None,
    search: str | None = None,
    page: int = 1,
    limit: int = 25,
) -> dict[str, Any]:
    page_data = nas_service.build_nas_dashboard_data(
        db,
        vendor=vendor,
        nas_type=nas_type,
        status=status,
        pop_site_id=pop_site_id,
        partner_org_id=partner_org_id,
        online_status=online_status,
        search=search,
        page=page,
        limit=limit,
    )
    return {
        **_base_context(request, db, active_page="nas"),
        **page_data,
        **_form_options(db),
    }


def device_form_context(
    request: Request,
    db: Session,
    *,
    device_id: str | None = None,
) -> dict[str, Any]:
    device = nas_service.NasDevices.get(db, device_id) if device_id else None
    selected_radius_pool_ids: list[str] = []
    selected_partner_org_ids: list[str] = []
    enhanced_fields: dict[str, Any] = {}
    pop_site_label = None

    if device:
        selected_radius_pool_ids = radius_pool_ids_from_tags(device.tags)
        selected_partner_org_ids = prefixed_values_from_tags(
            device.tags, "partner_org:"
        )
        enhanced_fields = extract_enhanced_fields(device.tags)
        pop_site_label = nas_service.pop_site_label(device)

    return {
        **_base_context(request, db, active_page="nas"),
        **_form_options(db),
        "device": device,
        "errors": [],
        "form_data": None,
        "pop_site_label": pop_site_label,
        "selected_radius_pool_ids": selected_radius_pool_ids,
        "selected_partner_org_ids": selected_partner_org_ids,
        "enhanced_fields": enhanced_fields,
    }


def create_device(
    request: Request,
    db: Session,
    form_data: dict[str, Any],
) -> NasActionResult:
    payload, errors = nas_service.build_nas_device_payload(
        db,
        form=form_data,
        existing_tags=None,
        for_update=False,
    )
    if errors:
        return NasActionResult(
            context=_device_form_context(
                request,
                db,
                device=None,
                errors=errors,
                form_data=parse_form_data_sync(request),
                pop_site_label=nas_service.pop_site_label_by_id(
                    db, form_data.get("pop_site_id")
                ),
                selected_radius_pool_ids=form_data.get("radius_pool_ids", []),
                selected_partner_org_ids=form_data.get("partner_org_ids", []),
                enhanced_fields={},
            ),
            errors=errors,
        )

    try:
        if not isinstance(payload, NasDeviceCreate):
            raise ValueError("Invalid payload type: expected NasDeviceCreate")
        device = nas_service.NasDevices.create(db, payload)
        log_audit_event(
            db=db,
            request=request,
            action="create",
            entity_type="nas_device",
            entity_id=str(device.id),
            actor_id=_actor_id(request),
            metadata={"name": device.name, "ip_address": device.ip_address},
        )
        return NasActionResult(redirect_url=f"/admin/network/nas/devices/{device.id}")
    except Exception as exc:
        error_list = errors or []
        error_list = [*error_list, str(exc)]
        return NasActionResult(
            context=_device_form_context(
                request,
                db,
                device=None,
                errors=error_list,
                form_data=parse_form_data_sync(request),
                pop_site_label=nas_service.pop_site_label_by_id(
                    db, form_data.get("pop_site_id")
                ),
                selected_radius_pool_ids=form_data.get("radius_pool_ids", []),
                selected_partner_org_ids=form_data.get("partner_org_ids", []),
                enhanced_fields={},
            ),
            errors=error_list,
        )


def device_detail_context(
    request: Request,
    db: Session,
    *,
    device_id: str,
    tab: str = "information",
    api_test_status: str | None = None,
    api_test_message: str | None = None,
    rule_status: str | None = None,
    rule_message: str | None = None,
) -> dict[str, Any]:
    page_data = nas_service.build_nas_device_detail_data(
        db,
        device_id=device_id,
        tab=tab,
        api_test_status=api_test_status,
        api_test_message=api_test_message,
        rule_status=rule_status,
        rule_message=rule_message,
        build_activities_fn=build_audit_activities,
    )
    return {
        **_base_context(request, db, active_page="nas"),
        **page_data,
    }


def update_device(
    request: Request,
    db: Session,
    device_id: str,
    form_data: dict[str, Any],
) -> NasActionResult:
    device = nas_service.NasDevices.get(db, device_id)
    before_snapshot = model_to_dict(device, exclude=DEVICE_AUDIT_EXCLUDE_FIELDS)
    payload, errors = nas_service.build_nas_device_payload(
        db,
        form=form_data,
        existing_tags=device.tags,
        for_update=True,
    )
    if errors:
        return NasActionResult(
            context=_device_form_context(
                request,
                db,
                device=device,
                errors=errors,
                form_data=parse_form_data_sync(request),
                pop_site_label=nas_service.pop_site_label(device),
                selected_radius_pool_ids=form_data.get("radius_pool_ids", []),
                selected_partner_org_ids=form_data.get("partner_org_ids", []),
                enhanced_fields=extract_enhanced_fields(device.tags),
            ),
            errors=errors,
        )

    try:
        if not isinstance(payload, NasDeviceUpdate):
            raise ValueError("Invalid payload type: expected NasDeviceUpdate")
        updated_device = nas_service.NasDevices.update(db, device_id, payload)
        after_snapshot = model_to_dict(
            updated_device, exclude=DEVICE_AUDIT_EXCLUDE_FIELDS
        )
        changes = diff_dicts(before_snapshot, after_snapshot)
        metadata = {"changes": changes} if changes else None
        log_audit_event(
            db=db,
            request=request,
            action="update",
            entity_type="nas_device",
            entity_id=str(updated_device.id),
            actor_id=_actor_id(request),
            metadata=metadata,
        )
        return NasActionResult(redirect_url=f"/admin/network/nas/devices/{device_id}")
    except Exception as exc:
        error_list = errors or []
        error_list = [*error_list, str(exc)]
        return NasActionResult(
            context=_device_form_context(
                request,
                db,
                device=device,
                errors=error_list,
                form_data=parse_form_data_sync(request),
                pop_site_label=nas_service.pop_site_label(device),
                selected_radius_pool_ids=form_data.get("radius_pool_ids", []),
                selected_partner_org_ids=form_data.get("partner_org_ids", []),
                enhanced_fields=extract_enhanced_fields(device.tags),
            ),
            errors=error_list,
        )


def delete_device(request: Request, db: Session, device_id: str) -> NasActionResult:
    device = nas_service.NasDevices.get(db, device_id)
    log_audit_event(
        db=db,
        request=request,
        action="delete",
        entity_type="nas_device",
        entity_id=str(device.id),
        actor_id=_actor_id(request),
        metadata={"name": device.name, "ip_address": device.ip_address},
    )
    nas_service.NasDevices.delete(db, device_id)
    return NasActionResult(redirect_url="/admin/network/nas")


def device_ping(db: Session, device_id: str) -> str:
    device = nas_service.NasDevices.get(db, device_id)
    ping_status = nas_service.get_ping_status(device.ip_address or device.management_ip)
    if ping_status.get("state") == "reachable":
        nas_service.NasDevices.update_last_seen(db, device_id)
    return f"/admin/network/nas/devices/{device_id}"


def create_connection_rule(
    db: Session,
    *,
    device_id: str,
    name: str,
    connection_type: str | None,
    ip_assignment_mode: str | None,
    rate_limit_profile: str | None,
    match_expression: str | None,
    priority: int,
    notes: str | None,
) -> str:
    try:
        message = nas_service.create_connection_rule_for_device(
            db,
            device_id=device_id,
            name=name,
            connection_type=connection_type or None,
            ip_assignment_mode=ip_assignment_mode,
            rate_limit_profile=rate_limit_profile,
            match_expression=match_expression,
            priority=priority,
            notes=notes,
        )
        return _rule_redirect(device_id, "success", message)
    except Exception as exc:
        return _rule_redirect(device_id, "error", _exception_message(exc))


def toggle_connection_rule(
    db: Session,
    *,
    device_id: str,
    rule_id: str,
    is_active_raw: str,
) -> str:
    try:
        message = nas_service.toggle_connection_rule_for_device(
            db,
            device_id=device_id,
            rule_id=rule_id,
            is_active_raw=is_active_raw,
        )
        return _rule_redirect(device_id, "success", message)
    except Exception as exc:
        return _rule_redirect(device_id, "error", _exception_message(exc))


def delete_connection_rule(
    db: Session,
    *,
    device_id: str,
    rule_id: str,
) -> str:
    try:
        message = nas_service.delete_connection_rule_for_device(
            db,
            device_id=device_id,
            rule_id=rule_id,
        )
        return _rule_redirect(device_id, "success", message)
    except Exception as exc:
        return _rule_redirect(device_id, "error", _exception_message(exc))


def test_mikrotik_api(
    db: Session,
    *,
    device_id: str,
) -> str:
    try:
        message = nas_service.refresh_mikrotik_status_for_device(
            db, device_id=device_id
        )
        status = "success"
    except Exception as exc:
        message = str(exc)
        status = "error"
    return (
        f"/admin/network/nas/devices/{device_id}"
        f"?tab=vendor-specific&api_test_status={status}&api_test_message={quote_plus(message)}"
    )


def live_bandwidth_redirect(db: Session, device_id: str) -> str:
    device = nas_service.NasDevices.get(db, device_id)
    if device.network_device_id:
        return f"/admin/network/core-devices/{device.network_device_id}"
    return (
        f"/admin/network/nas/devices/{device_id}"
        "?tab=information&api_test_status=error"
        "&api_test_message=No+linked+monitoring+device+for+live+bandwidth."
    )


def device_backups_context(
    request: Request,
    db: Session,
    *,
    device_id: str,
    page: int = 1,
    limit: int = 25,
) -> dict[str, Any]:
    page_data = nas_service.build_nas_device_backups_page_data(
        db,
        device_id=device_id,
        page=page,
        limit=limit,
    )
    return {
        **_base_context(request, db, active_page="nas"),
        **page_data,
    }


def trigger_backup(
    request: Request,
    db: Session,
    *,
    device_id: str,
    triggered_by: str,
) -> str:
    result = nas_service.trigger_backup_for_device(
        db,
        device_id=device_id,
        triggered_by=triggered_by,
    )
    if result["ok"]:
        backup = result["backup"]
        if backup is None:
            raise ValueError("Backup trigger succeeded but returned no backup record")
        log_audit_event(
            db=db,
            request=request,
            action="backup_triggered",
            entity_type="nas_backup",
            entity_id=str(backup.id),
            actor_id=_actor_id(request),
            metadata={
                "nas_device_id": str(backup.nas_device_id),
                "triggered_by": triggered_by,
            },
        )
        return f"/admin/network/nas/devices/{device_id}?message=Backup+triggered+successfully"
    return f"/admin/network/nas/devices/{device_id}?error={quote_plus(result['error'])}"


def backup_detail_context(
    request: Request,
    db: Session,
    *,
    backup_id: str,
) -> dict[str, Any]:
    page_data = nas_service.build_nas_backup_detail_data(
        db,
        backup_id=backup_id,
        build_activities_fn=build_audit_activities,
    )
    return {
        **_base_context(request, db, active_page="nas"),
        **page_data,
    }


def backup_compare_context(
    request: Request,
    db: Session,
    *,
    backup_id_1: str,
    backup_id_2: str,
) -> dict[str, Any]:
    page_data = nas_service.build_nas_backup_compare_data(
        db,
        backup_id_1=backup_id_1,
        backup_id_2=backup_id_2,
    )
    return {
        **_base_context(request, db, active_page="nas"),
        **page_data,
    }


def templates_list_context(
    request: Request,
    db: Session,
    *,
    vendor: str | None = None,
    action: str | None = None,
    page: int = 1,
    limit: int = 25,
) -> dict[str, Any]:
    page_data = nas_service.build_nas_templates_list_data(
        db,
        vendor=vendor,
        action=action,
        page=page,
        limit=limit,
    )
    return {
        **_base_context(request, db, active_page="nas"),
        **_form_options(db),
        **page_data,
    }


def template_form_context(
    request: Request,
    db: Session,
    *,
    template_id: str | None = None,
    errors: list[str] | None = None,
    form_data=None,
) -> dict[str, Any]:
    page_data = nas_service.build_nas_template_form_data(db, template_id=template_id)
    context = {
        **_base_context(request, db, active_page="nas"),
        **_form_options(db),
        **page_data,
        "errors": errors or [],
    }
    if form_data is not None:
        context["form_data"] = form_data
    return context


def create_template(
    request: Request,
    db: Session,
    form_data: dict[str, Any],
) -> NasActionResult:
    payload, errors = nas_service.build_provisioning_template_payload(
        form=form_data,
        for_update=False,
    )
    if errors:
        return NasActionResult(
            context=template_form_context(
                request,
                db,
                template_id=None,
                errors=errors,
                form_data=parse_form_data_sync(request),
            ),
            errors=errors,
        )

    try:
        if not isinstance(payload, ProvisioningTemplateCreate):
            raise ValueError(
                "Invalid payload type: expected ProvisioningTemplateCreate"
            )
        template, metadata = nas_service.create_provisioning_template_with_metadata(
            db,
            payload=payload,
        )
        log_audit_event(
            db=db,
            request=request,
            action="create",
            entity_type="nas_template",
            entity_id=str(template.id),
            actor_id=_actor_id(request),
            metadata=metadata,
        )
        return NasActionResult(
            redirect_url=f"/admin/network/nas/templates/{template.id}"
        )
    except Exception as exc:
        error_list = errors or []
        error_list = [*error_list, str(exc)]
        return NasActionResult(
            context=template_form_context(
                request,
                db,
                template_id=None,
                errors=error_list,
                form_data=parse_form_data_sync(request),
            ),
            errors=error_list,
        )


def template_detail_context(
    request: Request,
    db: Session,
    *,
    template_id: str,
) -> dict[str, Any]:
    template = nas_service.ProvisioningTemplates.get(db, template_id)
    activities = build_audit_activities(db, "nas_template", template_id, limit=10)
    return {
        **_base_context(request, db, active_page="nas"),
        "template": template,
        "activities": activities,
    }


def update_template(
    request: Request,
    db: Session,
    *,
    template_id: str,
    form_data: dict[str, Any],
) -> NasActionResult:
    template = nas_service.ProvisioningTemplates.get(db, template_id)
    payload, errors = nas_service.build_provisioning_template_payload(
        form=form_data,
        for_update=True,
    )
    if errors:
        return NasActionResult(
            context=template_form_context(
                request,
                db,
                template_id=template_id,
                errors=errors,
                form_data=parse_form_data_sync(request),
            ),
            errors=errors,
        )

    try:
        if not isinstance(payload, ProvisioningTemplateUpdate):
            raise ValueError(
                "Invalid payload type: expected ProvisioningTemplateUpdate"
            )
        updated_template, metadata = (
            nas_service.update_provisioning_template_with_metadata(
                db,
                template_id=template_id,
                payload=payload,
            )
        )
        log_audit_event(
            db=db,
            request=request,
            action="update",
            entity_type="nas_template",
            entity_id=str(updated_template.id),
            actor_id=_actor_id(request),
            metadata=metadata,
        )
        return NasActionResult(
            redirect_url=f"/admin/network/nas/templates/{template_id}"
        )
    except Exception as exc:
        error_list = errors or []
        error_list = [*error_list, str(exc)]
        return NasActionResult(
            context=template_form_context(
                request,
                db,
                template_id=template_id,
                errors=error_list,
                form_data=parse_form_data_sync(request),
            ),
            errors=error_list,
        )


def delete_template(request: Request, db: Session, template_id: str) -> NasActionResult:
    template = nas_service.ProvisioningTemplates.get(db, template_id)
    log_audit_event(
        db=db,
        request=request,
        action="delete",
        entity_type="nas_template",
        entity_id=str(template.id),
        actor_id=_actor_id(request),
        metadata={"name": template.name},
    )
    nas_service.ProvisioningTemplates.delete(db, template_id)
    return NasActionResult(redirect_url="/admin/network/nas/templates")


def logs_list_context(
    request: Request,
    db: Session,
    *,
    device_id: str | None = None,
    action: str | None = None,
    status: str | None = None,
    page: int = 1,
    limit: int = 50,
) -> dict[str, Any]:
    page_data = nas_service.build_nas_logs_list_data(
        db,
        device_id=device_id,
        action=action,
        status=status,
        page=page,
        limit=limit,
    )
    return {
        **_base_context(request, db, active_page="nas"),
        **_form_options(db),
        **page_data,
    }


def log_detail_context(
    request: Request,
    db: Session,
    *,
    log_id: str,
) -> dict[str, Any]:
    page_data = nas_service.build_nas_log_detail_data(
        db,
        log_id=log_id,
        build_activities_fn=build_audit_activities,
    )
    return {
        **_base_context(request, db, active_page="nas"),
        **page_data,
    }


def radius_pool_ids_from_tags(tags: list[str] | None) -> list[str]:
    return nas_service.radius_pool_ids_from_tags(tags)


def prefixed_values_from_tags(tags: list[str] | None, prefix: str) -> list[str]:
    return nas_service.prefixed_values_from_tags(tags, prefix)


def extract_enhanced_fields(tags: list[str] | None) -> dict[str, Any]:
    return nas_service.extract_enhanced_fields(tags)


def validate_ipv4_address(value: str | None, field_label: str) -> str | None:
    return nas_service.validate_ipv4_address(value, field_label)


def merge_radius_pool_tags(
    existing_tags: list[str] | None,
    radius_pool_ids: list[str],
) -> list[str] | None:
    return nas_service.merge_radius_pool_tags(existing_tags, radius_pool_ids)


def _rule_redirect(device_id: str, status: str, message: str) -> str:
    return (
        f"/admin/network/nas/devices/{device_id}"
        f"?tab=connection-rules&rule_status={status}&rule_message={quote_plus(message)}"
    )


def _exception_message(exc: Exception) -> str:
    detail = getattr(exc, "detail", None)
    return str(detail) if detail else str(exc)
