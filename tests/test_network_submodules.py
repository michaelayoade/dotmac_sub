"""Tests for network service submodules: CPE, IP, OLT, and NAS.

Covers CRUD operations and utility functions for:
- CPE devices and ports (app/services/network/cpe.py)
- IP pools and addresses (app/services/network/ip.py)
- OLT infrastructure (app/services/network/olt.py)
- NAS device management (app/services/nas.py)
- Utility functions (_redact_sensitive)
"""

import uuid
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from app.models.catalog import NasDeviceStatus, NasVendor
from app.models.network import DeviceStatus, DeviceType, IPVersion
from app.schemas.catalog import NasDeviceCreate, NasDeviceUpdate
from app.schemas.network import (
    CPEDeviceCreate,
    CPEDeviceUpdate,
    IPv4AddressCreate,
    IPv4AddressUpdate,
    IPv6AddressCreate,
    IPv6AddressUpdate,
    IpPoolCreate,
    IpPoolUpdate,
    OLTDeviceCreate,
    OLTDeviceUpdate,
    OltCardCreate,
    OltCardPortCreate,
    OltCardUpdate,
    OltPowerUnitCreate,
    OltPowerUnitUpdate,
    OltSfpModuleCreate,
    OltSfpModuleUpdate,
    OltShelfCreate,
    OltShelfUpdate,
    OntAssignmentCreate,
    OntAssignmentUpdate,
    OntUnitCreate,
    OntUnitUpdate,
    PonPortCreate,
    PonPortUpdate,
    PortCreate,
    PortUpdate,
    PortVlanCreate,
    PortVlanUpdate,
    VlanCreate,
    VlanUpdate,
)
from app.services import nas as nas_service
from app.services import network as network_service
from app.services.nas import _redact_sensitive


# =============================================================================
# CPE Device CRUD Tests
# =============================================================================


class TestCPEDevicesCRUD:
    """Tests for CPE device create, get, list, update, delete."""

    def test_create_cpe_device(self, db_session, subscriber):
        """Test creating a CPE device."""
        device = network_service.cpe_devices.create(
            db_session,
            CPEDeviceCreate(
                account_id=subscriber.id,
                device_type=DeviceType.ont,
                status=DeviceStatus.active,
                serial_number="CPE-SN-001",
                model="Nokia G-240W-A",
            ),
        )
        assert device.subscriber_id == subscriber.id
        assert device.serial_number == "CPE-SN-001"
        assert device.model == "Nokia G-240W-A"
        assert device.device_type == DeviceType.ont
        assert device.status == DeviceStatus.active

    def test_get_cpe_device(self, db_session, subscriber):
        """Test getting a CPE device by ID."""
        device = network_service.cpe_devices.create(
            db_session,
            CPEDeviceCreate(
                account_id=subscriber.id,
                serial_number="CPE-GET-001",
            ),
        )
        fetched = network_service.cpe_devices.get(db_session, str(device.id))
        assert fetched.id == device.id
        assert fetched.serial_number == "CPE-GET-001"

    def test_get_cpe_device_not_found(self, db_session):
        """Test 404 for non-existent CPE device."""
        with pytest.raises(HTTPException) as exc_info:
            network_service.cpe_devices.get(db_session, str(uuid.uuid4()))
        assert exc_info.value.status_code == 404

    def test_list_cpe_devices(self, db_session, subscriber):
        """Test listing CPE devices."""
        network_service.cpe_devices.create(
            db_session,
            CPEDeviceCreate(account_id=subscriber.id, serial_number="CPE-LIST-1"),
        )
        network_service.cpe_devices.create(
            db_session,
            CPEDeviceCreate(account_id=subscriber.id, serial_number="CPE-LIST-2"),
        )
        devices = network_service.cpe_devices.list(
            db_session,
            subscriber_id=str(subscriber.id),
            subscription_id=None,
            order_by="created_at",
            order_dir="asc",
            limit=100,
            offset=0,
        )
        assert len(devices) >= 2

    def test_list_cpe_devices_empty(self, db_session):
        """Test listing CPE devices when none exist for a subscriber."""
        devices = network_service.cpe_devices.list(
            db_session,
            subscriber_id=str(uuid.uuid4()),
            subscription_id=None,
            order_by="created_at",
            order_dir="asc",
            limit=100,
            offset=0,
        )
        assert len(devices) == 0

    def test_update_cpe_device(self, db_session, subscriber):
        """Test updating a CPE device."""
        device = network_service.cpe_devices.create(
            db_session,
            CPEDeviceCreate(
                account_id=subscriber.id,
                serial_number="CPE-UPD-001",
                model="Old Model",
            ),
        )
        updated = network_service.cpe_devices.update(
            db_session,
            str(device.id),
            CPEDeviceUpdate(model="New Model", mac_address="AA:BB:CC:DD:EE:FF"),
        )
        assert updated.model == "New Model"
        assert updated.mac_address == "AA:BB:CC:DD:EE:FF"
        # Unchanged fields preserved
        assert updated.serial_number == "CPE-UPD-001"

    def test_update_cpe_device_not_found(self, db_session):
        """Test 404 for updating non-existent CPE device."""
        with pytest.raises(HTTPException) as exc_info:
            network_service.cpe_devices.update(
                db_session,
                str(uuid.uuid4()),
                CPEDeviceUpdate(model="test"),
            )
        assert exc_info.value.status_code == 404

    def test_delete_cpe_device(self, db_session, subscriber):
        """Test deleting a CPE device (hard delete)."""
        device = network_service.cpe_devices.create(
            db_session,
            CPEDeviceCreate(account_id=subscriber.id, serial_number="CPE-DEL-001"),
        )
        device_id = str(device.id)
        network_service.cpe_devices.delete(db_session, device_id)
        with pytest.raises(HTTPException) as exc_info:
            network_service.cpe_devices.get(db_session, device_id)
        assert exc_info.value.status_code == 404

    def test_delete_cpe_device_not_found(self, db_session):
        """Test 404 for deleting non-existent CPE device."""
        with pytest.raises(HTTPException) as exc_info:
            network_service.cpe_devices.delete(db_session, str(uuid.uuid4()))
        assert exc_info.value.status_code == 404


# =============================================================================
# Port CRUD Tests
# =============================================================================


class TestPortsCRUD:
    """Tests for Port create, get, list, update, delete via cpe submodule."""

    def test_create_port(self, db_session, olt_device):
        """Test creating a port with olt_id."""
        port = network_service.ports.create(
            db_session,
            PortCreate(olt_id=olt_device.id, name="ge-0/0/1"),
        )
        assert port.name == "ge-0/0/1"
        assert port.device_id == olt_device.id

    def test_get_port(self, db_session, olt_device):
        """Test getting a port by ID."""
        port = network_service.ports.create(
            db_session,
            PortCreate(olt_id=olt_device.id, name="ge-0/0/2"),
        )
        fetched = network_service.ports.get(db_session, str(port.id))
        assert fetched.id == port.id

    def test_get_port_not_found(self, db_session):
        """Test 404 for non-existent port."""
        with pytest.raises(HTTPException) as exc_info:
            network_service.ports.get(db_session, str(uuid.uuid4()))
        assert exc_info.value.status_code == 404

    def test_list_ports_filtered(self, db_session, olt_device):
        """Test listing ports filtered by device_id."""
        network_service.ports.create(
            db_session, PortCreate(olt_id=olt_device.id, name="p-list-1")
        )
        network_service.ports.create(
            db_session, PortCreate(olt_id=olt_device.id, name="p-list-2")
        )
        ports = network_service.ports.list(
            db_session,
            device_id=str(olt_device.id),
            order_by="created_at",
            order_dir="asc",
            limit=100,
            offset=0,
        )
        assert len(ports) >= 2

    def test_update_port(self, db_session, olt_device):
        """Test updating a port."""
        port = network_service.ports.create(
            db_session, PortCreate(olt_id=olt_device.id, name="upd-port")
        )
        updated = network_service.ports.update(
            db_session, str(port.id), PortUpdate(name="upd-port-renamed")
        )
        assert updated.name == "upd-port-renamed"

    def test_delete_port(self, db_session, olt_device):
        """Test deleting a port (hard delete via CRUDManager)."""
        port = network_service.ports.create(
            db_session, PortCreate(olt_id=olt_device.id, name="del-port")
        )
        port_id = str(port.id)
        network_service.ports.delete(db_session, port_id)
        with pytest.raises(HTTPException) as exc_info:
            network_service.ports.get(db_session, port_id)
        assert exc_info.value.status_code == 404


