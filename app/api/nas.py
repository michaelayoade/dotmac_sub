"""
NAS Device Management API Endpoints

Provides REST API for:
- NAS Device CRUD operations
- Configuration backup and restore
- Provisioning templates
- Device provisioning execution
"""
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.db import SessionLocal


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
from app.models.catalog import (
    ConfigBackupMethod,
    ConnectionType,
    NasDeviceStatus,
    NasVendor,
    ProvisioningAction,
)
from app.schemas.catalog import (
    NasConfigBackupCreate,
    NasConfigBackupRead,
    NasDeviceCreate,
    NasDeviceRead,
    NasDeviceUpdate,
    ProvisioningLogRead,
    ProvisioningTemplateCreate,
    ProvisioningTemplateRead,
    ProvisioningTemplateUpdate,
)
from app.services.nas import (
    NasConfigBackups,
    NasDevices,
    DeviceProvisioner,
    ProvisioningLogs,
    ProvisioningTemplates,
)

router = APIRouter(prefix="/nas", tags=["nas-devices"])


# =============================================================================
# NAS DEVICE ENDPOINTS
# =============================================================================

@router.get("/devices", response_model=dict)
def list_nas_devices(
    db: Session = Depends(get_db),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    order_by: str = Query("name"),
    order_dir: str = Query("asc", pattern="^(asc|desc)$"),
    vendor: NasVendor | None = None,
    status: NasDeviceStatus | None = None,
    connection_type: ConnectionType | None = None,
    pop_site_id: UUID | None = None,
    is_active: bool | None = None,
    search: str | None = None,
):
    """List NAS devices with filtering and pagination."""
    return NasDevices.list_response(
        db,
        limit=limit,
        offset=offset,
        order_by=order_by,
        order_dir=order_dir,
        vendor=vendor,
        status=status,
        connection_type=connection_type,
        pop_site_id=pop_site_id,
        is_active=is_active,
        search=search,
    )


@router.post("/devices", response_model=NasDeviceRead, status_code=201)
def create_nas_device(
    payload: NasDeviceCreate,
    db: Session = Depends(get_db),
):
    """Create a new NAS device."""
    return NasDevices.create(db, payload)


@router.get("/devices/stats")
def get_nas_device_stats(db: Session = Depends(get_db)):
    """Get NAS device statistics by vendor and status."""
    return {
        "by_vendor": NasDevices.count_by_vendor(db),
        "by_status": NasDevices.count_by_status(db),
    }


@router.get("/devices/{device_id}", response_model=NasDeviceRead)
def get_nas_device(
    device_id: UUID,
    db: Session = Depends(get_db),
):
    """Get a NAS device by ID."""
    return NasDevices.get(db, device_id)


@router.patch("/devices/{device_id}", response_model=NasDeviceRead)
def update_nas_device(
    device_id: UUID,
    payload: NasDeviceUpdate,
    db: Session = Depends(get_db),
):
    """Update a NAS device."""
    return NasDevices.update(db, device_id, payload)


@router.delete("/devices/{device_id}", status_code=204)
def delete_nas_device(
    device_id: UUID,
    db: Session = Depends(get_db),
):
    """Delete a NAS device."""
    NasDevices.delete(db, device_id)


@router.post("/devices/{device_id}/ping", response_model=NasDeviceRead)
def ping_nas_device(
    device_id: UUID,
    db: Session = Depends(get_db),
):
    """Update the last_seen_at timestamp for a device (mark as reachable)."""
    return NasDevices.update_last_seen(db, device_id)


# =============================================================================
# CONFIG BACKUP ENDPOINTS
# =============================================================================

@router.get("/devices/{device_id}/backups", response_model=dict)
def list_device_backups(
    device_id: UUID,
    db: Session = Depends(get_db),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    has_changes: bool | None = None,
):
    """List configuration backups for a device."""
    return NasConfigBackups.list_response(
        db,
        nas_device_id=device_id,
        limit=limit,
        offset=offset,
        has_changes=has_changes,
    )


