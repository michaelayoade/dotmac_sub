import ipaddress
import logging
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy.orm import Session

from urllib.parse import urlparse

from app.models.catalog import Subscription
from app.models.connector import ConnectorConfig
from app.models.domain_settings import SettingDomain
from app.models.network import (
    CPEDevice,
    DeviceStatus,
    IPAssignment,
    IPVersion,
    IpPool,
    IPv4Address,
    IPv6Address,
)
from app.models.provisioning import (
    InstallAppointment,
    ProvisioningRun,
    ProvisioningRunStatus,
    ProvisioningStep,
    ProvisioningStepType,
    ProvisioningTask,
    ProvisioningVendor,
    ProvisioningWorkflow,
    ServiceOrder,
    ServiceStateTransition,
    AppointmentStatus,
    ServiceOrderStatus,
    TaskStatus,
)
from app.models.projects import Project, ProjectTemplate
from app.models.tr069 import Tr069CpeDevice
from app.services import settings_spec
from app.services.common import apply_ordering, apply_pagination, coerce_uuid, validate_enum
from app.services.response import ListResponseMixin
from app.services.secrets import resolve_secret
from app.services.events import emit_event
from app.services.events.types import EventType
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
from app.schemas.network import IPAssignmentCreate
from app.schemas.projects import ProjectCreate
from app.services import network as network_service
from app.services import projects as projects_service
from app.services.provisioning_adapters import get_provisioner
from app.validators import provisioning as provisioning_validators

logger = logging.getLogger(__name__)

def _build_service_order_project_name(order: ServiceOrder) -> str:
    base_name = "Service Order"
    if order.subscription and order.subscription.offer and order.subscription.offer.name:
        base_name = order.subscription.offer.name
    return f"{base_name} - {str(order.id)[:8]}"


def _resolve_project_template_id(db: Session, project_type):
    if not project_type:
        return None
    template = (
        db.query(ProjectTemplate)
        .filter(ProjectTemplate.project_type == project_type)
        .filter(ProjectTemplate.is_active.is_(True))
        .first()
    )
    return template.id if template else None


def _ensure_project_for_service_order(db: Session, order: ServiceOrder) -> Project:
    existing = (
        db.query(Project)
        .filter(Project.service_order_id == order.id)
        .first()
    )
    if existing:
        return existing
    payload = ProjectCreate(
        name=_build_service_order_project_name(order),
        description=order.notes,
        project_type=order.project_type,
        project_template_id=_resolve_project_template_id(db, order.project_type),
        account_id=order.account_id,
        service_order_id=order.id,
    )
    return projects_service.projects.create(db, payload)


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
        pool = db.get(IpPool, pool_uuid)
        if not pool or pool.ip_version != ip_version:
            raise HTTPException(status_code=404, detail="IP pool not found.")
        return pool
    return (
        db.query(IpPool)
        .filter(IpPool.ip_version == ip_version)
        .filter(IpPool.is_active.is_(True))
        .order_by(IpPool.name.asc())
        .first()
    )


def _get_or_create_address_by_value(
    db: Session, ip_version: IPVersion, value: str, pool: IpPool | None
) -> IPv4Address | IPv6Address:
    model = IPv4Address if ip_version == IPVersion.ipv4 else IPv6Address
    address = db.query(model).filter(model.address == value).first()
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
    address = db.get(model, address_uuid)
    if not address:
        raise HTTPException(status_code=404, detail="IP address not found.")
    return address


