from pathlib import Path

from starlette.routing import Match

from app.models.network_monitoring import (
    DeviceInterface,
    DeviceRole,
    DeviceStatus,
    DeviceType,
    NetworkDevice,
    PopSite,
    TopologyLinkMedium,
    TopologyLinkRole,
)
from app.services import network_topology as topology_service
from app.web.admin import network_weathermap as topology_routes


def _matched_route(router, path: str, method: str = "GET"):
    scope = {
        "type": "http",
        "path": path,
        "method": method,
        "root_path": "",
        "scheme": "http",
        "query_string": b"",
        "headers": [],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }
    for route in router.routes:
        match, _ = route.matches(scope)
        if match == Match.FULL:
            return route
    return None


def test_topology_link_update_can_change_endpoints(db_session):
    source_a = NetworkDevice(
        name="Source A",
        role=DeviceRole.core,
        status=DeviceStatus.online,
        is_active=True,
    )
    target_a = NetworkDevice(
        name="Target A",
        role=DeviceRole.edge,
        status=DeviceStatus.online,
        is_active=True,
    )
    source_b = NetworkDevice(
        name="Source B",
        role=DeviceRole.core,
        status=DeviceStatus.online,
        is_active=True,
    )
    target_b = NetworkDevice(
        name="Target B",
        role=DeviceRole.edge,
        status=DeviceStatus.online,
        is_active=True,
    )
    db_session.add_all([source_a, target_a, source_b, target_b])
    db_session.flush()

    iface_a1 = DeviceInterface(device_id=source_a.id, name="xe-0/0/0")
    iface_a2 = DeviceInterface(device_id=target_a.id, name="xe-0/0/1")
    iface_b1 = DeviceInterface(device_id=source_b.id, name="xe-0/0/2")
    iface_b2 = DeviceInterface(device_id=target_b.id, name="xe-0/0/3")
    db_session.add_all([iface_a1, iface_a2, iface_b1, iface_b2])
    db_session.flush()

    link = topology_service.topology_links.create(
        db_session,
        data={
            "source_device_id": str(source_a.id),
            "source_interface_id": str(iface_a1.id),
            "target_device_id": str(target_a.id),
            "target_interface_id": str(iface_a2.id),
            "link_role": "uplink",
            "medium": "fiber",
            "capacity_bps": "1000000000",
        },
    )

    updated = topology_service.topology_links.update(
        db_session,
        str(link.id),
        data={
            "source_device_id": str(source_b.id),
            "source_interface_id": str(iface_b1.id),
            "target_device_id": str(target_b.id),
            "target_interface_id": str(iface_b2.id),
            "link_role": "backhaul",
            "medium": "wireless",
            "capacity_bps": "500000000",
        },
    )

    assert updated.source_device_id == source_b.id
    assert updated.source_interface_id == iface_b1.id
    assert updated.target_device_id == target_b.id
    assert updated.target_interface_id == iface_b2.id
    assert updated.link_role == TopologyLinkRole.backhaul
    assert updated.medium == TopologyLinkMedium.wireless


def test_topology_link_update_allows_same_interfaces_on_same_link(db_session):
    source = NetworkDevice(
        name="Source", role=DeviceRole.core, status=DeviceStatus.online, is_active=True
    )
    target = NetworkDevice(
        name="Target", role=DeviceRole.edge, status=DeviceStatus.online, is_active=True
    )
    db_session.add_all([source, target])
    db_session.flush()

    src_iface = DeviceInterface(device_id=source.id, name="xe-0/0/0")
    tgt_iface = DeviceInterface(device_id=target.id, name="xe-0/0/1")
    db_session.add_all([src_iface, tgt_iface])
    db_session.flush()

    link = topology_service.topology_links.create(
        db_session,
        data={
            "source_device_id": str(source.id),
            "source_interface_id": str(src_iface.id),
            "target_device_id": str(target.id),
            "target_interface_id": str(tgt_iface.id),
            "link_role": "uplink",
            "medium": "fiber",
        },
    )

    updated = topology_service.topology_links.update(
        db_session,
        str(link.id),
        data={
            "source_device_id": str(source.id),
            "source_interface_id": str(src_iface.id),
            "target_device_id": str(target.id),
            "target_interface_id": str(tgt_iface.id),
            "link_role": "uplink",
            "medium": "fiber",
            "bundle_key": "lag-1",
        },
    )

    assert updated.bundle_key == "lag-1"