@router.post("/devices/{device_id}/backups", response_model=NasConfigBackupRead, status_code=201)
def create_device_backup(
    device_id: UUID,
    db: Session = Depends(get_db),
    triggered_by: str = Query("api", max_length=120),
):
    """
    Trigger a configuration backup from the device.

    This connects to the device and downloads its current configuration.
    """
    return DeviceProvisioner.backup_config(db, device_id, triggered_by)


@router.post("/devices/{device_id}/backups/manual", response_model=NasConfigBackupRead, status_code=201)
def upload_device_backup(
    device_id: UUID,
    payload: NasConfigBackupCreate,
    db: Session = Depends(get_db),
):
    """
    Manually upload a configuration backup.

    Use this when you have the config content and want to store it
    without connecting to the device.
    """
    # Ensure device_id matches
    payload.nas_device_id = device_id
    return NasConfigBackups.create(db, payload)


@router.get("/backups/{backup_id}", response_model=NasConfigBackupRead)
def get_backup(
    backup_id: UUID,
    db: Session = Depends(get_db),
):
    """Get a configuration backup by ID."""
    return NasConfigBackups.get(db, backup_id)


@router.get("/backups/{backup_id}/content")
def get_backup_content(
    backup_id: UUID,
    db: Session = Depends(get_db),
):
    """Get the raw configuration content of a backup."""
    backup = NasConfigBackups.get(db, backup_id)
    return {
        "id": str(backup.id),
        "config_format": backup.config_format,
        "content": backup.config_content,
    }


@router.get("/backups/compare")
def compare_backups(
    backup_id_1: UUID,
    backup_id_2: UUID,
    db: Session = Depends(get_db),
):
    """Compare two configuration backups and return the differences."""
    return NasConfigBackups.compare(db, backup_id_1, backup_id_2)


# =============================================================================
# PROVISIONING TEMPLATE ENDPOINTS
# =============================================================================

@router.get("/templates", response_model=dict)
def list_provisioning_templates(
    db: Session = Depends(get_db),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    vendor: NasVendor | None = None,
    connection_type: ConnectionType | None = None,
    action: ProvisioningAction | None = None,
    is_active: bool | None = None,
):
    """List provisioning templates with filtering."""
    return ProvisioningTemplates.list_response(
        db,
        limit=limit,
        offset=offset,
        vendor=vendor,
        connection_type=connection_type,
        action=action,
        is_active=is_active,
    )


@router.post("/templates", response_model=ProvisioningTemplateRead, status_code=201)
def create_provisioning_template(
    payload: ProvisioningTemplateCreate,
    db: Session = Depends(get_db),
):
    """Create a new provisioning template."""
    return ProvisioningTemplates.create(db, payload)


@router.get("/templates/{template_id}", response_model=ProvisioningTemplateRead)
def get_provisioning_template(
    template_id: UUID,
    db: Session = Depends(get_db),
):
    """Get a provisioning template by ID."""
    return ProvisioningTemplates.get(db, template_id)


@router.patch("/templates/{template_id}", response_model=ProvisioningTemplateRead)
def update_provisioning_template(
    template_id: UUID,
    payload: ProvisioningTemplateUpdate,
    db: Session = Depends(get_db),
):
    """Update a provisioning template."""
    return ProvisioningTemplates.update(db, template_id, payload)


@router.delete("/templates/{template_id}", status_code=204)
def delete_provisioning_template(
    template_id: UUID,
    db: Session = Depends(get_db),
):
    """Delete a provisioning template."""
    ProvisioningTemplates.delete(db, template_id)


@router.post("/templates/{template_id}/preview")
def preview_template(
    template_id: UUID,
    variables: dict[str, Any],
    db: Session = Depends(get_db),
):
    """Preview a template with given variables without executing it."""
    template = ProvisioningTemplates.get(db, template_id)
    rendered = ProvisioningTemplates.render(template, variables)
    return {
        "template_id": str(template.id),
        "template_name": template.name,
        "placeholders": template.placeholders,
        "variables_provided": variables,
        "rendered_content": rendered,
    }