# =============================================================================
# VLAN CRUD Tests
# =============================================================================


class TestVlansCRUD:
    """Tests for VLAN create, get, list, update, delete."""

    def test_create_vlan(self, db_session, region):
        """Test creating a VLAN."""
        vlan = network_service.vlans.create(
            db_session,
            VlanCreate(region_id=region.id, tag=500, name="Sub-VLAN-500"),
        )
        assert vlan.tag == 500
        assert vlan.name == "Sub-VLAN-500"
        assert vlan.is_active is True

    def test_get_vlan(self, db_session, region):
        """Test getting a VLAN by ID."""
        vlan = network_service.vlans.create(
            db_session,
            VlanCreate(region_id=region.id, tag=501, name="Get-VLAN"),
        )
        fetched = network_service.vlans.get(db_session, str(vlan.id))
        assert fetched.id == vlan.id
        assert fetched.tag == 501

    def test_get_vlan_not_found(self, db_session):
        """Test 404 for non-existent VLAN."""
        with pytest.raises(HTTPException) as exc_info:
            network_service.vlans.get(db_session, str(uuid.uuid4()))
        assert exc_info.value.status_code == 404

    def test_update_vlan(self, db_session, region):
        """Test updating a VLAN."""
        vlan = network_service.vlans.create(
            db_session,
            VlanCreate(region_id=region.id, tag=502, name="Old VLAN"),
        )
        updated = network_service.vlans.update(
            db_session, str(vlan.id), VlanUpdate(name="New VLAN", description="Updated")
        )
        assert updated.name == "New VLAN"
        assert updated.description == "Updated"

    def test_delete_vlan_soft(self, db_session, region):
        """Test soft deleting a VLAN (sets is_active=False)."""
        vlan = network_service.vlans.create(
            db_session,
            VlanCreate(region_id=region.id, tag=503, name="Delete-VLAN"),
        )
        network_service.vlans.delete(db_session, str(vlan.id))
        db_session.refresh(vlan)
        assert vlan.is_active is False

    def test_list_vlans_by_region(self, db_session, region):
        """Test listing VLANs filtered by region."""
        network_service.vlans.create(
            db_session,
            VlanCreate(region_id=region.id, tag=510, name="Region-VLAN-1"),
        )
        network_service.vlans.create(
            db_session,
            VlanCreate(region_id=region.id, tag=511, name="Region-VLAN-2"),
        )
        vlans = network_service.vlans.list(
            db_session,
            region_id=str(region.id),
            is_active=None,
            order_by="created_at",
            order_dir="asc",
            limit=100,
            offset=0,
        )
        assert len(vlans) >= 2


# =============================================================================
# PortVlan CRUD Tests
# =============================================================================


class TestPortVlansCRUD:
    """Tests for PortVlan link create, get, list, update, delete."""

    def test_create_port_vlan(self, db_session, olt_device, region):
        """Test creating a port-VLAN link."""
        port = network_service.ports.create(
            db_session, PortCreate(olt_id=olt_device.id, name="pv-port")
        )
        vlan = network_service.vlans.create(
            db_session, VlanCreate(region_id=region.id, tag=600, name="PV-VLAN")
        )
        pv = network_service.port_vlans.create(
            db_session,
            PortVlanCreate(port_id=port.id, vlan_id=vlan.id, is_tagged=True),
        )
        assert pv.port_id == port.id
        assert pv.vlan_id == vlan.id
        assert pv.is_tagged is True

    def test_get_port_vlan(self, db_session, olt_device, region):
        """Test getting a port-VLAN by ID."""
        port = network_service.ports.create(
            db_session, PortCreate(olt_id=olt_device.id, name="pv-get")
        )
        vlan = network_service.vlans.create(
            db_session, VlanCreate(region_id=region.id, tag=601, name="PV-GET")
        )
        pv = network_service.port_vlans.create(
            db_session, PortVlanCreate(port_id=port.id, vlan_id=vlan.id)
        )
        fetched = network_service.port_vlans.get(db_session, str(pv.id))
        assert fetched.id == pv.id

    def test_get_port_vlan_not_found(self, db_session):
        """Test 404 for non-existent port-VLAN."""
        with pytest.raises(HTTPException) as exc_info:
            network_service.port_vlans.get(db_session, str(uuid.uuid4()))
        assert exc_info.value.status_code == 404

    def test_update_port_vlan(self, db_session, olt_device, region):
        """Test updating a port-VLAN link."""
        port = network_service.ports.create(
            db_session, PortCreate(olt_id=olt_device.id, name="pv-upd")
        )
        vlan = network_service.vlans.create(
            db_session, VlanCreate(region_id=region.id, tag=602, name="PV-UPD")
        )
        pv = network_service.port_vlans.create(
            db_session, PortVlanCreate(port_id=port.id, vlan_id=vlan.id, is_tagged=False)
        )
        updated = network_service.port_vlans.update(
            db_session, str(pv.id), PortVlanUpdate(is_tagged=True)
        )
        assert updated.is_tagged is True

    def test_delete_port_vlan(self, db_session, olt_device, region):
        """Test deleting a port-VLAN link (hard delete)."""
        port = network_service.ports.create(
            db_session, PortCreate(olt_id=olt_device.id, name="pv-del")
        )
        vlan = network_service.vlans.create(
            db_session, VlanCreate(region_id=region.id, tag=603, name="PV-DEL")
        )
        pv = network_service.port_vlans.create(
            db_session, PortVlanCreate(port_id=port.id, vlan_id=vlan.id)
        )
        pv_id = str(pv.id)
        network_service.port_vlans.delete(db_session, pv_id)
        with pytest.raises(HTTPException) as exc_info:
            network_service.port_vlans.get(db_session, pv_id)
        assert exc_info.value.status_code == 404

    def test_list_port_vlans(self, db_session, olt_device, region):
        """Test listing port-VLAN links."""
        port = network_service.ports.create(
            db_session, PortCreate(olt_id=olt_device.id, name="pv-list")
        )
        v1 = network_service.vlans.create(
            db_session, VlanCreate(region_id=region.id, tag=604, name="PV-LIST-1")
        )
        v2 = network_service.vlans.create(
            db_session, VlanCreate(region_id=region.id, tag=605, name="PV-LIST-2")
        )
        network_service.port_vlans.create(
            db_session, PortVlanCreate(port_id=port.id, vlan_id=v1.id)
        )
        network_service.port_vlans.create(
            db_session, PortVlanCreate(port_id=port.id, vlan_id=v2.id)
        )
        pvs = network_service.port_vlans.list(
            db_session,
            port_id=str(port.id),
            vlan_id=None,
            order_by="port_id",
            order_dir="asc",
            limit=100,
            offset=0,
        )
        assert len(pvs) >= 2


