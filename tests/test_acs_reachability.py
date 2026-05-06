from __future__ import annotations

from app.models.catalog import RegionZone
from app.models.network import (
    IpBlock,
    IpPool,
    IPv4Address,
    IPVersion,
    OLTDevice,
    Vlan,
    VlanPurpose,
)
from app.models.tr069 import Tr069AcsServer
from app.services.network import olt_web_forms
from app.services.network.acs_reachability import (
    validate_olt_acs_management_reachability,
)
from app.services.network.olt_config_pack import validate_config_pack_comprehensive


def _acs_ready_olt(db_session, *, pool_cidr: str = "172.16.201.0/24"):
    region = RegionZone(name=f"ACS Reachability {pool_cidr}", code=pool_cidr[-6:])
    acs = Tr069AcsServer(
        name=f"ACS {pool_cidr}",
        base_url="http://genieacs.example:7557",
        is_active=True,
    )
    olt = OLTDevice(name=f"OLT {pool_cidr}", tr069_acs_server_id=acs.id)
    db_session.add_all([region, acs, olt])
    db_session.flush()
    vlan = Vlan(
        region_id=region.id,
        olt_device_id=olt.id,
        tag=201,
        name="Management",
        purpose=VlanPurpose.management,
        is_active=True,
    )
    pool = IpPool(
        name=f"Pool {pool_cidr}",
        ip_version=IPVersion.ipv4,
        cidr=pool_cidr,
        gateway=pool_cidr.rsplit(".", 1)[0] + ".1",
        olt_device_id=olt.id,
        vlan=vlan,
        is_active=True,
    )
    db_session.add_all([vlan, pool])
    db_session.flush()
    return olt, acs, vlan, pool


def test_acs_reachability_accepts_routable_management_pool(db_session):
    olt, acs, vlan, pool = _acs_ready_olt(db_session)

    error = validate_olt_acs_management_reachability(
        db_session,
        {
            "tr069_acs_server_id": acs.id,
            "default_tr069_olt_profile_id": 2,
            "management_vlan_id": vlan.id,
            "mgmt_ip_pool_id": pool.id,
        },
        current_olt=olt,
    )

    assert error is None
    block = (
        db_session.query(IpBlock)
        .filter(IpBlock.pool_id == pool.id, IpBlock.is_active.is_(True))
        .one()
    )
    assert block.cidr == pool.cidr
    assert pool.next_available_ip is not None
    assert pool.available_count and pool.available_count > 0


def test_acs_reachability_rejects_exhausted_management_pool(db_session):
    olt, acs, vlan, pool = _acs_ready_olt(db_session, pool_cidr="172.16.202.0/30")
    db_session.add(
        IpBlock(pool_id=pool.id, cidr="172.16.202.0/30", is_active=True)
    )
    db_session.add(
        IPv4Address(
            address="172.16.202.2",
            pool_id=pool.id,
            is_reserved=True,
        )
    )
    db_session.flush()

    error = validate_olt_acs_management_reachability(
        db_session,
        {
            "tr069_acs_server_id": acs.id,
            "default_tr069_olt_profile_id": 2,
            "management_vlan_id": vlan.id,
            "mgmt_ip_pool_id": pool.id,
        },
        current_olt=olt,
    )

    assert error == "Management IP pool must have at least one available address."


def test_acs_reachability_rejects_unroutable_management_pool(db_session):
    olt, acs, vlan, pool = _acs_ready_olt(db_session, pool_cidr="10.99.201.0/24")

    error = validate_olt_acs_management_reachability(
        db_session,
        {
            "tr069_acs_server_id": acs.id,
            "default_tr069_olt_profile_id": 2,
            "management_vlan_id": vlan.id,
            "mgmt_ip_pool_id": pool.id,
        },
        current_olt=olt,
    )

    assert error is not None
    assert "routable from GenieACS" in error


