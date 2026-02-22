from datetime import datetime

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.network import IPVersion
from app.schemas.bandwidth import (
    BandwidthSampleCreate,
    BandwidthSampleRead,
    BandwidthSeriesPoint,
)
from app.schemas.collections import (
    DunningActionLogCreate,
    DunningActionLogRead,
    DunningCaseCreate,
    DunningCaseRead,
    DunningCaseUpdate,
    DunningRunRequest,
    DunningRunResponse,
)
from app.schemas.common import ListResponse
from app.schemas.lifecycle import (
    SubscriptionLifecycleEventCreate,
    SubscriptionLifecycleEventRead,
)
from app.schemas.network import (
    CPEDeviceCreate,
    CPEDeviceRead,
    CPEDeviceUpdate,
    FdhCabinetCreate,
    FdhCabinetRead,
    FdhCabinetUpdate,
    FiberSegmentCreate,
    FiberSegmentRead,
    FiberSegmentUpdate,
    FiberSpliceClosureCreate,
    FiberSpliceClosureRead,
    FiberSpliceClosureUpdate,
    FiberSpliceCreate,
    FiberSpliceRead,
    FiberSpliceTrayCreate,
    FiberSpliceTrayRead,
    FiberSpliceTrayUpdate,
    FiberSpliceUpdate,
    FiberStrandCreate,
    FiberStrandRead,
    FiberStrandUpdate,
    FiberTerminationPointCreate,
    FiberTerminationPointRead,
    FiberTerminationPointUpdate,
    IPAssignmentCreate,
    IPAssignmentRead,
    IPAssignmentUpdate,
    IpBlockCreate,
    IpBlockRead,
    IpBlockUpdate,
    IpPoolCreate,
    IpPoolRead,
    IpPoolUpdate,
    IPv4AddressCreate,
    IPv4AddressRead,
    IPv4AddressUpdate,
    IPv6AddressCreate,
    IPv6AddressRead,
    IPv6AddressUpdate,
    OltCardCreate,
    OltCardPortCreate,
    OltCardPortRead,
    OltCardPortUpdate,
    OltCardRead,
    OltCardUpdate,
    OLTDeviceCreate,
    OLTDeviceRead,
    OLTDeviceUpdate,
    OltPowerUnitCreate,
    OltPowerUnitRead,
    OltPowerUnitUpdate,
    OltSfpModuleCreate,
    OltSfpModuleRead,
    OltSfpModuleUpdate,
    OltShelfCreate,
    OltShelfRead,
    OltShelfUpdate,
    OntAssignmentCreate,
    OntAssignmentRead,
    OntAssignmentUpdate,
    OntUnitCreate,
    OntUnitRead,
    OntUnitUpdate,
    PonPortCreate,
    PonPortRead,
    PonPortSplitterLinkCreate,
    PonPortSplitterLinkRead,
    PonPortSplitterLinkUpdate,
    PonPortUpdate,
    PortCreate,
    PortRead,
    PortUpdate,
    PortVlanCreate,
    PortVlanRead,
    SplitterCreate,
    SplitterPortAssignmentCreate,
    SplitterPortAssignmentRead,
    SplitterPortAssignmentUpdate,
    SplitterPortCreate,
    SplitterPortRead,
    SplitterPortUpdate,
    SplitterRead,
    SplitterUpdate,
    VlanCreate,
    VlanRead,
    VlanUpdate,
)
from app.schemas.network_metrics import FiberPathRead, PortUtilizationRead
from app.schemas.network_monitoring import (
    AlertAcknowledgeRequest,
    AlertBulkAcknowledgeRequest,
    AlertBulkActionResponse,
    AlertBulkResolveRequest,
    AlertEventRead,
    AlertRead,
    AlertResolveRequest,
    AlertRuleBulkUpdateRequest,
    AlertRuleBulkUpdateResponse,
    AlertRuleCreate,
    AlertRuleRead,
    AlertRuleUpdate,
    DeviceInterfaceCreate,
    DeviceInterfaceRead,
    DeviceInterfaceUpdate,
    DeviceMetricCreate,
    DeviceMetricRead,
    NetworkDeviceCreate,
    NetworkDeviceRead,
    NetworkDeviceUpdate,
    PopSiteCreate,
    PopSiteRead,
    PopSiteUpdate,
    UptimeReportRequest,
    UptimeReportResponse,
)
from app.schemas.provisioning import (
    InstallAppointmentCreate,
    InstallAppointmentRead,
    InstallAppointmentUpdate,
    ProvisioningRunCreate,
    ProvisioningRunRead,
    ProvisioningRunStart,
    ProvisioningRunUpdate,
    ProvisioningStepCreate,
    ProvisioningStepRead,
    ProvisioningStepUpdate,
    ProvisioningTaskCreate,
    ProvisioningTaskRead,
    ProvisioningTaskUpdate,
    ProvisioningWorkflowCreate,
    ProvisioningWorkflowRead,
    ProvisioningWorkflowUpdate,
    ServiceOrderCreate,
    ServiceOrderRead,
    ServiceOrderUpdate,
    ServiceStateTransitionCreate,
    ServiceStateTransitionRead,
)
from app.schemas.radius import (
    RadiusClientCreate,
    RadiusClientRead,
    RadiusClientUpdate,
    RadiusServerCreate,
    RadiusServerRead,
    RadiusServerUpdate,
    RadiusSyncJobCreate,
    RadiusSyncJobRead,
    RadiusSyncJobUpdate,
    RadiusSyncRunRead,
    RadiusUserRead,
)
from app.schemas.snmp import (
    SnmpCredentialCreate,
    SnmpCredentialRead,
    SnmpCredentialUpdate,
    SnmpOidCreate,
    SnmpOidRead,
    SnmpOidUpdate,
    SnmpPollerCreate,
    SnmpPollerRead,
    SnmpPollerUpdate,
    SnmpReadingCreate,
    SnmpReadingRead,
    SnmpTargetCreate,
    SnmpTargetRead,
    SnmpTargetUpdate,
)
from app.schemas.subscription_engine import (
    SubscriptionEngineCreate,
    SubscriptionEngineRead,
    SubscriptionEngineSettingCreate,
    SubscriptionEngineSettingRead,
    SubscriptionEngineSettingUpdate,
    SubscriptionEngineUpdate,
)
from app.schemas.tr069 import (
    Tr069AcsServerCreate,
    Tr069AcsServerRead,
    Tr069AcsServerUpdate,
    Tr069CpeDeviceCreate,
    Tr069CpeDeviceRead,
    Tr069CpeDeviceUpdate,
    Tr069JobCreate,
    Tr069JobRead,
    Tr069JobUpdate,
    Tr069ParameterCreate,
    Tr069ParameterRead,
    Tr069SessionCreate,
    Tr069SessionRead,
)
from app.schemas.usage import (
    QuotaBucketCreate,
    QuotaBucketRead,
    QuotaBucketUpdate,
    RadiusAccountingSessionCreate,
    RadiusAccountingSessionRead,
    UsageChargePostBatchRequest,
    UsageChargePostBatchResponse,
    UsageChargePostRequest,
    UsageChargeRead,
    UsageRatingRunRead,
    UsageRatingRunRequest,
    UsageRatingRunResponse,
    UsageRecordCreate,
    UsageRecordRead,
)
from app.services import (
    bandwidth as bandwidth_service,
)
from app.services import (
    collections as collections_service,
)
from app.services import (
    lifecycle as lifecycle_service,
)
from app.services import (
    network as network_service,
)
from app.services import (
    network_monitoring as monitoring_service,
)
from app.services import (
    provisioning as provisioning_service,
)
from app.services import (
    radius as radius_service,
)
from app.services import (
    snmp as snmp_service,
)
from app.services import (
    subscription_engine as subscription_engine_service,
)
from app.services import (
    tr069 as tr069_service,
)
from app.services import (
    usage as usage_service,
)
from app.services.auth_dependencies import require_permission

router = APIRouter()


@router.post(
    "/cpe-devices",
    response_model=CPEDeviceRead,
    status_code=status.HTTP_201_CREATED,
    tags=["network"],
)
def create_cpe_device(payload: CPEDeviceCreate, db: Session = Depends(get_db)):
    return network_service.cpe_devices.create(db, payload)


@router.get("/cpe-devices/{device_id}", response_model=CPEDeviceRead, tags=["network"])
def get_cpe_device(device_id: str, db: Session = Depends(get_db)):
    return network_service.cpe_devices.get(db, device_id)


