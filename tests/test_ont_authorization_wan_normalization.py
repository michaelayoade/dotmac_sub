from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.models.network import (
    IpBlock,
    IpPool,
    IPv4Address,
    IPVersion,
    MgmtIpMode,
    OLTDevice,
    OntAssignment,
    OntUnit,
)
from app.models.tr069 import Tr069AcsServer, Tr069CpeDevice
from app.services.credential_crypto import encrypt_credential
from app.services.network import acs_foundation, ont_authorization
from app.services.network.ont_desired_config import desired_config


def test_authorization_follow_up_applies_acs_foundation_inline(
    db_session, monkeypatch
):
    """Authorized ONTs apply the OLT-side ACS foundation synchronously."""
    foundation_calls: list[dict] = []
    olt = OLTDevice(name="OLT-Foundation-Inline", is_active=True)
    ont = OntUnit(serial_number="HWTCBOOTSTRAP", olt_device=olt, is_active=True)
    db_session.add_all([olt, ont])
    db_session.flush()

    monkeypatch.setattr(
        ont_authorization,
        "ensure_assignment_and_pon_port_for_authorized_ont",
        lambda *args, **kwargs: (True, "Linked ONT to PON port."),
    )
    monkeypatch.setattr(
        ont_authorization,
        "allocate_management_ip_for_ont",
        lambda *args, **kwargs: (True, "Allocated management IP.", "10.10.10.2"),
    )
    monkeypatch.setattr(
        "app.services.network.effective_ont_config.resolve_effective_ont_config",
        lambda db, ont, **kwargs: {
            "values": {
                "tr069_acs_server_id": "acs-1",
                "tr069_olt_profile_id": 7,
                "mgmt_vlan": 201,
                "mgmt_ip_address": "10.10.10.2",
            }
        },
    )

    monkeypatch.setattr(
        acs_foundation,
        "apply_acs_foundation",
        lambda db, **kwargs: foundation_calls.append(kwargs)
        or {
            "success": True,
            "message": "ACS foundation applied.",
            "steps": [],
        },
    )

    success, message, steps = ont_authorization.apply_authorization_foundation(
        db_session,
        ont_unit_id=str(ont.id),
        olt_id=str(olt.id),
        fsp="0/1/1",
        serial_number="HWTCBOOTSTRAP",
        ont_id_on_olt=7,
    )

    assert success is True
    assert message == "Authorization foundation completed with ACS connected."
    assert steps[-1]["name"] == "Apply ACS foundation"
    assert steps[-1]["success"] is True
    assert steps[-1]["message"] == "ACS foundation applied."
    assert foundation_calls == [
        {
            "ont_unit_id": str(ont.id),
            "olt_id": str(olt.id),
            "fsp": "0/1/1",
            "ont_id_on_olt": 7,
            "wait_for_acs_bootstrap": True,
        }
    ]


def test_authorization_foundation_fails_when_acs_foundation_fails(
    db_session, monkeypatch
):
    """A failed ACS foundation write makes authorization foundation fail."""
    olt = OLTDevice(name="OLT-Foundation-Fail", is_active=True)
    ont = OntUnit(serial_number="HWTCBOOTSTRAP", olt_device=olt, is_active=True)
    db_session.add_all([olt, ont])
    db_session.flush()

    monkeypatch.setattr(
        ont_authorization,
        "ensure_assignment_and_pon_port_for_authorized_ont",
        lambda *args, **kwargs: (True, "Linked ONT to PON port."),
    )
    monkeypatch.setattr(
        ont_authorization,
        "allocate_management_ip_for_ont",
        lambda *args, **kwargs: (True, "Allocated management IP.", "10.10.10.2"),
    )
    monkeypatch.setattr(
        "app.services.network.effective_ont_config.resolve_effective_ont_config",
        lambda db, ont, **kwargs: {
            "values": {
                "tr069_acs_server_id": "acs-1",
                "tr069_olt_profile_id": 7,
                "mgmt_vlan": 201,
                "mgmt_ip_address": "10.10.10.2",
            }
        },
    )
    monkeypatch.setattr(
        acs_foundation,
        "apply_acs_foundation",
        lambda db, **kwargs: {
            "success": False,
            "message": "OLT rejected TR-069 profile.",
            "steps": [],
        },
    )

    success, message, steps = ont_authorization.apply_authorization_foundation(
        db_session,
        ont_unit_id=str(ont.id),
        olt_id=str(olt.id),
        fsp="0/1/1",
        serial_number="HWTCBOOTSTRAP",
        ont_id_on_olt=7,
    )

    assert success is False
    assert message == "Authorization foundation failed: ACS foundation was not applied."
    assert steps[-1]["name"] == "Apply ACS foundation"
    assert steps[-1]["success"] is False
    assert "OLT rejected TR-069 profile" in steps[-1]["message"]