def _find_available_address(
    db: Session, ip_version: IPVersion, pool_id: str
) -> IPv4Address | IPv6Address | None:
    if ip_version == IPVersion.ipv4:
        return (
            db.query(IPv4Address)
            .outerjoin(IPAssignment, IPAssignment.ipv4_address_id == IPv4Address.id)
            .filter(IPv4Address.pool_id == pool_id)
            .filter(IPv4Address.is_reserved.is_(False))
            .filter(IPAssignment.id.is_(None))
            .order_by(IPv4Address.address.asc())
            .first()
        )
    return (
        db.query(IPv6Address)
        .outerjoin(IPAssignment, IPAssignment.ipv6_address_id == IPv6Address.id)
        .filter(IPv6Address.pool_id == pool_id)
        .filter(IPv6Address.is_reserved.is_(False))
        .filter(IPAssignment.id.is_(None))
        .order_by(IPv6Address.address.asc())
        .first()
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
            account_id=subscription.account_id,
            subscription_id=subscription.id,
            service_address_id=subscription.service_address_id,
            ip_version=ip_version,
            ipv4_address_id=address.id if ip_version == IPVersion.ipv4 else None,
            ipv6_address_id=address.id if ip_version == IPVersion.ipv6 else None,
            prefix_length=_pool_prefix_length(address.pool or pool),
            gateway=(address.pool or pool).gateway if (address.pool or pool) else None,
            dns_primary=(address.pool or pool).dns_primary if (address.pool or pool) else None,
            dns_secondary=(address.pool or pool).dns_secondary if (address.pool or pool) else None,
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
            workflow = db.get(ProvisioningWorkflow, workflow_uuid)
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
    return query.order_by(ProvisioningWorkflow.created_at.asc()).first()


class ServiceOrders(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: ServiceOrderCreate):
        provisioning_validators.validate_service_order_links(
            db,
            str(payload.account_id),
            str(payload.subscription_id) if payload.subscription_id else None,
            str(payload.requested_by_contact_id)
            if payload.requested_by_contact_id
            else None,
        )
        data = payload.model_dump()
        fields_set = payload.model_fields_set
        if "status" not in fields_set:
            default_status = settings_spec.resolve_value(
                db, SettingDomain.provisioning, "default_service_order_status"
            )
            if default_status:
                data["status"] = validate_enum(
                    default_status, ServiceOrderStatus, "status"
                )
        order = ServiceOrder(**data)
        db.add(order)
        db.commit()
        db.refresh(order)
        if order.status == ServiceOrderStatus.submitted:
            _ensure_project_for_service_order(db, order)

        # Emit service_order.created event
        emit_event(
            db,
            EventType.service_order_created,
            {
                "service_order_id": str(order.id),
                "status": order.status.value if order.status else None,
                "subscription_id": str(order.subscription_id) if order.subscription_id else None,
            },
            service_order_id=order.id,
            account_id=order.account_id,
            subscription_id=order.subscription_id,
        )

        return order

    @staticmethod
    def get(db: Session, order_id: str):
        order = db.get(ServiceOrder, order_id)
        if not order:
            raise HTTPException(status_code=404, detail="Service order not found")
        return order

    @staticmethod
    def list(
        db: Session,
        account_id: str | None,
        subscription_id: str | None,
        status: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(ServiceOrder)
        if account_id:
            query = query.filter(ServiceOrder.account_id == account_id)
        if subscription_id:
            query = query.filter(ServiceOrder.subscription_id == subscription_id)
        if status:
            query = query.filter(
                ServiceOrder.status
                == validate_enum(status, ServiceOrderStatus, "status")
            )
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": ServiceOrder.created_at, "status": ServiceOrder.status},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, order_id: str, payload: ServiceOrderUpdate):
        order = db.get(ServiceOrder, order_id)
        if not order:
            raise HTTPException(status_code=404, detail="Service order not found")
        previous_status = order.status
        data = payload.model_dump(exclude_unset=True)
        account_id = str(data.get("account_id", order.account_id))
        subscription_id = data.get("subscription_id", order.subscription_id)
        requested_by_contact_id = data.get(
            "requested_by_contact_id", order.requested_by_contact_id
        )
        provisioning_validators.validate_service_order_links(
            db,
            account_id,
            str(subscription_id) if subscription_id else None,
            str(requested_by_contact_id) if requested_by_contact_id else None,
        )
        for key, value in data.items():
            setattr(order, key, value)
        db.commit()
        db.refresh(order)
        if (
            order.status == ServiceOrderStatus.submitted
            and previous_status != ServiceOrderStatus.submitted
        ):
            _ensure_project_for_service_order(db, order)

        # Emit events based on status transitions
        new_status = order.status
        if previous_status != new_status:
            event_payload = {
                "service_order_id": str(order.id),
                "from_status": previous_status.value if previous_status else None,
                "to_status": new_status.value if new_status else None,
                "subscription_id": str(order.subscription_id) if order.subscription_id else None,
            }
            context = {
                "service_order_id": order.id,
                "account_id": order.account_id,
                "subscription_id": order.subscription_id,
            }
            if new_status == ServiceOrderStatus.active:
                emit_event(db, EventType.service_order_completed, event_payload, **context)
            elif new_status == ServiceOrderStatus.provisioning:
                emit_event(db, EventType.service_order_assigned, event_payload, **context)

        return order

    @staticmethod
    def delete(db: Session, order_id: str):
        order = db.get(ServiceOrder, order_id)
        if not order:
            raise HTTPException(status_code=404, detail="Service order not found")
        db.delete(order)
        db.commit()


class InstallAppointments(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: InstallAppointmentCreate):
        provisioning_validators.validate_install_appointment_links(
            db, str(payload.service_order_id)
        )
        data = payload.model_dump()
        fields_set = payload.model_fields_set
        if "status" not in fields_set:
            default_status = settings_spec.resolve_value(
                db, SettingDomain.provisioning, "default_appointment_status"
            )
            if default_status:
                data["status"] = validate_enum(
                    default_status, AppointmentStatus, "status"
                )
        appointment = InstallAppointment(**data)
        db.add(appointment)
        db.commit()
        db.refresh(appointment)
        return appointment

    @staticmethod
    def get(db: Session, appointment_id: str):
        appointment = db.get(InstallAppointment, appointment_id)
        if not appointment:
            raise HTTPException(status_code=404, detail="Install appointment not found")
        return appointment

    @staticmethod
    def list(
        db: Session,
        service_order_id: str | None,
        status: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(InstallAppointment)
        if service_order_id:
            query = query.filter(InstallAppointment.service_order_id == service_order_id)
        if status:
            query = query.filter(
                InstallAppointment.status
                == validate_enum(status, AppointmentStatus, "status")
            )
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {
                "created_at": InstallAppointment.created_at,
                "scheduled_start": InstallAppointment.scheduled_start,
            },
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, appointment_id: str, payload: InstallAppointmentUpdate):
        appointment = db.get(InstallAppointment, appointment_id)
        if not appointment:
            raise HTTPException(status_code=404, detail="Install appointment not found")
        data = payload.model_dump(exclude_unset=True)
        service_order_id = data.get("service_order_id", appointment.service_order_id)
        provisioning_validators.validate_install_appointment_links(
            db, str(service_order_id)
        )
        for key, value in data.items():
            setattr(appointment, key, value)
        db.commit()
        db.refresh(appointment)
        return appointment

    @staticmethod
    def delete(db: Session, appointment_id: str):
        appointment = db.get(InstallAppointment, appointment_id)
        if not appointment:
            raise HTTPException(status_code=404, detail="Install appointment not found")
        db.delete(appointment)
        db.commit()


class ProvisioningTasks(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: ProvisioningTaskCreate):
        provisioning_validators.validate_provisioning_task_links(
            db, str(payload.service_order_id)
        )
        data = payload.model_dump()
        fields_set = payload.model_fields_set
        if "status" not in fields_set:
            default_status = settings_spec.resolve_value(
                db, SettingDomain.provisioning, "default_task_status"
            )
            if default_status:
                data["status"] = validate_enum(
                    default_status, TaskStatus, "status"
                )
        task = ProvisioningTask(**data)
        db.add(task)
        db.commit()
        db.refresh(task)
        return task

    @staticmethod
    def get(db: Session, task_id: str):
        task = db.get(ProvisioningTask, task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Provisioning task not found")
        return task

    @staticmethod
    def list(
        db: Session,
        service_order_id: str | None,
        status: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(ProvisioningTask)
        if service_order_id:
            query = query.filter(ProvisioningTask.service_order_id == service_order_id)
        if status:
            query = query.filter(
                ProvisioningTask.status == validate_enum(status, TaskStatus, "status")
            )
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": ProvisioningTask.created_at, "status": ProvisioningTask.status},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, task_id: str, payload: ProvisioningTaskUpdate):
        task = db.get(ProvisioningTask, task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Provisioning task not found")
        data = payload.model_dump(exclude_unset=True)
        service_order_id = data.get("service_order_id", task.service_order_id)
        provisioning_validators.validate_provisioning_task_links(
            db, str(service_order_id)
        )
        for key, value in data.items():
            setattr(task, key, value)
        db.commit()
        db.refresh(task)
        return task

    @staticmethod
    def delete(db: Session, task_id: str):
        task = db.get(ProvisioningTask, task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Provisioning task not found")
        db.delete(task)
        db.commit()


class ServiceStateTransitions(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: ServiceStateTransitionCreate):
        provisioning_validators.validate_state_transition_links(
            db, str(payload.service_order_id)
        )
        transition = ServiceStateTransition(**payload.model_dump())
        db.add(transition)
        db.commit()
        db.refresh(transition)
        return transition

    @staticmethod
    def get(db: Session, transition_id: str):
        transition = db.get(ServiceStateTransition, transition_id)
        if not transition:
            raise HTTPException(status_code=404, detail="State transition not found")
        return transition

    @staticmethod
    def list(
        db: Session,
        service_order_id: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(ServiceStateTransition)
        if service_order_id:
            query = query.filter(
                ServiceStateTransition.service_order_id == service_order_id
            )
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {
                "changed_at": ServiceStateTransition.changed_at,
                "to_state": ServiceStateTransition.to_state,
            },
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, transition_id: str, payload: ServiceStateTransitionUpdate):
        transition = db.get(ServiceStateTransition, transition_id)
        if not transition:
            raise HTTPException(status_code=404, detail="State transition not found")
        data = payload.model_dump(exclude_unset=True)
        service_order_id = data.get("service_order_id", transition.service_order_id)
        provisioning_validators.validate_state_transition_links(
            db, str(service_order_id)
        )
        for key, value in data.items():
            setattr(transition, key, value)
        db.commit()
        db.refresh(transition)
        return transition

    @staticmethod
    def delete(db: Session, transition_id: str):
        transition = db.get(ServiceStateTransition, transition_id)
        if not transition:
            raise HTTPException(status_code=404, detail="State transition not found")
        db.delete(transition)
        db.commit()


class ProvisioningWorkflows(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: ProvisioningWorkflowCreate):
        data = payload.model_dump()
        fields_set = payload.model_fields_set
        if "vendor" not in fields_set:
            default_vendor = settings_spec.resolve_value(
                db, SettingDomain.provisioning, "default_vendor"
            )
            if default_vendor:
                data["vendor"] = validate_enum(
                    default_vendor, ProvisioningVendor, "vendor"
                )
        workflow = ProvisioningWorkflow(**data)
        db.add(workflow)
        db.commit()
        db.refresh(workflow)
        return workflow

    @staticmethod
    def get(db: Session, workflow_id: str):
        workflow = db.get(ProvisioningWorkflow, workflow_id)
        if not workflow:
            raise HTTPException(status_code=404, detail="Provisioning workflow not found")
        return workflow

    @staticmethod
    def list(
        db: Session,
        vendor: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(ProvisioningWorkflow)
        if vendor:
            query = query.filter(
                ProvisioningWorkflow.vendor
                == validate_enum(vendor, ProvisioningVendor, "vendor")
            )
        if is_active is None:
            query = query.filter(ProvisioningWorkflow.is_active.is_(True))
        else:
            query = query.filter(ProvisioningWorkflow.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": ProvisioningWorkflow.created_at, "name": ProvisioningWorkflow.name},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, workflow_id: str, payload: ProvisioningWorkflowUpdate):
        workflow = db.get(ProvisioningWorkflow, workflow_id)
        if not workflow:
            raise HTTPException(status_code=404, detail="Provisioning workflow not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(workflow, key, value)
        db.commit()
        db.refresh(workflow)
        return workflow

    @staticmethod
    def delete(db: Session, workflow_id: str):
        workflow = db.get(ProvisioningWorkflow, workflow_id)
        if not workflow:
            raise HTTPException(status_code=404, detail="Provisioning workflow not found")
        workflow.is_active = False
        db.commit()


class ProvisioningSteps(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: ProvisioningStepCreate):
        workflow = db.get(ProvisioningWorkflow, payload.workflow_id)
        if not workflow:
            raise HTTPException(status_code=404, detail="Provisioning workflow not found")
        step = ProvisioningStep(**payload.model_dump())
        db.add(step)
        db.commit()
        db.refresh(step)
        return step

    @staticmethod
    def get(db: Session, step_id: str):
        step = db.get(ProvisioningStep, step_id)
        if not step:
            raise HTTPException(status_code=404, detail="Provisioning step not found")
        return step

    @staticmethod
    def list(
        db: Session,
        workflow_id: str | None,
        step_type: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(ProvisioningStep)
        if workflow_id:
            query = query.filter(ProvisioningStep.workflow_id == workflow_id)
        if step_type:
            query = query.filter(
                ProvisioningStep.step_type
                == validate_enum(step_type, ProvisioningStepType, "step_type")
            )
        if is_active is None:
            query = query.filter(ProvisioningStep.is_active.is_(True))
        else:
            query = query.filter(ProvisioningStep.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {
                "created_at": ProvisioningStep.created_at,
                "order_index": ProvisioningStep.order_index,
            },
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, step_id: str, payload: ProvisioningStepUpdate):
        step = db.get(ProvisioningStep, step_id)
        if not step:
            raise HTTPException(status_code=404, detail="Provisioning step not found")
        data = payload.model_dump(exclude_unset=True)
        if "workflow_id" in data:
            workflow = db.get(ProvisioningWorkflow, data["workflow_id"])
            if not workflow:
                raise HTTPException(status_code=404, detail="Provisioning workflow not found")
        for key, value in data.items():
            setattr(step, key, value)
        db.commit()
        db.refresh(step)
        return step

    @staticmethod
    def delete(db: Session, step_id: str):
        step = db.get(ProvisioningStep, step_id)
        if not step:
            raise HTTPException(status_code=404, detail="Provisioning step not found")
        step.is_active = False
        db.commit()


class ProvisioningRuns(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: ProvisioningRunCreate):
        workflow = db.get(ProvisioningWorkflow, payload.workflow_id)
        if not workflow:
            raise HTTPException(status_code=404, detail="Provisioning workflow not found")
        run = ProvisioningRun(**payload.model_dump())
        db.add(run)
        db.commit()
        db.refresh(run)
        return run

    @staticmethod
    def get(db: Session, run_id: str):
        run = db.get(ProvisioningRun, run_id)
        if not run:
            raise HTTPException(status_code=404, detail="Provisioning run not found")
        return run

    @staticmethod
    def list(
        db: Session,
        workflow_id: str | None,
        status: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(ProvisioningRun)
        if workflow_id:
            query = query.filter(ProvisioningRun.workflow_id == workflow_id)
        if status:
            query = query.filter(
                ProvisioningRun.status
                == validate_enum(status, ProvisioningRunStatus, "status")
            )
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": ProvisioningRun.created_at, "status": ProvisioningRun.status},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, run_id: str, payload: ProvisioningRunUpdate):
        run = db.get(ProvisioningRun, run_id)
        if not run:
            raise HTTPException(status_code=404, detail="Provisioning run not found")
        data = payload.model_dump(exclude_unset=True)
        if "workflow_id" in data:
            workflow = db.get(ProvisioningWorkflow, data["workflow_id"])
            if not workflow:
                raise HTTPException(status_code=404, detail="Provisioning workflow not found")
        for key, value in data.items():
            setattr(run, key, value)
        db.commit()
        db.refresh(run)
        return run

    @staticmethod
    def run(db: Session, workflow_id: str, payload: ProvisioningRunStart | None = None):
        workflow = db.get(ProvisioningWorkflow, workflow_id)
        if not workflow or not workflow.is_active:
            raise HTTPException(status_code=404, detail="Provisioning workflow not found")
        if payload is None:
            payload = ProvisioningRunStart()
        run = ProvisioningRun(
            workflow_id=workflow_id,
            service_order_id=payload.service_order_id,
            subscription_id=payload.subscription_id,
            status=ProvisioningRunStatus.running,
            started_at=datetime.now(timezone.utc),
            input_payload=payload.input_payload,
        )
        db.add(run)
        db.commit()
        db.refresh(run)

        context = dict(payload.input_payload or {})
        _extend_provisioning_context(db, payload.subscription_id, context)

        emit_event(
            db,
            EventType.provisioning_started,
            {
                "provisioning_run_id": str(run.id),
                "workflow_id": str(run.workflow_id),
                "status": run.status.value,
                "service_order_id": str(run.service_order_id)
                if run.service_order_id
                else None,
                "subscription_id": str(run.subscription_id)
                if run.subscription_id
                else None,
            },
            service_order_id=run.service_order_id,
            subscription_id=run.subscription_id,
        )
        try:
            context.update(_ensure_ip_assignments(db, payload.subscription_id, context))
        except Exception as exc:
            error_message = str(getattr(exc, "detail", exc))
            run.status = ProvisioningRunStatus.failed
            run.output_payload = {"results": []}
            run.error_message = error_message
            run.completed_at = datetime.now(timezone.utc)
            db.commit()
            db.refresh(run)
            emit_event(
                db,
                EventType.provisioning_failed,
                {
                    "provisioning_run_id": str(run.id),
                    "workflow_id": str(run.workflow_id),
                    "status": run.status.value,
                    "service_order_id": str(run.service_order_id)
                    if run.service_order_id
                    else None,
                    "subscription_id": str(run.subscription_id)
                    if run.subscription_id
                    else None,
                    "error_message": error_message,
                },
                service_order_id=run.service_order_id,
                subscription_id=run.subscription_id,
            )
            return run

        steps = (
            db.query(ProvisioningStep)
            .filter(ProvisioningStep.workflow_id == workflow_id)
            .filter(ProvisioningStep.is_active.is_(True))
            .order_by(ProvisioningStep.order_index.asc(), ProvisioningStep.created_at.asc())
            .all()
        )
        provisioner = get_provisioner(workflow.vendor)
        results: list[dict] = []
        status = ProvisioningRunStatus.success
        error_message = None
        for step in steps:
            step_context = dict(context)
            connector_context = _resolve_connector_context(db, step.config or {})
            if connector_context:
                step_context["connector"] = connector_context
            try:
                if step.step_type == ProvisioningStepType.assign_ont:
                    result = provisioner.assign_ont(step_context, step.config)
                elif step.step_type == ProvisioningStepType.push_config:
                    result = provisioner.push_config(step_context, step.config)
                elif step.step_type == ProvisioningStepType.confirm_up:
                    result = provisioner.confirm_up(step_context, step.config)
                else:
                    raise HTTPException(status_code=400, detail="Unsupported step type")
                results.append(
                    {
                        "step_id": str(step.id),
                        "step_type": step.step_type.value,
                        "status": result.status,
                        "detail": result.detail,
                        "payload": result.payload,
                    }
                )
                if result.payload:
                    context.update(result.payload)
            except Exception as exc:
                status = ProvisioningRunStatus.failed
                error_message = str(exc)
                results.append(
                    {
                        "step_id": str(step.id),
                        "step_type": step.step_type.value,
                        "status": "failed",
                        "detail": error_message,
                    }
                )
                break
        run.status = status
        run.output_payload = {"results": results}
        run.error_message = error_message
        run.completed_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(run)

        # Emit provisioning events based on run result
        event_payload = {
            "provisioning_run_id": str(run.id),
            "workflow_id": str(run.workflow_id),
            "status": status.value,
            "service_order_id": str(run.service_order_id) if run.service_order_id else None,
            "subscription_id": str(run.subscription_id) if run.subscription_id else None,
            "error_message": error_message,
        }
        context = {
            "service_order_id": run.service_order_id,
            "subscription_id": run.subscription_id,
        }
        if status == ProvisioningRunStatus.success:
            emit_event(db, EventType.provisioning_completed, event_payload, **context)
        elif status == ProvisioningRunStatus.failed:
            emit_event(db, EventType.provisioning_failed, event_payload, **context)

        return run


service_orders = ServiceOrders()
install_appointments = InstallAppointments()
provisioning_tasks = ProvisioningTasks()
service_state_transitions = ServiceStateTransitions()
provisioning_workflows = ProvisioningWorkflows()
provisioning_steps = ProvisioningSteps()
provisioning_runs = ProvisioningRuns()
