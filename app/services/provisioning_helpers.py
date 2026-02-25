"""Helper functions for provisioning service flows."""

import ipaddress
import logging
from datetime import UTC, datetime
from typing import cast
from urllib.parse import urlparse

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.catalog import Subscription
from app.models.connector import ConnectorConfig
from app.models.domain_settings import SettingDomain
from app.models.network import (
    CPEDevice,
    DeviceStatus,
    IPAssignment,
    IpPool,
    IPv4Address,
    IPv6Address,
    IPVersion,
)
from app.models.provisioning import (
    AppointmentStatus,
    InstallAppointment,
    ProvisioningRun,
    ProvisioningRunStatus,
    ProvisioningStep,
    ProvisioningStepType,
    ProvisioningTask,
    ProvisioningVendor,
    ProvisioningWorkflow,
    ServiceOrder,
    ServiceOrderStatus,
    ServiceStateTransition,
    TaskStatus,
)
from app.models.tr069 import Tr069CpeDevice
from app.schemas.network import IPAssignmentCreate
from app.schemas.provisioning import (
    InstallAppointmentCreate,
    InstallAppointmentUpdate,
    ProvisioningRunCreate,
    ProvisioningRunStart,
    ProvisioningRunUpdate,
    ProvisioningStepCreate,
    ProvisioningStepUpdate,
    ProvisioningTaskCreate,
    ProvisioningTaskUpdate,
    ProvisioningWorkflowCreate,
    ProvisioningWorkflowUpdate,
    ServiceOrderCreate,
    ServiceOrderUpdate,
    ServiceStateTransitionCreate,
    ServiceStateTransitionUpdate,
)
from app.services import network as network_service
from app.services import settings_spec
from app.services.common import (
    apply_ordering,
    apply_pagination,
    coerce_uuid,
    validate_enum,
)
from app.services.crud import CRUDManager
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.provisioning_adapters import get_provisioner
from app.services.query_builders import apply_active_state, apply_optional_equals
from app.services.secrets import resolve_secret
from app.validators import provisioning as provisioning_validators

logger = logging.getLogger(__name__)

def _resolve_connector_context(db: Session, config: dict | None) -> dict | None:
    if not config:
        return None
    connector_id = config.get("connector_config_id") or config.get("connector_id")
    connector_name = config.get("connector_name")
    connector = None
    if connector_id:
        connector = db.get(ConnectorConfig, connector_id)
    elif connector_name:
        connector = (
            db.query(ConnectorConfig)
            .filter(ConnectorConfig.name == connector_name)
            .first()
        )
    if not connector:
        return None
    auth_config = dict(connector.auth_config or {})
    for key, value in auth_config.items():
        if isinstance(value, str):
            auth_config[key] = resolve_secret(value)
    base_url = connector.base_url
    host = auth_config.get("host")
    port = auth_config.get("port")
    if base_url and not host:
        parsed = urlparse(base_url)
        host = parsed.hostname or base_url
        port = port or parsed.port
    return {
        "base_url": base_url,
        "headers": connector.headers,
        "timeout_sec": connector.timeout_sec,
        "auth_config": {
            **auth_config,
            "host": host,
            "port": port,
        },
    }


def _parse_ip_value(
    value: str, label: str
) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    try:
        return ipaddress.ip_address(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"{label} must be a valid IP address.") from exc


def _pool_prefix_length(pool: IpPool | None) -> int | None:
    if not pool or not pool.cidr:
        return None
    try:
        return ipaddress.ip_network(pool.cidr, strict=False).prefixlen
    except ValueError:
        return None


def _resolve_pool_for_version(
    db: Session, ip_version: IPVersion, pool_id: str | None
) -> IpPool | None:
    if pool_id:
        try:
            pool_uuid = coerce_uuid(pool_id)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="Invalid pool_id.") from exc
        pool = cast(IpPool | None, db.get(IpPool, pool_uuid))
        if not pool or pool.ip_version != ip_version:
            raise HTTPException(status_code=404, detail="IP pool not found.")
        return pool
    return cast(
        IpPool | None,
        (
            db.query(IpPool)
            .filter(IpPool.ip_version == ip_version)
            .filter(IpPool.is_active.is_(True))
            .order_by(IpPool.name.asc())
            .first()
        ),
    )


def _get_or_create_address_by_value(
    db: Session, ip_version: IPVersion, value: str, pool: IpPool | None
) -> IPv4Address | IPv6Address:
    model = IPv4Address if ip_version == IPVersion.ipv4 else IPv6Address
    address = cast(
        IPv4Address | IPv6Address | None,
        db.query(model).filter(model.address == value).first(),
    )
    if address:
        return address
    address = model(address=value, pool_id=pool.id if pool else None)
    db.add(address)
    db.commit()
    db.refresh(address)
    return address


