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
from app.services.auth_dependencies import require_permission

router = APIRouter()


@router.post(
    "/cpe-devices",
    response_model=CPEDeviceRead,
    status_code=status.HTTP_201_CREATED,
    tags=["network"],
    dependencies=[Depends(require_permission("network:write"))],
)
def create_cpe_device(payload: CPEDeviceCreate, db: Session = Depends(get_db)):
    return network_service.cpe_devices.create(db, payload)


@router.get(
    "/cpe-devices/{device_id}",
    response_model=CPEDeviceRead,
    tags=["network"],
    dependencies=[Depends(require_permission("network:read"))],
)
def get_cpe_device(device_id: str, db: Session = Depends(get_db)):
    return network_service.cpe_devices.get(db, device_id)


@router.get(
    "/cpe-devices",
    response_model=ListResponse[CPEDeviceRead],
    tags=["network"],
    dependencies=[Depends(require_permission("network:read"))],
)
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


@router.patch(
    "/cpe-devices/{device_id}",
    response_model=CPEDeviceRead,
    tags=["network"],
    dependencies=[Depends(require_permission("network:write"))],
)
def update_cpe_device(
    device_id: str, payload: CPEDeviceUpdate, db: Session = Depends(get_db)
):
    return network_service.cpe_devices.update(db, device_id, payload)


@router.delete(
    "/cpe-devices/{device_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["network"],
    dependencies=[Depends(require_permission("network:write"))],
)
def delete_cpe_device(device_id: str, db: Session = Depends(get_db)):
    network_service.cpe_devices.delete(db, device_id)


@router.post(
    "/ports",
    response_model=PortRead,
    status_code=status.HTTP_201_CREATED,
    tags=["network"],
    dependencies=[Depends(require_permission("network:write"))],
)
def create_port(payload: PortCreate, db: Session = Depends(get_db)):
    return network_service.ports.create(db, payload)


@router.get(
    "/ports/{port_id}",
    response_model=PortRead,
    tags=["network"],
    dependencies=[Depends(require_permission("network:read"))],
)
def get_port(port_id: str, db: Session = Depends(get_db)):
    return network_service.ports.get(db, port_id)