def test_authorization_foundation_fails_when_management_ip_not_allocated(
    db_session, monkeypatch
):
    """ACS foundation is not applied if management IP allocation fails."""
    monkeypatch.setattr(
        ont_authorization,
        "ensure_assignment_and_pon_port_for_authorized_ont",
        lambda *args, **kwargs: (True, "Linked ONT to PON port."),
    )
    monkeypatch.setattr(
        ont_authorization,
        "allocate_management_ip_for_ont",
        lambda *args, **kwargs: (False, "Management IP pool exhausted.", None),
    )

    success, message, steps = ont_authorization.apply_authorization_foundation(
        db_session,
        ont_unit_id="ont-1",
        olt_id="olt-1",
        fsp="0/1/1",
        serial_number="HWTCBOOTSTRAP",
        ont_id_on_olt=7,
    )

    assert success is False
    assert message == "Management IP pool exhausted."
    assert steps[-1]["name"] == "Allocate management IP"
    assert steps[-1]["success"] is False


def test_authorization_foundation_requires_allocated_static_management_ip(
    db_session, monkeypatch
):
    """Authorization cannot complete if allocation succeeds without an IP address."""
    monkeypatch.setattr(
        ont_authorization,
        "ensure_assignment_and_pon_port_for_authorized_ont",
        lambda *args, **kwargs: (True, "Linked ONT to PON port."),
    )
    monkeypatch.setattr(
        ont_authorization,
        "allocate_management_ip_for_ont",
        lambda *args, **kwargs: (True, "No management IP allocated.", None),
    )
    olt = OLTDevice(name="OLT-No-Allocated-IP", is_active=True)
    ont = OntUnit(serial_number="HWTCNOALLOCIP", olt_device=olt, is_active=True)
    db_session.add_all([olt, ont])
    db_session.flush()

    success, message, steps = ont_authorization.apply_authorization_foundation(
        db_session,
        ont_unit_id=str(ont.id),
        olt_id=str(olt.id),
        fsp="0/1/1",
        serial_number="HWTCNOALLOCIP",
        ont_id_on_olt=7,
    )

    assert success is False
    assert "static management IP is required" in message
    assert steps[-1]["name"] == "Verify ACS prerequisites"
    assert steps[-1]["success"] is False


def test_management_ip_allocation_rejects_released_address_from_different_pool(
    db_session,
):
    """Allocator must not claim an IPv4 row that belongs to another pool."""
    olt = OLTDevice(name="OLT-Mgmt-Pool-Guard", is_active=True)
    ont = OntUnit(serial_number="HWTCPOOLGUARD", olt_device=olt, is_active=True)
    other_pool = IpPool(
        name="Other Mgmt Pool",
        ip_version=IPVersion.ipv4,
        cidr="172.16.201.0/24",
        gateway="172.16.201.1",
        is_active=True,
    )
    active_pool = IpPool(
        name="Active Mgmt Pool",
        ip_version=IPVersion.ipv4,
        cidr="172.16.201.0/24",
        gateway="172.16.201.1",
        is_active=True,
        olt_device=olt,
    )
    db_session.add_all([olt, ont, other_pool, active_pool])
    db_session.flush()
    db_session.add(
        IpBlock(pool_id=active_pool.id, cidr="172.16.201.0/30", is_active=True)
    )
    db_session.add(
        IPv4Address(
            address="172.16.201.2",
            pool_id=other_pool.id,
            is_reserved=False,
        )
    )
    olt.mgmt_ip_pool_id = active_pool.id
    db_session.commit()

    ok, message, allocated_ip = ont_authorization.allocate_management_ip_for_ont(
        db_session,
        ont_unit_id=str(ont.id),
        olt_id=str(olt.id),
    )

    assert ok is False
    assert allocated_ip is None
    assert "different pool" in message


def test_management_ip_allocation_fails_without_olt_pool(db_session):
    """Authorization cannot guarantee ACS reachability without an OLT mgmt pool."""
    olt = OLTDevice(name="OLT-No-Mgmt-Pool", is_active=True)
    ont = OntUnit(serial_number="HWTCNOPool01", olt_device=olt, is_active=True)
    db_session.add_all([olt, ont])
    db_session.commit()

    ok, message, allocated_ip = ont_authorization.allocate_management_ip_for_ont(
        db_session,
        ont_unit_id=str(ont.id),
        olt_id=str(olt.id),
    )

    assert ok is False
    assert allocated_ip is None
    assert "No management IP pool configured" in message


