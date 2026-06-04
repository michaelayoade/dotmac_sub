from __future__ import annotations

from types import SimpleNamespace

from sqlalchemy import select


def _acs_ready_olt(db_session, *, pool_cidr: str = "172.16.210.0/24"):
    from app.models.catalog import RegionZone
    from app.models.network import IpPool, IPVersion, OLTDevice, Vlan, VlanPurpose
    from app.models.tr069 import Tr069AcsServer

    region = RegionZone(name=f"Config Pack Resolution {pool_cidr}", code=pool_cidr[-6:])
    acs = Tr069AcsServer(
        name=f"ACS {pool_cidr}",
        base_url="http://genieacs.example:7557",
        is_active=True,
    )
    olt = OLTDevice(
        name=f"OLT {pool_cidr}",
        wan_provisioning_mode="omci_wan_config",
        supports_ont_internet_config=True,
        supports_ont_wan_config=True,
    )
    db_session.add_all([region, acs, olt])
    db_session.flush()
    olt.tr069_acs_server_id = acs.id

    mgmt_vlan = Vlan(
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
        vlan=mgmt_vlan,
        is_active=True,
    )
    db_session.add_all([mgmt_vlan, pool])
    db_session.flush()
    olt.mgmt_ip_pool_id = pool.id
    return olt, acs, mgmt_vlan, pool


def test_authorization_baseline_records_config_pack_resolution_snapshot(
    db_session, monkeypatch
) -> None:
    from app.models.network import OntProvisioningEvent, OntProvisioningStatus, OntUnit
    from app.services.network.ont_provision_steps import apply_authorization_baseline
    from app.services.network.ont_provisioning.result import StepResult

    olt, _acs, mgmt_vlan, _pool = _acs_ready_olt(db_session)
    from app.models.network import Vlan, VlanPurpose

    internet_vlan = Vlan(
        region_id=mgmt_vlan.region_id,
        olt_device_id=olt.id,
        tag=203,
        name="Internet",
        purpose=VlanPurpose.internet,
        is_active=True,
    )
    db_session.add(internet_vlan)
    db_session.flush()

    olt.config_pack = {
        "internet_vlan_id": str(internet_vlan.id),
        "management_vlan_id": str(mgmt_vlan.id),
        "tr069_olt_profile_id": 5,
        "internet_config_ip_index": 1,
        "wan_config_profile_id": 7,
        "pppoe_wcd_index": 2,
        "mgmt_wcd_index": 1,
        "cr_username": "acs-user",
        "cr_password": "super-secret",
    }

    ont = OntUnit(
        serial_number="CFG-STAGE-OK-001",
        olt_device_id=olt.id,
        desired_config={"wan": {"mode": "pppoe"}},
        provisioning_status=OntProvisioningStatus.unprovisioned,
    )
    db_session.add(ont)
    db_session.flush()

    monkeypatch.setattr(
        "app.services.network.ont_provision_steps.validate_prerequisites",
        lambda *args, **kwargs: {"ready_to_provision": True, "checks": []},
    )
    monkeypatch.setattr(
        "app.services.network.ont_provision_steps.provision_with_reconciliation",
        lambda *args, **kwargs: StepResult("provision_reconciled", True, "ok"),
    )
    monkeypatch.setattr(
        "app.services.network._resolve.reconcile_ont_tr069_device",
        lambda *_args, **_kwargs: (
            SimpleNamespace(id="device-1", genieacs_device_id="genie-1"),
            "linked",
        ),
    )

    result = apply_authorization_baseline(db_session, str(ont.id))
    db_session.flush()
    db_session.refresh(ont)

    assert result.success is True
    assert result.waiting is True
    assert ont.provisioning_status == OntProvisioningStatus.pending_acs_registration

    event = db_session.scalar(
        select(OntProvisioningEvent).where(
            OntProvisioningEvent.ont_unit_id == ont.id,
            OntProvisioningEvent.step_name == "resolve_effective_config_pack",
        )
    )
    assert event is not None
    assert event.status.value == "succeeded"
    assert event.event_data["resolved_config_pack"]["internet_vlan"]["tag"] == 203
    assert event.event_data["validation"]["tr069_vlan_source"] == "management_vlan"
    assert event.event_data["effective_values"]["pppoe_wcd_index"] == 2
    assert "cr_password" not in event.event_data["raw_config_pack"]
    assert "failure_class" not in event.event_data
    assert (
        result.data["domain_outcomes"]["config_pack_resolution"]["status"]
        == "succeeded"
    )
    assert (
        result.data["domain_outcomes"]["acs_bootstrap_verify"]["status"]
        == "pending_verification"
    )


def test_authorization_baseline_blocks_on_incomplete_config_pack_before_write(
    db_session, monkeypatch
) -> None:
    from app.models.network import OntProvisioningEvent, OntProvisioningStatus, OntUnit
    from app.services.network.ont_provision_steps import apply_authorization_baseline

    olt, _acs, _mgmt_vlan, _pool = _acs_ready_olt(db_session)
    from app.models.network import Vlan, VlanPurpose

    internet_vlan = Vlan(
        region_id=_mgmt_vlan.region_id,
        olt_device_id=olt.id,
        tag=203,
        name="Internet",
        purpose=VlanPurpose.internet,
        is_active=True,
    )
    db_session.add(internet_vlan)
    db_session.flush()

    olt.config_pack = {
        "internet_vlan_id": str(internet_vlan.id),
        "tr069_olt_profile_id": 5,
        "internet_config_ip_index": 1,
        "wan_config_profile_id": 7,
        "pppoe_wcd_index": 2,
        "mgmt_wcd_index": 1,
    }

    ont = OntUnit(
        serial_number="CFG-STAGE-BLOCK-001",
        olt_device_id=olt.id,
        desired_config={"wan": {"mode": "pppoe"}},
        provisioning_status=OntProvisioningStatus.unprovisioned,
    )
    db_session.add(ont)
    db_session.flush()

    calls = {"preflight": 0, "provision": 0}

    monkeypatch.setattr(
        "app.services.network.ont_provision_steps.validate_prerequisites",
        lambda *args, **kwargs: (
            calls.__setitem__("preflight", calls["preflight"] + 1)
            or {"ready_to_provision": True, "checks": []}
        ),
    )
    monkeypatch.setattr(
        "app.services.network.ont_provision_steps.provision_with_reconciliation",
        lambda *args, **kwargs: calls.__setitem__("provision", calls["provision"] + 1),
    )

    result = apply_authorization_baseline(db_session, str(ont.id))
    db_session.flush()
    db_session.refresh(ont)

    assert result.success is False
    assert "config-pack incomplete" in result.message.lower()
    assert calls == {"preflight": 0, "provision": 0}
    assert ont.provisioning_status == OntProvisioningStatus.failed

    event = db_session.scalar(
        select(OntProvisioningEvent).where(
            OntProvisioningEvent.ont_unit_id == ont.id,
            OntProvisioningEvent.step_name == "resolve_effective_config_pack",
        )
    )
    assert event is not None
    assert event.status.value == "failed"
    assert event.event_data["failure_class"] == "config_pack_incomplete"
    assert "management_vlan" in event.event_data["validation"]["incomplete_fields"]
    assert (
        result.data["domain_outcomes"]["config_pack_resolution"]["status"]
        == "terminal_failure"
    )