@router.get("/cpe-devices", response_model=ListResponse[CPEDeviceRead], tags=["network"])
def list_cpe_devices(
    subscriber_id: str | None = None,
    account_id: str | None = None,
    subscription_id: str | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    if not subscriber_id and account_id:
        subscriber_id = account_id
    return network_service.cpe_devices.list_response(
        db, subscriber_id, subscription_id, order_by, order_dir, limit, offset
    )


@router.patch("/cpe-devices/{device_id}", response_model=CPEDeviceRead, tags=["network"])
def update_cpe_device(
    device_id: str, payload: CPEDeviceUpdate, db: Session = Depends(get_db)
):
    return network_service.cpe_devices.update(
        db, device_id, payload
    )


@router.delete(
    "/cpe-devices/{device_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["network"]
)
def delete_cpe_device(device_id: str, db: Session = Depends(get_db)):
    network_service.cpe_devices.delete(db, device_id)


@router.post(
    "/ports",
    response_model=PortRead,
    status_code=status.HTTP_201_CREATED,
    tags=["network"],
)
def create_port(payload: PortCreate, db: Session = Depends(get_db)):
    return network_service.ports.create(db, payload)


@router.get("/ports/{port_id}", response_model=PortRead, tags=["network"])
def get_port(port_id: str, db: Session = Depends(get_db)):
    return network_service.ports.get(db, port_id)


@router.get("/ports", response_model=ListResponse[PortRead], tags=["network"])
def list_ports(
    device_id: str | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return network_service.ports.list_response(db, device_id, order_by, order_dir, limit, offset)


@router.patch("/ports/{port_id}", response_model=PortRead, tags=["network"])
def update_port(port_id: str, payload: PortUpdate, db: Session = Depends(get_db)):
    return network_service.ports.update(db, port_id, payload)


@router.delete(
    "/ports/{port_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["network"]
)
def delete_port(port_id: str, db: Session = Depends(get_db)):
    network_service.ports.delete(db, port_id)


@router.post(
    "/vlans",
    response_model=VlanRead,
    status_code=status.HTTP_201_CREATED,
    tags=["network"],
)
def create_vlan(payload: VlanCreate, db: Session = Depends(get_db)):
    return network_service.vlans.create(db, payload)


@router.get("/vlans/{vlan_id}", response_model=VlanRead, tags=["network"])
def get_vlan(vlan_id: str, db: Session = Depends(get_db)):
    return network_service.vlans.get(db, vlan_id)


@router.get("/vlans", response_model=ListResponse[VlanRead], tags=["network"])
def list_vlans(
    region_id: str | None = None,
    is_active: bool | None = None,
    order_by: str = Query(default="tag"),
    order_dir: str = Query(default="asc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return network_service.vlans.list_response(
        db, region_id, is_active, order_by, order_dir, limit, offset
    )


@router.patch("/vlans/{vlan_id}", response_model=VlanRead, tags=["network"])
def update_vlan(vlan_id: str, payload: VlanUpdate, db: Session = Depends(get_db)):
    return network_service.vlans.update(db, vlan_id, payload)


@router.delete(
    "/vlans/{vlan_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["network"]
)
def delete_vlan(vlan_id: str, db: Session = Depends(get_db)):
    network_service.vlans.delete(db, vlan_id)


@router.post(
    "/port-vlans",
    response_model=PortVlanRead,
    status_code=status.HTTP_201_CREATED,
    tags=["network"],
)
def create_port_vlan(payload: PortVlanCreate, db: Session = Depends(get_db)):
    return network_service.port_vlans.create(db, payload)


@router.get("/port-vlans/{link_id}", response_model=PortVlanRead, tags=["network"])
def get_port_vlan(link_id: str, db: Session = Depends(get_db)):
    return network_service.port_vlans.get(db, link_id)


@router.get("/port-vlans", response_model=ListResponse[PortVlanRead], tags=["network"])
def list_port_vlans(
    port_id: str | None = None,
    vlan_id: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return network_service.port_vlans.list_response(db, port_id, vlan_id, limit, offset)


@router.delete(
    "/port-vlans/{link_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["network"]
)
def delete_port_vlan(link_id: str, db: Session = Depends(get_db)):
    network_service.port_vlans.delete(db, link_id)


@router.post(
    "/ip-assignments",
    response_model=IPAssignmentRead,
    status_code=status.HTTP_201_CREATED,
    tags=["network"],
)
def create_ip_assignment(payload: IPAssignmentCreate, db: Session = Depends(get_db)):
    return network_service.ip_assignments.create(db, payload)


@router.get(
    "/ip-assignments/{assignment_id}", response_model=IPAssignmentRead, tags=["network"]
)
def get_ip_assignment(assignment_id: str, db: Session = Depends(get_db)):
    return network_service.ip_assignments.get(db, assignment_id)


@router.get(
    "/ip-assignments", response_model=ListResponse[IPAssignmentRead], tags=["network"]
)
def list_ip_assignments(
    subscriber_id: str | None = None,
    account_id: str | None = None,
    subscription_id: str | None = None,
    ip_version: IPVersion | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    if not subscriber_id and account_id:
        subscriber_id = account_id
    return network_service.ip_assignments.list_response(
        db, subscriber_id, subscription_id, ip_version, order_by, order_dir, limit, offset
    )


@router.patch(
    "/ip-assignments/{assignment_id}", response_model=IPAssignmentRead, tags=["network"]
)
def update_ip_assignment(
    assignment_id: str, payload: IPAssignmentUpdate, db: Session = Depends(get_db)
):
    return network_service.ip_assignments.update(
        db, assignment_id, payload
    )


@router.delete(
    "/ip-assignments/{assignment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["network"],
)
def delete_ip_assignment(assignment_id: str, db: Session = Depends(get_db)):
    network_service.ip_assignments.delete(db, assignment_id)


@router.post(
    "/ip-pools",
    response_model=IpPoolRead,
    status_code=status.HTTP_201_CREATED,
    tags=["network"],
)
def create_ip_pool(payload: IpPoolCreate, db: Session = Depends(get_db)):
    return network_service.ip_pools.create(db, payload)


@router.get("/ip-pools/{pool_id}", response_model=IpPoolRead, tags=["network"])
def get_ip_pool(pool_id: str, db: Session = Depends(get_db)):
    return network_service.ip_pools.get(db, pool_id)


@router.get("/ip-pools", response_model=ListResponse[IpPoolRead], tags=["network"])
def list_ip_pools(
    ip_version: IPVersion | None = None,
    is_active: bool | None = None,
    order_by: str = Query(default="name"),
    order_dir: str = Query(default="asc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return network_service.ip_pools.list_response(
        db, ip_version, is_active, order_by, order_dir, limit, offset
    )


@router.patch("/ip-pools/{pool_id}", response_model=IpPoolRead, tags=["network"])
def update_ip_pool(pool_id: str, payload: IpPoolUpdate, db: Session = Depends(get_db)):
    return network_service.ip_pools.update(db, pool_id, payload)


@router.delete(
    "/ip-pools/{pool_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["network"]
)
def delete_ip_pool(pool_id: str, db: Session = Depends(get_db)):
    network_service.ip_pools.delete(db, pool_id)


@router.post(
    "/ip-blocks",
    response_model=IpBlockRead,
    status_code=status.HTTP_201_CREATED,
    tags=["network"],
)
def create_ip_block(payload: IpBlockCreate, db: Session = Depends(get_db)):
    return network_service.ip_blocks.create(db, payload)


@router.get("/ip-blocks/{block_id}", response_model=IpBlockRead, tags=["network"])
def get_ip_block(block_id: str, db: Session = Depends(get_db)):
    return network_service.ip_blocks.get(db, block_id)


@router.get("/ip-blocks", response_model=ListResponse[IpBlockRead], tags=["network"])
def list_ip_blocks(
    pool_id: str | None = None,
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return network_service.ip_blocks.list_response(
        db, pool_id, is_active, order_by, order_dir, limit, offset
    )


@router.patch(
    "/ip-blocks/{block_id}", response_model=IpBlockRead, tags=["network"]
)
def update_ip_block(block_id: str, payload: IpBlockUpdate, db: Session = Depends(get_db)):
    return network_service.ip_blocks.update(db, block_id, payload)


@router.delete(
    "/ip-blocks/{block_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["network"]
)
def delete_ip_block(block_id: str, db: Session = Depends(get_db)):
    network_service.ip_blocks.delete(db, block_id)


@router.post(
    "/ipv4-addresses",
    response_model=IPv4AddressRead,
    status_code=status.HTTP_201_CREATED,
    tags=["network"],
)
def create_ipv4_address(payload: IPv4AddressCreate, db: Session = Depends(get_db)):
    return network_service.ipv4_addresses.create(db, payload)


@router.get(
    "/ipv4-addresses/{record_id}", response_model=IPv4AddressRead, tags=["network"]
)
def get_ipv4_address(record_id: str, db: Session = Depends(get_db)):
    return network_service.ipv4_addresses.get(db, record_id)


@router.get("/ipv4-addresses", response_model=ListResponse[IPv4AddressRead], tags=["network"])
def list_ipv4_addresses(
    pool_id: str | None = None,
    is_reserved: bool | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return network_service.ipv4_addresses.list_response(db, pool_id, is_reserved, limit, offset)


@router.patch(
    "/ipv4-addresses/{record_id}", response_model=IPv4AddressRead, tags=["network"]
)
def update_ipv4_address(
    record_id: str, payload: IPv4AddressUpdate, db: Session = Depends(get_db)
):
    return network_service.ipv4_addresses.update(
        db, record_id, payload
    )


@router.delete(
    "/ipv4-addresses/{record_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["network"],
)
def delete_ipv4_address(record_id: str, db: Session = Depends(get_db)):
    network_service.ipv4_addresses.delete(db, record_id)


@router.post(
    "/ipv6-addresses",
    response_model=IPv6AddressRead,
    status_code=status.HTTP_201_CREATED,
    tags=["network"],
)
def create_ipv6_address(payload: IPv6AddressCreate, db: Session = Depends(get_db)):
    return network_service.ipv6_addresses.create(db, payload)


@router.get(
    "/ipv6-addresses/{record_id}", response_model=IPv6AddressRead, tags=["network"]
)
def get_ipv6_address(record_id: str, db: Session = Depends(get_db)):
    return network_service.ipv6_addresses.get(db, record_id)


@router.get("/ipv6-addresses", response_model=ListResponse[IPv6AddressRead], tags=["network"])
def list_ipv6_addresses(
    pool_id: str | None = None,
    is_reserved: bool | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return network_service.ipv6_addresses.list_response(db, pool_id, is_reserved, limit, offset)


@router.patch(
    "/ipv6-addresses/{record_id}", response_model=IPv6AddressRead, tags=["network"]
)
def update_ipv6_address(
    record_id: str, payload: IPv6AddressUpdate, db: Session = Depends(get_db)
):
    return network_service.ipv6_addresses.update(
        db, record_id, payload
    )


@router.delete(
    "/ipv6-addresses/{record_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["network"],
)
def delete_ipv6_address(record_id: str, db: Session = Depends(get_db)):
    network_service.ipv6_addresses.delete(db, record_id)


@router.post(
    "/olt-devices",
    response_model=OLTDeviceRead,
    status_code=status.HTTP_201_CREATED,
    tags=["network"],
    dependencies=[Depends(require_permission("network:write"))],
)
def create_olt_device(payload: OLTDeviceCreate, db: Session = Depends(get_db)):
    return network_service.olt_devices.create(db, payload)


@router.get(
    "/olt-devices/{device_id}",
    response_model=OLTDeviceRead,
    tags=["network"],
    dependencies=[Depends(require_permission("network:read"))],
)
def get_olt_device(device_id: str, db: Session = Depends(get_db)):
    return network_service.olt_devices.get(db, device_id)


@router.get(
    "/olt-devices",
    response_model=ListResponse[OLTDeviceRead],
    tags=["network"],
    dependencies=[Depends(require_permission("network:read"))],
)
def list_olt_devices(
    is_active: bool | None = None,
    order_by: str = Query(default="name"),
    order_dir: str = Query(default="asc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return network_service.olt_devices.list_response(
        db, is_active, order_by, order_dir, limit, offset
    )


@router.patch(
    "/olt-devices/{device_id}",
    response_model=OLTDeviceRead,
    tags=["network"],
    dependencies=[Depends(require_permission("network:write"))],
)
def update_olt_device(
    device_id: str, payload: OLTDeviceUpdate, db: Session = Depends(get_db)
):
    return network_service.olt_devices.update(db, device_id, payload)


@router.delete(
    "/olt-devices/{device_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["network"],
    dependencies=[Depends(require_permission("network:write"))],
)
def delete_olt_device(device_id: str, db: Session = Depends(get_db)):
    network_service.olt_devices.delete(db, device_id)


@router.post(
    "/olt-power-units",
    response_model=OltPowerUnitRead,
    status_code=status.HTTP_201_CREATED,
    tags=["network"],
    dependencies=[Depends(require_permission("network:write"))],
)
def create_olt_power_unit(payload: OltPowerUnitCreate, db: Session = Depends(get_db)):
    return network_service.olt_power_units.create(db, payload)


@router.get(
    "/olt-power-units/{unit_id}",
    response_model=OltPowerUnitRead,
    tags=["network"],
    dependencies=[Depends(require_permission("network:read"))],
)
def get_olt_power_unit(unit_id: str, db: Session = Depends(get_db)):
    return network_service.olt_power_units.get(db, unit_id)


@router.get(
    "/olt-power-units",
    response_model=ListResponse[OltPowerUnitRead],
    tags=["network"],
    dependencies=[Depends(require_permission("network:read"))],
)
def list_olt_power_units(
    olt_id: str | None = None,
    is_active: bool | None = None,
    order_by: str = Query(default="slot"),
    order_dir: str = Query(default="asc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return network_service.olt_power_units.list_response(
        db, olt_id, is_active, order_by, order_dir, limit, offset
    )


@router.patch(
    "/olt-power-units/{unit_id}",
    response_model=OltPowerUnitRead,
    tags=["network"],
    dependencies=[Depends(require_permission("network:write"))],
)
def update_olt_power_unit(
    unit_id: str, payload: OltPowerUnitUpdate, db: Session = Depends(get_db)
):
    return network_service.olt_power_units.update(db, unit_id, payload)


@router.delete(
    "/olt-power-units/{unit_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["network"],
    dependencies=[Depends(require_permission("network:write"))],
)
def delete_olt_power_unit(unit_id: str, db: Session = Depends(get_db)):
    network_service.olt_power_units.delete(db, unit_id)


@router.post(
    "/olt-shelves",
    response_model=OltShelfRead,
    status_code=status.HTTP_201_CREATED,
    tags=["network"],
    dependencies=[Depends(require_permission("network:write"))],
)
def create_olt_shelf(payload: OltShelfCreate, db: Session = Depends(get_db)):
    return network_service.olt_shelves.create(db, payload)


@router.get(
    "/olt-shelves/{shelf_id}",
    response_model=OltShelfRead,
    tags=["network"],
    dependencies=[Depends(require_permission("network:read"))],
)
def get_olt_shelf(shelf_id: str, db: Session = Depends(get_db)):
    return network_service.olt_shelves.get(db, shelf_id)


@router.get(
    "/olt-shelves",
    response_model=ListResponse[OltShelfRead],
    tags=["network"],
    dependencies=[Depends(require_permission("network:read"))],
)
def list_olt_shelves(
    olt_id: str | None = None,
    order_by: str = Query(default="shelf_number"),
    order_dir: str = Query(default="asc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return network_service.olt_shelves.list_response(
        db, olt_id, order_by, order_dir, limit, offset
    )


@router.patch(
    "/olt-shelves/{shelf_id}",
    response_model=OltShelfRead,
    tags=["network"],
    dependencies=[Depends(require_permission("network:write"))],
)
def update_olt_shelf(
    shelf_id: str, payload: OltShelfUpdate, db: Session = Depends(get_db)
):
    return network_service.olt_shelves.update(db, shelf_id, payload)


@router.delete(
    "/olt-shelves/{shelf_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["network"],
    dependencies=[Depends(require_permission("network:write"))],
)
def delete_olt_shelf(shelf_id: str, db: Session = Depends(get_db)):
    network_service.olt_shelves.delete(db, shelf_id)


@router.post(
    "/olt-cards",
    response_model=OltCardRead,
    status_code=status.HTTP_201_CREATED,
    tags=["network"],
    dependencies=[Depends(require_permission("network:write"))],
)
def create_olt_card(payload: OltCardCreate, db: Session = Depends(get_db)):
    return network_service.olt_cards.create(db, payload)


@router.get(
    "/olt-cards/{card_id}",
    response_model=OltCardRead,
    tags=["network"],
    dependencies=[Depends(require_permission("network:read"))],
)
def get_olt_card(card_id: str, db: Session = Depends(get_db)):
    return network_service.olt_cards.get(db, card_id)


@router.get(
    "/olt-cards",
    response_model=ListResponse[OltCardRead],
    tags=["network"],
    dependencies=[Depends(require_permission("network:read"))],
)
def list_olt_cards(
    shelf_id: str | None = None,
    order_by: str = Query(default="slot_number"),
    order_dir: str = Query(default="asc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return network_service.olt_cards.list_response(
        db, shelf_id, order_by, order_dir, limit, offset
    )


@router.patch(
    "/olt-cards/{card_id}",
    response_model=OltCardRead,
    tags=["network"],
    dependencies=[Depends(require_permission("network:write"))],
)
def update_olt_card(
    card_id: str, payload: OltCardUpdate, db: Session = Depends(get_db)
):
    return network_service.olt_cards.update(db, card_id, payload)


@router.delete(
    "/olt-cards/{card_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["network"],
    dependencies=[Depends(require_permission("network:write"))],
)
def delete_olt_card(card_id: str, db: Session = Depends(get_db)):
    network_service.olt_cards.delete(db, card_id)


@router.post(
    "/olt-card-ports",
    response_model=OltCardPortRead,
    status_code=status.HTTP_201_CREATED,
    tags=["network"],
    dependencies=[Depends(require_permission("network:write"))],
)
def create_olt_card_port(payload: OltCardPortCreate, db: Session = Depends(get_db)):
    return network_service.olt_card_ports.create(db, payload)


@router.get(
    "/olt-card-ports/{port_id}",
    response_model=OltCardPortRead,
    tags=["network"],
    dependencies=[Depends(require_permission("network:read"))],
)
def get_olt_card_port(port_id: str, db: Session = Depends(get_db)):
    return network_service.olt_card_ports.get(db, port_id)


@router.get(
    "/olt-card-ports",
    response_model=ListResponse[OltCardPortRead],
    tags=["network"],
    dependencies=[Depends(require_permission("network:read"))],
)
def list_olt_card_ports(
    card_id: str | None = None,
    order_by: str = Query(default="port_number"),
    order_dir: str = Query(default="asc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return network_service.olt_card_ports.list_response(
        db, card_id, order_by, order_dir, limit, offset
    )


@router.patch(
    "/olt-card-ports/{port_id}",
    response_model=OltCardPortRead,
    tags=["network"],
    dependencies=[Depends(require_permission("network:write"))],
)
def update_olt_card_port(
    port_id: str, payload: OltCardPortUpdate, db: Session = Depends(get_db)
):
    return network_service.olt_card_ports.update(db, port_id, payload)


@router.delete(
    "/olt-card-ports/{port_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["network"],
    dependencies=[Depends(require_permission("network:write"))],
)
def delete_olt_card_port(port_id: str, db: Session = Depends(get_db)):
    network_service.olt_card_ports.delete(db, port_id)


@router.post(
    "/olt-sfp-modules",
    response_model=OltSfpModuleRead,
    status_code=status.HTTP_201_CREATED,
    tags=["network"],
    dependencies=[Depends(require_permission("network:write"))],
)
def create_olt_sfp_module(payload: OltSfpModuleCreate, db: Session = Depends(get_db)):
    return network_service.olt_sfp_modules.create(db, payload)


@router.get(
    "/olt-sfp-modules/{module_id}",
    response_model=OltSfpModuleRead,
    tags=["network"],
    dependencies=[Depends(require_permission("network:read"))],
)
def get_olt_sfp_module(module_id: str, db: Session = Depends(get_db)):
    return network_service.olt_sfp_modules.get(db, module_id)


@router.get(
    "/olt-sfp-modules",
    response_model=ListResponse[OltSfpModuleRead],
    tags=["network"],
    dependencies=[Depends(require_permission("network:read"))],
)
def list_olt_sfp_modules(
    olt_card_port_id: str | None = None,
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return network_service.olt_sfp_modules.list_response(
        db, olt_card_port_id, is_active, order_by, order_dir, limit, offset
    )


@router.patch(
    "/olt-sfp-modules/{module_id}",
    response_model=OltSfpModuleRead,
    tags=["network"],
    dependencies=[Depends(require_permission("network:write"))],
)
def update_olt_sfp_module(
    module_id: str, payload: OltSfpModuleUpdate, db: Session = Depends(get_db)
):
    return network_service.olt_sfp_modules.update(db, module_id, payload)


@router.delete(
    "/olt-sfp-modules/{module_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["network"],
    dependencies=[Depends(require_permission("network:write"))],
)
def delete_olt_sfp_module(module_id: str, db: Session = Depends(get_db)):
    network_service.olt_sfp_modules.delete(db, module_id)


@router.post(
    "/pon-ports",
    response_model=PonPortRead,
    status_code=status.HTTP_201_CREATED,
    tags=["network"],
    dependencies=[Depends(require_permission("network:write"))],
)
def create_pon_port(payload: PonPortCreate, db: Session = Depends(get_db)):
    return network_service.pon_ports.create(db, payload)


@router.get(
    "/pon-ports/utilization",
    response_model=PortUtilizationRead,
    tags=["network"],
    dependencies=[Depends(require_permission("network:read"))],
)
def get_pon_port_utilization(
    olt_id: str | None = None,
    db: Session = Depends(get_db),
):
    return network_service.pon_ports.utilization(db, olt_id)


@router.get(
    "/pon-ports/{port_id}",
    response_model=PonPortRead,
    tags=["network"],
    dependencies=[Depends(require_permission("network:read"))],
)
def get_pon_port(port_id: str, db: Session = Depends(get_db)):
    return network_service.pon_ports.get(db, port_id)


@router.get(
    "/pon-ports",
    response_model=ListResponse[PonPortRead],
    tags=["network"],
    dependencies=[Depends(require_permission("network:read"))],
)
def list_pon_ports(
    olt_id: str | None = None,
    is_active: bool | None = None,
    order_by: str = Query(default="name"),
    order_dir: str = Query(default="asc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return network_service.pon_ports.list_response(
        db, olt_id, is_active, order_by, order_dir, limit, offset
    )


@router.patch(
    "/pon-ports/{port_id}",
    response_model=PonPortRead,
    tags=["network"],
    dependencies=[Depends(require_permission("network:write"))],
)
def update_pon_port(
    port_id: str, payload: PonPortUpdate, db: Session = Depends(get_db)
):
    return network_service.pon_ports.update(db, port_id, payload)


@router.delete(
    "/pon-ports/{port_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["network"],
    dependencies=[Depends(require_permission("network:write"))],
)
def delete_pon_port(port_id: str, db: Session = Depends(get_db)):
    network_service.pon_ports.delete(db, port_id)


@router.post(
    "/pon-port-splitter-links",
    response_model=PonPortSplitterLinkRead,
    status_code=status.HTTP_201_CREATED,
    tags=["network"],
)
def create_pon_port_splitter_link(
    payload: PonPortSplitterLinkCreate, db: Session = Depends(get_db)
):
    return network_service.pon_port_splitter_links.create(db, payload)


@router.get(
    "/pon-port-splitter-links/{link_id}",
    response_model=PonPortSplitterLinkRead,
    tags=["network"],
)
def get_pon_port_splitter_link(link_id: str, db: Session = Depends(get_db)):
    return network_service.pon_port_splitter_links.get(db, link_id)


@router.get(
    "/pon-port-splitter-links",
    response_model=ListResponse[PonPortSplitterLinkRead],
    tags=["network"],
)
def list_pon_port_splitter_links(
    pon_port_id: str | None = None,
    splitter_port_id: str | None = None,
    active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return network_service.pon_port_splitter_links.list_response(
        db, pon_port_id, splitter_port_id, active, order_by, order_dir, limit, offset
    )


@router.patch(
    "/pon-port-splitter-links/{link_id}",
    response_model=PonPortSplitterLinkRead,
    tags=["network"],
)
def update_pon_port_splitter_link(
    link_id: str, payload: PonPortSplitterLinkUpdate, db: Session = Depends(get_db)
):
    return network_service.pon_port_splitter_links.update(db, link_id, payload)


@router.delete(
    "/pon-port-splitter-links/{link_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["network"],
)
def delete_pon_port_splitter_link(link_id: str, db: Session = Depends(get_db)):
    network_service.pon_port_splitter_links.delete(db, link_id)


@router.post(
    "/ont-units",
    response_model=OntUnitRead,
    status_code=status.HTTP_201_CREATED,
    tags=["network"],
)
def create_ont_unit(payload: OntUnitCreate, db: Session = Depends(get_db)):
    return network_service.ont_units.create(db, payload)


@router.get("/ont-units/{unit_id}", response_model=OntUnitRead, tags=["network"])
def get_ont_unit(unit_id: str, db: Session = Depends(get_db)):
    return network_service.ont_units.get(db, unit_id)


@router.get("/ont-units", response_model=ListResponse[OntUnitRead], tags=["network"])
def list_ont_units(
    is_active: bool | None = None,
    order_by: str = Query(default="serial_number"),
    order_dir: str = Query(default="asc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return network_service.ont_units.list_response(
        db, is_active, order_by, order_dir, limit, offset
    )


@router.patch(
    "/ont-units/{unit_id}", response_model=OntUnitRead, tags=["network"]
)
def update_ont_unit(
    unit_id: str, payload: OntUnitUpdate, db: Session = Depends(get_db)
):
    return network_service.ont_units.update(
        db, unit_id, payload
    )


@router.delete(
    "/ont-units/{unit_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["network"]
)
def delete_ont_unit(unit_id: str, db: Session = Depends(get_db)):
    network_service.ont_units.delete(db, unit_id)


@router.post(
    "/ont-assignments",
    response_model=OntAssignmentRead,
    status_code=status.HTTP_201_CREATED,
    tags=["network"],
)
def create_ont_assignment(payload: OntAssignmentCreate, db: Session = Depends(get_db)):
    return network_service.ont_assignments.create(db, payload)


@router.get(
    "/ont-assignments/{assignment_id}", response_model=OntAssignmentRead, tags=["network"]
)
def get_ont_assignment(assignment_id: str, db: Session = Depends(get_db)):
    return network_service.ont_assignments.get(db, assignment_id)


@router.get(
    "/ont-assignments", response_model=ListResponse[OntAssignmentRead], tags=["network"]
)
def list_ont_assignments(
    pon_port_id: str | None = None,
    subscriber_id: str | None = None,
    account_id: str | None = None,
    subscription_id: str | None = None,
    active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    if not subscriber_id and account_id:
        subscriber_id = account_id
    return network_service.ont_assignments.list_response(
        db,
        pon_port_id,
        subscriber_id,
        subscription_id,
        active,
        order_by,
        order_dir,
        limit,
        offset,
    )


@router.patch(
    "/ont-assignments/{assignment_id}", response_model=OntAssignmentRead, tags=["network"]
)
def update_ont_assignment(
    assignment_id: str, payload: OntAssignmentUpdate, db: Session = Depends(get_db)
):
    return network_service.ont_assignments.update(
        db, assignment_id, payload
    )


@router.delete(
    "/ont-assignments/{assignment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["network"],
)
def delete_ont_assignment(assignment_id: str, db: Session = Depends(get_db)):
    network_service.ont_assignments.delete(db, assignment_id)


@router.post(
    "/fdh-cabinets",
    response_model=FdhCabinetRead,
    status_code=status.HTTP_201_CREATED,
    tags=["network"],
)
def create_fdh_cabinet(payload: FdhCabinetCreate, db: Session = Depends(get_db)):
    return network_service.fdh_cabinets.create(db, payload)


@router.get(
    "/fdh-cabinets/{cabinet_id}", response_model=FdhCabinetRead, tags=["network"]
)
def get_fdh_cabinet(cabinet_id: str, db: Session = Depends(get_db)):
    return network_service.fdh_cabinets.get(db, cabinet_id)


@router.get("/fdh-cabinets", response_model=ListResponse[FdhCabinetRead], tags=["network"])
def list_fdh_cabinets(
    region_id: str | None = None,
    order_by: str = Query(default="name"),
    order_dir: str = Query(default="asc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return network_service.fdh_cabinets.list_response(
        db, region_id, order_by, order_dir, limit, offset
    )


@router.patch(
    "/fdh-cabinets/{cabinet_id}", response_model=FdhCabinetRead, tags=["network"]
)
def update_fdh_cabinet(
    cabinet_id: str, payload: FdhCabinetUpdate, db: Session = Depends(get_db)
):
    return network_service.fdh_cabinets.update(
        db, cabinet_id, payload
    )


@router.delete(
    "/fdh-cabinets/{cabinet_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["network"],
)
def delete_fdh_cabinet(cabinet_id: str, db: Session = Depends(get_db)):
    network_service.fdh_cabinets.delete(db, cabinet_id)


@router.post(
    "/splitters",
    response_model=SplitterRead,
    status_code=status.HTTP_201_CREATED,
    tags=["network"],
)
def create_splitter(payload: SplitterCreate, db: Session = Depends(get_db)):
    return network_service.splitters.create(db, payload)


@router.get("/splitters/{splitter_id}", response_model=SplitterRead, tags=["network"])
def get_splitter(splitter_id: str, db: Session = Depends(get_db)):
    return network_service.splitters.get(db, splitter_id)


@router.get("/splitters", response_model=ListResponse[SplitterRead], tags=["network"])
def list_splitters(
    fdh_id: str | None = None,
    order_by: str = Query(default="name"),
    order_dir: str = Query(default="asc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return network_service.splitters.list_response(
        db, fdh_id, order_by, order_dir, limit, offset
    )


@router.patch(
    "/splitters/{splitter_id}", response_model=SplitterRead, tags=["network"]
)
def update_splitter(
    splitter_id: str, payload: SplitterUpdate, db: Session = Depends(get_db)
):
    return network_service.splitters.update(
        db, splitter_id, payload
    )


@router.delete(
    "/splitters/{splitter_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["network"],
)
def delete_splitter(splitter_id: str, db: Session = Depends(get_db)):
    network_service.splitters.delete(db, splitter_id)


@router.post(
    "/splitter-ports",
    response_model=SplitterPortRead,
    status_code=status.HTTP_201_CREATED,
    tags=["network"],
)
def create_splitter_port(payload: SplitterPortCreate, db: Session = Depends(get_db)):
    return network_service.splitter_ports.create(db, payload)


@router.get(
    "/splitter-ports/{port_id}", response_model=SplitterPortRead, tags=["network"]
)
def get_splitter_port(port_id: str, db: Session = Depends(get_db)):
    return network_service.splitter_ports.get(db, port_id)


@router.get("/splitter-ports", response_model=ListResponse[SplitterPortRead], tags=["network"])
def list_splitter_ports(
    splitter_id: str | None = None,
    port_type: str | None = None,
    order_by: str = Query(default="port_number"),
    order_dir: str = Query(default="asc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return network_service.splitter_ports.list_response(
        db, splitter_id, port_type, order_by, order_dir, limit, offset
    )


@router.get(
    "/splitter-ports/{splitter_id}/utilization",
    response_model=PortUtilizationRead,
    tags=["network"],
)
def get_splitter_port_utilization(splitter_id: str, db: Session = Depends(get_db)):
    return network_service.splitter_ports.utilization(db, splitter_id)


@router.patch(
    "/splitter-ports/{port_id}", response_model=SplitterPortRead, tags=["network"]
)
def update_splitter_port(
    port_id: str, payload: SplitterPortUpdate, db: Session = Depends(get_db)
):
    return network_service.splitter_ports.update(
        db, port_id, payload
    )


@router.delete(
    "/splitter-ports/{port_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["network"],
)
def delete_splitter_port(port_id: str, db: Session = Depends(get_db)):
    network_service.splitter_ports.delete(db, port_id)


@router.post(
    "/splitter-port-assignments",
    response_model=SplitterPortAssignmentRead,
    status_code=status.HTTP_201_CREATED,
    tags=["network"],
)
def create_splitter_port_assignment(
    payload: SplitterPortAssignmentCreate, db: Session = Depends(get_db)
):
    return network_service.splitter_port_assignments.create(db, payload)


@router.get(
    "/splitter-port-assignments/{assignment_id}",
    response_model=SplitterPortAssignmentRead,
    tags=["network"],
)
def get_splitter_port_assignment(
    assignment_id: str, db: Session = Depends(get_db)
):
    return network_service.splitter_port_assignments.get(db, assignment_id)


@router.get(
    "/splitter-port-assignments",
    response_model=ListResponse[SplitterPortAssignmentRead],
    tags=["network"],
)
def list_splitter_port_assignments(
    splitter_port_id: str | None = None,
    subscriber_id: str | None = None,
    account_id: str | None = None,
    subscription_id: str | None = None,
    active: bool | None = None,
    order_by: str = Query(default="assigned_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    if not subscriber_id and account_id:
        subscriber_id = account_id
    return network_service.splitter_port_assignments.list_response(
        db,
        splitter_port_id,
        subscriber_id,
        subscription_id,
        active,
        order_by,
        order_dir,
        limit,
        offset,
    )


@router.patch(
    "/splitter-port-assignments/{assignment_id}",
    response_model=SplitterPortAssignmentRead,
    tags=["network"],
)
def update_splitter_port_assignment(
    assignment_id: str,
    payload: SplitterPortAssignmentUpdate,
    db: Session = Depends(get_db),
):
    return network_service.splitter_port_assignments.update(
        db, assignment_id, payload
    )


@router.delete(
    "/splitter-port-assignments/{assignment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["network"],
)
def delete_splitter_port_assignment(
    assignment_id: str, db: Session = Depends(get_db)
):
    network_service.splitter_port_assignments.delete(db, assignment_id)


@router.post(
    "/fiber-strands",
    response_model=FiberStrandRead,
    status_code=status.HTTP_201_CREATED,
    tags=["network"],
)
def create_fiber_strand(payload: FiberStrandCreate, db: Session = Depends(get_db)):
    return network_service.fiber_strands.create(db, payload)


@router.get(
    "/fiber-strands/{strand_id}", response_model=FiberStrandRead, tags=["network"]
)
def get_fiber_strand(strand_id: str, db: Session = Depends(get_db)):
    return network_service.fiber_strands.get(db, strand_id)


@router.get("/fiber-strands", response_model=ListResponse[FiberStrandRead], tags=["network"])
def list_fiber_strands(
    cable_name: str | None = None,
    status: str | None = None,
    order_by: str = Query(default="strand_number"),
    order_dir: str = Query(default="asc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return network_service.fiber_strands.list_response(
        db, cable_name, status, order_by, order_dir, limit, offset
    )


@router.patch(
    "/fiber-strands/{strand_id}", response_model=FiberStrandRead, tags=["network"]
)
def update_fiber_strand(
    strand_id: str, payload: FiberStrandUpdate, db: Session = Depends(get_db)
):
    return network_service.fiber_strands.update(
        db, strand_id, payload
    )


@router.delete(
    "/fiber-strands/{strand_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["network"],
)
def delete_fiber_strand(strand_id: str, db: Session = Depends(get_db)):
    network_service.fiber_strands.delete(db, strand_id)


@router.post(
    "/fiber-splice-closures",
    response_model=FiberSpliceClosureRead,
    status_code=status.HTTP_201_CREATED,
    tags=["network"],
)
def create_fiber_splice_closure(
    payload: FiberSpliceClosureCreate, db: Session = Depends(get_db)
):
    return network_service.fiber_splice_closures.create(db, payload)


@router.get(
    "/fiber-splice-closures/{closure_id}",
    response_model=FiberSpliceClosureRead,
    tags=["network"],
)
def get_fiber_splice_closure(closure_id: str, db: Session = Depends(get_db)):
    return network_service.fiber_splice_closures.get(db, closure_id)


@router.get(
    "/fiber-splice-closures",
    response_model=ListResponse[FiberSpliceClosureRead],
    tags=["network"],
)
def list_fiber_splice_closures(
    is_active: bool | None = None,
    order_by: str = Query(default="name"),
    order_dir: str = Query(default="asc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return network_service.fiber_splice_closures.list_response(
        db, is_active, order_by, order_dir, limit, offset
    )


@router.patch(
    "/fiber-splice-closures/{closure_id}",
    response_model=FiberSpliceClosureRead,
    tags=["network"],
)
def update_fiber_splice_closure(
    closure_id: str, payload: FiberSpliceClosureUpdate, db: Session = Depends(get_db)
):
    return network_service.fiber_splice_closures.update(
        db, closure_id, payload
    )


@router.delete(
    "/fiber-splice-closures/{closure_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["network"],
)
def delete_fiber_splice_closure(closure_id: str, db: Session = Depends(get_db)):
    network_service.fiber_splice_closures.delete(db, closure_id)


@router.post(
    "/fiber-splice-trays",
    response_model=FiberSpliceTrayRead,
    status_code=status.HTTP_201_CREATED,
    tags=["network"],
)
def create_fiber_splice_tray(
    payload: FiberSpliceTrayCreate, db: Session = Depends(get_db)
):
    return network_service.fiber_splice_trays.create(db, payload)


@router.get(
    "/fiber-splice-trays/{tray_id}",
    response_model=FiberSpliceTrayRead,
    tags=["network"],
)
def get_fiber_splice_tray(tray_id: str, db: Session = Depends(get_db)):
    return network_service.fiber_splice_trays.get(db, tray_id)


@router.get(
    "/fiber-splice-trays",
    response_model=ListResponse[FiberSpliceTrayRead],
    tags=["network"],
)
def list_fiber_splice_trays(
    closure_id: str | None = None,
    order_by: str = Query(default="tray_number"),
    order_dir: str = Query(default="asc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return network_service.fiber_splice_trays.list_response(
        db, closure_id, order_by, order_dir, limit, offset
    )


@router.patch(
    "/fiber-splice-trays/{tray_id}",
    response_model=FiberSpliceTrayRead,
    tags=["network"],
)
def update_fiber_splice_tray(
    tray_id: str, payload: FiberSpliceTrayUpdate, db: Session = Depends(get_db)
):
    return network_service.fiber_splice_trays.update(db, tray_id, payload)


@router.delete(
    "/fiber-splice-trays/{tray_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["network"],
)
def delete_fiber_splice_tray(tray_id: str, db: Session = Depends(get_db)):
    network_service.fiber_splice_trays.delete(db, tray_id)


@router.post(
    "/fiber-splices",
    response_model=FiberSpliceRead,
    status_code=status.HTTP_201_CREATED,
    tags=["network"],
)
def create_fiber_splice(payload: FiberSpliceCreate, db: Session = Depends(get_db)):
    return network_service.fiber_splices.create(db, payload)


@router.get(
    "/fiber-splices/{splice_id}", response_model=FiberSpliceRead, tags=["network"]
)
def get_fiber_splice(splice_id: str, db: Session = Depends(get_db)):
    return network_service.fiber_splices.get(db, splice_id)


@router.get("/fiber-splices", response_model=ListResponse[FiberSpliceRead], tags=["network"])
def list_fiber_splices(
    closure_id: str | None = None,
    strand_id: str | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return network_service.fiber_splices.list_response(
        db, closure_id, strand_id, order_by, order_dir, limit, offset
    )


@router.post(
    "/fiber-termination-points",
    response_model=FiberTerminationPointRead,
    status_code=status.HTTP_201_CREATED,
    tags=["network"],
)
def create_fiber_termination_point(
    payload: FiberTerminationPointCreate, db: Session = Depends(get_db)
):
    return network_service.fiber_termination_points.create(db, payload)


@router.get(
    "/fiber-termination-points/{point_id}",
    response_model=FiberTerminationPointRead,
    tags=["network"],
)
def get_fiber_termination_point(point_id: str, db: Session = Depends(get_db)):
    return network_service.fiber_termination_points.get(db, point_id)


@router.get(
    "/fiber-termination-points",
    response_model=ListResponse[FiberTerminationPointRead],
    tags=["network"],
)
def list_fiber_termination_points(
    endpoint_type: str | None = None,
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return network_service.fiber_termination_points.list_response(
        db, endpoint_type, is_active, order_by, order_dir, limit, offset
    )


@router.patch(
    "/fiber-termination-points/{point_id}",
    response_model=FiberTerminationPointRead,
    tags=["network"],
)
def update_fiber_termination_point(
    point_id: str, payload: FiberTerminationPointUpdate, db: Session = Depends(get_db)
):
    return network_service.fiber_termination_points.update(db, point_id, payload)


@router.delete(
    "/fiber-termination-points/{point_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["network"],
)
def delete_fiber_termination_point(point_id: str, db: Session = Depends(get_db)):
    network_service.fiber_termination_points.delete(db, point_id)


@router.post(
    "/fiber-segments",
    response_model=FiberSegmentRead,
    status_code=status.HTTP_201_CREATED,
    tags=["network"],
)
def create_fiber_segment(payload: FiberSegmentCreate, db: Session = Depends(get_db)):
    return network_service.fiber_segments.create(db, payload)


@router.get(
    "/fiber-segments/{segment_id}",
    response_model=FiberSegmentRead,
    tags=["network"],
)
def get_fiber_segment(segment_id: str, db: Session = Depends(get_db)):
    return network_service.fiber_segments.get(db, segment_id)


@router.get(
    "/fiber-segments",
    response_model=ListResponse[FiberSegmentRead],
    tags=["network"],
)
def list_fiber_segments(
    segment_type: str | None = None,
    fiber_strand_id: str | None = None,
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return network_service.fiber_segments.list_response(
        db, segment_type, fiber_strand_id, is_active, order_by, order_dir, limit, offset
    )


@router.patch(
    "/fiber-segments/{segment_id}",
    response_model=FiberSegmentRead,
    tags=["network"],
)
def update_fiber_segment(
    segment_id: str, payload: FiberSegmentUpdate, db: Session = Depends(get_db)
):
    return network_service.fiber_segments.update(db, segment_id, payload)


@router.delete(
    "/fiber-segments/{segment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["network"],
)
def delete_fiber_segment(segment_id: str, db: Session = Depends(get_db)):
    network_service.fiber_segments.delete(db, segment_id)


@router.get(
    "/fiber-strands/{strand_id}/trace",
    response_model=FiberPathRead,
    tags=["network"],
)
def trace_fiber_path(
    strand_id: str,
    max_hops: int = Query(default=25, ge=1, le=200),
    db: Session = Depends(get_db),
):
    return network_service.fiber_splices.trace_response(db, strand_id, max_hops)


@router.patch(
    "/fiber-splices/{splice_id}", response_model=FiberSpliceRead, tags=["network"]
)
def update_fiber_splice(
    splice_id: str, payload: FiberSpliceUpdate, db: Session = Depends(get_db)
):
    return network_service.fiber_splices.update(db, splice_id, payload)


@router.delete(
    "/fiber-splices/{splice_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["network"],
)
def delete_fiber_splice(splice_id: str, db: Session = Depends(get_db)):
    network_service.fiber_splices.delete(db, splice_id)


@router.post(
    "/uptime-reports",
    response_model=UptimeReportResponse,
    tags=["network-monitoring"],
)
def generate_uptime_report(
    payload: UptimeReportRequest, db: Session = Depends(get_db)
):
    return monitoring_service.uptime_report(db, payload)


@router.get("/pop-sites", response_model=ListResponse[PopSiteRead], tags=["network-monitoring"])
def list_pop_sites(
    is_active: bool | None = None,
    order_by: str = Query(default="name"),
    order_dir: str = Query(default="asc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return monitoring_service.pop_sites.list_response(
        db, is_active, order_by, order_dir, limit, offset
    )


@router.post(
    "/pop-sites",
    response_model=PopSiteRead,
    status_code=status.HTTP_201_CREATED,
    tags=["network-monitoring"],
)
def create_pop_site(payload: PopSiteCreate, db: Session = Depends(get_db)):
    return monitoring_service.pop_sites.create(db, payload)


@router.get("/pop-sites/{site_id}", response_model=PopSiteRead, tags=["network-monitoring"])
def get_pop_site(site_id: str, db: Session = Depends(get_db)):
    return monitoring_service.pop_sites.get(db, site_id)


@router.patch(
    "/pop-sites/{site_id}", response_model=PopSiteRead, tags=["network-monitoring"]
)
def update_pop_site(site_id: str, payload: PopSiteUpdate, db: Session = Depends(get_db)):
    return monitoring_service.pop_sites.update(
        db, site_id, payload
    )


@router.delete(
    "/pop-sites/{site_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["network-monitoring"],
)
def delete_pop_site(site_id: str, db: Session = Depends(get_db)):
    monitoring_service.pop_sites.delete(db, site_id)


@router.get(
    "/network-devices", response_model=ListResponse[NetworkDeviceRead], tags=["network-monitoring"]
)
def list_network_devices(
    pop_site_id: str | None = None,
    is_active: bool | None = None,
    order_by: str = Query(default="name"),
    order_dir: str = Query(default="asc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return monitoring_service.network_devices.list_response(
        db, pop_site_id, is_active, order_by, order_dir, limit, offset
    )


@router.post(
    "/network-devices",
    response_model=NetworkDeviceRead,
    status_code=status.HTTP_201_CREATED,
    tags=["network-monitoring"],
)
def create_network_device(payload: NetworkDeviceCreate, db: Session = Depends(get_db)):
    return monitoring_service.network_devices.create(db, payload)


@router.get(
    "/network-devices/{device_id}", response_model=NetworkDeviceRead, tags=["network-monitoring"]
)
def get_network_device(device_id: str, db: Session = Depends(get_db)):
    return monitoring_service.network_devices.get(db, device_id)


@router.patch(
    "/network-devices/{device_id}", response_model=NetworkDeviceRead, tags=["network-monitoring"]
)
def update_network_device(
    device_id: str, payload: NetworkDeviceUpdate, db: Session = Depends(get_db)
):
    return monitoring_service.network_devices.update(
        db, device_id, payload
    )


@router.delete(
    "/network-devices/{device_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["network-monitoring"],
)
def delete_network_device(device_id: str, db: Session = Depends(get_db)):
    monitoring_service.network_devices.delete(db, device_id)


@router.get(
    "/device-interfaces", response_model=ListResponse[DeviceInterfaceRead], tags=["network-monitoring"]
)
def list_device_interfaces(
    device_id: str | None = None,
    order_by: str = Query(default="name"),
    order_dir: str = Query(default="asc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return monitoring_service.device_interfaces.list_response(
        db, device_id, order_by, order_dir, limit, offset
    )


@router.post(
    "/device-interfaces",
    response_model=DeviceInterfaceRead,
    status_code=status.HTTP_201_CREATED,
    tags=["network-monitoring"],
)
def create_device_interface(payload: DeviceInterfaceCreate, db: Session = Depends(get_db)):
    return monitoring_service.device_interfaces.create(db, payload)


@router.get(
    "/device-interfaces/{interface_id}", response_model=DeviceInterfaceRead, tags=["network-monitoring"]
)
def get_device_interface(interface_id: str, db: Session = Depends(get_db)):
    return monitoring_service.device_interfaces.get(db, interface_id)


@router.patch(
    "/device-interfaces/{interface_id}", response_model=DeviceInterfaceRead, tags=["network-monitoring"]
)
def update_device_interface(
    interface_id: str, payload: DeviceInterfaceUpdate, db: Session = Depends(get_db)
):
    return monitoring_service.device_interfaces.update(
        db, interface_id, payload
    )


@router.delete(
    "/device-interfaces/{interface_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["network-monitoring"],
)
def delete_device_interface(interface_id: str, db: Session = Depends(get_db)):
    monitoring_service.device_interfaces.delete(db, interface_id)


@router.get(
    "/device-metrics", response_model=ListResponse[DeviceMetricRead], tags=["network-monitoring"]
)
def list_device_metrics(
    device_id: str | None = None,
    interface_id: str | None = None,
    order_by: str = Query(default="recorded_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return monitoring_service.device_metrics.list_response(
        db, device_id, interface_id, order_by, order_dir, limit, offset
    )


@router.post(
    "/device-metrics",
    response_model=DeviceMetricRead,
    status_code=status.HTTP_201_CREATED,
    tags=["network-monitoring"],
)
def create_device_metric(payload: DeviceMetricCreate, db: Session = Depends(get_db)):
    return monitoring_service.device_metrics.create(db, payload)


@router.get(
    "/device-metrics/{metric_id}", response_model=DeviceMetricRead, tags=["network-monitoring"]
)
def get_device_metric(metric_id: str, db: Session = Depends(get_db)):
    return monitoring_service.device_metrics.get(db, metric_id)


@router.delete(
    "/device-metrics/{metric_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["network-monitoring"],
)
def delete_device_metric(metric_id: str, db: Session = Depends(get_db)):
    monitoring_service.device_metrics.delete(db, metric_id)


@router.get(
    "/alert-rules",
    response_model=ListResponse[AlertRuleRead],
    tags=["network-monitoring"],
)
def list_alert_rules(
    metric_type: str | None = None,
    device_id: str | None = None,
    interface_id: str | None = None,
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return monitoring_service.alert_rules.list_response(
        db,
        metric_type,
        device_id,
        interface_id,
        is_active,
        order_by,
        order_dir,
        limit,
        offset,
    )


@router.post(
    "/alert-rules",
    response_model=AlertRuleRead,
    status_code=status.HTTP_201_CREATED,
    tags=["network-monitoring"],
)
def create_alert_rule(payload: AlertRuleCreate, db: Session = Depends(get_db)):
    return monitoring_service.alert_rules.create(db, payload)


@router.get(
    "/alert-rules/{rule_id}",
    response_model=AlertRuleRead,
    tags=["network-monitoring"],
)
def get_alert_rule(rule_id: str, db: Session = Depends(get_db)):
    return monitoring_service.alert_rules.get(db, rule_id)


@router.patch(
    "/alert-rules/{rule_id}",
    response_model=AlertRuleRead,
    tags=["network-monitoring"],
)
def update_alert_rule(
    rule_id: str, payload: AlertRuleUpdate, db: Session = Depends(get_db)
):
    return monitoring_service.alert_rules.update(db, rule_id, payload)


@router.delete(
    "/alert-rules/{rule_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["network-monitoring"],
)
def delete_alert_rule(rule_id: str, db: Session = Depends(get_db)):
    monitoring_service.alert_rules.delete(db, rule_id)


@router.post(
    "/alert-rules/bulk/status",
    response_model=AlertRuleBulkUpdateResponse,
    tags=["network-monitoring"],
)
def bulk_update_alert_rules(
    payload: AlertRuleBulkUpdateRequest, db: Session = Depends(get_db)
):
    response = monitoring_service.alert_rules.bulk_update_response(db, payload)
    return AlertRuleBulkUpdateResponse(**response)


@router.get(
    "/alerts",
    response_model=ListResponse[AlertRead],
    tags=["network-monitoring"],
)
def list_alerts(
    rule_id: str | None = None,
    device_id: str | None = None,
    interface_id: str | None = None,
    status: str | None = None,
    severity: str | None = None,
    order_by: str = Query(default="triggered_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return monitoring_service.alerts.list_response(
        db,
        rule_id,
        device_id,
        interface_id,
        status,
        severity,
        order_by,
        order_dir,
        limit,
        offset,
    )


@router.get(
    "/alerts/{alert_id}",
    response_model=AlertRead,
    tags=["network-monitoring"],
)
def get_alert(alert_id: str, db: Session = Depends(get_db)):
    return monitoring_service.alerts.get(db, alert_id)


@router.post(
    "/alerts/{alert_id}/ack",
    response_model=AlertRead,
    tags=["network-monitoring"],
)
def acknowledge_alert(
    alert_id: str, payload: AlertAcknowledgeRequest, db: Session = Depends(get_db)
):
    return monitoring_service.alerts.acknowledge(db, alert_id, payload)


@router.post(
    "/alerts/{alert_id}/resolve",
    response_model=AlertRead,
    tags=["network-monitoring"],
)
def resolve_alert(
    alert_id: str, payload: AlertResolveRequest, db: Session = Depends(get_db)
):
    return monitoring_service.alerts.resolve(db, alert_id, payload)


@router.post(
    "/alerts/bulk/ack",
    response_model=AlertBulkActionResponse,
    tags=["network-monitoring"],
)
def bulk_acknowledge_alerts(
    payload: AlertBulkAcknowledgeRequest, db: Session = Depends(get_db)
):
    ack_payload = AlertAcknowledgeRequest(message=payload.message)
    response = monitoring_service.alerts.bulk_acknowledge_response(
        db, [str(alert_id) for alert_id in payload.alert_ids], ack_payload
    )
    return AlertBulkActionResponse(**response)


@router.post(
    "/alerts/bulk/resolve",
    response_model=AlertBulkActionResponse,
    tags=["network-monitoring"],
)
def bulk_resolve_alerts(
    payload: AlertBulkResolveRequest, db: Session = Depends(get_db)
):
    resolve_payload = AlertResolveRequest(message=payload.message)
    response = monitoring_service.alerts.bulk_resolve_response(
        db, [str(alert_id) for alert_id in payload.alert_ids], resolve_payload
    )
    return AlertBulkActionResponse(**response)


@router.get(
    "/alert-events",
    response_model=ListResponse[AlertEventRead],
    tags=["network-monitoring"],
)
def list_alert_events(
    alert_id: str | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return monitoring_service.alert_events.list_response(
        db, alert_id, order_by, order_dir, limit, offset
    )


@router.get("/service-orders", response_model=ListResponse[ServiceOrderRead], tags=["provisioning"])
def list_service_orders(
    subscriber_id: str | None = None,
    account_id: str | None = None,
    subscription_id: str | None = None,
    status: str | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    if not subscriber_id and account_id:
        subscriber_id = account_id
    return provisioning_service.service_orders.list_response(
        db, subscriber_id, subscription_id, status, order_by, order_dir, limit, offset
    )


@router.post(
    "/service-orders",
    response_model=ServiceOrderRead,
    status_code=status.HTTP_201_CREATED,
    tags=["provisioning"],
)
def create_service_order(payload: ServiceOrderCreate, db: Session = Depends(get_db)):
    return provisioning_service.service_orders.create(db, payload)


@router.get(
    "/service-orders/{order_id}", response_model=ServiceOrderRead, tags=["provisioning"]
)
def get_service_order(order_id: str, db: Session = Depends(get_db)):
    return provisioning_service.service_orders.get(db, order_id)


@router.patch(
    "/service-orders/{order_id}", response_model=ServiceOrderRead, tags=["provisioning"]
)
def update_service_order(
    order_id: str, payload: ServiceOrderUpdate, db: Session = Depends(get_db)
):
    return provisioning_service.service_orders.update(
        db, order_id, payload
    )


@router.delete(
    "/service-orders/{order_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["provisioning"],
)
def delete_service_order(order_id: str, db: Session = Depends(get_db)):
    provisioning_service.service_orders.delete(db, order_id)


@router.get(
    "/install-appointments",
    response_model=ListResponse[InstallAppointmentRead],
    tags=["provisioning"],
)
def list_install_appointments(
    service_order_id: str | None = None,
    status: str | None = None,
    order_by: str = Query(default="scheduled_start"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return provisioning_service.install_appointments.list_response(
        db, service_order_id, status, order_by, order_dir, limit, offset
    )


@router.post(
    "/install-appointments",
    response_model=InstallAppointmentRead,
    status_code=status.HTTP_201_CREATED,
    tags=["provisioning"],
)
def create_install_appointment(
    payload: InstallAppointmentCreate, db: Session = Depends(get_db)
):
    return provisioning_service.install_appointments.create(db, payload)


@router.get(
    "/install-appointments/{appointment_id}",
    response_model=InstallAppointmentRead,
    tags=["provisioning"],
)
def get_install_appointment(appointment_id: str, db: Session = Depends(get_db)):
    return provisioning_service.install_appointments.get(db, appointment_id)


@router.patch(
    "/install-appointments/{appointment_id}",
    response_model=InstallAppointmentRead,
    tags=["provisioning"],
)
def update_install_appointment(
    appointment_id: str,
    payload: InstallAppointmentUpdate,
    db: Session = Depends(get_db),
):
    return provisioning_service.install_appointments.update(
        db, appointment_id, payload
    )


@router.delete(
    "/install-appointments/{appointment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["provisioning"],
)
def delete_install_appointment(appointment_id: str, db: Session = Depends(get_db)):
    provisioning_service.install_appointments.delete(db, appointment_id)


@router.get(
    "/provisioning-tasks", response_model=ListResponse[ProvisioningTaskRead], tags=["provisioning"]
)
def list_provisioning_tasks(
    service_order_id: str | None = None,
    status: str | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return provisioning_service.provisioning_tasks.list_response(
        db, service_order_id, status, order_by, order_dir, limit, offset
    )


@router.post(
    "/provisioning-tasks",
    response_model=ProvisioningTaskRead,
    status_code=status.HTTP_201_CREATED,
    tags=["provisioning"],
)
def create_provisioning_task(
    payload: ProvisioningTaskCreate, db: Session = Depends(get_db)
):
    return provisioning_service.provisioning_tasks.create(db, payload)


@router.get(
    "/provisioning-tasks/{task_id}", response_model=ProvisioningTaskRead, tags=["provisioning"]
)
def get_provisioning_task(task_id: str, db: Session = Depends(get_db)):
    return provisioning_service.provisioning_tasks.get(db, task_id)


@router.patch(
    "/provisioning-tasks/{task_id}", response_model=ProvisioningTaskRead, tags=["provisioning"]
)
def update_provisioning_task(
    task_id: str, payload: ProvisioningTaskUpdate, db: Session = Depends(get_db)
):
    return provisioning_service.provisioning_tasks.update(
        db, task_id, payload
    )


@router.delete(
    "/provisioning-tasks/{task_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["provisioning"],
)
def delete_provisioning_task(task_id: str, db: Session = Depends(get_db)):
    provisioning_service.provisioning_tasks.delete(db, task_id)


@router.get(
    "/provisioning-workflows",
    response_model=ListResponse[ProvisioningWorkflowRead],
    tags=["provisioning"],
)
def list_provisioning_workflows(
    vendor: str | None = None,
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return provisioning_service.provisioning_workflows.list_response(
        db, vendor, is_active, order_by, order_dir, limit, offset
    )


@router.post(
    "/provisioning-workflows",
    response_model=ProvisioningWorkflowRead,
    status_code=status.HTTP_201_CREATED,
    tags=["provisioning"],
)
def create_provisioning_workflow(
    payload: ProvisioningWorkflowCreate, db: Session = Depends(get_db)
):
    return provisioning_service.provisioning_workflows.create(db, payload)


@router.get(
    "/provisioning-workflows/{workflow_id}",
    response_model=ProvisioningWorkflowRead,
    tags=["provisioning"],
)
def get_provisioning_workflow(workflow_id: str, db: Session = Depends(get_db)):
    return provisioning_service.provisioning_workflows.get(db, workflow_id)


@router.patch(
    "/provisioning-workflows/{workflow_id}",
    response_model=ProvisioningWorkflowRead,
    tags=["provisioning"],
)
def update_provisioning_workflow(
    workflow_id: str, payload: ProvisioningWorkflowUpdate, db: Session = Depends(get_db)
):
    return provisioning_service.provisioning_workflows.update(db, workflow_id, payload)


@router.delete(
    "/provisioning-workflows/{workflow_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["provisioning"],
)
def delete_provisioning_workflow(workflow_id: str, db: Session = Depends(get_db)):
    provisioning_service.provisioning_workflows.delete(db, workflow_id)


@router.get(
    "/provisioning-steps",
    response_model=ListResponse[ProvisioningStepRead],
    tags=["provisioning"],
)
def list_provisioning_steps(
    workflow_id: str | None = None,
    step_type: str | None = None,
    is_active: bool | None = None,
    order_by: str = Query(default="order_index"),
    order_dir: str = Query(default="asc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return provisioning_service.provisioning_steps.list_response(
        db, workflow_id, step_type, is_active, order_by, order_dir, limit, offset
    )


@router.post(
    "/provisioning-steps",
    response_model=ProvisioningStepRead,
    status_code=status.HTTP_201_CREATED,
    tags=["provisioning"],
)
def create_provisioning_step(
    payload: ProvisioningStepCreate, db: Session = Depends(get_db)
):
    return provisioning_service.provisioning_steps.create(db, payload)


@router.get(
    "/provisioning-steps/{step_id}",
    response_model=ProvisioningStepRead,
    tags=["provisioning"],
)
def get_provisioning_step(step_id: str, db: Session = Depends(get_db)):
    return provisioning_service.provisioning_steps.get(db, step_id)


@router.patch(
    "/provisioning-steps/{step_id}",
    response_model=ProvisioningStepRead,
    tags=["provisioning"],
)
def update_provisioning_step(
    step_id: str, payload: ProvisioningStepUpdate, db: Session = Depends(get_db)
):
    return provisioning_service.provisioning_steps.update(db, step_id, payload)


@router.delete(
    "/provisioning-steps/{step_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["provisioning"],
)
def delete_provisioning_step(step_id: str, db: Session = Depends(get_db)):
    provisioning_service.provisioning_steps.delete(db, step_id)


@router.get(
    "/provisioning-runs",
    response_model=ListResponse[ProvisioningRunRead],
    tags=["provisioning"],
)
def list_provisioning_runs(
    workflow_id: str | None = None,
    status_filter: str | None = Query(default=None, alias="status"),
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return provisioning_service.provisioning_runs.list_response(
        db, workflow_id, status_filter, order_by, order_dir, limit, offset
    )


@router.post(
    "/provisioning-runs",
    response_model=ProvisioningRunRead,
    status_code=status.HTTP_201_CREATED,
    tags=["provisioning"],
)
def create_provisioning_run(
    payload: ProvisioningRunCreate, db: Session = Depends(get_db)
):
    return provisioning_service.provisioning_runs.create(db, payload)


@router.post(
    "/provisioning-workflows/{workflow_id}/runs",
    response_model=ProvisioningRunRead,
    status_code=status.HTTP_201_CREATED,
    tags=["provisioning"],
)
def run_provisioning_workflow(
    workflow_id: str, payload: ProvisioningRunStart, db: Session = Depends(get_db)
):
    return provisioning_service.provisioning_runs.run(db, workflow_id, payload)


@router.get(
    "/provisioning-runs/{run_id}",
    response_model=ProvisioningRunRead,
    tags=["provisioning"],
)
def get_provisioning_run(run_id: str, db: Session = Depends(get_db)):
    return provisioning_service.provisioning_runs.get(db, run_id)


@router.patch(
    "/provisioning-runs/{run_id}",
    response_model=ProvisioningRunRead,
    tags=["provisioning"],
)
def update_provisioning_run(
    run_id: str, payload: ProvisioningRunUpdate, db: Session = Depends(get_db)
):
    return provisioning_service.provisioning_runs.update(db, run_id, payload)


@router.get(
    "/service-state-transitions",
    response_model=ListResponse[ServiceStateTransitionRead],
    tags=["provisioning"],
)
def list_service_state_transitions(
    service_order_id: str | None = None,
    order_by: str = Query(default="changed_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return provisioning_service.service_state_transitions.list_response(
        db, service_order_id, order_by, order_dir, limit, offset
    )


@router.post(
    "/service-state-transitions",
    response_model=ServiceStateTransitionRead,
    status_code=status.HTTP_201_CREATED,
    tags=["provisioning"],
)
def create_service_state_transition(
    payload: ServiceStateTransitionCreate, db: Session = Depends(get_db)
):
    return provisioning_service.service_state_transitions.create(db, payload)


@router.get(
    "/service-state-transitions/{transition_id}",
    response_model=ServiceStateTransitionRead,
    tags=["provisioning"],
)
def get_service_state_transition(transition_id: str, db: Session = Depends(get_db)):
    return provisioning_service.service_state_transitions.get(db, transition_id)


@router.get("/quota-buckets", response_model=ListResponse[QuotaBucketRead], tags=["usage"])
def list_quota_buckets(
    subscription_id: str | None = None,
    order_by: str = Query(default="period_start"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return usage_service.quota_buckets.list_response(
        db, subscription_id, order_by, order_dir, limit, offset
    )


@router.post(
    "/quota-buckets",
    response_model=QuotaBucketRead,
    status_code=status.HTTP_201_CREATED,
    tags=["usage"],
)
def create_quota_bucket(payload: QuotaBucketCreate, db: Session = Depends(get_db)):
    return usage_service.quota_buckets.create(db, payload)


@router.get(
    "/quota-buckets/{bucket_id}", response_model=QuotaBucketRead, tags=["usage"]
)
def get_quota_bucket(bucket_id: str, db: Session = Depends(get_db)):
    return usage_service.quota_buckets.get(db, bucket_id)


@router.patch(
    "/quota-buckets/{bucket_id}", response_model=QuotaBucketRead, tags=["usage"]
)
def update_quota_bucket(
    bucket_id: str, payload: QuotaBucketUpdate, db: Session = Depends(get_db)
):
    return usage_service.quota_buckets.update(
        db, bucket_id, payload
    )


@router.get(
    "/radius-accounting-sessions",
    response_model=ListResponse[RadiusAccountingSessionRead],
    tags=["usage"],
)
def list_radius_accounting_sessions(
    subscription_id: str | None = None,
    access_credential_id: str | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return usage_service.radius_accounting_sessions.list_response(
        db, subscription_id, access_credential_id, order_by, order_dir, limit, offset
    )


@router.post(
    "/radius-accounting-sessions",
    response_model=RadiusAccountingSessionRead,
    status_code=status.HTTP_201_CREATED,
    tags=["usage"],
)
def create_radius_accounting_session(
    payload: RadiusAccountingSessionCreate, db: Session = Depends(get_db)
):
    return usage_service.radius_accounting_sessions.create(db, payload)


@router.get(
    "/radius-accounting-sessions/{session_id}",
    response_model=RadiusAccountingSessionRead,
    tags=["usage"],
)
def get_radius_accounting_session(session_id: str, db: Session = Depends(get_db)):
    return usage_service.radius_accounting_sessions.get(db, session_id)


@router.get("/usage-records", response_model=ListResponse[UsageRecordRead], tags=["usage"])
def list_usage_records(
    subscription_id: str | None = None,
    quota_bucket_id: str | None = None,
    order_by: str = Query(default="recorded_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return usage_service.usage_records.list_response(
        db, subscription_id, quota_bucket_id, order_by, order_dir, limit, offset
    )


@router.post(
    "/usage-records",
    response_model=UsageRecordRead,
    status_code=status.HTTP_201_CREATED,
    tags=["usage"],
)
def create_usage_record(payload: UsageRecordCreate, db: Session = Depends(get_db)):
    return usage_service.usage_records.create(db, payload)


@router.get("/usage-records/{record_id}", response_model=UsageRecordRead, tags=["usage"])
def get_usage_record(record_id: str, db: Session = Depends(get_db)):
    return usage_service.usage_records.get(db, record_id)


@router.get("/usage-charges", response_model=ListResponse[UsageChargeRead], tags=["usage"])
def list_usage_charges(
    subscription_id: str | None = None,
    subscriber_id: str | None = None,
    account_id: str | None = None,
    status: str | None = None,
    order_by: str = Query(default="period_start"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    if not subscriber_id and account_id:
        subscriber_id = account_id
    return usage_service.usage_charges.list_response(
        db, subscription_id, subscriber_id, status, order_by, order_dir, limit, offset
    )


@router.get("/usage-charges/{charge_id}", response_model=UsageChargeRead, tags=["usage"])
def get_usage_charge(charge_id: str, db: Session = Depends(get_db)):
    return usage_service.usage_charges.get(db, charge_id)


@router.post(
    "/usage-charges/{charge_id}/post",
    response_model=UsageChargeRead,
    tags=["usage"],
)
def post_usage_charge(
    charge_id: str,
    payload: UsageChargePostRequest,
    db: Session = Depends(get_db),
):
    return usage_service.usage_charges.post(db, charge_id, payload)


@router.post(
    "/usage-charges/post-batch",
    response_model=UsageChargePostBatchResponse,
    tags=["usage"],
)
def post_usage_charges_batch(
    payload: UsageChargePostBatchRequest, db: Session = Depends(get_db)
):
    posted = usage_service.usage_charges.post_batch(db, payload)
    return UsageChargePostBatchResponse(posted=posted)


@router.post(
    "/usage-rating-runs",
    response_model=UsageRatingRunResponse,
    tags=["usage"],
)
def run_usage_rating(
    payload: UsageRatingRunRequest, db: Session = Depends(get_db)
):
    return usage_service.usage_rating_runs.run(db, payload)


@router.get(
    "/usage-rating-runs",
    response_model=ListResponse[UsageRatingRunRead],
    tags=["usage"],
)
def list_usage_rating_runs(
    status: str | None = None,
    order_by: str = Query(default="run_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return usage_service.usage_rating_runs.list_response(
        db, status, order_by, order_dir, limit, offset
    )


@router.get(
    "/usage-rating-runs/{run_id}",
    response_model=UsageRatingRunRead,
    tags=["usage"],
)
def get_usage_rating_run(run_id: str, db: Session = Depends(get_db)):
    return usage_service.usage_rating_runs.get(db, run_id)


@router.get("/radius-servers", response_model=ListResponse[RadiusServerRead], tags=["radius"])
def list_radius_servers(
    is_active: bool | None = None,
    order_by: str = Query(default="name"),
    order_dir: str = Query(default="asc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return radius_service.radius_servers.list_response(
        db, is_active, order_by, order_dir, limit, offset
    )


@router.post(
    "/radius-servers",
    response_model=RadiusServerRead,
    status_code=status.HTTP_201_CREATED,
    tags=["radius"],
)
def create_radius_server(payload: RadiusServerCreate, db: Session = Depends(get_db)):
    return radius_service.radius_servers.create(db, payload)


@router.get("/radius-servers/{server_id}", response_model=RadiusServerRead, tags=["radius"])
def get_radius_server(server_id: str, db: Session = Depends(get_db)):
    return radius_service.radius_servers.get(db, server_id)


@router.patch(
    "/radius-servers/{server_id}", response_model=RadiusServerRead, tags=["radius"]
)
def update_radius_server(
    server_id: str, payload: RadiusServerUpdate, db: Session = Depends(get_db)
):
    return radius_service.radius_servers.update(
        db, server_id, payload
    )


@router.delete(
    "/radius-servers/{server_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["radius"],
)
def delete_radius_server(server_id: str, db: Session = Depends(get_db)):
    radius_service.radius_servers.delete(db, server_id)


@router.get("/radius-clients", response_model=ListResponse[RadiusClientRead], tags=["radius"])
def list_radius_clients(
    server_id: str | None = None,
    is_active: bool | None = None,
    order_by: str = Query(default="client_ip"),
    order_dir: str = Query(default="asc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return radius_service.radius_clients.list_response(
        db, server_id, is_active, order_by, order_dir, limit, offset
    )


@router.post(
    "/radius-clients",
    response_model=RadiusClientRead,
    status_code=status.HTTP_201_CREATED,
    tags=["radius"],
)
def create_radius_client(payload: RadiusClientCreate, db: Session = Depends(get_db)):
    return radius_service.radius_clients.create(db, payload)


@router.get("/radius-clients/{client_id}", response_model=RadiusClientRead, tags=["radius"])
def get_radius_client(client_id: str, db: Session = Depends(get_db)):
    return radius_service.radius_clients.get(db, client_id)


@router.patch(
    "/radius-clients/{client_id}", response_model=RadiusClientRead, tags=["radius"]
)
def update_radius_client(
    client_id: str, payload: RadiusClientUpdate, db: Session = Depends(get_db)
):
    return radius_service.radius_clients.update(
        db, client_id, payload
    )


@router.delete(
    "/radius-clients/{client_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["radius"],
)
def delete_radius_client(client_id: str, db: Session = Depends(get_db)):
    radius_service.radius_clients.delete(db, client_id)


@router.get("/radius-users", response_model=ListResponse[RadiusUserRead], tags=["radius"])
def list_radius_users(
    account_id: str | None = None,
    is_active: bool | None = None,
    order_by: str = Query(default="username"),
    order_dir: str = Query(default="asc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return radius_service.radius_users.list_response(
        db, account_id, is_active, order_by, order_dir, limit, offset
    )


@router.get("/radius-users/{user_id}", response_model=RadiusUserRead, tags=["radius"])
def get_radius_user(user_id: str, db: Session = Depends(get_db)):
    return radius_service.radius_users.get(db, user_id)


@router.get(
    "/radius-sync-jobs", response_model=ListResponse[RadiusSyncJobRead], tags=["radius"]
)
def list_radius_sync_jobs(
    server_id: str | None = None,
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return radius_service.radius_sync_jobs.list_response(
        db, server_id, is_active, order_by, order_dir, limit, offset
    )


@router.post(
    "/radius-sync-jobs",
    response_model=RadiusSyncJobRead,
    status_code=status.HTTP_201_CREATED,
    tags=["radius"],
)
def create_radius_sync_job(payload: RadiusSyncJobCreate, db: Session = Depends(get_db)):
    return radius_service.radius_sync_jobs.create(db, payload)


@router.get(
    "/radius-sync-jobs/{job_id}", response_model=RadiusSyncJobRead, tags=["radius"]
)
def get_radius_sync_job(job_id: str, db: Session = Depends(get_db)):
    return radius_service.radius_sync_jobs.get(db, job_id)


@router.patch(
    "/radius-sync-jobs/{job_id}", response_model=RadiusSyncJobRead, tags=["radius"]
)
def update_radius_sync_job(
    job_id: str, payload: RadiusSyncJobUpdate, db: Session = Depends(get_db)
):
    return radius_service.radius_sync_jobs.update(db, job_id, payload)


@router.delete(
    "/radius-sync-jobs/{job_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["radius"],
)
def delete_radius_sync_job(job_id: str, db: Session = Depends(get_db)):
    radius_service.radius_sync_jobs.delete(db, job_id)


@router.post(
    "/radius-sync-jobs/{job_id}/run",
    response_model=RadiusSyncRunRead,
    tags=["radius"],
)
def run_radius_sync_job(job_id: str, db: Session = Depends(get_db)):
    return radius_service.radius_sync_jobs.run(db, job_id)


@router.get("/radius-sync-runs/{run_id}", response_model=RadiusSyncRunRead, tags=["radius"])
def get_radius_sync_run(run_id: str, db: Session = Depends(get_db)):
    return radius_service.radius_sync_runs.get(db, run_id)


@router.get("/radius-sync-runs", response_model=ListResponse[RadiusSyncRunRead], tags=["radius"])
def list_radius_sync_runs(
    job_id: str | None = None,
    status: str | None = None,
    order_by: str = Query(default="started_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return radius_service.radius_sync_runs.list_response(
        db, job_id, status, order_by, order_dir, limit, offset
    )


@router.get(
    "/dunning-cases", response_model=ListResponse[DunningCaseRead], tags=["collections"]
)
def list_dunning_cases(
    account_id: str | None = None,
    status: str | None = None,
    order_by: str = Query(default="started_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return collections_service.dunning_cases.list_response(
        db, account_id, status, order_by, order_dir, limit, offset
    )


@router.post(
    "/dunning-cases",
    response_model=DunningCaseRead,
    status_code=status.HTTP_201_CREATED,
    tags=["collections"],
)
def create_dunning_case(payload: DunningCaseCreate, db: Session = Depends(get_db)):
    return collections_service.dunning_cases.create(db, payload)


@router.get(
    "/dunning-cases/{case_id}", response_model=DunningCaseRead, tags=["collections"]
)
def get_dunning_case(case_id: str, db: Session = Depends(get_db)):
    return collections_service.dunning_cases.get(db, case_id)


@router.patch(
    "/dunning-cases/{case_id}", response_model=DunningCaseRead, tags=["collections"]
)
def update_dunning_case(
    case_id: str, payload: DunningCaseUpdate, db: Session = Depends(get_db)
):
    return collections_service.dunning_cases.update(
        db, case_id, payload
    )


@router.post(
    "/dunning-runs",
    response_model=DunningRunResponse,
    tags=["collections"],
)
def run_dunning(payload: DunningRunRequest, db: Session = Depends(get_db)):
    return collections_service.dunning_workflow.run(db, payload)


@router.get(
    "/dunning-action-logs",
    response_model=ListResponse[DunningActionLogRead],
    tags=["collections"],
)
def list_dunning_action_logs(
    case_id: str | None = None,
    invoice_id: str | None = None,
    payment_id: str | None = None,
    order_by: str = Query(default="executed_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return collections_service.dunning_action_logs.list_response(
        db, case_id, invoice_id, payment_id, order_by, order_dir, limit, offset
    )


@router.post(
    "/dunning-action-logs",
    response_model=DunningActionLogRead,
    status_code=status.HTTP_201_CREATED,
    tags=["collections"],
)
def create_dunning_action_log(payload: DunningActionLogCreate, db: Session = Depends(get_db)):
    return collections_service.dunning_action_logs.create(db, payload)


@router.get(
    "/dunning-action-logs/{log_id}", response_model=DunningActionLogRead, tags=["collections"]
)
def get_dunning_action_log(log_id: str, db: Session = Depends(get_db)):
    return collections_service.dunning_action_logs.get(db, log_id)


@router.get(
    "/subscription-lifecycle-events",
    response_model=ListResponse[SubscriptionLifecycleEventRead],
    tags=["lifecycle"],
)
def list_lifecycle_events(
    subscription_id: str | None = None,
    event_type: str | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return lifecycle_service.subscription_lifecycle_events.list_response(
        db, subscription_id, event_type, order_by, order_dir, limit, offset
    )


@router.post(
    "/subscription-lifecycle-events",
    response_model=SubscriptionLifecycleEventRead,
    status_code=status.HTTP_201_CREATED,
    tags=["lifecycle"],
)
def create_lifecycle_event(
    payload: SubscriptionLifecycleEventCreate, db: Session = Depends(get_db)
):
    return lifecycle_service.subscription_lifecycle_events.create(db, payload)


@router.get(
    "/subscription-lifecycle-events/{event_id}",
    response_model=SubscriptionLifecycleEventRead,
    tags=["lifecycle"],
)
def get_lifecycle_event(event_id: str, db: Session = Depends(get_db)):
    return lifecycle_service.subscription_lifecycle_events.get(db, event_id)


@router.get(
    "/tr069-acs-servers", response_model=ListResponse[Tr069AcsServerRead], tags=["tr069"]
)
def list_tr069_acs_servers(
    is_active: bool | None = None,
    order_by: str = Query(default="name"),
    order_dir: str = Query(default="asc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return tr069_service.acs_servers.list_response(
        db, is_active, order_by, order_dir, limit, offset
    )


@router.post(
    "/tr069-acs-servers",
    response_model=Tr069AcsServerRead,
    status_code=status.HTTP_201_CREATED,
    tags=["tr069"],
)
def create_tr069_acs_server(
    payload: Tr069AcsServerCreate, db: Session = Depends(get_db)
):
    return tr069_service.acs_servers.create(db, payload)


@router.get(
    "/tr069-acs-servers/{server_id}", response_model=Tr069AcsServerRead, tags=["tr069"]
)
def get_tr069_acs_server(server_id: str, db: Session = Depends(get_db)):
    return tr069_service.acs_servers.get(db, server_id)


@router.patch(
    "/tr069-acs-servers/{server_id}", response_model=Tr069AcsServerRead, tags=["tr069"]
)
def update_tr069_acs_server(
    server_id: str, payload: Tr069AcsServerUpdate, db: Session = Depends(get_db)
):
    return tr069_service.acs_servers.update(
        db, server_id, payload
    )


@router.get(
    "/tr069-cpe-devices", response_model=ListResponse[Tr069CpeDeviceRead], tags=["tr069"]
)
def list_tr069_cpe_devices(
    acs_server_id: str | None = None,
    cpe_device_id: str | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return tr069_service.cpe_devices.list_response(
        db, acs_server_id, cpe_device_id, order_by, order_dir, limit, offset
    )


@router.post(
    "/tr069-cpe-devices",
    response_model=Tr069CpeDeviceRead,
    status_code=status.HTTP_201_CREATED,
    tags=["tr069"],
)
def create_tr069_cpe_device(
    payload: Tr069CpeDeviceCreate, db: Session = Depends(get_db)
):
    return tr069_service.cpe_devices.create(db, payload)


@router.get(
    "/tr069-cpe-devices/{device_id}", response_model=Tr069CpeDeviceRead, tags=["tr069"]
)
def get_tr069_cpe_device(device_id: str, db: Session = Depends(get_db)):
    return tr069_service.cpe_devices.get(db, device_id)


@router.patch(
    "/tr069-cpe-devices/{device_id}", response_model=Tr069CpeDeviceRead, tags=["tr069"]
)
def update_tr069_cpe_device(
    device_id: str, payload: Tr069CpeDeviceUpdate, db: Session = Depends(get_db)
):
    return tr069_service.cpe_devices.update(
        db, device_id, payload
    )


@router.get("/tr069-sessions", response_model=ListResponse[Tr069SessionRead], tags=["tr069"])
def list_tr069_sessions(
    device_id: str | None = None,
    order_by: str = Query(default="started_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return tr069_service.sessions.list_response(
        db, device_id, order_by, order_dir, limit, offset
    )


@router.post(
    "/tr069-sessions",
    response_model=Tr069SessionRead,
    status_code=status.HTTP_201_CREATED,
    tags=["tr069"],
)
def create_tr069_session(payload: Tr069SessionCreate, db: Session = Depends(get_db)):
    return tr069_service.sessions.create(db, payload)


@router.get(
    "/tr069-parameters", response_model=ListResponse[Tr069ParameterRead], tags=["tr069"]
)
def list_tr069_parameters(
    device_id: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return tr069_service.parameters.list_response(db, device_id, limit, offset)


@router.post(
    "/tr069-parameters",
    response_model=Tr069ParameterRead,
    status_code=status.HTTP_201_CREATED,
    tags=["tr069"],
)
def create_tr069_parameter(payload: Tr069ParameterCreate, db: Session = Depends(get_db)):
    return tr069_service.parameters.create(db, payload)


@router.get("/tr069-jobs", response_model=ListResponse[Tr069JobRead], tags=["tr069"])
def list_tr069_jobs(
    device_id: str | None = None,
    status: str | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return tr069_service.jobs.list_response(
        db, device_id, status, order_by, order_dir, limit, offset
    )


@router.post(
    "/tr069-jobs",
    response_model=Tr069JobRead,
    status_code=status.HTTP_201_CREATED,
    tags=["tr069"],
)
def create_tr069_job(payload: Tr069JobCreate, db: Session = Depends(get_db)):
    return tr069_service.jobs.create(db, payload)


@router.get("/tr069-jobs/{job_id}", response_model=Tr069JobRead, tags=["tr069"])
def get_tr069_job(job_id: str, db: Session = Depends(get_db)):
    return tr069_service.jobs.get(db, job_id)


@router.patch(
    "/tr069-jobs/{job_id}", response_model=Tr069JobRead, tags=["tr069"]
)
def update_tr069_job(
    job_id: str, payload: Tr069JobUpdate, db: Session = Depends(get_db)
):
    return tr069_service.jobs.update(db, job_id, payload)


@router.get("/snmp-credentials", response_model=ListResponse[SnmpCredentialRead], tags=["snmp"])
def list_snmp_credentials(
    is_active: bool | None = None,
    order_by: str = Query(default="name"),
    order_dir: str = Query(default="asc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return snmp_service.snmp_credentials.list_response(
        db, is_active, order_by, order_dir, limit, offset
    )


@router.post(
    "/snmp-credentials",
    response_model=SnmpCredentialRead,
    status_code=status.HTTP_201_CREATED,
    tags=["snmp"],
)
def create_snmp_credential(payload: SnmpCredentialCreate, db: Session = Depends(get_db)):
    return snmp_service.snmp_credentials.create(db, payload)


@router.get(
    "/snmp-credentials/{credential_id}", response_model=SnmpCredentialRead, tags=["snmp"]
)
def get_snmp_credential(credential_id: str, db: Session = Depends(get_db)):
    return snmp_service.snmp_credentials.get(db, credential_id)


@router.patch(
    "/snmp-credentials/{credential_id}", response_model=SnmpCredentialRead, tags=["snmp"]
)
def update_snmp_credential(
    credential_id: str, payload: SnmpCredentialUpdate, db: Session = Depends(get_db)
):
    return snmp_service.snmp_credentials.update(
        db, credential_id, payload
    )


@router.get("/snmp-targets", response_model=ListResponse[SnmpTargetRead], tags=["snmp"])
def list_snmp_targets(
    device_id: str | None = None,
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return snmp_service.snmp_targets.list_response(
        db, device_id, is_active, order_by, order_dir, limit, offset
    )


@router.post(
    "/snmp-targets",
    response_model=SnmpTargetRead,
    status_code=status.HTTP_201_CREATED,
    tags=["snmp"],
)
def create_snmp_target(payload: SnmpTargetCreate, db: Session = Depends(get_db)):
    return snmp_service.snmp_targets.create(db, payload)


@router.get("/snmp-targets/{target_id}", response_model=SnmpTargetRead, tags=["snmp"])
def get_snmp_target(target_id: str, db: Session = Depends(get_db)):
    return snmp_service.snmp_targets.get(db, target_id)


@router.patch(
    "/snmp-targets/{target_id}", response_model=SnmpTargetRead, tags=["snmp"]
)
def update_snmp_target(
    target_id: str, payload: SnmpTargetUpdate, db: Session = Depends(get_db)
):
    return snmp_service.snmp_targets.update(
        db, target_id, payload
    )


@router.get("/snmp-oids", response_model=ListResponse[SnmpOidRead], tags=["snmp"])
def list_snmp_oids(
    is_active: bool | None = None,
    order_by: str = Query(default="name"),
    order_dir: str = Query(default="asc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return snmp_service.snmp_oids.list_response(
        db, is_active, order_by, order_dir, limit, offset
    )


@router.post(
    "/snmp-oids",
    response_model=SnmpOidRead,
    status_code=status.HTTP_201_CREATED,
    tags=["snmp"],
)
def create_snmp_oid(payload: SnmpOidCreate, db: Session = Depends(get_db)):
    return snmp_service.snmp_oids.create(db, payload)


@router.get("/snmp-oids/{oid_id}", response_model=SnmpOidRead, tags=["snmp"])
def get_snmp_oid(oid_id: str, db: Session = Depends(get_db)):
    return snmp_service.snmp_oids.get(db, oid_id)


@router.patch("/snmp-oids/{oid_id}", response_model=SnmpOidRead, tags=["snmp"])
def update_snmp_oid(oid_id: str, payload: SnmpOidUpdate, db: Session = Depends(get_db)):
    return snmp_service.snmp_oids.update(db, oid_id, payload)


@router.get("/snmp-pollers", response_model=ListResponse[SnmpPollerRead], tags=["snmp"])
def list_snmp_pollers(
    target_id: str | None = None,
    oid_id: str | None = None,
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return snmp_service.snmp_pollers.list_response(
        db, target_id, oid_id, is_active, order_by, order_dir, limit, offset
    )


@router.post(
    "/snmp-pollers",
    response_model=SnmpPollerRead,
    status_code=status.HTTP_201_CREATED,
    tags=["snmp"],
)
def create_snmp_poller(payload: SnmpPollerCreate, db: Session = Depends(get_db)):
    return snmp_service.snmp_pollers.create(db, payload)


@router.get("/snmp-pollers/{poller_id}", response_model=SnmpPollerRead, tags=["snmp"])
def get_snmp_poller(poller_id: str, db: Session = Depends(get_db)):
    return snmp_service.snmp_pollers.get(db, poller_id)


@router.patch(
    "/snmp-pollers/{poller_id}", response_model=SnmpPollerRead, tags=["snmp"]
)
def update_snmp_poller(
    poller_id: str, payload: SnmpPollerUpdate, db: Session = Depends(get_db)
):
    return snmp_service.snmp_pollers.update(
        db, poller_id, payload
    )


@router.get("/snmp-readings", response_model=ListResponse[SnmpReadingRead], tags=["snmp"])
def list_snmp_readings(
    poller_id: str | None = None,
    order_by: str = Query(default="recorded_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return snmp_service.snmp_readings.list_response(
        db, poller_id, order_by, order_dir, limit, offset
    )


@router.post(
    "/snmp-readings",
    response_model=SnmpReadingRead,
    status_code=status.HTTP_201_CREATED,
    tags=["snmp"],
)
def create_snmp_reading(payload: SnmpReadingCreate, db: Session = Depends(get_db)):
    return snmp_service.snmp_readings.create(db, payload)


@router.get("/bandwidth-samples", response_model=ListResponse[BandwidthSampleRead], tags=["bandwidth"])
def list_bandwidth_samples(
    subscription_id: str | None = None,
    device_id: str | None = None,
    interface_id: str | None = None,
    order_by: str = Query(default="sample_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return bandwidth_service.bandwidth_samples.list_response(
        db, subscription_id, device_id, interface_id, order_by, order_dir, limit, offset
    )


@router.get(
    "/bandwidth-series",
    response_model=ListResponse[BandwidthSeriesPoint],
    tags=["bandwidth"],
)
def get_bandwidth_series(
    subscription_id: str | None = None,
    device_id: str | None = None,
    interface_id: str | None = None,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
    interval: str = Query(default="hour", pattern="^(minute|hour|day)$"),
    agg: str = Query(default="avg", pattern="^(avg|max|min)$"),
    db: Session = Depends(get_db),
):
    return bandwidth_service.bandwidth_samples.series_response(
        db,
        subscription_id,
        device_id,
        interface_id,
        start_at,
        end_at,
        interval,
        agg,
    )


@router.post(
    "/bandwidth-samples",
    response_model=BandwidthSampleRead,
    status_code=status.HTTP_201_CREATED,
    tags=["bandwidth"],
)
def create_bandwidth_sample(payload: BandwidthSampleCreate, db: Session = Depends(get_db)):
    return bandwidth_service.bandwidth_samples.create(db, payload)


@router.get(
    "/bandwidth-samples/{sample_id}", response_model=BandwidthSampleRead, tags=["bandwidth"]
)
def get_bandwidth_sample(sample_id: str, db: Session = Depends(get_db)):
    return bandwidth_service.bandwidth_samples.get(db, sample_id)


@router.get(
    "/subscription-engines",
    response_model=ListResponse[SubscriptionEngineRead],
    tags=["subscription-engine"],
)
def list_subscription_engines(
    is_active: bool | None = None,
    order_by: str = Query(default="name"),
    order_dir: str = Query(default="asc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return subscription_engine_service.subscription_engines.list_response(
        db, is_active, order_by, order_dir, limit, offset
    )


@router.post(
    "/subscription-engines",
    response_model=SubscriptionEngineRead,
    status_code=status.HTTP_201_CREATED,
    tags=["subscription-engine"],
)
def create_subscription_engine(
    payload: SubscriptionEngineCreate, db: Session = Depends(get_db)
):
    return subscription_engine_service.subscription_engines.create(db, payload)


@router.get(
    "/subscription-engines/{engine_id}", response_model=SubscriptionEngineRead, tags=["subscription-engine"]
)
def get_subscription_engine(engine_id: str, db: Session = Depends(get_db)):
    return subscription_engine_service.subscription_engines.get(db, engine_id)


@router.patch(
    "/subscription-engines/{engine_id}", response_model=SubscriptionEngineRead, tags=["subscription-engine"]
)
def update_subscription_engine(
    engine_id: str, payload: SubscriptionEngineUpdate, db: Session = Depends(get_db)
):
    return subscription_engine_service.subscription_engines.update(db, engine_id, payload)


@router.get(
    "/subscription-engine-settings",
    response_model=ListResponse[SubscriptionEngineSettingRead],
    tags=["subscription-engine"],
)
def list_subscription_engine_settings(
    engine_id: str | None = None,
    order_by: str = Query(default="key"),
    order_dir: str = Query(default="asc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return subscription_engine_service.subscription_engine_settings.list_response(
        db, engine_id, order_by, order_dir, limit, offset
    )


@router.post(
    "/subscription-engine-settings",
    response_model=SubscriptionEngineSettingRead,
    status_code=status.HTTP_201_CREATED,
    tags=["subscription-engine"],
)
def create_subscription_engine_setting(
    payload: SubscriptionEngineSettingCreate, db: Session = Depends(get_db)
):
    return subscription_engine_service.subscription_engine_settings.create(db, payload)


@router.get(
    "/subscription-engine-settings/{setting_id}",
    response_model=SubscriptionEngineSettingRead,
    tags=["subscription-engine"],
)
def get_subscription_engine_setting(setting_id: str, db: Session = Depends(get_db)):
    return subscription_engine_service.subscription_engine_settings.get(db, setting_id)


@router.patch(
    "/subscription-engine-settings/{setting_id}",
    response_model=SubscriptionEngineSettingRead,
    tags=["subscription-engine"],
)
def update_subscription_engine_setting(
    setting_id: str, payload: SubscriptionEngineSettingUpdate, db: Session = Depends(get_db)
):
    return subscription_engine_service.subscription_engine_settings.update(
        db, setting_id, payload
    )


@router.delete(
    "/quota-buckets/{bucket_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["usage"],
)
def delete_quota_bucket(bucket_id: str, db: Session = Depends(get_db)):
    usage_service.quota_buckets.delete(db, bucket_id)


@router.delete(
    "/radius-accounting-sessions/{session_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["usage"],
)
def delete_radius_accounting_session(session_id: str, db: Session = Depends(get_db)):
    usage_service.radius_accounting_sessions.delete(db, session_id)


@router.delete(
    "/usage-records/{record_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["usage"],
)
def delete_usage_record(record_id: str, db: Session = Depends(get_db)):
    usage_service.usage_records.delete(db, record_id)


@router.delete(
    "/dunning-cases/{case_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["collections"],
)
def delete_dunning_case(case_id: str, db: Session = Depends(get_db)):
    collections_service.dunning_cases.delete(db, case_id)


@router.delete(
    "/dunning-action-logs/{log_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["collections"],
)
def delete_dunning_action_log(log_id: str, db: Session = Depends(get_db)):
    collections_service.dunning_action_logs.delete(db, log_id)


@router.delete(
    "/subscription-lifecycle-events/{event_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["lifecycle"],
)
def delete_lifecycle_event(event_id: str, db: Session = Depends(get_db)):
    lifecycle_service.subscription_lifecycle_events.delete(db, event_id)


@router.delete(
    "/tr069-acs-servers/{server_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["tr069"],
)
def delete_tr069_acs_server(server_id: str, db: Session = Depends(get_db)):
    tr069_service.acs_servers.delete(db, server_id)


@router.delete(
    "/tr069-cpe-devices/{device_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["tr069"],
)
def delete_tr069_cpe_device(device_id: str, db: Session = Depends(get_db)):
    tr069_service.cpe_devices.delete(db, device_id)


@router.delete(
    "/tr069-sessions/{session_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["tr069"],
)
def delete_tr069_session(session_id: str, db: Session = Depends(get_db)):
    tr069_service.sessions.delete(db, session_id)


@router.delete(
    "/tr069-parameters/{parameter_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["tr069"],
)
def delete_tr069_parameter(parameter_id: str, db: Session = Depends(get_db)):
    tr069_service.parameters.delete(db, parameter_id)


@router.delete(
    "/tr069-jobs/{job_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["tr069"],
)
def delete_tr069_job(job_id: str, db: Session = Depends(get_db)):
    tr069_service.jobs.delete(db, job_id)


@router.post(
    "/tr069-cpe-devices/sync",
    tags=["tr069"],
)
def sync_tr069_devices(acs_server_id: str, db: Session = Depends(get_db)):
    """Sync TR-069 devices from GenieACS to local database."""
    return tr069_service.cpe_devices.sync_from_genieacs(db, acs_server_id)


@router.post(
    "/tr069-jobs/{job_id}/execute",
    response_model=Tr069JobRead,
    tags=["tr069"],
)
def execute_tr069_job(job_id: str, db: Session = Depends(get_db)):
    """Execute a TR-069 job via GenieACS."""
    return tr069_service.jobs.execute(db, job_id)


@router.post(
    "/tr069-jobs/{job_id}/cancel",
    response_model=Tr069JobRead,
    tags=["tr069"],
)
def cancel_tr069_job(job_id: str, db: Session = Depends(get_db)):
    """Cancel a queued TR-069 job."""
    return tr069_service.jobs.cancel(db, job_id)


@router.delete(
    "/snmp-credentials/{credential_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["snmp"],
)
def delete_snmp_credential(credential_id: str, db: Session = Depends(get_db)):
    snmp_service.snmp_credentials.delete(db, credential_id)


@router.delete(
    "/snmp-targets/{target_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["snmp"],
)
def delete_snmp_target(target_id: str, db: Session = Depends(get_db)):
    snmp_service.snmp_targets.delete(db, target_id)


@router.delete(
    "/snmp-oids/{oid_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["snmp"],
)
def delete_snmp_oid(oid_id: str, db: Session = Depends(get_db)):
    snmp_service.snmp_oids.delete(db, oid_id)


@router.delete(
    "/snmp-pollers/{poller_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["snmp"],
)
def delete_snmp_poller(poller_id: str, db: Session = Depends(get_db)):
    snmp_service.snmp_pollers.delete(db, poller_id)


@router.delete(
    "/snmp-readings/{reading_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["snmp"],
)
def delete_snmp_reading(reading_id: str, db: Session = Depends(get_db)):
    snmp_service.snmp_readings.delete(db, reading_id)


@router.delete(
    "/bandwidth-samples/{sample_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["bandwidth"],
)
def delete_bandwidth_sample(sample_id: str, db: Session = Depends(get_db)):
    bandwidth_service.bandwidth_samples.delete(db, sample_id)


@router.delete(
    "/subscription-engines/{engine_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["subscription-engine"],
)
def delete_subscription_engine(engine_id: str, db: Session = Depends(get_db)):
    subscription_engine_service.subscription_engines.delete(db, engine_id)


@router.delete(
    "/subscription-engine-settings/{setting_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["subscription-engine"],
)
def delete_subscription_engine_setting(setting_id: str, db: Session = Depends(get_db)):
    subscription_engine_service.subscription_engine_settings.delete(db, setting_id)


@router.delete(
    "/service-state-transitions/{transition_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["provisioning"],
)
def delete_service_state_transition(transition_id: str, db: Session = Depends(get_db)):
    provisioning_service.service_state_transitions.delete(db, transition_id)