def test_management_ip_allocation_reuses_existing_ip_only_when_in_current_pool(
    db_session,
):
    """Existing management IPs are valid only when they belong to the OLT pool."""
    olt = OLTDevice(name="OLT-Existing-Mgmt-Pool", is_active=True)
    ont = OntUnit(serial_number="HWTCVALIDMGMT", olt_device=olt, is_active=True)
    pool = IpPool(
        name="Existing Mgmt Pool",
        ip_version=IPVersion.ipv4,
        cidr="172.16.202.0/24",
        gateway="172.16.202.1",
        is_active=True,
        olt_device=olt,
    )
    db_session.add_all([olt, ont, pool])
    db_session.flush()
    db_session.add(IpBlock(pool_id=pool.id, cidr="172.16.202.0/30", is_active=True))
    assignment = ont_authorization._get_or_create_active_assignment(db_session, ont)
    assignment.mgmt_ip_mode = MgmtIpMode.static_ip
    assignment.mgmt_ip_address = "172.16.202.2"
    olt.mgmt_ip_pool_id = pool.id
    db_session.commit()

    ok, message, allocated_ip = ont_authorization.allocate_management_ip_for_ont(
        db_session,
        ont_unit_id=str(ont.id),
        olt_id=str(olt.id),
    )

    assert ok is True
    assert allocated_ip == "172.16.202.2"
    assert "already has management IP" in message
    record = db_session.query(IPv4Address).filter_by(address="172.16.202.2").one()
    assert record.pool_id == pool.id
    assert record.ont_unit_id == ont.id
    assert record.allocation_type == "management"


def test_management_ip_allocation_uses_valid_cached_next_ip(
    db_session,
    monkeypatch,
):
    olt = OLTDevice(name="OLT-Cached-Mgmt-Pool", is_active=True)
    ont = OntUnit(serial_number="HWTCCACHEMGMT", olt_device=olt, is_active=True)
    pool = IpPool(
        name="Cached Mgmt Pool",
        ip_version=IPVersion.ipv4,
        cidr="172.16.203.0/24",
        gateway="172.16.203.1",
        is_active=True,
        olt_device=olt,
        next_available_ip="172.16.203.2",
        available_count=2,
    )
    db_session.add_all([olt, ont, pool])
    db_session.flush()
    db_session.add(IpBlock(pool_id=pool.id, cidr="172.16.203.0/29", is_active=True))
    olt.mgmt_ip_pool_id = pool.id
    db_session.commit()

    def fail_refresh(*args, **kwargs):
        raise AssertionError("refresh_pool_availability should not run")

    monkeypatch.setattr(ont_authorization, "refresh_pool_availability", fail_refresh)

    ok, message, allocated_ip = ont_authorization.allocate_management_ip_for_ont(
        db_session,
        ont_unit_id=str(ont.id),
        olt_id=str(olt.id),
    )

    assert ok is True
    assert allocated_ip == "172.16.203.2"
    assert "Allocated management IP" in message
    assignment = db_session.query(OntAssignment).filter_by(ont_unit_id=ont.id).one()
    assert assignment.mgmt_ip_mode == MgmtIpMode.static_ip
    assert assignment.mgmt_ip_address == "172.16.203.2"
    assert assignment.mgmt_subnet == "255.255.255.0"
    assert assignment.mgmt_gateway == "172.16.203.1"
    assert desired_config(ont)["management"] == {
        "ip_address": "172.16.203.2",
        "ip_mode": "static_ip",
        "subnet": "255.255.255.0",
        "gateway": "172.16.203.1",
    }
    db_session.refresh(pool)
    assert pool.next_available_ip == "172.16.203.3"
    # /29 block has 6 usable hosts (.1-.6), minus gateway (.1) and allocated (.2) = 4 remaining
    assert pool.available_count == 4


