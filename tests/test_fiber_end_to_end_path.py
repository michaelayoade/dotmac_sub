from __future__ import annotations

from app.models.catalog import NasDevice
from app.models.network_monitoring import DeviceInterface, PopSite
from app.services.network.access_path import resolve_fiber_end_to_end_path
from tests.test_fiber_physical_continuity import install_complete_core_path
from tests.test_fiber_subscription_trace import _complete_path
from tests.test_forwarding_topology import (
    _apply_decision,
    _control_observation,
    _device,
    _internal_payload,
    _lldp,
)


def test_composed_fiber_path_reaches_provisioning_nas_and_core(
    db_session,
    subscription,
    subscriber,
    olt_device,
    network_device,
):
    assets = _complete_path(
        db_session, subscription, subscriber, olt_device, network_device
    )
    install_complete_core_path(db_session, assets, network_device)
    site = db_session.get(PopSite, network_device.pop_site_id)
    assert site is not None
    _nas_site, nas_node, nas_interface = _device(
        db_session, "Serving NAS node", site=site
    )
    nas_core_interface = DeviceInterface(
        device_id=nas_node.id,
        name="to-core",
    )
    db_session.add(nas_core_interface)
    _core_site, core_node, core_interface = _device(db_session, "Core node", site=site)
    olt_interface = DeviceInterface(
        device_id=network_device.id,
        name="to-serving-nas",
    )
    db_session.add(olt_interface)
    db_session.flush()

    nas = NasDevice(
        name="Serving NAS",
        network_device_id=nas_node.id,
        pop_site_id=site.id,
        is_active=True,
    )
    db_session.add(nas)
    db_session.flush()
    subscription.provisioning_nas_device_id = nas.id

    olt_to_nas = _internal_payload(
        network_device,
        olt_interface,
        site,
        nas_node,
        nas_interface,
        site,
        path_key="fiber-e2e:olt-nas",
        downstream_role="access",
        upstream_role="nas",
    )
    _apply_decision(
        db_session,
        action="declare",
        declaration=olt_to_nas,
        path_key="fiber-e2e:olt-nas",
    )
    _lldp(db_session, network_device, olt_interface, nas_node, nas_interface)

    nas_to_core = {
        **_internal_payload(
            nas_node,
            nas_core_interface,
            site,
            core_node,
            core_interface,
            site,
            path_key="fiber-e2e:nas-core",
            downstream_role="nas",
            upstream_role="core",
        ),
        "nas_device_id": str(nas.id),
        "next_hop_ip": "10.0.0.254",
        "path_kind": "nas_termination",
        "route_prefix": "0.0.0.0/0",
    }
    _apply_decision(
        db_session,
        action="declare",
        declaration=nas_to_core,
        path_key="fiber-e2e:nas-core",
    )
    _lldp(db_session, nas_node, nas_core_interface, core_node, core_interface)
    _control_observation(
        db_session,
        nas_node,
        nas_core_interface,
        source_type="routing_table",
        route_prefix="0.0.0.0/0",
        next_hop_ip="10.0.0.254",
    )
    db_session.flush()

    result = resolve_fiber_end_to_end_path(db_session, subscription)

    assert result.gaps == ()
    assert result.complete is True
    assert result.passive_complete is True
    assert result.core_continuity_complete is True
    assert result.forwarding_complete is True
    assert result.provisioning_nas_device_id == nas.id
    assert result.live_nas_device_id is None
    assert result.live_nas_state == "missing_observation"
    passive_kinds = [hop.kind for hop in result.hops if hop.domain == "passive_fiber"]
    assert passive_kinds[0] == "customer"
    assert "ont" in passive_kinds
    core_kinds = [hop.kind for hop in result.hops if hop.domain == "physical_core"]
    assert "fiber_rack" in core_kinds
    assert "odf" in core_kinds
    assert "patch_cord" in core_kinds
    assert [hop.asset_id for hop in result.hops if hop.domain == "forwarding"] == [
        network_device.id,
        nas_node.id,
        core_node.id,
    ]
    assert len(result.forwarding_declaration_ids) == 2
    assert len(result.core_continuity_sha256) == 64
    assert len(result.evidence_sha256) == 64


def test_composed_path_does_not_infer_missing_provisioning_nas(
    db_session,
    subscription,
    subscriber,
    olt_device,
    network_device,
):
    assets = _complete_path(
        db_session, subscription, subscriber, olt_device, network_device
    )
    install_complete_core_path(db_session, assets, network_device)

    result = resolve_fiber_end_to_end_path(db_session, subscription)

    assert result.complete is False
    assert result.core_continuity_complete is True
    assert result.provisioning_nas_device_id is None
    assert "provisioning.nas_missing" in {gap.code for gap in result.gaps}
    assert result.live_nas_state == "missing_observation"