# =============================================================================
# IP Pool CRUD Tests
# =============================================================================


class TestIpPoolsCRUD:
    """Tests for IP pool create, get, list, update, delete."""

    def test_create_ip_pool(self, db_session):
        """Test creating an IP pool with full parameters."""
        pool = network_service.ip_pools.create(
            db_session,
            IpPoolCreate(
                name="Sub Pool v4",
                cidr="10.100.0.0/24",
                ip_version=IPVersion.ipv4,
                gateway="10.100.0.1",
                dns_primary="8.8.8.8",
                dns_secondary="8.8.4.4",
            ),
        )
        assert pool.name == "Sub Pool v4"
        assert pool.cidr == "10.100.0.0/24"
        assert pool.gateway == "10.100.0.1"
        assert pool.dns_primary == "8.8.8.8"
        assert pool.is_active is True

    def test_get_ip_pool(self, db_session):
        """Test getting an IP pool by ID."""
        pool = network_service.ip_pools.create(
            db_session,
            IpPoolCreate(name="Get Pool", cidr="10.101.0.0/24", ip_version=IPVersion.ipv4),
        )
        fetched = network_service.ip_pools.get(db_session, str(pool.id))
        assert fetched.id == pool.id
        assert fetched.name == "Get Pool"

    def test_get_ip_pool_not_found(self, db_session):
        """Test 404 for non-existent IP pool."""
        with pytest.raises(HTTPException) as exc_info:
            network_service.ip_pools.get(db_session, str(uuid.uuid4()))
        assert exc_info.value.status_code == 404

    def test_update_ip_pool(self, db_session):
        """Test updating an IP pool."""
        pool = network_service.ip_pools.create(
            db_session,
            IpPoolCreate(name="Upd Pool", cidr="10.102.0.0/24", ip_version=IPVersion.ipv4),
        )
        updated = network_service.ip_pools.update(
            db_session, str(pool.id), IpPoolUpdate(name="Updated Pool", notes="changed")
        )
        assert updated.name == "Updated Pool"
        assert updated.notes == "changed"

    def test_update_ip_pool_not_found(self, db_session):
        """Test 404 for updating non-existent IP pool."""
        with pytest.raises(HTTPException) as exc_info:
            network_service.ip_pools.update(
                db_session, str(uuid.uuid4()), IpPoolUpdate(name="nope")
            )
        assert exc_info.value.status_code == 404

    def test_delete_ip_pool_soft(self, db_session):
        """Test soft deleting an IP pool (sets is_active=False)."""
        pool = network_service.ip_pools.create(
            db_session,
            IpPoolCreate(name="Del Pool", cidr="10.103.0.0/24", ip_version=IPVersion.ipv4),
        )
        network_service.ip_pools.delete(db_session, str(pool.id))
        db_session.refresh(pool)
        assert pool.is_active is False

    def test_delete_ip_pool_not_found(self, db_session):
        """Test 404 for deleting non-existent IP pool."""
        with pytest.raises(HTTPException) as exc_info:
            network_service.ip_pools.delete(db_session, str(uuid.uuid4()))
        assert exc_info.value.status_code == 404

    def test_list_ip_pools_filter_version(self, db_session):
        """Test listing IP pools with ip_version filter."""
        network_service.ip_pools.create(
            db_session,
            IpPoolCreate(name="v4 Pool", cidr="10.104.0.0/24", ip_version=IPVersion.ipv4),
        )
        pools = network_service.ip_pools.list(
            db_session,
            ip_version="ipv4",
            is_active=None,
            order_by="created_at",
            order_dir="asc",
            limit=100,
            offset=0,
        )
        assert len(pools) >= 1


# =============================================================================
# IPv4 Address CRUD Tests
# =============================================================================


class TestIPv4AddressesCRUD:
    """Tests for IPv4 address create, get, list, update, delete."""

    def test_create_ipv4_address(self, db_session):
        """Test creating an IPv4 address."""
        pool = network_service.ip_pools.create(
            db_session,
            IpPoolCreate(name="v4 Addr Pool", cidr="10.200.0.0/24", ip_version=IPVersion.ipv4),
        )
        addr = network_service.ipv4_addresses.create(
            db_session,
            IPv4AddressCreate(address="10.200.0.10", pool_id=pool.id),
        )
        assert addr.address == "10.200.0.10"
        assert addr.pool_id == pool.id

    def test_get_ipv4_address(self, db_session):
        """Test getting an IPv4 address by ID."""
        addr = network_service.ipv4_addresses.create(
            db_session,
            IPv4AddressCreate(address="10.200.0.11"),
        )
        fetched = network_service.ipv4_addresses.get(db_session, str(addr.id))
        assert fetched.id == addr.id

    def test_get_ipv4_address_not_found(self, db_session):
        """Test 404 for non-existent IPv4 address."""
        with pytest.raises(HTTPException) as exc_info:
            network_service.ipv4_addresses.get(db_session, str(uuid.uuid4()))
        assert exc_info.value.status_code == 404

    def test_update_ipv4_address(self, db_session):
        """Test updating an IPv4 address."""
        addr = network_service.ipv4_addresses.create(
            db_session,
            IPv4AddressCreate(address="10.200.0.12"),
        )
        updated = network_service.ipv4_addresses.update(
            db_session, str(addr.id), IPv4AddressUpdate(is_reserved=True)
        )
        assert updated.is_reserved is True

    def test_delete_ipv4_address(self, db_session):
        """Test deleting an IPv4 address (hard delete)."""
        addr = network_service.ipv4_addresses.create(
            db_session,
            IPv4AddressCreate(address="10.200.0.13"),
        )
        addr_id = str(addr.id)
        network_service.ipv4_addresses.delete(db_session, addr_id)
        with pytest.raises(HTTPException) as exc_info:
            network_service.ipv4_addresses.get(db_session, addr_id)
        assert exc_info.value.status_code == 404

    def test_list_ipv4_addresses(self, db_session):
        """Test listing IPv4 addresses."""
        network_service.ipv4_addresses.create(
            db_session, IPv4AddressCreate(address="10.200.1.1")
        )
        network_service.ipv4_addresses.create(
            db_session, IPv4AddressCreate(address="10.200.1.2")
        )
        addrs = network_service.ipv4_addresses.list(
            db_session,
            pool_id=None,
            is_reserved=None,
            order_by="created_at",
            order_dir="asc",
            limit=100,
            offset=0,
        )
        assert len(addrs) >= 2


# =============================================================================
# IPv6 Address CRUD Tests
# =============================================================================