def _get_address_by_id(
    db: Session, ip_version: IPVersion, address_id: str
) -> IPv4Address | IPv6Address:
    try:
        address_uuid = coerce_uuid(address_id)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Invalid address_id.") from exc
    model = IPv4Address if ip_version == IPVersion.ipv4 else IPv6Address
    address = cast(IPv4Address | IPv6Address | None, db.get(model, address_uuid))
    if not address:
        raise HTTPException(status_code=404, detail="IP address not found.")
    return address


def _find_available_address(
    db: Session, ip_version: IPVersion, pool_id: str
) -> IPv4Address | IPv6Address | None:
    if ip_version == IPVersion.ipv4:
        return cast(
            IPv4Address | None,
            (
                db.query(IPv4Address)
                .outerjoin(IPAssignment, IPAssignment.ipv4_address_id == IPv4Address.id)
                .filter(IPv4Address.pool_id == pool_id)
                .filter(IPv4Address.is_reserved.is_(False))
                .filter(IPAssignment.id.is_(None))
                .order_by(IPv4Address.address.asc())
                .first()
            ),
        )
    return cast(
        IPv6Address | None,
        (
            db.query(IPv6Address)
            .outerjoin(IPAssignment, IPAssignment.ipv6_address_id == IPv6Address.id)
            .filter(IPv6Address.pool_id == pool_id)
            .filter(IPv6Address.is_reserved.is_(False))
            .filter(IPAssignment.id.is_(None))
            .order_by(IPv6Address.address.asc())
            .first()
        ),
    )


def _ensure_ip_assignment_for_version(
    db: Session,
    subscription: Subscription,
    ip_version: IPVersion,
    context: dict,
) -> tuple[IPAssignment | None, IPv4Address | IPv6Address | None]:
    assignment = (
        db.query(IPAssignment)
        .filter(IPAssignment.subscription_id == subscription.id)
        .filter(IPAssignment.ip_version == ip_version)
        .filter(IPAssignment.is_active.is_(True))
        .first()
    )
    version_key = ip_version.value
    override_address_id = context.get(f"{version_key}_address_id")
    override_address_value = context.get(f"{version_key}_address")
    subscription_address_value = getattr(subscription, f"{version_key}_address") or None
    override_pool_id = context.get(f"{version_key}_pool_id")

    if assignment:
        address = assignment.ipv4_address if ip_version == IPVersion.ipv4 else assignment.ipv6_address
        if override_address_id and address and str(address.id) != str(override_address_id):
            raise HTTPException(
                status_code=400,
                detail=f"{version_key} address override does not match existing assignment.",
            )
        if override_address_value and address and address.address != override_address_value:
            raise HTTPException(
                status_code=400,
                detail=f"{version_key} address override does not match existing assignment.",
            )
        if address:
            setattr(subscription, f"{version_key}_address", address.address)
        return assignment, address

    address = None
    pool = _resolve_pool_for_version(db, ip_version, override_pool_id)

    if override_address_id:
        address = _get_address_by_id(db, ip_version, override_address_id)

    manual_value = override_address_value or subscription_address_value
    if manual_value:
        parsed = _parse_ip_value(manual_value, f"{version_key} address")
        if parsed.version != (6 if ip_version == IPVersion.ipv6 else 4):
            raise HTTPException(
                status_code=400,
                detail=f"{version_key} address override does not match IP version.",
            )
        if address and address.address != manual_value:
            raise HTTPException(
                status_code=400,
                detail=f"{version_key} address override does not match address_id.",
            )
        if not address:
            address = _get_or_create_address_by_value(db, ip_version, manual_value, pool)

    if not address:
        if not pool:
            raise HTTPException(
                status_code=400,
                detail=f"No active {version_key} pool available for assignment.",
            )
        address = _find_available_address(db, ip_version, str(pool.id))
        if not address:
            raise HTTPException(
                status_code=400,
                detail=f"No available {version_key} addresses in pool {pool.name}.",
            )

    if address.assignment and address.assignment.subscription_id != subscription.id:
        raise HTTPException(
            status_code=400,
            detail=f"{version_key} address is already assigned.",
        )

    if address.assignment:
        assignment = address.assignment
    else:
        assignment_payload = IPAssignmentCreate(
            subscriber_id=subscription.subscriber_id,
            subscription_id=subscription.id,
            service_address_id=subscription.service_address_id,
            ip_version=ip_version,
            ipv4_address_id=address.id if ip_version == IPVersion.ipv4 else None,
            ipv6_address_id=address.id if ip_version == IPVersion.ipv6 else None,
            prefix_length=_pool_prefix_length(pool_to_use := (cast(IpPool | None, address.pool) or pool)),
            gateway=pool_to_use.gateway if pool_to_use else None,
            dns_primary=pool_to_use.dns_primary if pool_to_use else None,
            dns_secondary=pool_to_use.dns_secondary if pool_to_use else None,
        )
        assignment = network_service.ip_assignments.create(db, assignment_payload)

    setattr(subscription, f"{version_key}_address", address.address)
    return assignment, address