def test_management_ip_allocation_refreshes_stale_cached_next_ip(
    db_session,
):
    olt = OLTDevice(name="OLT-Stale-Cache-Mgmt-Pool", is_active=True)
    ont = OntUnit(serial_number="HWTCSTALECACHE", olt_device=olt, is_active=True)
    pool = IpPool(
        name="Stale Cache Mgmt Pool",
        ip_version=IPVersion.ipv4,
        cidr="172.16.204.0/24",
        gateway="172.16.204.1",
        is_active=True,
        olt_device=olt,
        next_available_ip="172.16.204.2",
        available_count=2,
    )
    db_session.add_all([olt, ont, pool])
    db_session.flush()
    db_session.add(IpBlock(pool_id=pool.id, cidr="172.16.204.0/29", is_active=True))
    db_session.add(
        IPv4Address(
            address="172.16.204.2",
            pool_id=pool.id,
            is_reserved=True,
            notes="held",
        )
    )
    olt.mgmt_ip_pool_id = pool.id
    db_session.commit()

    ok, _message, allocated_ip = ont_authorization.allocate_management_ip_for_ont(
        db_session,
        ont_unit_id=str(ont.id),
        olt_id=str(olt.id),
    )

    assert ok is True
    assert allocated_ip == "172.16.204.3"
    db_session.refresh(pool)
    assert pool.next_available_ip == "172.16.204.4"


def test_management_ip_allocation_replaces_stale_existing_ip_from_wrong_pool(
    db_session,
):
    """Stale assignment IPs do not bypass allocation from the current OLT pool."""
    olt = OLTDevice(name="OLT-Stale-Mgmt-Pool", is_active=True)
    ont = OntUnit(serial_number="HWTCSTALEMGMT", olt_device=olt, is_active=True)
    old_pool = IpPool(
        name="Old Mgmt Pool",
        ip_version=IPVersion.ipv4,
        cidr="172.16.201.0/24",
        gateway="172.16.201.1",
        is_active=True,
    )
    current_pool = IpPool(
        name="Current Mgmt Pool",
        ip_version=IPVersion.ipv4,
        cidr="172.16.202.0/24",
        gateway="172.16.202.1",
        is_active=True,
        olt_device=olt,
    )
    db_session.add_all([olt, ont, old_pool, current_pool])
    db_session.flush()
    db_session.add(
        IpBlock(pool_id=current_pool.id, cidr="172.16.202.0/30", is_active=True)
    )
    assignment = ont_authorization._get_or_create_active_assignment(db_session, ont)
    assignment.mgmt_ip_mode = MgmtIpMode.static_ip
    assignment.mgmt_ip_address = "172.16.201.2"
    stale_record = IPv4Address(
        address="172.16.201.2",
        pool_id=old_pool.id,
        is_reserved=True,
        notes=f"ont:{ont.id}",
        ont_unit_id=ont.id,
        allocation_type="management",
    )
    db_session.add(stale_record)
    olt.mgmt_ip_pool_id = current_pool.id
    db_session.commit()

    ok, message, allocated_ip = ont_authorization.allocate_management_ip_for_ont(
        db_session,
        ont_unit_id=str(ont.id),
        olt_id=str(olt.id),
    )

    assert ok is True
    assert allocated_ip == "172.16.202.2"
    assert "Allocated management IP" in message
    db_session.refresh(assignment)
    db_session.refresh(stale_record)
    assert assignment.mgmt_ip_address == "172.16.202.2"
    assert stale_record.is_reserved is False
    assert stale_record.ont_unit_id is None
    assert stale_record.allocation_type is None