class TestIPv6AddressesCRUD:
    """Tests for IPv6 address create, get, list, update, delete."""

    def test_create_ipv6_address(self, db_session):
        """Test creating an IPv6 address."""
        addr = network_service.ipv6_addresses.create(
            db_session,
            IPv6AddressCreate(address="2001:db8::1"),
        )
        assert addr.address == "2001:db8::1"

    def test_get_ipv6_address(self, db_session):
        """Test getting an IPv6 address by ID."""
        addr = network_service.ipv6_addresses.create(
            db_session,
            IPv6AddressCreate(address="2001:db8::2"),
        )
        fetched = network_service.ipv6_addresses.get(db_session, str(addr.id))
        assert fetched.id == addr.id

    def test_get_ipv6_address_not_found(self, db_session):
        """Test 404 for non-existent IPv6 address."""
        with pytest.raises(HTTPException) as exc_info:
            network_service.ipv6_addresses.get(db_session, str(uuid.uuid4()))
        assert exc_info.value.status_code == 404

    def test_update_ipv6_address(self, db_session):
        """Test updating an IPv6 address."""
        addr = network_service.ipv6_addresses.create(
            db_session,
            IPv6AddressCreate(address="2001:db8::3"),
        )
        updated = network_service.ipv6_addresses.update(
            db_session, str(addr.id), IPv6AddressUpdate(is_reserved=True, notes="reserved")
        )
        assert updated.is_reserved is True
        assert updated.notes == "reserved"

    def test_delete_ipv6_address(self, db_session):
        """Test deleting an IPv6 address (hard delete)."""
        addr = network_service.ipv6_addresses.create(
            db_session,
            IPv6AddressCreate(address="2001:db8::4"),
        )
        addr_id = str(addr.id)
        network_service.ipv6_addresses.delete(db_session, addr_id)
        with pytest.raises(HTTPException) as exc_info:
            network_service.ipv6_addresses.get(db_session, addr_id)
        assert exc_info.value.status_code == 404

    def test_list_ipv6_addresses(self, db_session):
        """Test listing IPv6 addresses."""
        network_service.ipv6_addresses.create(
            db_session, IPv6AddressCreate(address="2001:db8::10")
        )
        network_service.ipv6_addresses.create(
            db_session, IPv6AddressCreate(address="2001:db8::11")
        )
        addrs = network_service.ipv6_addresses.list(
            db_session,
            pool_id=None,
            is_reserved=None,
            order_by="created_at",
            order_dir="asc",
            limit=100,
            offset=0,
        )
        assert len(addrs) >= 2


# =============================================================================
# OLT Device CRUD Tests
# =============================================================================


class TestOLTDevicesCRUD:
    """Tests for OLT device create, get, list, update, delete."""

    def test_create_olt_device(self, db_session):
        """Test creating an OLT device with full parameters."""
        olt = network_service.olt_devices.create(
            db_session,
            OLTDeviceCreate(
                name="OLT-Sub-001",
                hostname="olt-sub-001.fiber.local",
                mgmt_ip="192.168.1.100",
                vendor="Huawei",
                model="MA5800-X15",
                serial_number="HW12345",
            ),
        )
        assert olt.name == "OLT-Sub-001"
        assert olt.hostname == "olt-sub-001.fiber.local"
        assert olt.mgmt_ip == "192.168.1.100"
        assert olt.vendor == "Huawei"
        assert olt.is_active is True

    def test_get_olt_device(self, db_session):
        """Test getting an OLT device by ID."""
        olt = network_service.olt_devices.create(
            db_session,
            OLTDeviceCreate(name="OLT-GET", hostname="olt-get.local"),
        )
        fetched = network_service.olt_devices.get(db_session, str(olt.id))
        assert fetched.id == olt.id

    def test_get_olt_device_not_found(self, db_session):
        """Test 404 for non-existent OLT device."""
        with pytest.raises(HTTPException) as exc_info:
            network_service.olt_devices.get(db_session, str(uuid.uuid4()))
        assert exc_info.value.status_code == 404

    def test_update_olt_device(self, db_session):
        """Test updating an OLT device."""
        olt = network_service.olt_devices.create(
            db_session,
            OLTDeviceCreate(name="OLT-UPD", hostname="olt-upd.local"),
        )
        updated = network_service.olt_devices.update(
            db_session,
            str(olt.id),
            OLTDeviceUpdate(name="OLT-UPDATED", notes="firmware upgraded"),
        )
        assert updated.name == "OLT-UPDATED"
        assert updated.notes == "firmware upgraded"

    def test_update_olt_device_not_found(self, db_session):
        """Test 404 for updating non-existent OLT device."""
        with pytest.raises(HTTPException) as exc_info:
            network_service.olt_devices.update(
                db_session, str(uuid.uuid4()), OLTDeviceUpdate(name="nope")
            )
        assert exc_info.value.status_code == 404

    def test_delete_olt_device_soft(self, db_session):
        """Test soft deleting an OLT device (sets is_active=False)."""
        olt = network_service.olt_devices.create(
            db_session,
            OLTDeviceCreate(name="OLT-DEL", hostname="olt-del.local"),
        )
        network_service.olt_devices.delete(db_session, str(olt.id))
        db_session.refresh(olt)
        assert olt.is_active is False

    def test_delete_olt_device_not_found(self, db_session):
        """Test 404 for deleting non-existent OLT device."""
        with pytest.raises(HTTPException) as exc_info:
            network_service.olt_devices.delete(db_session, str(uuid.uuid4()))
        assert exc_info.value.status_code == 404

    def test_list_olt_devices(self, db_session):
        """Test listing OLT devices."""
        network_service.olt_devices.create(
            db_session, OLTDeviceCreate(name="OLT-LIST-1", hostname="olt-list-1.local")
        )
        network_service.olt_devices.create(
            db_session, OLTDeviceCreate(name="OLT-LIST-2", hostname="olt-list-2.local")
        )
        olts = network_service.olt_devices.list(
            db_session,
            is_active=None,
            order_by="created_at",
            order_dir="asc",
            limit=100,
            offset=0,
        )
        assert len(olts) >= 2

    def test_list_olt_devices_pagination(self, db_session):
        """Test OLT device list pagination with limit and offset."""
        for i in range(5):
            network_service.olt_devices.create(
                db_session,
                OLTDeviceCreate(name=f"OLT-PAGE-{i}", hostname=f"olt-page-{i}.local"),
            )
        page1 = network_service.olt_devices.list(
            db_session,
            is_active=None,
            order_by="created_at",
            order_dir="asc",
            limit=2,
            offset=0,
        )
        page2 = network_service.olt_devices.list(
            db_session,
            is_active=None,
            order_by="created_at",
            order_dir="asc",
            limit=2,
            offset=2,
        )
        assert len(page1) == 2
        assert len(page2) == 2
        # Ensure different records
        page1_ids = {str(o.id) for o in page1}
        page2_ids = {str(o.id) for o in page2}
        assert page1_ids.isdisjoint(page2_ids)


# =============================================================================
# PON Port CRUD Tests
# =============================================================================