def _ensure_ip_assignments(
    db: Session, subscription_id: str | None, context: dict
) -> dict:
    if not subscription_id:
        return {}
    subscription = db.get(Subscription, coerce_uuid(subscription_id))
    if not subscription:
        raise HTTPException(status_code=404, detail="Subscription not found")

    updates: dict[str, object] = {}
    for ip_version in (IPVersion.ipv4, IPVersion.ipv6):
        assignment, address = _ensure_ip_assignment_for_version(
            db, subscription, ip_version, context
        )
        if assignment and address:
            version_key = ip_version.value
            updates.update(
                {
                    f"{version_key}_address": address.address,
                    f"{version_key}_address_id": str(address.id),
                    f"{version_key}_gateway": assignment.gateway,
                    f"{version_key}_dns_primary": assignment.dns_primary,
                    f"{version_key}_dns_secondary": assignment.dns_secondary,
                    f"{version_key}_prefix_length": assignment.prefix_length,
                }
            )
    db.commit()
    return updates


def ensure_ip_assignments_for_subscription(
    db: Session, subscription_id: str, context: dict | None = None
) -> dict:
    """Allocate IP assignments for a subscription using pool defaults."""
    context = context or {}
    return _ensure_ip_assignments(db, subscription_id, context)


def _extend_provisioning_context(
    db: Session,
    subscription_id: str | None,
    context: dict,
) -> dict:
    if not subscription_id:
        return context
    subscription = db.get(Subscription, coerce_uuid(subscription_id))
    if not subscription:
        return context
    device = (
        db.query(CPEDevice)
        .filter(CPEDevice.subscription_id == subscription.id)
        .filter(CPEDevice.status == DeviceStatus.active)
        .order_by(CPEDevice.created_at.desc())
        .first()
    )
    if not device:
        return context
    context.update(
        {
            "cpe_device_id": str(device.id),
            "cpe_serial_number": device.serial_number,
        }
    )
    tr069_device = None
    if device.id:
        tr069_device = (
            db.query(Tr069CpeDevice)
            .filter(Tr069CpeDevice.cpe_device_id == device.id)
            .first()
        )
    if not tr069_device and device.serial_number:
        tr069_device = (
            db.query(Tr069CpeDevice)
            .filter(Tr069CpeDevice.serial_number == device.serial_number)
            .filter(Tr069CpeDevice.is_active.is_(True))
            .first()
        )
    if tr069_device:
        context.update(
            {
                "tr069_cpe_device_id": str(tr069_device.id),
                "tr069_serial_number": tr069_device.serial_number,
                "tr069_oui": tr069_device.oui,
                "tr069_product_class": tr069_device.product_class,
                "tr069_acs_server_id": str(tr069_device.acs_server_id),
            }
        )
        if tr069_device.oui and tr069_device.product_class and tr069_device.serial_number:
            context["genieacs_device_id"] = (
                f"{tr069_device.oui}-{tr069_device.product_class}-{tr069_device.serial_number}"
            )
    return context


def resolve_workflow_for_service_order(
    db: Session, service_order: ServiceOrder
) -> ProvisioningWorkflow | None:
    default_workflow_id = settings_spec.resolve_value(
        db, SettingDomain.provisioning, "default_workflow_id"
    )
    if default_workflow_id:
        try:
            workflow_uuid = coerce_uuid(default_workflow_id)
        except (TypeError, ValueError):
            logger.warning("Invalid provisioning default_workflow_id setting value.")
            workflow_uuid = None
        if workflow_uuid:
            workflow = cast(
                ProvisioningWorkflow | None, db.get(ProvisioningWorkflow, workflow_uuid)
            )
            if workflow and workflow.is_active:
                return workflow
            logger.warning(
                "Provisioning default_workflow_id %s not found or inactive.",
                default_workflow_id,
            )
    vendor_value = settings_spec.resolve_value(
        db, SettingDomain.provisioning, "default_vendor"
    )
    vendor = None
    if vendor_value:
        try:
            vendor = validate_enum(vendor_value, ProvisioningVendor, "vendor")
        except HTTPException:
            logger.warning("Invalid provisioning default_vendor setting value.")
            vendor = None
    query = db.query(ProvisioningWorkflow).filter(ProvisioningWorkflow.is_active.is_(True))
    if vendor:
        query = query.filter(ProvisioningWorkflow.vendor == vendor)
    return cast(
        ProvisioningWorkflow | None,
        query.order_by(ProvisioningWorkflow.created_at.asc()).first(),
    )