def test_authorization_cleans_stale_registration_before_retry(
    db_session,
    monkeypatch,
):
    """Normal authorization handles serial-already-registered on a stale FSP."""
    olt = OLTDevice(name="OLT-Stale-Registration", is_active=True)
    db_session.add(olt)
    db_session.commit()

    calls: list[tuple[str, object]] = []
    existing = SimpleNamespace(fsp="0/1/5", onu_id=3)

    class FakeAdapter:
        def find_ont_by_serial(self, serial_number):
            calls.append(("find", serial_number))
            return SimpleNamespace(
                success=True,
                message="Found existing registration.",
                data={"registration": existing},
            )

        def deauthorize_ont(self, fsp, ont_id):
            calls.append(("deauthorize", fsp, ont_id))
            return SimpleNamespace(success=True, message="Deleted existing registration.")

        def authorize_ont(
            self,
            fsp,
            serial_number,
            *,
            line_profile_id=None,
            service_profile_id=None,
        ):
            calls.append(("authorize", fsp, serial_number))
            if len([call for call in calls if call[0] == "authorize"]) == 1:
                return SimpleNamespace(
                    success=False,
                    message="SN already exists",
                    ont_id=None,
                )
            return SimpleNamespace(
                success=True,
                message="Authorized on requested port.",
                ont_id=7,
            )

    monkeypatch.setattr(
        "app.services.network.olt_protocol_adapters.get_protocol_adapter",
        lambda _olt: FakeAdapter(),
    )
    monkeypatch.setattr(
        "app.services.network.olt_config_pack.validate_config_pack_comprehensive",
        lambda db, olt_id: SimpleNamespace(is_valid=True),
    )
    monkeypatch.setattr(
        "app.services.network.olt_config_pack.get_validation_summary",
        lambda validation: "Config pack is complete and ready for provisioning",
    )
    monkeypatch.setattr(
        "app.services.network.olt_profile_resolution.resolve_authorization_profiles_from_db",
        lambda db, olt: (
            True,
            "Using OLT authorization profiles.",
            SimpleNamespace(line_profile_id=10, service_profile_id=20),
        ),
    )
    monkeypatch.setattr(
        "app.services.network.olt_write_reconciliation.verify_ont_absent",
        lambda *args, **kwargs: SimpleNamespace(
            success=True,
            message="Verified ONT registration is absent on the OLT.",
        ),
    )

    result = ont_authorization.authorize_autofind_ont(
        db_session,
        str(olt.id),
        "0/1/6",
        "HWTCSTALE123",
    )

    assert result.success is True
    assert result.ont_id_on_olt == 7
    assert result.completed_authorization is True
    assert ("deauthorize", "0/1/5", 3) in calls
    assert calls.count(("authorize", "0/1/6", "HWTCSTALE123")) == 2
    assert "Removed existing ONT registration" in result.steps[-1].message


def test_authorization_reports_partial_failure_when_local_record_setup_fails(
    db_session,
    monkeypatch,
):
    olt = OLTDevice(name="OLT-Partial-Local", is_active=True)
    db_session.add(olt)
    db_session.commit()

    class FakeAdapter:
        def authorize_ont(
            self,
            fsp,
            serial_number,
            *,
            line_profile_id=None,
            service_profile_id=None,
        ):
            return SimpleNamespace(
                success=True,
                message="Authorized.",
                ont_id=7,
            )

    monkeypatch.setattr(
        "app.services.network.olt_protocol_adapters.get_protocol_adapter",
        lambda _olt: FakeAdapter(),
    )
    monkeypatch.setattr(
        "app.services.network.olt_config_pack.validate_config_pack_comprehensive",
        lambda db, olt_id: SimpleNamespace(is_valid=True),
    )
    monkeypatch.setattr(
        "app.services.network.olt_config_pack.get_validation_summary",
        lambda validation: "Config pack is complete and ready for provisioning",
    )
    monkeypatch.setattr(
        "app.services.network.olt_profile_resolution.resolve_authorization_profiles_from_db",
        lambda db, olt: (
            True,
            "Using OLT authorization profiles.",
            SimpleNamespace(line_profile_id=10, service_profile_id=20),
        ),
    )
    monkeypatch.setattr(
        ont_authorization,
        "create_or_find_ont_for_authorized_serial",
        lambda *args, **kwargs: (None, "Failed to create ONT record."),
    )

    result = ont_authorization.authorize_autofind_ont(
        db_session,
        str(olt.id),
        "0/1/6",
        "HWTCPARTIAL1",
    )

    assert result.success is False
    assert result.completed_authorization is True
    assert result.partial_success is True
    assert result.status == "error"
    assert "local inventory record setup failed" in result.message


def test_authorization_fails_when_acs_foundation_fails(
    db_session, monkeypatch
):
    """Foreground authorization surfaces failed inline ACS foundation setup."""
    result = ont_authorization.AuthorizationWorkflowResult(
        success=True,
        message="ONT authorization completed.",
        status="success",
        ont_unit_id="ont-1",
        ont_id_on_olt=7,
        completed_authorization=True,
    )

    monkeypatch.setattr(
        ont_authorization,
        "authorize_autofind_ont",
        lambda *args, **kwargs: result,
    )
    monkeypatch.setattr(
        ont_authorization,
        "apply_authorization_foundation",
        lambda *args, **kwargs: (
            False,
            "Authorization foundation failed: ACS foundation was not applied.",
            [
                {
                    "name": "Link ONT to PON port",
                    "success": True,
                    "message": "Linked ONT to PON port.",
                },
                {
                    "name": "Apply ACS foundation",
                    "success": False,
                    "message": "OLT rejected TR-069 profile.",
                },
            ],
        ),
    )
    response = ont_authorization.authorize_ont(
        db_session,
        "olt-1",
        "0/1/1",
        "HWTCWARNFOLLOWUP",
    )

    assert response.success is False
    assert response.completed_authorization is True
    assert response.partial_success is True
    assert response.status == "error"
    assert response.message == (
        "ONT authorized, but ACS foundation setup failed: "
        "Authorization foundation failed: ACS foundation was not applied."
    )
    assert [step.name for step in response.steps] == [
        "Bring ONT onto ACS",
    ]
    assert response.steps[-1].success is False
    assert response.steps[-1].message == "OLT rejected TR-069 profile."