class TestPonPortsCRUD:
    """Tests for PON port create, get, list, update, delete."""

    def _make_olt(self, db_session, name="Pon OLT"):
        return network_service.olt_devices.create(
            db_session,
            OLTDeviceCreate(name=name, hostname=f"{name.lower().replace(' ', '-')}.local"),
        )

    def test_create_pon_port(self, db_session):
        """Test creating a PON port."""
        olt = self._make_olt(db_session, "Pon Create OLT")
        pon = network_service.pon_ports.create(
            db_session,
            PonPortCreate(olt_id=olt.id, port_number=1),
        )
        assert pon.olt_id == olt.id
        assert pon.port_number == 1
        assert pon.name == "pon-1"  # auto-generated

    def test_create_pon_port_bad_olt(self, db_session):
        """Test 404 when creating PON port with invalid OLT ID."""
        with pytest.raises(HTTPException) as exc_info:
            network_service.pon_ports.create(
                db_session,
                PonPortCreate(olt_id=uuid.uuid4(), port_number=1),
            )
        assert exc_info.value.status_code == 404

    def test_get_pon_port(self, db_session):
        """Test getting a PON port by ID."""
        olt = self._make_olt(db_session, "Pon Get OLT")
        pon = network_service.pon_ports.create(
            db_session, PonPortCreate(olt_id=olt.id, port_number=2)
        )
        fetched = network_service.pon_ports.get(db_session, str(pon.id))
        assert fetched.id == pon.id

    def test_get_pon_port_not_found(self, db_session):
        """Test 404 for non-existent PON port."""
        with pytest.raises(HTTPException) as exc_info:
            network_service.pon_ports.get(db_session, str(uuid.uuid4()))
        assert exc_info.value.status_code == 404

    def test_update_pon_port(self, db_session):
        """Test updating a PON port."""
        olt = self._make_olt(db_session, "Pon Upd OLT")
        pon = network_service.pon_ports.create(
            db_session, PonPortCreate(olt_id=olt.id, port_number=3)
        )
        updated = network_service.pon_ports.update(
            db_session, str(pon.id), PonPortUpdate(name="pon-renamed", notes="updated")
        )
        assert updated.name == "pon-renamed"
        assert updated.notes == "updated"

    def test_update_pon_port_not_found(self, db_session):
        """Test 404 for updating non-existent PON port."""
        with pytest.raises(HTTPException) as exc_info:
            network_service.pon_ports.update(
                db_session, str(uuid.uuid4()), PonPortUpdate(name="nope")
            )
        assert exc_info.value.status_code == 404

    def test_delete_pon_port_soft(self, db_session):
        """Test soft deleting a PON port."""
        olt = self._make_olt(db_session, "Pon Del OLT")
        pon = network_service.pon_ports.create(
            db_session, PonPortCreate(olt_id=olt.id, port_number=4)
        )
        pon_id = str(pon.id)
        network_service.pon_ports.delete(db_session, pon_id)
        # Soft delete sets is_active=False
        with pytest.raises(HTTPException) as exc_info:
            network_service.pon_ports.get(db_session, pon_id)
        assert exc_info.value.status_code == 404

    def test_list_pon_ports(self, db_session):
        """Test listing PON ports filtered by OLT."""
        olt = self._make_olt(db_session, "Pon List OLT")
        network_service.pon_ports.create(
            db_session, PonPortCreate(olt_id=olt.id, port_number=5)
        )
        network_service.pon_ports.create(
            db_session, PonPortCreate(olt_id=olt.id, port_number=6)
        )
        pons = network_service.pon_ports.list(
            db_session,
            olt_id=str(olt.id),
            is_active=None,
            order_by="created_at",
            order_dir="asc",
            limit=100,
            offset=0,
        )
        assert len(pons) >= 2

    def test_pon_port_utilization(self, db_session):
        """Test PON port utilization report."""
        olt = self._make_olt(db_session, "Pon Util OLT")
        network_service.pon_ports.create(
            db_session, PonPortCreate(olt_id=olt.id, port_number=7)
        )
        network_service.pon_ports.create(
            db_session, PonPortCreate(olt_id=olt.id, port_number=8)
        )
        stats = network_service.pon_ports.utilization(db_session, str(olt.id))
        assert stats["olt_id"] == str(olt.id)
        assert stats["total_ports"] >= 2
        assert stats["assigned_ports"] >= 0

    def test_pon_port_utilization_no_olt(self, db_session):
        """Test PON port utilization report without OLT filter."""
        stats = network_service.pon_ports.utilization(db_session, None)
        assert stats["olt_id"] is None
        assert "total_ports" in stats
        assert "assigned_ports" in stats


# =============================================================================
# ONT Unit & Assignment CRUD Tests
# =============================================================================


class TestOntUnitsCRUD:
    """Tests for ONT unit create, get, list, update, delete."""

    def test_create_ont_unit(self, db_session):
        """Test creating an ONT unit."""
        ont = network_service.ont_units.create(
            db_session,
            OntUnitCreate(serial_number="ONT-SUB-001", vendor="ZTE", model="F660"),
        )
        assert ont.serial_number == "ONT-SUB-001"
        assert ont.vendor == "ZTE"

    def test_get_ont_unit(self, db_session):
        """Test getting an ONT unit by ID."""
        ont = network_service.ont_units.create(
            db_session, OntUnitCreate(serial_number="ONT-GET-SUB")
        )
        fetched = network_service.ont_units.get(db_session, str(ont.id))
        assert fetched.id == ont.id

    def test_get_ont_unit_not_found(self, db_session):
        """Test 404 for non-existent ONT unit."""
        with pytest.raises(HTTPException) as exc_info:
            network_service.ont_units.get(db_session, str(uuid.uuid4()))
        assert exc_info.value.status_code == 404

    def test_update_ont_unit(self, db_session):
        """Test updating an ONT unit."""
        ont = network_service.ont_units.create(
            db_session, OntUnitCreate(serial_number="ONT-UPD-SUB")
        )
        updated = network_service.ont_units.update(
            db_session,
            str(ont.id),
            OntUnitUpdate(firmware_version="2.5.1", notes="upgraded"),
        )
        assert updated.firmware_version == "2.5.1"
        assert updated.notes == "upgraded"

    def test_delete_ont_unit_soft(self, db_session):
        """Test soft deleting an ONT unit."""
        ont = network_service.ont_units.create(
            db_session, OntUnitCreate(serial_number="ONT-DEL-SUB")
        )
        network_service.ont_units.delete(db_session, str(ont.id))
        db_session.refresh(ont)
        assert ont.is_active is False

    def test_list_ont_units(self, db_session):
        """Test listing ONT units."""
        network_service.ont_units.create(
            db_session, OntUnitCreate(serial_number="ONT-LS-1")
        )
        network_service.ont_units.create(
            db_session, OntUnitCreate(serial_number="ONT-LS-2")
        )
        onts = network_service.ont_units.list(
            db_session,
            is_active=None,
            order_by="created_at",
            order_dir="asc",
            limit=100,
            offset=0,
        )
        assert len(onts) >= 2