def test_acs_reachability_rejects_pool_on_different_vlan(db_session):
    olt, acs, vlan, pool = _acs_ready_olt(db_session)
    other_vlan = Vlan(
        region_id=vlan.region_id,
        olt_device_id=olt.id,
        tag=202,
        name="Other",
        purpose=VlanPurpose.internet,
        is_active=True,
    )
    db_session.add(other_vlan)
    db_session.flush()

    error = validate_olt_acs_management_reachability(
        db_session,
        {
            "tr069_acs_server_id": acs.id,
            "default_tr069_olt_profile_id": 2,
            "management_vlan_id": other_vlan.id,
            "mgmt_ip_pool_id": pool.id,
        },
        current_olt=olt,
    )

    assert error == "Management IP pool must be associated with the selected management VLAN."


def test_active_olt_form_requires_complete_authorization_pack(db_session):
    error = olt_web_forms.validate_values(
        db_session,
        {
            "name": "Incomplete Active OLT",
            "is_active": True,
            "netconf_enabled": False,
        },
    )

    assert error is not None
    assert "complete authorization and ACS config pack" in error
    assert "management IP pool" in error


def test_olt_create_payload_persists_config_pack_and_pool(db_session):
    olt, acs, management_vlan, pool = _acs_ready_olt(db_session)
    internet_vlan = Vlan(
        region_id=management_vlan.region_id,
        olt_device_id=olt.id,
        tag=200,
        name="Internet",
        purpose=VlanPurpose.internet,
        is_active=True,
    )
    db_session.add(internet_vlan)
    db_session.flush()

    values = {
        "name": "Packed OLT",
        "is_active": True,
        "tr069_acs_server_id": acs.id,
        "internet_vlan_id": internet_vlan.id,
        "management_vlan_id": management_vlan.id,
        "default_tr069_olt_profile_id": 30,
        "mgmt_ip_pool_id": pool.id,
    }
    payload = olt_web_forms.create_payload(values)

    assert "line_profile_id" not in payload.config_pack
    assert "service_profile_id" not in payload.config_pack
    assert payload.config_pack["internet_vlan_id"] == str(internet_vlan.id)
    assert payload.config_pack["management_vlan_id"] == str(management_vlan.id)
    assert payload.config_pack["tr069_olt_profile_id"] == 30
    assert payload.mgmt_ip_pool_id == pool.id


def test_config_pack_comprehensive_requires_management_ip_pool(db_session):
    olt, acs, management_vlan, pool = _acs_ready_olt(db_session)
    internet_vlan = Vlan(
        region_id=management_vlan.region_id,
        olt_device_id=olt.id,
        tag=200,
        name="Internet",
        purpose=VlanPurpose.internet,
        is_active=True,
    )
    db_session.add(internet_vlan)
    db_session.flush()
    olt.config_pack = {
        "line_profile_id": 10,
        "service_profile_id": 20,
        "internet_vlan_id": str(internet_vlan.id),
        "management_vlan_id": str(management_vlan.id),
        "tr069_olt_profile_id": 30,
    }
    olt.tr069_acs_server_id = acs.id
    olt.mgmt_ip_pool_id = None
    db_session.commit()

    validation = validate_config_pack_comprehensive(db_session, olt.id)

    assert validation.is_valid is False
    assert any("Missing management IP pool" in error for error in validation.errors)


def test_config_pack_comprehensive_ignores_legacy_gem_index(db_session):
    olt, acs, management_vlan, pool = _acs_ready_olt(db_session)
    internet_vlan = Vlan(
        region_id=management_vlan.region_id,
        olt_device_id=olt.id,
        tag=200,
        name="Internet",
        purpose=VlanPurpose.internet,
        is_active=True,
    )
    db_session.add(internet_vlan)
    db_session.flush()
    olt.config_pack = {
        "line_profile_id": 10,
        "service_profile_id": 20,
        "internet_vlan_id": str(internet_vlan.id),
        "management_vlan_id": str(management_vlan.id),
        "tr069_olt_profile_id": 30,
        "internet_gem_index": 9,
    }
    olt.tr069_acs_server_id = acs.id
    olt.mgmt_ip_pool_id = pool.id
    db_session.commit()

    validation = validate_config_pack_comprehensive(db_session, olt.id)

    assert validation.is_valid is True
    assert not any("GEM index" in error for error in validation.errors)