def test_authorization_duration_includes_foundation_work(
    db_session, monkeypatch
):
    """The synchronous result duration covers authorization and foundation."""
    result = ont_authorization.AuthorizationWorkflowResult(
        success=True,
        message="ONT authorization completed.",
        status="success",
        ont_unit_id="ont-1",
        ont_id_on_olt=7,
        completed_authorization=True,
        duration_ms=1,
    )
    ticks = iter([10.0, 15.0])

    monkeypatch.setattr(ont_authorization, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(
        ont_authorization,
        "authorize_autofind_ont",
        lambda *args, **kwargs: result,
    )
    monkeypatch.setattr(
        ont_authorization,
        "apply_authorization_foundation",
        lambda *args, **kwargs: (
            True,
            "Authorization foundation completed.",
            [
                {
                    "name": "Allocate management IP",
                    "success": True,
                    "message": "Allocated management IP 10.0.0.1.",
                    "allocated_ip": "10.0.0.1",
                },
                {
                    "name": "Apply ACS foundation",
                    "success": True,
                    "message": "ACS foundation applied.",
                },
            ],
        ),
    )

    response = ont_authorization.authorize_ont(
        db_session,
        "olt-1",
        "0/1/1",
        "HWTCDURATION",
    )

    assert response.duration_ms == 5000
    assert [step.name for step in response.steps] == [
        "Bring ONT onto ACS",
    ]


def test_acs_connectivity_fails_when_acs_has_no_olt_tr069_profile(
    db_session, monkeypatch
):
    """ACS intent without an OLT TR-069 profile cannot produce ACS reachability."""
    olt = OLTDevice(name="OLT-Missing-TR069-Profile", is_active=True)
    ont = OntUnit(serial_number="HWTCNOPROFILE", olt_device=olt, is_active=True)
    db_session.add_all([olt, ont])
    db_session.commit()

    monkeypatch.setattr(
        acs_foundation,
        "resolve_effective_ont_config",
        lambda db, ont: {
            "config_pack": SimpleNamespace(),
            "values": {
                "tr069_acs_server_id": "acs-1",
                "tr069_olt_profile_id": None,
            },
        },
    )

    with pytest.raises(RuntimeError, match="no OLT TR-069 profile ID"):
        acs_foundation.apply_acs_foundation(
            db_session,
            ont_unit_id=str(ont.id),
            olt_id=str(olt.id),
            fsp="0/1/1",
            ont_id_on_olt=7,
        )


def test_acs_connectivity_refuses_tr069_without_management_vlan(
    db_session, monkeypatch
):
    """TR-069 binding without a management VLAN creates unreachable ACS devices."""
    olt = OLTDevice(name="OLT-Missing-Mgmt-VLAN", is_active=True)
    ont = OntUnit(serial_number="HWTCNOMGMTVLAN", olt_device=olt, is_active=True)
    db_session.add_all([olt, ont])
    db_session.commit()

    monkeypatch.setattr(
        acs_foundation,
        "resolve_effective_ont_config",
        lambda db, ont: {
            "config_pack": SimpleNamespace(mgmt_gem_index=2),
            "values": {
                "tr069_acs_server_id": "acs-1",
                "tr069_olt_profile_id": 7,
                "mgmt_vlan": None,
            },
        },
    )

    with pytest.raises(RuntimeError, match="no management VLAN"):
        acs_foundation.apply_acs_foundation(
            db_session,
            ont_unit_id=str(ont.id),
            olt_id=str(olt.id),
            fsp="0/1/1",
            ont_id_on_olt=7,
        )


def test_acs_connectivity_does_not_auto_normalize_wan_after_inform(
    db_session, monkeypatch
):
    """The ACS bootstrap path stops after reachability, without WAN normalization."""
    olt = OLTDevice(name="OLT-ACS-Bootstrap", is_active=True)
    ont = OntUnit(serial_number="HWTCBOOTACS", olt_device=olt, is_active=True)
    acs_server = Tr069AcsServer(
        name="Post Auth ACS",
        base_url="http://genieacs.example:7557",
        connection_request_username="cr-user",
        connection_request_password=encrypt_credential("cr-pass"),
        periodic_inform_interval=300,
        is_active=True,
    )
    db_session.add_all([olt, ont, acs_server])
    db_session.flush()
    db_session.add(
        Tr069CpeDevice(
            serial_number=ont.serial_number,
            ont_unit_id=ont.id,
            acs_server_id=acs_server.id,
            genieacs_device_id="ABC-ONT-HWTCBOOTACS",
            last_inform_at=None,
        )
    )
    db_session.commit()

    captured_specs: list[object] = []

    monkeypatch.setattr(
        acs_foundation,
        "resolve_effective_ont_config",
        lambda db, ont: {
            "config_pack": SimpleNamespace(mgmt_gem_index=2),
            "values": {
                "tr069_acs_server_id": str(acs_server.id),
                "tr069_olt_profile_id": 7,
                "mgmt_vlan": 201,
                "internet_config_ip_index": 0,
                "wan_config_profile_id": 9,
            },
        },
    )
    monkeypatch.setattr(
        acs_foundation,
        "get_protocol_adapter",
        lambda olt: SimpleNamespace(
            configure_management_batch=lambda spec: (
                captured_specs.append(spec)
                or SimpleNamespace(
                    success=True,
                    message="bound tr069",
                    data={},
                )
            )
        ),
    )
    monkeypatch.setattr(
        "app.services.network.ont_provision_steps.wait_tr069_bootstrap",
        lambda db, ont_id, **kwargs: SimpleNamespace(
            success=True,
            message="Device registered in ACS",
            duration_ms=25,
        ),
    )
    result = acs_foundation.apply_acs_foundation(
        db_session,
        ont_unit_id=str(ont.id),
        olt_id=str(olt.id),
        fsp="0/1/1",
        ont_id_on_olt=7,
        wait_for_acs_bootstrap=True,
    )

    assert result["success"] is True
    step_names = [step["name"] for step in result["steps"]]
    assert "Run batched OLT management setup" in step_names
    assert "Wait for ACS inform" in step_names
    assert "Apply ACS inform settings" not in step_names
    assert "Normalize WAN structure" not in step_names
    assert result["message"] == "ONT connected to ACS."
    assert captured_specs
    assert captured_specs[0].mgmt_vlan_tag == 201
    assert captured_specs[0].ip_mode == "dhcp"
    assert captured_specs[0].internet_config_ip_index == 0
    assert captured_specs[0].wan_config_profile_id is None


def test_acs_connection_request_credentials_are_not_required_during_authorization(
    db_session, monkeypatch
):
    """Authorization applies OLT-side ACS reachability only; ACS settings wait for inform."""
    olt = OLTDevice(name="OLT-ACS-No-CR-Credentials", is_active=True)
    ont = OntUnit(serial_number="HWTCNOCRCRED", olt_device=olt, is_active=True)
    acs_server = Tr069AcsServer(
        name="Post Auth ACS Missing CR",
        base_url="http://genieacs.example:7557",
        connection_request_username="",
        connection_request_password=None,
        periodic_inform_interval=300,
        is_active=True,
    )
    db_session.add_all([olt, ont, acs_server])
    db_session.flush()
    db_session.add(
        Tr069CpeDevice(
            serial_number=ont.serial_number,
            ont_unit_id=ont.id,
            acs_server_id=acs_server.id,
            genieacs_device_id="ABC-ONT-HWTCNOCRCRED",
            last_inform_at=None,
        )
    )
    db_session.commit()

    monkeypatch.setattr(
        acs_foundation,
        "resolve_effective_ont_config",
        lambda db, ont: {
            "config_pack": SimpleNamespace(mgmt_gem_index=2),
            "values": {
                "tr069_acs_server_id": str(acs_server.id),
                "tr069_olt_profile_id": 7,
                "mgmt_vlan": 201,
            },
        },
    )
    monkeypatch.setattr(
        acs_foundation,
        "get_protocol_adapter",
        lambda olt: SimpleNamespace(
            configure_management_batch=lambda spec: SimpleNamespace(
                success=True,
                message="bound tr069",
                data={},
            )
        ),
    )
    monkeypatch.setattr(
        "app.services.network.ont_provision_steps.wait_tr069_bootstrap",
        lambda db, ont_id, **kwargs: SimpleNamespace(
            success=True,
            message="Device registered in ACS",
            duration_ms=25,
        ),
    )
    result = acs_foundation.apply_acs_foundation(
        db_session,
        ont_unit_id=str(ont.id),
        olt_id=str(olt.id),
        fsp="0/1/1",
        ont_id_on_olt=7,
        wait_for_acs_bootstrap=True,
    )

    assert result["success"] is True


def test_acs_connectivity_requires_acs_bootstrap_when_authorization_waits(
    db_session, monkeypatch
):
    """Foreground authorization fails synchronously if the ONT never informs ACS."""
    olt = OLTDevice(name="OLT-ACS-Wait-Failure", is_active=True)
    ont = OntUnit(serial_number="HWTCWAITFAIL", olt_device=olt, is_active=True)
    acs_server = Tr069AcsServer(
        name="Post Auth ACS Timeout",
        base_url="http://genieacs.example:7557",
        connection_request_username="cr-user",
        connection_request_password=encrypt_credential("cr-pass"),
        periodic_inform_interval=300,
        is_active=True,
    )
    db_session.add_all([olt, ont, acs_server])
    db_session.flush()
    db_session.add(
        Tr069CpeDevice(
            serial_number=ont.serial_number,
            ont_unit_id=ont.id,
            acs_server_id=acs_server.id,
            genieacs_device_id=None,
            last_inform_at=None,
        )
    )
    db_session.commit()

    monkeypatch.setattr(
        acs_foundation,
        "resolve_effective_ont_config",
        lambda db, ont: {
            "config_pack": SimpleNamespace(mgmt_gem_index=2),
            "values": {
                "tr069_acs_server_id": str(acs_server.id),
                "tr069_olt_profile_id": 7,
                "mgmt_vlan": 201,
            },
        },
    )
    monkeypatch.setattr(
        acs_foundation,
        "get_protocol_adapter",
        lambda olt: SimpleNamespace(
            configure_management_batch=lambda spec: SimpleNamespace(
                success=True,
                message="bound tr069",
                data={},
            )
        ),
    )
    monkeypatch.setattr(
        "app.services.network.ont_provision_steps.wait_tr069_bootstrap",
        lambda db, ont_id, **kwargs: SimpleNamespace(
            success=False,
            message="Device not found in ACS after 120s",
            duration_ms=120000,
        ),
    )

    with pytest.raises(RuntimeError, match="Device did not register with ACS"):
        acs_foundation.apply_acs_foundation(
            db_session,
            ont_unit_id=str(ont.id),
            olt_id=str(olt.id),
            fsp="0/1/1",
            ont_id_on_olt=7,
            wait_for_acs_bootstrap=True,
        )


def test_acs_connectivity_failure_is_synchronous(
    db_session, monkeypatch
):
    """Transient ACS foundation failures return as real synchronous failures."""
    olt = OLTDevice(name="OLT-ACS-Retry-Backoff", is_active=True)
    ont = OntUnit(serial_number="HWTCRETRYWAIT", olt_device=olt, is_active=True)
    acs_server = Tr069AcsServer(
        name="Post Auth ACS Retry",
        base_url="http://genieacs.example:7557",
        connection_request_username="cr-user",
        connection_request_password=encrypt_credential("cr-pass"),
        periodic_inform_interval=300,
        is_active=True,
    )
    db_session.add_all([olt, ont, acs_server])
    db_session.flush()
    db_session.add(
        Tr069CpeDevice(
            serial_number=ont.serial_number,
            ont_unit_id=ont.id,
            acs_server_id=acs_server.id,
            genieacs_device_id="ABC-ONT-HWTCRETRYWAIT",
            last_inform_at=None,
        )
    )
    db_session.commit()

    monkeypatch.setattr(
        acs_foundation,
        "resolve_effective_ont_config",
        lambda db, ont: {
            "config_pack": SimpleNamespace(mgmt_gem_index=2),
            "values": {
                "tr069_acs_server_id": str(acs_server.id),
                "tr069_olt_profile_id": 7,
                "mgmt_ip_address": "10.10.10.2",
                "mgmt_vlan": 100,
            },
        },
    )
    monkeypatch.setattr(
        acs_foundation,
        "get_protocol_adapter",
        lambda olt: SimpleNamespace(
            configure_management_batch=lambda spec: SimpleNamespace(
                success=False,
                message="OLT busy",
                data={},
            )
        ),
    )

    with pytest.raises(RuntimeError, match="Batched OLT management setup failed"):
        acs_foundation.apply_acs_foundation(
            db_session,
            ont_unit_id=str(ont.id),
            olt_id=str(olt.id),
            fsp="0/1/1",
            ont_id_on_olt=7,
        )