# =============================================================================
# PROVISIONING EXECUTION ENDPOINTS
# =============================================================================

@router.post("/devices/{device_id}/provision", response_model=ProvisioningLogRead)
def provision_device(
    device_id: UUID,
    action: ProvisioningAction,
    variables: dict[str, Any],
    db: Session = Depends(get_db),
    triggered_by: str = Query("api", max_length=120),
):
    """
    Execute a provisioning action on a NAS device.

    This finds the appropriate template, renders it with the provided
    variables, and executes the command on the device.

    Common variables:
    - username: PPPoE/DHCP username
    - password: PPPoE password
    - speed_down: Download speed in Kbps
    - speed_up: Upload speed in Kbps
    - ip_address: Assigned IP address
    - mac_address: Client MAC address
    - profile: RADIUS profile name
    - subscription_id: UUID of the subscription (required for bandwidth monitoring)
    - queue_name: MikroTik queue name (auto-generated from username if not provided)

    For bandwidth monitoring integration:
    - On create_user: Creates a QueueMapping to link the NAS queue to the subscription
    - On delete_user/suspend_user: Deactivates the QueueMapping
    - On unsuspend_user: Re-activates the QueueMapping
    """
    return DeviceProvisioner.provision_user(
        db, device_id, action, variables, triggered_by
    )


# =============================================================================
# PROVISIONING LOG ENDPOINTS
# =============================================================================

@router.get("/logs", response_model=dict)
def list_provisioning_logs(
    db: Session = Depends(get_db),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    nas_device_id: UUID | None = None,
    subscriber_id: UUID | None = None,
    action: ProvisioningAction | None = None,
    status: str | None = None,
):
    """List provisioning logs with filtering."""
    return ProvisioningLogs.list_response(
        db,
        limit=limit,
        offset=offset,
        nas_device_id=nas_device_id,
        subscriber_id=subscriber_id,
        action=action,
        status=status,
    )


@router.get("/logs/{log_id}", response_model=ProvisioningLogRead)
def get_provisioning_log(
    log_id: UUID,
    db: Session = Depends(get_db),
):
    """Get a provisioning log entry by ID."""
    return ProvisioningLogs.get(db, log_id)


@router.get("/devices/{device_id}/logs", response_model=dict)
def list_device_provisioning_logs(
    device_id: UUID,
    db: Session = Depends(get_db),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    action: ProvisioningAction | None = None,
    status: str | None = None,
):
    """List provisioning logs for a specific device."""
    return ProvisioningLogs.list_response(
        db,
        nas_device_id=device_id,
        limit=limit,
        offset=offset,
        action=action,
        status=status,
    )


# =============================================================================
# UTILITY ENDPOINTS
# =============================================================================

@router.get("/vendors")
def list_vendors():
    """List all supported NAS vendors."""
    return {
        "vendors": [
            {"value": v.value, "label": v.value.title()}
            for v in NasVendor
        ]
    }


@router.get("/connection-types")
def list_connection_types():
    """List all supported connection types."""
    descriptions = {
        "pppoe": "Point-to-Point Protocol over Ethernet - username/password authentication",
        "dhcp": "Dynamic Host Configuration Protocol - no authentication",
        "ipoe": "IP over Ethernet - DHCP with RADIUS authentication via Option 82",
        "static": "Static IP assignment - manual configuration",
        "hotspot": "Web portal login (MikroTik specific)",
    }
    return {
        "connection_types": [
            {"value": ct.value, "label": ct.value.upper(), "description": descriptions.get(ct.value, "")}
            for ct in ConnectionType
        ]
    }


@router.get("/provisioning-actions")
def list_provisioning_actions():
    """List all supported provisioning actions."""
    return {
        "actions": [
            {"value": a.value, "label": a.value.replace("_", " ").title()}
            for a in ProvisioningAction
        ]
    }


@router.get("/backup-methods")
def list_backup_methods():
    """List all supported configuration backup methods."""
    return {
        "methods": [
            {"value": m.value, "label": m.value.upper()}
            for m in ConfigBackupMethod
        ]
    }