@router.get(
    "/ports",
    response_model=ListResponse[PortRead],
    tags=["network"],
    dependencies=[Depends(require_permission("network:read"))],
)
def list_ports(
    device_id: str | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return network_service.ports.list_response(
        db, device_id, order_by, order_dir, limit, offset
    )


@router.patch(
    "/ports/{port_id}",
    response_model=PortRead,
    tags=["network"],
    dependencies=[Depends(require_permission("network:write"))],
)
def update_port(port_id: str, payload: PortUpdate, db: Session = Depends(get_db)):
    return network_service.ports.update(db, port_id, payload)


@router.delete(
    "/ports/{port_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["network"],
    dependencies=[Depends(require_permission("network:write"))],
)
def delete_port(port_id: str, db: Session = Depends(get_db)):
    network_service.ports.delete(db, port_id)


@router.post(
    "/vlans",
    response_model=VlanRead,
    status_code=status.HTTP_201_CREATED,
    tags=["network"],
    dependencies=[Depends(require_permission("network:write"))],
)
def create_vlan(payload: VlanCreate, db: Session = Depends(get_db)):
    return network_service.vlans.create(db, payload)


@router.get(
    "/vlans/{vlan_id}",
    response_model=VlanRead,
    tags=["network"],
    dependencies=[Depends(require_permission("network:read"))],
)
def get_vlan(vlan_id: str, db: Session = Depends(get_db)):
    return network_service.vlans.get(db, vlan_id)


@router.get(
    "/vlans",
    response_model=ListResponse[VlanRead],
    tags=["network"],
    dependencies=[Depends(require_permission("network:read"))],
)
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


@router.patch(
    "/vlans/{vlan_id}",
    response_model=VlanRead,
    tags=["network"],
    dependencies=[Depends(require_permission("network:write"))],
)
def update_vlan(vlan_id: str, payload: VlanUpdate, db: Session = Depends(get_db)):
    return network_service.vlans.update(db, vlan_id, payload)


@router.delete(
    "/vlans/{vlan_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["network"],
    dependencies=[Depends(require_permission("network:write"))],
)
def delete_vlan(vlan_id: str, db: Session = Depends(get_db)):
    network_service.vlans.delete(db, vlan_id)


@router.post(
    "/port-vlans",
    response_model=PortVlanRead,
    status_code=status.HTTP_201_CREATED,
    tags=["network"],
    dependencies=[Depends(require_permission("network:write"))],
)
def create_port_vlan(payload: PortVlanCreate, db: Session = Depends(get_db)):
    return network_service.port_vlans.create(db, payload)


@router.get(
    "/port-vlans/{link_id}",
    response_model=PortVlanRead,
    tags=["network"],
    dependencies=[Depends(require_permission("network:read"))],
)
def get_port_vlan(link_id: str, db: Session = Depends(get_db)):
    return network_service.port_vlans.get(db, link_id)


@router.get(
    "/port-vlans",
    response_model=ListResponse[PortVlanRead],
    tags=["network"],
    dependencies=[Depends(require_permission("network:read"))],
)
def list_port_vlans(
    port_id: str | None = None,
    vlan_id: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return network_service.port_vlans.list_response(db, port_id, vlan_id, limit, offset)


@router.delete(
    "/port-vlans/{link_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["network"],
    dependencies=[Depends(require_permission("network:write"))],
)
def delete_port_vlan(link_id: str, db: Session = Depends(get_db)):
    network_service.port_vlans.delete(db, link_id)


@router.post(
    "/ip-assignments",
    response_model=IPAssignmentRead,
    status_code=status.HTTP_201_CREATED,
    tags=["network"],
    dependencies=[Depends(require_permission("network:write"))],
)
def create_ip_assignment(payload: IPAssignmentCreate, db: Session = Depends(get_db)):
    return network_service.ip_assignments.create(db, payload)


@router.get(
    "/ip-assignments/{assignment_id}",
    response_model=IPAssignmentRead,
    tags=["network"],
    dependencies=[Depends(require_permission("network:read"))],
)
def get_ip_assignment(assignment_id: str, db: Session = Depends(get_db)):
    return network_service.ip_assignments.get(db, assignment_id)


@router.get(
    "/ip-assignments",
    response_model=ListResponse[IPAssignmentRead],
    tags=["network"],
    dependencies=[Depends(require_permission("network:read"))],
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
        db,
        subscriber_id,
        subscription_id,
        ip_version,
        order_by,
        order_dir,
        limit,
        offset,
    )


@router.patch(
    "/ip-assignments/{assignment_id}",
    response_model=IPAssignmentRead,
    tags=["network"],
    dependencies=[Depends(require_permission("network:write"))],
)
def update_ip_assignment(
    assignment_id: str, payload: IPAssignmentUpdate, db: Session = Depends(get_db)
):
    return network_service.ip_assignments.update(db, assignment_id, payload)


@router.delete(
    "/ip-assignments/{assignment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["network"],
    dependencies=[Depends(require_permission("network:write"))],
)
def delete_ip_assignment(assignment_id: str, db: Session = Depends(get_db)):
    network_service.ip_assignments.delete(db, assignment_id)


@router.post(
    "/ip-pools",
    response_model=IpPoolRead,
    status_code=status.HTTP_201_CREATED,
    tags=["network"],
    dependencies=[Depends(require_permission("network:write"))],
)
def create_ip_pool(payload: IpPoolCreate, db: Session = Depends(get_db)):
    return network_service.ip_pools.create(db, payload)


@router.get(
    "/ip-pools/{pool_id}",
    response_model=IpPoolRead,
    tags=["network"],
    dependencies=[Depends(require_permission("network:read"))],
)
def get_ip_pool(pool_id: str, db: Session = Depends(get_db)):
    return network_service.ip_pools.get(db, pool_id)


@router.get(
    "/ip-pools",
    response_model=ListResponse[IpPoolRead],
    tags=["network"],
    dependencies=[Depends(require_permission("network:read"))],
)
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


@router.patch(
    "/ip-pools/{pool_id}",
    response_model=IpPoolRead,
    tags=["network"],
    dependencies=[Depends(require_permission("network:write"))],
)
def update_ip_pool(pool_id: str, payload: IpPoolUpdate, db: Session = Depends(get_db)):
    return network_service.ip_pools.update(db, pool_id, payload)


@router.delete(
    "/ip-pools/{pool_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["network"],
    dependencies=[Depends(require_permission("network:write"))],
)
def delete_ip_pool(pool_id: str, db: Session = Depends(get_db)):
    network_service.ip_pools.delete(db, pool_id)


@router.post(
    "/ip-blocks",
    response_model=IpBlockRead,
    status_code=status.HTTP_201_CREATED,
    tags=["network"],
    dependencies=[Depends(require_permission("network:write"))],
)
def create_ip_block(payload: IpBlockCreate, db: Session = Depends(get_db)):
    return network_service.ip_blocks.create(db, payload)


@router.get(
    "/ip-blocks/{block_id}",
    response_model=IpBlockRead,
    tags=["network"],
    dependencies=[Depends(require_permission("network:read"))],
)
def get_ip_block(block_id: str, db: Session = Depends(get_db)):
    return network_service.ip_blocks.get(db, block_id)


@router.get(
    "/ip-blocks",
    response_model=ListResponse[IpBlockRead],
    tags=["network"],
    dependencies=[Depends(require_permission("network:read"))],
)
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
    "/ip-blocks/{block_id}",
    response_model=IpBlockRead,
    tags=["network"],
    dependencies=[Depends(require_permission("network:write"))],
)
def update_ip_block(
    block_id: str, payload: IpBlockUpdate, db: Session = Depends(get_db)
):
    return network_service.ip_blocks.update(db, block_id, payload)


@router.delete(
    "/ip-blocks/{block_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["network"],
    dependencies=[Depends(require_permission("network:write"))],
)
def delete_ip_block(block_id: str, db: Session = Depends(get_db)):
    network_service.ip_blocks.delete(db, block_id)


@router.post(
    "/ipv4-addresses",
    response_model=IPv4AddressRead,
    status_code=status.HTTP_201_CREATED,
    tags=["network"],
    dependencies=[Depends(require_permission("network:write"))],
)
def create_ipv4_address(payload: IPv4AddressCreate, db: Session = Depends(get_db)):
    return network_service.ipv4_addresses.create(db, payload)


@router.get(
    "/ipv4-addresses/{record_id}",
    response_model=IPv4AddressRead,
    tags=["network"],
    dependencies=[Depends(require_permission("network:read"))],
)
def get_ipv4_address(record_id: str, db: Session = Depends(get_db)):
    return network_service.ipv4_addresses.get(db, record_id)


@router.get(
    "/ipv4-addresses",
    response_model=ListResponse[IPv4AddressRead],
    tags=["network"],
    dependencies=[Depends(require_permission("network:read"))],
)
def list_ipv4_addresses(
    pool_id: str | None = None,
    is_reserved: bool | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return network_service.ipv4_addresses.list_response(
        db, pool_id, is_reserved, limit, offset
    )


@router.patch(
    "/ipv4-addresses/{record_id}",
    response_model=IPv4AddressRead,
    tags=["network"],
    dependencies=[Depends(require_permission("network:write"))],
)
def update_ipv4_address(
    record_id: str, payload: IPv4AddressUpdate, db: Session = Depends(get_db)
):
    return network_service.ipv4_addresses.update(db, record_id, payload)


@router.delete(
    "/ipv4-addresses/{record_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["network"],
    dependencies=[Depends(require_permission("network:write"))],
)
def delete_ipv4_address(record_id: str, db: Session = Depends(get_db)):
    network_service.ipv4_addresses.delete(db, record_id)


@router.post(
    "/ipv6-addresses",
    response_model=IPv6AddressRead,
    status_code=status.HTTP_201_CREATED,
    tags=["network"],
    dependencies=[Depends(require_permission("network:write"))],
)
def create_ipv6_address(payload: IPv6AddressCreate, db: Session = Depends(get_db)):
    return network_service.ipv6_addresses.create(db, payload)


@router.get(
    "/ipv6-addresses/{record_id}",
    response_model=IPv6AddressRead,
    tags=["network"],
    dependencies=[Depends(require_permission("network:read"))],
)
def get_ipv6_address(record_id: str, db: Session = Depends(get_db)):
    return network_service.ipv6_addresses.get(db, record_id)


@router.get(
    "/ipv6-addresses",
    response_model=ListResponse[IPv6AddressRead],
    tags=["network"],
    dependencies=[Depends(require_permission("network:read"))],
)
def list_ipv6_addresses(
    pool_id: str | None = None,
    is_reserved: bool | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return network_service.ipv6_addresses.list_response(
        db, pool_id, is_reserved, limit, offset
    )


@router.patch(
    "/ipv6-addresses/{record_id}",
    response_model=IPv6AddressRead,
    tags=["network"],
    dependencies=[Depends(require_permission("network:write"))],
)
def update_ipv6_address(
    record_id: str, payload: IPv6AddressUpdate, db: Session = Depends(get_db)
):
    return network_service.ipv6_addresses.update(db, record_id, payload)


@router.delete(
    "/ipv6-addresses/{record_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["network"],
    dependencies=[Depends(require_permission("network:write"))],
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
    dependencies=[Depends(require_permission("network:write"))],
)
def create_pon_port_splitter_link(
    payload: PonPortSplitterLinkCreate, db: Session = Depends(get_db)
):
    return network_service.pon_port_splitter_links.create(db, payload)


@router.get(
    "/pon-port-splitter-links/{link_id}",
    response_model=PonPortSplitterLinkRead,
    tags=["network"],
    dependencies=[Depends(require_permission("network:read"))],
)
def get_pon_port_splitter_link(link_id: str, db: Session = Depends(get_db)):
    return network_service.pon_port_splitter_links.get(db, link_id)


@router.get(
    "/pon-port-splitter-links",
    response_model=ListResponse[PonPortSplitterLinkRead],
    tags=["network"],
    dependencies=[Depends(require_permission("network:read"))],
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
    dependencies=[Depends(require_permission("network:write"))],
)
def update_pon_port_splitter_link(
    link_id: str, payload: PonPortSplitterLinkUpdate, db: Session = Depends(get_db)
):
    return network_service.pon_port_splitter_links.update(db, link_id, payload)


@router.delete(
    "/pon-port-splitter-links/{link_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["network"],
    dependencies=[Depends(require_permission("network:write"))],
)
def delete_pon_port_splitter_link(link_id: str, db: Session = Depends(get_db)):
    network_service.pon_port_splitter_links.delete(db, link_id)


@router.post(
    "/ont-units",
    response_model=OntUnitRead,
    status_code=status.HTTP_201_CREATED,
    tags=["network"],
    dependencies=[Depends(require_permission("network:write"))],
)
def create_ont_unit(payload: OntUnitCreate, db: Session = Depends(get_db)):
    return network_service.ont_units.create(db, payload)


@router.get(
    "/ont-units/{unit_id}",
    response_model=OntUnitRead,
    tags=["network"],
    dependencies=[Depends(require_permission("network:read"))],
)
def get_ont_unit(unit_id: str, db: Session = Depends(get_db)):
    return network_service.ont_units.get(db, unit_id)


@router.get(
    "/ont-units",
    response_model=ListResponse[OntUnitRead],
    tags=["network"],
    dependencies=[Depends(require_permission("network:read"))],
)
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
    "/ont-units/{unit_id}",
    response_model=OntUnitRead,
    tags=["network"],
    dependencies=[Depends(require_permission("network:write"))],
)
def update_ont_unit(
    unit_id: str, payload: OntUnitUpdate, db: Session = Depends(get_db)
):
    return network_service.ont_units.update(db, unit_id, payload)


@router.delete(
    "/ont-units/{unit_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["network"],
    dependencies=[Depends(require_permission("network:write"))],
)
def delete_ont_unit(unit_id: str, db: Session = Depends(get_db)):
    network_service.ont_units.delete(db, unit_id)


@router.post(
    "/ont-assignments",
    response_model=OntAssignmentRead,
    status_code=status.HTTP_201_CREATED,
    tags=["network"],
    dependencies=[Depends(require_permission("network:write"))],
)
def create_ont_assignment(payload: OntAssignmentCreate, db: Session = Depends(get_db)):
    return network_service.ont_assignments.create(db, payload)


@router.get(
    "/ont-assignments/{assignment_id}",
    response_model=OntAssignmentRead,
    tags=["network"],
    dependencies=[Depends(require_permission("network:read"))],
)
def get_ont_assignment(assignment_id: str, db: Session = Depends(get_db)):
    return network_service.ont_assignments.get(db, assignment_id)


@router.get(
    "/ont-assignments",
    response_model=ListResponse[OntAssignmentRead],
    tags=["network"],
    dependencies=[Depends(require_permission("network:read"))],
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
    "/ont-assignments/{assignment_id}",
    response_model=OntAssignmentRead,
    tags=["network"],
    dependencies=[Depends(require_permission("network:write"))],
)
def update_ont_assignment(
    assignment_id: str, payload: OntAssignmentUpdate, db: Session = Depends(get_db)
):
    return network_service.ont_assignments.update(db, assignment_id, payload)


@router.delete(
    "/ont-assignments/{assignment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["network"],
    dependencies=[Depends(require_permission("network:write"))],
)
def delete_ont_assignment(assignment_id: str, db: Session = Depends(get_db)):
    network_service.ont_assignments.delete(db, assignment_id)