class TestOntAssignmentsCRUD:
    """Tests for ONT assignment create, get, list, update, delete."""

    def _make_ont_and_pon(self, db_session):
        """Helper to create ONT unit and PON port."""
        ont = network_service.ont_units.create(
            db_session,
            OntUnitCreate(serial_number=f"ONT-ASG-{uuid.uuid4().hex[:6]}"),
        )
        olt = network_service.olt_devices.create(
            db_session,
            OLTDeviceCreate(
                name=f"Asg OLT {uuid.uuid4().hex[:6]}",
                hostname=f"asg-olt-{uuid.uuid4().hex[:6]}.local",
            ),
        )
        pon = network_service.pon_ports.create(
            db_session, PonPortCreate(olt_id=olt.id, port_number=1)
        )
        return ont, pon

    def test_create_ont_assignment(self, db_session):
        """Test creating an ONT assignment."""
        ont, pon = self._make_ont_and_pon(db_session)
        asg = network_service.ont_assignments.create(
            db_session,
            OntAssignmentCreate(ont_unit_id=ont.id, pon_port_id=pon.id),
        )
        assert asg.ont_unit_id == ont.id
        assert asg.pon_port_id == pon.id
        assert asg.active is True

    def test_get_ont_assignment(self, db_session):
        """Test getting an ONT assignment by ID."""
        ont, pon = self._make_ont_and_pon(db_session)
        asg = network_service.ont_assignments.create(
            db_session,
            OntAssignmentCreate(ont_unit_id=ont.id, pon_port_id=pon.id),
        )
        fetched = network_service.ont_assignments.get(db_session, str(asg.id))
        assert fetched.id == asg.id

    def test_get_ont_assignment_not_found(self, db_session):
        """Test 404 for non-existent ONT assignment."""
        with pytest.raises(HTTPException) as exc_info:
            network_service.ont_assignments.get(db_session, str(uuid.uuid4()))
        assert exc_info.value.status_code == 404

    def test_update_ont_assignment(self, db_session):
        """Test updating an ONT assignment."""
        ont, pon = self._make_ont_and_pon(db_session)
        asg = network_service.ont_assignments.create(
            db_session,
            OntAssignmentCreate(ont_unit_id=ont.id, pon_port_id=pon.id),
        )
        updated = network_service.ont_assignments.update(
            db_session,
            str(asg.id),
            OntAssignmentUpdate(active=False, notes="decommissioned"),
        )
        assert updated.active is False
        assert updated.notes == "decommissioned"

    def test_delete_ont_assignment(self, db_session):
        """Test deleting an ONT assignment (hard delete)."""
        ont, pon = self._make_ont_and_pon(db_session)
        asg = network_service.ont_assignments.create(
            db_session,
            OntAssignmentCreate(ont_unit_id=ont.id, pon_port_id=pon.id),
        )
        asg_id = str(asg.id)
        network_service.ont_assignments.delete(db_session, asg_id)
        with pytest.raises(HTTPException) as exc_info:
            network_service.ont_assignments.get(db_session, asg_id)
        assert exc_info.value.status_code == 404

    def test_list_ont_assignments(self, db_session):
        """Test listing ONT assignments."""
        ont1, pon1 = self._make_ont_and_pon(db_session)
        ont2, pon2 = self._make_ont_and_pon(db_session)
        network_service.ont_assignments.create(
            db_session,
            OntAssignmentCreate(ont_unit_id=ont1.id, pon_port_id=pon1.id),
        )
        network_service.ont_assignments.create(
            db_session,
            OntAssignmentCreate(ont_unit_id=ont2.id, pon_port_id=pon2.id),
        )
        asgs = network_service.ont_assignments.list(
            db_session,
            ont_unit_id=None,
            pon_port_id=None,
            order_by="created_at",
            order_dir="asc",
            limit=100,
            offset=0,
        )
        assert len(asgs) >= 2


# =============================================================================
# OLT Shelf CRUD Tests
# =============================================================================


class TestOltShelvesCRUD:
    """Tests for OLT shelf create with parent validation."""

    def test_create_shelf_bad_olt(self, db_session):
        """Test 404 when creating shelf with non-existent OLT."""
        with pytest.raises(HTTPException) as exc_info:
            network_service.olt_shelves.create(
                db_session,
                OltShelfCreate(olt_id=uuid.uuid4(), shelf_number=1),
            )
        assert exc_info.value.status_code == 404

    def test_update_shelf_bad_olt(self, db_session):
        """Test 404 when updating shelf to reference non-existent OLT."""
        olt = network_service.olt_devices.create(
            db_session, OLTDeviceCreate(name="Shelf Bad OLT", hostname="sbolt.local")
        )
        shelf = network_service.olt_shelves.create(
            db_session, OltShelfCreate(olt_id=olt.id, shelf_number=1)
        )
        with pytest.raises(HTTPException) as exc_info:
            network_service.olt_shelves.update(
                db_session, str(shelf.id), OltShelfUpdate(olt_id=uuid.uuid4())
            )
        assert exc_info.value.status_code == 404


# =============================================================================
# OLT Card CRUD Tests
# =============================================================================


class TestOltCardsCRUD:
    """Tests for OLT card create with parent validation."""

    def test_create_card_bad_shelf(self, db_session):
        """Test 404 when creating card with non-existent shelf."""
        with pytest.raises(HTTPException) as exc_info:
            network_service.olt_cards.create(
                db_session,
                OltCardCreate(shelf_id=uuid.uuid4(), slot_number=1),
            )
        assert exc_info.value.status_code == 404

    def test_update_card_bad_shelf(self, db_session):
        """Test 404 when updating card to reference non-existent shelf."""
        olt = network_service.olt_devices.create(
            db_session, OLTDeviceCreate(name="Card Bad OLT", hostname="cbolt.local")
        )
        shelf = network_service.olt_shelves.create(
            db_session, OltShelfCreate(olt_id=olt.id, shelf_number=1)
        )
        card = network_service.olt_cards.create(
            db_session, OltCardCreate(shelf_id=shelf.id, slot_number=1)
        )
        with pytest.raises(HTTPException) as exc_info:
            network_service.olt_cards.update(
                db_session, str(card.id), OltCardUpdate(shelf_id=uuid.uuid4())
            )
        assert exc_info.value.status_code == 404


# =============================================================================
# OLT Card Port CRUD Tests
# =============================================================================


class TestOltCardPortsCRUD:
    """Tests for OLT card port create with parent validation."""

    def test_create_card_port_bad_card(self, db_session):
        """Test 404 when creating card port with non-existent card."""
        with pytest.raises(HTTPException) as exc_info:
            network_service.olt_card_ports.create(
                db_session,
                OltCardPortCreate(card_id=uuid.uuid4(), port_number=1),
            )
        assert exc_info.value.status_code == 404

    def test_update_card_port_bad_card(self, db_session):
        """Test 404 when updating card port to reference non-existent card."""
        olt = network_service.olt_devices.create(
            db_session, OLTDeviceCreate(name="CP Bad OLT", hostname="cpbolt.local")
        )
        shelf = network_service.olt_shelves.create(
            db_session, OltShelfCreate(olt_id=olt.id, shelf_number=1)
        )
        card = network_service.olt_cards.create(
            db_session, OltCardCreate(shelf_id=shelf.id, slot_number=1)
        )
        port = network_service.olt_card_ports.create(
            db_session, OltCardPortCreate(card_id=card.id, port_number=1)
        )
        from app.schemas.network import OltCardPortUpdate
        with pytest.raises(HTTPException) as exc_info:
            network_service.olt_card_ports.update(
                db_session,
                str(port.id),
                OltCardPortUpdate(card_id=uuid.uuid4()),
            )
        assert exc_info.value.status_code == 404


# =============================================================================
# OLT Power Unit CRUD Tests
# =============================================================================