def test_topology_graph_falls_back_to_active_inventory_when_no_links(db_session):
    active_a = NetworkDevice(
        name="Core A",
        role=DeviceRole.core,
        device_type=DeviceType.router,
        status=DeviceStatus.online,
        is_active=True,
    )
    active_b = NetworkDevice(
        name="Edge B",
        role=DeviceRole.edge,
        device_type=DeviceType.switch,
        status=DeviceStatus.online,
        is_active=True,
    )
    excluded_cpe = NetworkDevice(
        name="Customer CPE",
        role=DeviceRole.cpe,
        device_type=DeviceType.modem,
        status=DeviceStatus.online,
        is_active=True,
    )
    excluded_server = NetworkDevice(
        name="Metrics Server",
        role=DeviceRole.edge,
        device_type=DeviceType.server,
        status=DeviceStatus.online,
        is_active=True,
    )
    inactive = NetworkDevice(
        name="Old Device",
        role=DeviceRole.edge,
        device_type=DeviceType.router,
        status=DeviceStatus.offline,
        is_active=False,
    )
    db_session.add_all([active_a, active_b, excluded_cpe, excluded_server, inactive])
    db_session.flush()

    graph = topology_service.list_nodes_and_edges(db_session)

    node_ids = {node["id"] for node in graph["nodes"]}
    assert str(active_a.id) in node_ids
    assert str(active_b.id) in node_ids
    assert str(excluded_cpe.id) not in node_ids
    assert str(excluded_server.id) not in node_ids
    assert str(inactive.id) not in node_ids
    assert graph["edges"] == []
    assert graph["stats"]["node_count"] == 2


def test_node_summary_includes_device_interfaces(db_session):
    device = NetworkDevice(
        name="Core A",
        role=DeviceRole.core,
        device_type=DeviceType.router,
        status=DeviceStatus.online,
        is_active=True,
    )
    db_session.add(device)
    db_session.flush()

    iface_a = DeviceInterface(
        device_id=device.id,
        name="xe-0/0/0",
        status="up",
        speed_mbps=1000,
        monitored=True,
    )
    iface_b = DeviceInterface(
        device_id=device.id,
        name="xe-0/0/1",
        status="down",
        speed_mbps=10000,
        monitored=False,
    )
    db_session.add_all([iface_a, iface_b])
    db_session.flush()

    summary = topology_service.node_summary(db_session, str(device.id))

    assert summary["interface_count"] == 2
    assert {iface["name"] for iface in summary["interfaces"]} == {
        "xe-0/0/0",
        "xe-0/0/1",
    }


def test_topology_graph_aggregates_and_filters_by_pop_site(db_session):
    site_a = PopSite(name="POP A", city="Lagos", region="Ikeja", is_active=True)
    site_b = PopSite(name="POP B", city="Abuja", region="Mabushi", is_active=True)
    db_session.add_all([site_a, site_b])
    db_session.flush()

    dev_a = NetworkDevice(
        name="Core A",
        pop_site_id=site_a.id,
        role=DeviceRole.core,
        device_type=DeviceType.router,
        status=DeviceStatus.online,
        is_active=True,
    )
    dev_b = NetworkDevice(
        name="Edge B",
        pop_site_id=site_b.id,
        role=DeviceRole.edge,
        device_type=DeviceType.switch,
        status=DeviceStatus.online,
        is_active=True,
    )
    db_session.add_all([dev_a, dev_b])
    db_session.flush()

    full_graph = topology_service.list_nodes_and_edges(db_session)
    filtered_graph = topology_service.list_nodes_and_edges(
        db_session, pop_site_id=str(site_a.id)
    )

    assert full_graph["stats"]["site_count"] == 2
    assert {item["pop_site_name"] for item in full_graph["site_summaries"]} == {
        "POP A",
        "POP B",
    }
    assert {node["pop_site_name"] for node in filtered_graph["nodes"]} == {"POP A"}


def test_get_device_interfaces_returns_full_inventory(db_session):
    device = NetworkDevice(
        name="Dense Switch",
        role=DeviceRole.access,
        device_type=DeviceType.switch,
        status=DeviceStatus.online,
        is_active=True,
    )
    db_session.add(device)
    db_session.flush()

    db_session.add_all(
        [
            DeviceInterface(
                device_id=device.id, name=f"ge-0/0/{index:03d}", monitored=True
            )
            for index in range(205)
        ]
    )
    db_session.flush()

    interfaces = topology_service.get_device_interfaces(db_session, str(device.id))

    assert len(interfaces) == 205


def test_legacy_weathermap_redirect_route_exists():
    route = _matched_route(topology_routes.router, "/network/weathermap")
    assert route is not None
    assert route.endpoint is topology_routes.network_weathermap_redirect


def test_topology_routes_include_ajax_endpoints():
    assert (
        _matched_route(topology_routes.router, "/network/topology/api/interfaces/123")
        is not None
    )
    assert (
        _matched_route(topology_routes.router, "/network/topology/api/node/123")
        is not None
    )
    assert (
        _matched_route(topology_routes.router, "/network/topology/api/graph")
        is not None
    )


def test_topology_link_form_restores_selected_interfaces():
    template = Path("templates/admin/network/topology/link_form.html").read_text()
    assert 'id="sourceInterfaceSelected"' in template
    assert 'id="targetInterfaceSelected"' in template
    assert "if (deviceSelect.value)" in template


def test_legacy_weathermap_assets_removed():
    assert not Path("app/services/web_network_weathermap.py").exists()
    assert not Path("templates/admin/network/weathermap/index.html").exists()


def test_admin_labels_switched_to_topology():
    layout = Path("templates/layouts/admin.html").read_text()
    network_hub = Path("templates/admin/network/index.html").read_text()
    design_system = Path("templates/admin/design_system/modules.html").read_text()
    assert "'topology': 'Network Topology'" in layout
    assert '"href": "/admin/network/topology"' in network_hub
    assert (
        '"label": "Network Topology", "url": "/admin/network/topology"' in design_system
    )