class TestOltPowerUnitsCRUD:
    """Tests for OLT power unit create, get, list, update, delete."""

    def test_create_power_unit(self, db_session):
        """Test creating an OLT power unit."""
        olt = network_service.olt_devices.create(
            db_session, OLTDeviceCreate(name="PU OLT", hostname="pu-olt.local")
        )
        pu = network_service.olt_power_units.create(
            db_session,
            OltPowerUnitCreate(olt_id=olt.id, slot="PSU-A", status="active"),
        )
        assert pu.olt_id == olt.id
        assert pu.slot == "PSU-A"

    def test_get_power_unit(self, db_session):
        """Test getting a power unit by ID."""
        olt = network_service.olt_devices.create(
            db_session, OLTDeviceCreate(name="PU Get OLT", hostname="pu-get.local")
        )
        pu = network_service.olt_power_units.create(
            db_session, OltPowerUnitCreate(olt_id=olt.id, slot="PSU-B")
        )
        fetched = network_service.olt_power_units.get(db_session, str(pu.id))
        assert fetched.id == pu.id

    def test_get_power_unit_not_found(self, db_session):
        """Test 404 for non-existent power unit."""
        with pytest.raises(HTTPException) as exc_info:
            network_service.olt_power_units.get(db_session, str(uuid.uuid4()))
        assert exc_info.value.status_code == 404

    def test_update_power_unit(self, db_session):
        """Test updating a power unit."""
        olt = network_service.olt_devices.create(
            db_session, OLTDeviceCreate(name="PU Upd OLT", hostname="pu-upd.local")
        )
        pu = network_service.olt_power_units.create(
            db_session, OltPowerUnitCreate(olt_id=olt.id, slot="PSU-C")
        )
        updated = network_service.olt_power_units.update(
            db_session, str(pu.id), OltPowerUnitUpdate(status="inactive", notes="replaced")
        )
        assert updated.status.value == "inactive"
        assert updated.notes == "replaced"

    def test_delete_power_unit_soft(self, db_session):
        """Test soft deleting a power unit."""
        olt = network_service.olt_devices.create(
            db_session, OLTDeviceCreate(name="PU Del OLT", hostname="pu-del.local")
        )
        pu = network_service.olt_power_units.create(
            db_session, OltPowerUnitCreate(olt_id=olt.id, slot="PSU-D")
        )
        network_service.olt_power_units.delete(db_session, str(pu.id))
        db_session.refresh(pu)
        assert pu.is_active is False

    def test_list_power_units(self, db_session):
        """Test listing power units filtered by OLT."""
        olt = network_service.olt_devices.create(
            db_session, OLTDeviceCreate(name="PU List OLT", hostname="pu-list.local")
        )
        network_service.olt_power_units.create(
            db_session, OltPowerUnitCreate(olt_id=olt.id, slot="PSU-1")
        )
        network_service.olt_power_units.create(
            db_session, OltPowerUnitCreate(olt_id=olt.id, slot="PSU-2")
        )
        pus = network_service.olt_power_units.list(
            db_session,
            olt_id=str(olt.id),
            is_active=None,
            order_by="created_at",
            order_dir="asc",
            limit=100,
            offset=0,
        )
        assert len(pus) >= 2


# =============================================================================
# OLT SFP Module CRUD Tests
# =============================================================================


class TestOltSfpModulesCRUD:
    """Tests for OLT SFP module create, get, list, update, delete."""

    def _make_card_port(self, db_session):
        """Helper to create OLT -> shelf -> card -> card_port."""
        olt = network_service.olt_devices.create(
            db_session,
            OLTDeviceCreate(
                name=f"SFP OLT {uuid.uuid4().hex[:6]}",
                hostname=f"sfp-olt-{uuid.uuid4().hex[:6]}.local",
            ),
        )
        shelf = network_service.olt_shelves.create(
            db_session, OltShelfCreate(olt_id=olt.id, shelf_number=1)
        )
        card = network_service.olt_cards.create(
            db_session, OltCardCreate(shelf_id=shelf.id, slot_number=1)
        )
        card_port = network_service.olt_card_ports.create(
            db_session, OltCardPortCreate(card_id=card.id, port_number=1)
        )
        return card_port

    def test_create_sfp_module(self, db_session):
        """Test creating an SFP module."""
        card_port = self._make_card_port(db_session)
        sfp = network_service.olt_sfp_modules.create(
            db_session,
            OltSfpModuleCreate(
                olt_card_port_id=card_port.id,
                vendor="Finisar",
                serial_number="SFP-001",
                wavelength_nm=1310,
            ),
        )
        assert sfp.olt_card_port_id == card_port.id
        assert sfp.vendor == "Finisar"
        assert sfp.wavelength_nm == 1310

    def test_get_sfp_module(self, db_session):
        """Test getting an SFP module by ID."""
        card_port = self._make_card_port(db_session)
        sfp = network_service.olt_sfp_modules.create(
            db_session,
            OltSfpModuleCreate(olt_card_port_id=card_port.id, serial_number="SFP-GET"),
        )
        fetched = network_service.olt_sfp_modules.get(db_session, str(sfp.id))
        assert fetched.id == sfp.id

    def test_get_sfp_module_not_found(self, db_session):
        """Test 404 for non-existent SFP module."""
        with pytest.raises(HTTPException) as exc_info:
            network_service.olt_sfp_modules.get(db_session, str(uuid.uuid4()))
        assert exc_info.value.status_code == 404

    def test_update_sfp_module(self, db_session):
        """Test updating an SFP module."""
        card_port = self._make_card_port(db_session)
        sfp = network_service.olt_sfp_modules.create(
            db_session,
            OltSfpModuleCreate(olt_card_port_id=card_port.id, serial_number="SFP-UPD"),
        )
        updated = network_service.olt_sfp_modules.update(
            db_session,
            str(sfp.id),
            OltSfpModuleUpdate(rx_power_dbm=-12.5, tx_power_dbm=2.3),
        )
        assert updated.rx_power_dbm == -12.5
        assert updated.tx_power_dbm == 2.3

    def test_delete_sfp_module_soft(self, db_session):
        """Test soft deleting an SFP module."""
        card_port = self._make_card_port(db_session)
        sfp = network_service.olt_sfp_modules.create(
            db_session,
            OltSfpModuleCreate(olt_card_port_id=card_port.id, serial_number="SFP-DEL"),
        )
        network_service.olt_sfp_modules.delete(db_session, str(sfp.id))
        db_session.refresh(sfp)
        assert sfp.is_active is False

    def test_list_sfp_modules(self, db_session):
        """Test listing SFP modules."""
        cp = self._make_card_port(db_session)
        network_service.olt_sfp_modules.create(
            db_session,
            OltSfpModuleCreate(olt_card_port_id=cp.id, serial_number="SFP-L1"),
        )
        network_service.olt_sfp_modules.create(
            db_session,
            OltSfpModuleCreate(olt_card_port_id=cp.id, serial_number="SFP-L2"),
        )
        sfps = network_service.olt_sfp_modules.list(
            db_session,
            olt_card_port_id=str(cp.id),
            is_active=None,
            order_by="created_at",
            order_dir="asc",
            limit=100,
            offset=0,
        )
        assert len(sfps) >= 2


# =============================================================================
# NAS Device CRUD Tests
# =============================================================================


class TestNasDevicesCRUD:
    """Tests for NAS device create, get, list, update, delete."""

    def test_create_nas_device(self, db_session):
        """Test creating a NAS device."""
        device = nas_service.nas_devices.create(
            db_session,
            NasDeviceCreate(
                name="MikroTik-001",
                ip_address="10.0.0.1",
                management_ip="10.0.0.1",
                vendor=NasVendor.mikrotik,
            ),
        )
        assert device.name == "MikroTik-001"
        assert device.ip_address == "10.0.0.1"
        assert device.vendor == NasVendor.mikrotik
        assert device.is_active is True
        assert device.status == NasDeviceStatus.active

    def test_create_nas_device_with_pop_site(self, db_session, pop_site):
        """Test creating a NAS device linked to a POP site."""
        device = nas_service.nas_devices.create(
            db_session,
            NasDeviceCreate(
                name="POP-NAS-001",
                ip_address="10.0.1.1",
                pop_site_id=pop_site.id,
            ),
        )
        assert device.pop_site_id == pop_site.id

    def test_create_nas_device_bad_pop_site(self, db_session):
        """Test 404 when creating NAS device with non-existent POP site."""
        with pytest.raises(HTTPException) as exc_info:
            nas_service.nas_devices.create(
                db_session,
                NasDeviceCreate(
                    name="Bad-POP-NAS",
                    ip_address="10.0.2.1",
                    pop_site_id=uuid.uuid4(),
                ),
            )
        assert exc_info.value.status_code == 404

    def test_get_nas_device(self, db_session):
        """Test getting a NAS device by ID."""
        device = nas_service.nas_devices.create(
            db_session,
            NasDeviceCreate(name="Get-NAS", ip_address="10.1.0.1"),
        )
        fetched = nas_service.nas_devices.get(db_session, str(device.id))
        assert fetched.id == device.id
        assert fetched.name == "Get-NAS"

    def test_get_nas_device_not_found(self, db_session):
        """Test 404 for non-existent NAS device."""
        with pytest.raises(HTTPException) as exc_info:
            nas_service.nas_devices.get(db_session, str(uuid.uuid4()))
        assert exc_info.value.status_code == 404

    def test_get_nas_device_by_code(self, db_session):
        """Test getting a NAS device by code."""
        device = nas_service.nas_devices.create(
            db_session,
            NasDeviceCreate(name="Code-NAS", ip_address="10.1.1.1", code="NAS-CODE-01"),
        )
        fetched = nas_service.nas_devices.get_by_code(db_session, "NAS-CODE-01")
        assert fetched is not None
        assert fetched.id == device.id

    def test_get_nas_device_by_code_not_found(self, db_session):
        """Test None for non-existent NAS device code."""
        result = nas_service.nas_devices.get_by_code(db_session, "NONEXISTENT-CODE")
        assert result is None

    def test_list_nas_devices(self, db_session):
        """Test listing NAS devices."""
        nas_service.nas_devices.create(
            db_session,
            NasDeviceCreate(name="List-NAS-1", ip_address="10.2.0.1"),
        )
        nas_service.nas_devices.create(
            db_session,
            NasDeviceCreate(name="List-NAS-2", ip_address="10.2.0.2"),
        )
        devices = nas_service.nas_devices.list(db_session, limit=100)
        assert len(devices) >= 2

    def test_list_nas_devices_search(self, db_session):
        """Test listing NAS devices with search filter."""
        unique_name = f"SearchNAS-{uuid.uuid4().hex[:8]}"
        nas_service.nas_devices.create(
            db_session,
            NasDeviceCreate(name=unique_name, ip_address="10.3.0.1"),
        )
        devices = nas_service.nas_devices.list(db_session, search=unique_name)
        assert len(devices) >= 1
        assert devices[0].name == unique_name

    def test_update_nas_device(self, db_session):
        """Test updating a NAS device."""
        device = nas_service.nas_devices.create(
            db_session,
            NasDeviceCreate(name="Upd-NAS", ip_address="10.4.0.1"),
        )
        updated = nas_service.nas_devices.update(
            db_session,
            str(device.id),
            NasDeviceUpdate(name="Updated-NAS", description="changed"),
        )
        assert updated.name == "Updated-NAS"
        assert updated.description == "changed"

    def test_update_nas_device_not_found(self, db_session):
        """Test 404 for updating non-existent NAS device."""
        with pytest.raises(HTTPException) as exc_info:
            nas_service.nas_devices.update(
                db_session, str(uuid.uuid4()), NasDeviceUpdate(name="nope")
            )
        assert exc_info.value.status_code == 404

    def test_update_nas_device_bad_pop_site(self, db_session):
        """Test 404 when updating NAS device with non-existent POP site."""
        device = nas_service.nas_devices.create(
            db_session,
            NasDeviceCreate(name="BadPOP-UPD-NAS", ip_address="10.4.1.1"),
        )
        with pytest.raises(HTTPException) as exc_info:
            nas_service.nas_devices.update(
                db_session,
                str(device.id),
                NasDeviceUpdate(pop_site_id=uuid.uuid4()),
            )
        assert exc_info.value.status_code == 404

    def test_delete_nas_device(self, db_session):
        """Test deleting a NAS device (soft delete - decommissions)."""
        device = nas_service.nas_devices.create(
            db_session,
            NasDeviceCreate(name="Del-NAS", ip_address="10.5.0.1"),
        )
        nas_service.nas_devices.delete(db_session, str(device.id))
        db_session.refresh(device)
        assert device.is_active is False
        assert device.status == NasDeviceStatus.decommissioned

    def test_update_last_seen(self, db_session):
        """Test updating last_seen_at timestamp."""
        device = nas_service.nas_devices.create(
            db_session,
            NasDeviceCreate(name="Seen-NAS", ip_address="10.6.0.1"),
        )
        assert device.last_seen_at is None
        updated = nas_service.nas_devices.update_last_seen(db_session, str(device.id))
        assert updated.last_seen_at is not None

    def test_count_nas_devices(self, db_session):
        """Test counting NAS devices."""
        nas_service.nas_devices.create(
            db_session,
            NasDeviceCreate(name="Count-NAS-1", ip_address="10.7.0.1"),
        )
        count = nas_service.nas_devices.count(db_session)
        assert count >= 1

    def test_get_stats(self, db_session):
        """Test getting NAS device statistics."""
        nas_service.nas_devices.create(
            db_session,
            NasDeviceCreate(
                name="Stats-NAS",
                ip_address="10.8.0.1",
                vendor=NasVendor.mikrotik,
            ),
        )
        stats = nas_service.nas_devices.get_stats(db_session)
        assert "by_vendor" in stats
        assert "by_status" in stats


# =============================================================================
# Utility Function Tests
# =============================================================================


class TestRedactSensitive:
    """Tests for the _redact_sensitive utility function."""

    def test_redact_password(self):
        """Test that password fields are redacted."""
        data = {"username": "admin", "password": "secret123"}
        redacted = _redact_sensitive(data)
        assert redacted["username"] == "admin"
        assert redacted["password"] == "***redacted***"

    def test_redact_multiple_keys(self):
        """Test that all sensitive keys are redacted."""
        data = {
            "name": "device",
            "secret": "mysecret",
            "token": "mytoken",
            "api_key": "key123",
            "ssh_key": "private_key_data",
            "shared_secret": "radius_secret",
        }
        redacted = _redact_sensitive(data)
        assert redacted["name"] == "device"
        assert redacted["secret"] == "***redacted***"
        assert redacted["token"] == "***redacted***"
        assert redacted["api_key"] == "***redacted***"
        assert redacted["ssh_key"] == "***redacted***"
        assert redacted["shared_secret"] == "***redacted***"

    def test_redact_nested_dict(self):
        """Test that nested dicts are also redacted."""
        data = {"config": {"password": "nested_pw", "host": "example.com"}}
        redacted = _redact_sensitive(data)
        assert redacted["config"]["password"] == "***redacted***"
        assert redacted["config"]["host"] == "example.com"

    def test_redact_empty_dict(self):
        """Test that empty dict returns empty dict."""
        assert _redact_sensitive({}) == {}

    def test_redact_none_safe(self):
        """Test that None is handled safely."""
        # The function uses (data or {}) so None should work
        assert _redact_sensitive(None) == {}

    def test_redact_list_values(self):
        """Test that list values with nested dicts are redacted."""
        data = {
            "items": [
                {"password": "pw1", "name": "a"},
                {"password": "pw2", "name": "b"},
            ]
        }
        redacted = _redact_sensitive(data)
        assert redacted["items"][0]["password"] == "***redacted***"
        assert redacted["items"][0]["name"] == "a"
        assert redacted["items"][1]["password"] == "***redacted***"

    def test_redact_case_insensitive(self):
        """Test that redaction is case-insensitive for key matching."""
        data = {"Password": "secret", "TOKEN": "tok"}
        redacted = _redact_sensitive(data)
        assert redacted["Password"] == "***redacted***"
        assert redacted["TOKEN"] == "***redacted***"

    def test_non_sensitive_keys_preserved(self):
        """Test that non-sensitive keys are not redacted."""
        data = {"name": "device", "ip_address": "10.0.0.1", "status": "active"}
        redacted = _redact_sensitive(data)
        assert redacted == data
