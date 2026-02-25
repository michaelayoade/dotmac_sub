from pathlib import Path
from types import SimpleNamespace
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException

from app.services import nas as nas_service
from app.services import backup_alerts as backup_alerts_service
from app.services import web_network_core_runtime as core_runtime
from app.services import web_network_core_devices_forms as core_devices_forms
from app.services import web_network_cpes as web_network_cpes_service
from app.services import web_network_ip as web_network_ip_service
from app.services import web_network_dns_threats as web_network_dns_threats_service
from app.services import web_network_olts as web_network_olts_service
from app.services import web_network_speedtests as web_network_speedtests_service
from app.services import web_network_weathermap as web_network_weathermap_service
from app.services import web_network_tr069 as web_network_tr069_service
from app.services import network_map as network_map_service
from app.models.catalog import ConnectionType, NasConfigBackup, NasDevice, NasVendor
from app.models.network import OLTDevice, OltConfigBackup, OltConfigBackupType
from app.models.subscriber import Address, AddressType, Subscriber
from app.models.notification import Notification, NotificationChannel, NotificationStatus
from app.models.network_monitoring import (
    DeviceRole,
    DeviceStatus,
    NetworkDevice,
    NetworkDeviceBandwidthGraph,
    NetworkDeviceBandwidthGraphSource,
    NetworkDeviceSnmpOid,
    PopSite,
    SpeedTestResult,
    SpeedTestSource,
    DnsThreatAction,
    DnsThreatEvent,
    DnsThreatSeverity,
    DeviceMetric,
    MetricType,
)
from app.models.tr069 import Tr069JobStatus
from app.models.tr069 import Tr069Job
from app.schemas.network import CPEDeviceCreate, IPAssignmentCreate, IPv4AddressCreate, IPv6AddressCreate
from app.schemas.network_monitoring import NetworkDeviceCreate
from app.schemas.tr069 import Tr069CpeDeviceCreate, Tr069JobCreate
from app.services import network_monitoring as monitoring_service
from app.services import network as network_service
from app.services import tr069 as tr069_service
from app.services import web_network_core_devices_views as core_devices_views
from app.schemas.catalog import NasDeviceCreate
from app.web.admin import nas as nas_web


def test_validate_ipv4_address_rejects_invalid_octet():
    error = nas_web._validate_ipv4_address("172.16.300.5", "IP address")
    assert error == "IP address must be a valid IPv4 address."


def test_merge_radius_pool_tags_replaces_previous_radius_tags():
    merged = nas_web._merge_radius_pool_tags(
        ["site:pop1", "radius_pool:old-1"],
        ["pool-a", "pool-b"],
    )
    assert merged == ["site:pop1", "radius_pool:pool-a", "radius_pool:pool-b"]


def test_extract_enhanced_fields_from_tags():
    fields = nas_web._extract_enhanced_fields(
        [
            "partner_org:11111111-1111-1111-1111-111111111111",
            "authorization_type:ppp_dhcp_radius",
            "accounting_type:radius_accounting",
            "physical_address:Main Street",
            "latitude:9.0820",
            "longitude:8.6753",
        ]
    )
    assert fields["partner_org_ids"] == ["11111111-1111-1111-1111-111111111111"]
    assert fields["authorization_type"] == "ppp_dhcp_radius"
    assert fields["accounting_type"] == "radius_accounting"


def test_extract_enhanced_fields_includes_shaper_and_mikrotik_api_tags():
    fields = nas_web._extract_enhanced_fields(
        [
            "mikrotik_api_enabled:true",
            "mikrotik_api_port:8728",
            "shaper_enabled:true",
            "shaper_target:this_router",
            "shaping_type:queue_tree",
            "wireless_access_list:true",
            "disabled_customers_address_list:false",
            "blocking_rules_enabled:true",
        ]
    )
    assert fields["mikrotik_api_enabled"] == "true"
    assert fields["mikrotik_api_port"] == "8728"
    assert fields["shaper_enabled"] == "true"
    assert fields["shaper_target"] == "this_router"
    assert fields["shaping_type"] == "queue_tree"
    assert fields["blocking_rules_enabled"] == "true"


def test_build_nas_payload_requires_nas_ip_when_radius_authorization_selected(db_session):
    payload, errors = nas_service.build_nas_device_payload(
        db_session,
        form={
            "name": "Auth NAS",
            "vendor": "mikrotik",
            "ip_address": "192.0.2.100",
            "status": "active",
            "authorization_type": "ppp_dhcp_radius",
            "supported_connection_types": "[]",
        },
        existing_tags=None,
        for_update=False,
    )
    assert payload is None
    assert any("authorization type is PPP/DHCP Radius" in err for err in errors)


def test_usable_ipv4_count_handles_common_prefixes():
    assert web_network_ip_service._usable_ipv4_count("10.0.0.0/24") == 254
    assert web_network_ip_service._usable_ipv4_count("10.0.0.0/31") == 2
    assert web_network_ip_service._usable_ipv4_count("not-a-cidr") == 0


def test_get_ping_status_reachable_with_latency(monkeypatch):
    def _fake_run(*_args, **_kwargs):
        return SimpleNamespace(returncode=0, stdout="64 bytes time=12.5 ms", stderr="")

    monkeypatch.setattr(nas_service.subprocess, "run", _fake_run)
    status = nas_service.get_ping_status("192.0.2.10")
    assert status["state"] == "reachable"
    assert status["latency_ms"] == 12.5


def test_get_ping_status_unreachable(monkeypatch):
    def _fake_run(*_args, **_kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="timeout")

    monkeypatch.setattr(nas_service.subprocess, "run", _fake_run)
    status = nas_service.get_ping_status("192.0.2.11")
    assert status == {"state": "unreachable", "label": "Unreachable"}


def test_get_mikrotik_api_status_success(db_session, monkeypatch):
    device = nas_service.NasDevices.create(
        db_session,
        NasDeviceCreate(
            name="MK1",
            vendor=NasVendor.mikrotik,
            api_url="https://router.example",
            api_username="admin",
            api_password="secret",
        ),
    )

    class _Resp:
        def __init__(self, payload):
            self._payload = payload
            self.text = "ok"

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    def _fake_get(url, **kwargs):
        if url.endswith("/rest/system/resource"):
            return _Resp({"platform": "MikroTik", "board-name": "CCR", "cpu-load": 17, "ipv6": True})
        return _Resp([{"name": "routeros", "version": "7.15"}])

    monkeypatch.setattr("requests.get", _fake_get)
    status = nas_service.get_mikrotik_api_status(device)
    assert status["platform"] == "MikroTik"
    assert status["board_name"] == "CCR"
    assert status["routeros_version"] == "7.15"


def test_nas_connection_rules_create_list_toggle_delete(db_session):
    device = nas_service.NasDevices.create(
        db_session,
        NasDeviceCreate(name="RuleNAS", vendor=NasVendor.mikrotik, ip_address="192.0.2.21"),
    )
    created = nas_service.NasConnectionRules.create(
        db_session,
        nas_device_id=device.id,
        name="Corp PPPoE",
        connection_type=ConnectionType.pppoe,
        ip_assignment_mode="pool",
        rate_limit_profile="gold",
        priority=10,
    )
    assert created.name == "Corp PPPoE"
    assert created.connection_type == ConnectionType.pppoe
    rules = nas_service.NasConnectionRules.list(db_session, nas_device_id=device.id, is_active=None)
    assert [r.name for r in rules] == ["Corp PPPoE"]

    disabled = nas_service.NasConnectionRules.set_active(
        db_session,
        rule_id=created.id,
        nas_device_id=device.id,
        is_active=False,
    )
    assert disabled.is_active is False

    nas_service.NasConnectionRules.delete(db_session, rule_id=created.id, nas_device_id=device.id)
    rules_after_delete = nas_service.NasConnectionRules.list(
        db_session,
        nas_device_id=device.id,
        is_active=None,
    )
    assert rules_after_delete == []


def test_nas_connection_rules_reject_duplicate_name_per_device(db_session):
    device = nas_service.NasDevices.create(
        db_session,
        NasDeviceCreate(name="RuleNAS-2", vendor=NasVendor.mikrotik, ip_address="192.0.2.31"),
    )
    nas_service.NasConnectionRules.create(
        db_session,
        nas_device_id=device.id,
        name="Office Rule",
        connection_type=ConnectionType.dhcp,
    )
    with pytest.raises(HTTPException) as exc_info:
        nas_service.NasConnectionRules.create(
            db_session,
            nas_device_id=device.id,
            name="Office Rule",
            connection_type=ConnectionType.pppoe,
        )
    assert "already exists" in str(exc_info.value.detail).lower()


def test_core_devices_list_page_data_filters_site_status_and_search(db_session):
    pop_a = PopSite(name="Alpha Site", is_active=True)
    pop_b = PopSite(name="Beta Site", is_active=True)
    db_session.add_all([pop_a, pop_b])
    db_session.flush()

    dev_a = NetworkDevice(
        name="Core Alpha Router",
        pop_site_id=pop_a.id,
        mgmt_ip="10.100.0.1",
        vendor="MikroTik",
        model="CCR",
        is_active=True,
    )
    dev_b = NetworkDevice(
        name="Edge Beta Switch",
        pop_site_id=pop_b.id,
        mgmt_ip="10.200.0.2",
        vendor="Ubiquiti",
        model="EdgeSwitch",
        is_active=False,
    )
    db_session.add_all([dev_a, dev_b])
    db_session.commit()

    filtered = core_devices_forms.list_page_data(
        db_session,
        role=None,
        status="active",
        pop_site_id=str(pop_a.id),
        search="alpha",
    )
    assert filtered["stats"]["total"] == 1
    assert len(filtered["devices"]) == 1
    assert filtered["devices"][0].name == "Core Alpha Router"


def test_core_devices_list_page_data_includes_uptime_ping_history_and_backup(db_session):
    pop = PopSite(name="Gamma Site", is_active=True)
    db_session.add(pop)
    db_session.flush()
    device = NetworkDevice(
        name="Gamma Router",
        pop_site_id=pop.id,
        mgmt_ip="10.210.0.1",
        is_active=True,
        ping_enabled=True,
    )
    db_session.add(device)
    db_session.flush()

    db_session.add(
        DeviceMetric(
            device_id=device.id,
            metric_type=MetricType.uptime,
            value=3661,
            unit="seconds",
            recorded_at=datetime.now(UTC),
        )
    )
    db_session.add(
        DeviceMetric(
            device_id=device.id,
            metric_type=MetricType.custom,
            value=12,
            unit="ping_ms",
            recorded_at=datetime.now(UTC),
        )
    )
    nas = NasDevice(
        name="Gamma NAS",
        vendor=NasVendor.mikrotik,
        ip_address="10.210.0.1",
        management_ip="10.210.0.1",
        network_device_id=device.id,
    )
    db_session.add(nas)
    db_session.flush()
    db_session.add(
        NasConfigBackup(
            nas_device_id=nas.id,
            config_content="export compact",
            config_hash="abc",
            created_at=datetime.now(UTC),
        )
    )
    db_session.commit()

    payload = core_devices_forms.list_page_data(db_session, role=None, status="active")
    key = str(device.id)
    assert payload["uptime_map"][key] is not None
    assert payload["ping_history_map"][key][0]["ok"] is True
    assert payload["backup_map"][key]["status"] == "success"


def test_create_snmp_oid_for_device_and_poll_success(db_session):
    device = NetworkDevice(name="SNMP Core", mgmt_ip="192.0.2.90", snmp_enabled=True, is_active=True)
    db_session.add(device)
    db_session.commit()

    ok, msg = core_devices_forms.create_snmp_oid_for_device(
        db_session,
        device_id=str(device.id),
        title="ifIn",
        oid="1.3.6.1.2.1.31.1.1.1.6.1",
        check_interval_seconds=60,
        rrd_data_source_type="counter",
        is_enabled=True,
    )
    assert ok is True
    assert "added" in msg.lower()

    oid = db_session.query(NetworkDeviceSnmpOid).filter_by(device_id=device.id).first()
    assert oid is not None

    def _fake_walk(*_args, **_kwargs):
        return ["SNMPv2-SMI::mib-2.2.2.1.10.1 = Counter32: 100"]

    from app.services import snmp_discovery as snmp_discovery_service

    original = snmp_discovery_service._run_snmpwalk
    snmp_discovery_service._run_snmpwalk = _fake_walk
    try:
        ok, msg = core_devices_forms.poll_snmp_oid_for_device(
            db_session, device_id=str(device.id), snmp_oid_id=str(oid.id)
        )
    finally:
        snmp_discovery_service._run_snmpwalk = original
    assert ok is True
    assert "succeeded" in msg.lower()


def test_create_bandwidth_graph_add_source_clone_and_public_toggle(db_session):
    device = NetworkDevice(name="Graph Core", mgmt_ip="192.0.2.120", snmp_enabled=True, is_active=True)
    db_session.add(device)
    db_session.flush()

    oid = NetworkDeviceSnmpOid(
        device_id=device.id,
        title="ifOutOctets",
        oid="1.3.6.1.2.1.31.1.1.1.10.5",
        is_enabled=True,
    )
    db_session.add(oid)
    db_session.commit()

    ok, msg = core_devices_forms.create_bandwidth_graph_for_device(
        db_session,
        device_id=str(device.id),
        title="Uplink",
        vertical_axis_title="Bandwidth",
        height_px=160,
        is_public=False,
    )
    assert ok is True
    assert "created" in msg.lower()

    graph = db_session.query(NetworkDeviceBandwidthGraph).filter_by(device_id=device.id).first()
    assert graph is not None
    assert graph.is_public is False

    ok, msg = core_devices_forms.add_bandwidth_graph_source(
        db_session,
        device_id=str(device.id),
        graph_id=str(graph.id),
        source_device_id=str(device.id),
        snmp_oid_id=str(oid.id),
        factor=1.0,
        color_hex="#22c55e",
        draw_type="LINE1",
        stack_enabled=False,
        value_unit="Bps",
    )
    assert ok is True
    assert "added" in msg.lower()
    assert (
        db_session.query(NetworkDeviceBandwidthGraphSource)
        .filter_by(graph_id=graph.id)
        .count()
        == 1
    )

    ok, _ = core_devices_forms.clone_bandwidth_graph_for_device(
        db_session,
        device_id=str(device.id),
        graph_id=str(graph.id),
    )
    assert ok is True
    cloned = (
        db_session.query(NetworkDeviceBandwidthGraph)
        .filter(NetworkDeviceBandwidthGraph.title.like("Uplink (Copy)%"))
        .first()
    )
    assert cloned is not None

    ok, _ = core_devices_forms.toggle_bandwidth_graph_public(
        db_session,
        device_id=str(device.id),
        graph_id=str(graph.id),
        is_public=True,
    )
    assert ok is True
    db_session.refresh(graph)
    assert graph.public_token is not None
    public_graph = core_devices_forms.get_public_bandwidth_graph(db_session, token=graph.public_token)
    assert public_graph is not None


def test_bandwidth_graph_preview_snapshot_uses_snmp_values(db_session):
    device = NetworkDevice(name="Preview Device", mgmt_ip="192.0.2.121", snmp_enabled=True, is_active=True)
    db_session.add(device)
    db_session.flush()

    oid = NetworkDeviceSnmpOid(
        device_id=device.id,
        title="ifInOctets",
        oid="1.3.6.1.2.1.31.1.1.1.6.7",
        is_enabled=True,
    )
    graph = NetworkDeviceBandwidthGraph(
        device_id=device.id,
        title="Preview Graph",
        vertical_axis_title="Bandwidth",
        height_px=150,
    )
    db_session.add_all([oid, graph])
    db_session.flush()
    db_session.add(
        NetworkDeviceBandwidthGraphSource(
            graph_id=graph.id,
            source_device_id=device.id,
            snmp_oid_id=oid.id,
            factor=2.0,
            color_hex="#ef4444",
            draw_type="LINE1",
            value_unit="Bps",
            sort_order=1,
        )
    )
    db_session.commit()

    from app.services import snmp_discovery as snmp_discovery_service

    def _fake_walk(*_args, **_kwargs):
        return ["SNMPv2-SMI::mib-2.2.2.1.10.7 = Counter32: 123"]

    original = snmp_discovery_service._run_snmpwalk
    snmp_discovery_service._run_snmpwalk = _fake_walk
    try:
        payload = core_devices_forms.bandwidth_graphs_page_data(
            db_session,
            str(device.id),
            preview_graph_id=str(graph.id),
        )
    finally:
        snmp_discovery_service._run_snmpwalk = original

    assert payload is not None
    assert payload["preview_rows"]
    assert payload["preview_rows"][0]["last"] == 246.0


def test_core_backup_settings_history_filter_and_compare(db_session):
    core = NetworkDevice(name="Backup Core", mgmt_ip="192.0.2.130", is_active=True)
    db_session.add(core)
    db_session.flush()
    nas = NasDevice(
        name="Backup NAS",
        vendor=NasVendor.mikrotik,
        ip_address="192.0.2.130",
        management_ip="192.0.2.130",
        network_device_id=core.id,
    )
    db_session.add(nas)
    db_session.flush()

    old_backup = NasConfigBackup(
        nas_device_id=nas.id,
        config_content="line_a\nline_b",
        config_hash="h1",
        created_at=datetime(2026, 2, 1, tzinfo=UTC),
    )
    new_backup = NasConfigBackup(
        nas_device_id=nas.id,
        config_content="line_a\nline_c",
        config_hash="h2",
        created_at=datetime(2026, 2, 20, tzinfo=UTC),
    )
    db_session.add_all([old_backup, new_backup])
    db_session.commit()

    ok, msg = core_devices_forms.update_backup_settings_for_device(
        db_session,
        device_id=str(core.id),
        enabled=True,
        ssh_username="backup-user",
        ssh_password="secret",
        ssh_port=2222,
        backup_type="commands",
        backup_commands="export",
        hours_csv="2,8,14,20",
    )
    assert ok is True
    assert "saved" in msg.lower()

    payload = core_devices_forms.backup_page_data(
        db_session,
        str(core.id),
        date_from="2026-02-10",
        date_to="2026-02-28",
    )
    assert payload is not None
    assert payload["nas_device"] is not None
    assert len(payload["backups"]) == 1
    assert payload["backups"][0].id == new_backup.id
    assert payload["backup_config"]["ssh_port"] == 2222
    assert payload["backup_config"]["hours_csv"] == "2,8,14,20"

    cmp_payload = core_devices_forms.backup_compare_page_data(
        db_session,
        device_id=str(core.id),
        backup_id_1=str(old_backup.id),
        backup_id_2=str(new_backup.id),
    )
    assert cmp_payload is not None
    assert cmp_payload["diff"]["added_lines"] >= 1
    assert cmp_payload["diff"]["removed_lines"] >= 1
    assert "@@" in cmp_payload["diff"]["unified_diff"]


def test_trigger_backup_for_core_device_without_nas_mapping_returns_error(db_session):
    core = NetworkDevice(name="Orphan Core", mgmt_ip="192.0.2.131", is_active=True)
    db_session.add(core)
    db_session.commit()

    ok, msg = core_devices_forms.trigger_backup_for_core_device(
        db_session,
        device_id=str(core.id),
    )
    assert ok is False
    assert "no linked nas device" in msg.lower()


def test_cpe_notes_metadata_round_trip():
    notes = "[winbox_host:192.0.2.1]\n[api_host:192.0.2.2]\n[api_port:8728]\n[api_user:admin]\nInstalled in suite A"
    meta, cleaned = web_network_cpes_service.parse_cpe_notes_metadata(notes)
    assert meta["winbox_host"] == "192.0.2.1"
    assert meta["api_host"] == "192.0.2.2"
    assert meta["api_port"] == "8728"
    assert meta["api_user"] == "admin"
    assert cleaned == "Installed in suite A"

    normalized = web_network_cpes_service.normalize_cpe_notes(
        notes=cleaned,
        winbox_host=meta["winbox_host"],
        api_host=meta["api_host"],
        api_port=meta["api_port"],
        api_user=meta["api_user"],
    )
    assert normalized is not None
    assert "[winbox_host:192.0.2.1]" in normalized
    assert "Installed in suite A" in normalized


def test_build_cpe_list_data_filters_and_stats(db_session, subscriber):
    other = Subscriber(
        first_name="Other",
        last_name="User",
        email=f"other-{datetime.now(UTC).timestamp()}@example.com",
    )
    db_session.add(other)
    db_session.commit()
    db_session.refresh(other)

    network_service.cpe_devices.create(
        db_session,
        CPEDeviceCreate(
            account_id=subscriber.id,
            serial_number="MK-1000",
            vendor="MikroTik",
            model="hAP ac2",
            mac_address="AA:BB:CC:DD:EE:01",
            notes="[api_host:192.0.2.20]\ncustomer edge",
        ),
    )
    network_service.cpe_devices.create(
        db_session,
        CPEDeviceCreate(
            account_id=other.id,
            serial_number="ZTE-2000",
            vendor="ZTE",
            model="F660",
            mac_address="AA:BB:CC:DD:EE:02",
        ),
    )

    data = web_network_cpes_service.build_cpe_list_data(
        db_session,
        search="MK-1000",
        status="active",
        vendor="mikrotik",
        subscriber_id=str(subscriber.id),
    )
    assert len(data["cpes"]) == 1
    assert data["cpes"][0].serial_number == "MK-1000"
    assert data["stats"]["total"] == 1
    assert data["stats"]["active"] == 1
    assert data["stats"]["mikrotik"] == 1


def test_tr069_dashboard_data_filters_and_stats(db_session, acs_server):
    device_seen = tr069_service.cpe_devices.create(
        db_session,
        Tr069CpeDeviceCreate(
            acs_server_id=acs_server.id,
            serial_number="ZTE-ONT-001",
            oui="A1B2C3",
            product_class="F680",
            is_active=True,
            last_inform_at=datetime.now(UTC),
        ),
    )
    tr069_service.cpe_devices.create(
        db_session,
        Tr069CpeDeviceCreate(
            acs_server_id=acs_server.id,
            serial_number="HUAWEI-002",
            oui="D4E5F6",
            product_class="HG8145X6",
            is_active=True,
            last_inform_at=datetime.now(UTC) - timedelta(days=3),
        ),
    )
    tr069_service.jobs.create(
        db_session,
        Tr069JobCreate(
            device_id=device_seen.id,
            name="Reboot Device",
            command="reboot",
            status=Tr069JobStatus.failed,
        ),
    )

    data = web_network_tr069_service.tr069_dashboard_data(
        db_session,
        acs_server_id=str(acs_server.id),
        search="zte",
        only_unlinked=True,
    )
    assert len(data["devices"]) == 1
    assert data["devices"][0].serial_number == "ZTE-ONT-001"
    assert data["stats"]["devices"] == 1
    assert data["stats"]["seen_24h"] == 1
    assert data["stats"]["jobs_failed"] >= 1


def test_tr069_queue_device_job_creates_and_executes(db_session, acs_server, monkeypatch):
    device = tr069_service.cpe_devices.create(
        db_session,
        Tr069CpeDeviceCreate(
            acs_server_id=acs_server.id,
            serial_number="TEST-TR069-003",
            oui="112233",
            product_class="ONT",
            is_active=True,
        ),
    )

    def _fake_execute(db, job_id: str):
        job = db.get(Tr069Job, job_id)
        job.status = Tr069JobStatus.succeeded
        db.commit()
        db.refresh(job)
        return job

    monkeypatch.setattr(tr069_service.jobs, "execute", _fake_execute)
    job = web_network_tr069_service.queue_device_job(
        db_session,
        tr069_device_id=str(device.id),
        action="refresh",
    )
    assert job.command == "refreshObject"
    assert job.status == Tr069JobStatus.succeeded


def test_network_map_context_includes_network_device_markers(db_session):
    pop = PopSite(
        name="Map POP",
        code="MAP-POP",
        latitude=9.05,
        longitude=7.49,
        is_active=True,
    )
    db_session.add(pop)
    db_session.flush()

    db_session.add(
        NetworkDevice(
            name="Core Router 1",
            pop_site_id=pop.id,
            role=DeviceRole.core,
            status=DeviceStatus.online,
            is_active=True,
        )
    )
    db_session.commit()

    context = network_map_service.build_network_map_context(db_session)
    device_features = [
        item
        for item in context["map_data"]["features"]
        if item.get("properties", {}).get("type") == "network_device"
    ]
    assert len(device_features) == 1
    assert device_features[0]["properties"]["name"] == "Core Router 1"
    assert device_features[0]["properties"]["status"] == "online"
    assert context["stats"]["network_devices"] == 1
    assert context["stats"]["network_devices_online"] == 1


def test_speedtest_form_parse_and_validate():
    values = web_network_speedtests_service.parse_speedtest_form(
        {
            "download_mbps": "98.5",
            "upload_mbps": "45.25",
            "latency_ms": "8.1",
            "source": "manual",
        }
    )
    assert values["download_mbps"] == 98.5
    assert values["upload_mbps"] == 45.25
    assert values["latency_ms"] == 8.1
    assert web_network_speedtests_service.validate_speedtest_values(values) is None

    bad = dict(values)
    bad["download_mbps"] = -1
    assert web_network_speedtests_service.validate_speedtest_values(bad) is not None


def test_speedtest_list_page_data_filters_and_stats(db_session, subscriber):
    pop = PopSite(
        name="Speed POP",
        code="SPD-POP",
        latitude=9.1,
        longitude=7.5,
        is_active=True,
    )
    db_session.add(pop)
    db_session.flush()

    device = NetworkDevice(
        name="Speed Device",
        pop_site_id=pop.id,
        role=DeviceRole.edge,
        status=DeviceStatus.online,
        is_active=True,
    )
    db_session.add(device)
    db_session.flush()

    db_session.add_all(
        [
            SpeedTestResult(
                subscriber_id=subscriber.id,
                network_device_id=device.id,
                pop_site_id=pop.id,
                source=SpeedTestSource.manual,
                target_label="Link A",
                provider="Ookla",
                server_name="Abuja-1",
                download_mbps=100,
                upload_mbps=50,
                latency_ms=10,
                tested_at=datetime.now(UTC),
            ),
            SpeedTestResult(
                subscriber_id=subscriber.id,
                network_device_id=device.id,
                pop_site_id=pop.id,
                source=SpeedTestSource.api,
                target_label="Link B",
                provider="LibreSpeed",
                server_name="Abuja-2",
                download_mbps=80,
                upload_mbps=40,
                latency_ms=20,
                tested_at=datetime.now(UTC),
            ),
        ]
    )
    db_session.commit()

    data = web_network_speedtests_service.list_page_data(
        db_session,
        search="ookla",
        subscriber_id=str(subscriber.id),
        network_device_id=str(device.id),
        source="manual",
    )
    assert len(data["results"]) == 1
    assert data["results"][0].provider == "Ookla"
    assert data["stats"]["total"] == 1
    assert data["stats"]["avg_download"] == 100.0


def test_dns_threat_form_parse_and_validate():
    values = web_network_dns_threats_service.parse_event_form(
        {
            "queried_domain": "malicious.example",
            "severity": "high",
            "action": "blocked",
            "confidence_score": "92.4",
        }
    )
    assert values["queried_domain"] == "malicious.example"
    assert values["confidence_score"] == 92.4
    assert web_network_dns_threats_service.validate_event_values(values) is None

    bad = dict(values)
    bad["queried_domain"] = ""
    assert web_network_dns_threats_service.validate_event_values(bad) is not None


def test_dns_threat_list_page_data_filters_and_stats(db_session, subscriber):
    pop = PopSite(name="DNS POP", code="DNS-POP", latitude=9.2, longitude=7.6, is_active=True)
    db_session.add(pop)
    db_session.flush()

    device = NetworkDevice(
        name="DNS Guard",
        pop_site_id=pop.id,
        role=DeviceRole.core,
        status=DeviceStatus.online,
        is_active=True,
    )
    db_session.add(device)
    db_session.flush()

    db_session.add_all(
        [
            DnsThreatEvent(
                subscriber_id=subscriber.id,
                network_device_id=device.id,
                pop_site_id=pop.id,
                queried_domain="phishing.example",
                source_ip="192.0.2.11",
                threat_category="phishing",
                severity=DnsThreatSeverity.critical,
                action=DnsThreatAction.blocked,
                confidence_score=97.2,
                occurred_at=datetime.now(UTC),
            ),
            DnsThreatEvent(
                subscriber_id=subscriber.id,
                network_device_id=device.id,
                pop_site_id=pop.id,
                queried_domain="telemetry.example",
                source_ip="192.0.2.12",
                threat_category="suspicious",
                severity=DnsThreatSeverity.low,
                action=DnsThreatAction.monitored,
                confidence_score=40.0,
                occurred_at=datetime.now(UTC),
            ),
        ]
    )
    db_session.commit()

    data = web_network_dns_threats_service.list_page_data(
        db_session,
        search="phishing",
        severity="critical",
        action="blocked",
        subscriber_id=str(subscriber.id),
        network_device_id=str(device.id),
    )
    assert len(data["events"]) == 1
    assert data["events"][0].queried_domain == "phishing.example"
    assert data["stats"]["total"] == 1
    assert data["stats"]["blocked"] == 1
    assert data["stats"]["critical"] == 1


def test_weathermap_data_builds_link_states_from_metrics(db_session, pop_site):
    parent = NetworkDevice(
        name="WM Parent",
        pop_site_id=pop_site.id,
        role=DeviceRole.core,
        status=DeviceStatus.online,
        is_active=True,
    )
    child = NetworkDevice(
        name="WM Child",
        pop_site_id=pop_site.id,
        role=DeviceRole.edge,
        status=DeviceStatus.online,
        parent_device=parent,
        is_active=True,
    )
    db_session.add_all([parent, child])
    db_session.flush()

    now = datetime.now(UTC)
    db_session.add_all(
        [
            DeviceMetric(
                device_id=child.id,
                metric_type=MetricType.rx_bps,
                value=600_000_000,  # 600 Mbps (high)
                recorded_at=now,
            ),
            DeviceMetric(
                device_id=child.id,
                metric_type=MetricType.tx_bps,
                value=120_000_000,  # 120 Mbps
                recorded_at=now,
            ),
        ]
    )
    db_session.commit()

    data = web_network_weathermap_service.build_weathermap_data(db_session)
    assert data["stats"]["nodes"] >= 2
    assert data["stats"]["links"] >= 1
    assert data["stats"]["high_links"] >= 1
    assert any(link["state"] == "high" for link in data["links"])


def test_core_device_validate_values_rejects_parent_cycle(db_session):
    parent = NetworkDevice(name="Parent A", is_active=True)
    child = NetworkDevice(name="Child B", is_active=True, parent_device=parent)
    db_session.add_all([parent, child])
    db_session.commit()

    values = {
        "name": "Parent A",
        "hostname": None,
        "mgmt_ip": None,
        "role_value": "edge",
        "device_type_value": "",
        "pop_site_id": None,
        "parent_device_id": str(child.id),
        "ping_enabled": False,
        "snmp_enabled": False,
        "snmp_port_value": "",
        "vendor": None,
        "model": None,
        "serial_number": None,
        "snmp_version": None,
        "snmp_community": None,
        "snmp_username": None,
        "snmp_auth_protocol": None,
        "snmp_auth_secret": None,
        "snmp_priv_protocol": None,
        "snmp_priv_secret": None,
        "notes": None,
        "is_active": True,
    }
    normalized, error = core_devices_forms.validate_values(
        db_session,
        values,
        current_device=parent,
    )
    assert normalized is None
    assert error is not None
    assert "cycle" in error.lower()


def test_ping_device_respects_notification_delay_before_offline(db_session, pop_site, monkeypatch):
    device = monitoring_service.network_devices.create(
        db_session,
        NetworkDeviceCreate(
            name="Delay Ping Device",
            pop_site_id=pop_site.id,
            mgmt_ip="192.0.2.80",
            notification_delay_minutes=5,
            status="online",
        ),
    )

    def _fake_run(*_args, **_kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="timeout")

    monkeypatch.setattr(core_runtime.subprocess, "run", _fake_run)
    updated, err, ok = core_runtime.ping_device(db_session, str(device.id))
    assert err is None
    assert ok is False
    assert updated is not None
    assert updated.status.value == "online"
    assert updated.ping_down_since is not None

    updated.ping_down_since = datetime.now(UTC) - timedelta(minutes=6)
    db_session.flush()
    updated2, err2, ok2 = core_runtime.ping_device(db_session, str(device.id))
    assert err2 is None
    assert ok2 is False
    assert updated2 is not None
    assert updated2.status.value == "offline"


def test_snmp_check_sets_degraded_after_delay(db_session, pop_site, monkeypatch):
    device = monitoring_service.network_devices.create(
        db_session,
        NetworkDeviceCreate(
            name="Delay SNMP Device",
            pop_site_id=pop_site.id,
            mgmt_ip="192.0.2.81",
            snmp_enabled=True,
            ping_enabled=False,
            notification_delay_minutes=5,
            status="online",
        ),
    )

    def _fake_walk(*_args, **_kwargs):
        raise RuntimeError("snmp fail")

    monkeypatch.setattr("app.services.snmp_discovery._run_snmpwalk", _fake_walk)

    updated, err = core_runtime.snmp_check_device(db_session, str(device.id))
    assert err is None
    assert updated is not None
    assert updated.status.value == "online"
    assert updated.snmp_down_since is not None

    updated.snmp_down_since = datetime.now(UTC) - timedelta(minutes=6)
    db_session.flush()
    updated2, err2 = core_runtime.snmp_check_device(db_session, str(device.id))
    assert err2 is None
    assert updated2 is not None
    assert updated2.status.value == "degraded"


def test_parent_status_rollup_from_child_ping_failure_and_recovery(
    db_session, pop_site, monkeypatch
):
    parent = monitoring_service.network_devices.create(
        db_session,
        NetworkDeviceCreate(
            name="Parent Node",
            pop_site_id=pop_site.id,
            mgmt_ip="192.0.2.90",
            notification_delay_minutes=0,
            status="online",
        ),
    )
    child = monitoring_service.network_devices.create(
        db_session,
        NetworkDeviceCreate(
            name="Child Node",
            pop_site_id=pop_site.id,
            parent_device_id=parent.id,
            mgmt_ip="192.0.2.91",
            notification_delay_minutes=0,
            status="online",
        ),
    )
    assert child.parent_device_id == parent.id

    def _fail_run(*_args, **_kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="timeout")

    monkeypatch.setattr(core_runtime.subprocess, "run", _fail_run)
    core_runtime.ping_device(db_session, str(child.id))
    db_session.refresh(child)
    assert child.status.value == "offline"
    db_session.refresh(parent)
    assert parent.status.value == "degraded"

    def _ok_run(*_args, **_kwargs):
        return SimpleNamespace(returncode=0, stdout="64 bytes time=1.1 ms", stderr="")

    monkeypatch.setattr(core_runtime.subprocess, "run", _ok_run)
    core_runtime.ping_device(db_session, str(child.id))
    db_session.refresh(parent)
    assert parent.status.value == "online"


def test_parent_devices_for_forms_scopes_by_site_and_excludes_descendants(db_session):
    site_a = PopSite(name="Site A", is_active=True)
    site_b = PopSite(name="Site B", is_active=True)
    db_session.add_all([site_a, site_b])
    db_session.flush()

    root = NetworkDevice(name="Root A", pop_site_id=site_a.id, is_active=True)
    child = NetworkDevice(name="Child A", pop_site_id=site_a.id, is_active=True, parent_device=root)
    grandchild = NetworkDevice(
        name="Grandchild A",
        pop_site_id=site_a.id,
        is_active=True,
        parent_device=child,
    )
    other_site = NetworkDevice(name="Other Site Root", pop_site_id=site_b.id, is_active=True)
    db_session.add_all([root, child, grandchild, other_site])
    db_session.commit()

    candidates = core_devices_forms.parent_devices_for_forms(
        db_session,
        current_device_id=root.id,
        pop_site_id=site_a.id,
    )
    candidate_ids = {device.id for device in candidates}
    assert child.id not in candidate_ids
    assert grandchild.id not in candidate_ids
    assert other_site.id not in candidate_ids


def test_backup_overview_page_data_classifies_and_filters(db_session):
    pop_site = PopSite(name="POP A", is_active=True)
    db_session.add(pop_site)
    db_session.flush()

    nas_fresh = nas_service.NasDevices.create(
        db_session,
        NasDeviceCreate(
            name="NAS Fresh",
            vendor=NasVendor.mikrotik,
            management_ip="192.0.2.10",
            pop_site_id=pop_site.id,
        ),
    )
    nas_stale = nas_service.NasDevices.create(
        db_session,
        NasDeviceCreate(
            name="NAS Stale",
            vendor=NasVendor.mikrotik,
            management_ip="192.0.2.11",
            pop_site_id=pop_site.id,
        ),
    )
    nas_failed = nas_service.NasDevices.create(
        db_session,
        NasDeviceCreate(
            name="NAS Failed",
            vendor=NasVendor.mikrotik,
            management_ip="192.0.2.12",
            pop_site_id=pop_site.id,
        ),
    )

    fresh_backup = nas_service.NasConfigBackups.create(
        db_session,
        nas_service.NasConfigBackupCreate(
            nas_device_id=nas_fresh.id,
            config_content="/export compact",
        ),
    )
    stale_backup = nas_service.NasConfigBackups.create(
        db_session,
        nas_service.NasConfigBackupCreate(
            nas_device_id=nas_stale.id,
            config_content="/export stale",
        ),
    )
    failed_backup = nas_service.NasConfigBackups.create(
        db_session,
        nas_service.NasConfigBackupCreate(
            nas_device_id=nas_failed.id,
            config_content="x",
            notes="Backup failed due to timeout",
        ),
    )
    stale_backup.created_at = datetime.now(UTC) - timedelta(hours=60)
    db_session.flush()

    olt = OLTDevice(name="OLT North", mgmt_ip="198.51.100.20", vendor="Huawei", model="MA5800")
    db_session.add(olt)
    db_session.flush()
    olt_backup = OltConfigBackup(
        olt_device_id=olt.id,
        backup_type=OltConfigBackupType.auto,
        file_path="olt/backup.txt",
        file_size_bytes=1024,
        created_at=datetime.now(UTC),
    )
    db_session.add(olt_backup)
    db_session.commit()

    page = core_devices_views.backup_overview_page_data(db_session, stale_hours=24)
    by_name = {row["device_name"]: row for row in page["rows"]}
    assert by_name["NAS Fresh"]["backup_status"] == "success"
    assert by_name["NAS Stale"]["backup_status"] == "stale"
    assert by_name["NAS Failed"]["backup_status"] == "failed"
    assert by_name["OLT North"]["backup_status"] == "success"
    assert page["stats"]["total"] == 4
    assert page["stats"]["stale"] == 1
    assert page["stats"]["failed"] == 1
    assert fresh_backup.id != stale_backup.id != failed_backup.id

    stale_only = core_devices_views.backup_overview_page_data(
        db_session,
        stale_hours=24,
        status="stale",
        device_type="nas",
    )
    assert stale_only["stats"]["total"] == 1
    assert stale_only["rows"][0]["device_name"] == "NAS Stale"


def test_backup_overview_page_data_search_and_sort(db_session):
    nas = nas_service.NasDevices.create(
        db_session,
        NasDeviceCreate(
            name="NAS Searchable",
            vendor=NasVendor.ubiquiti,
            management_ip="203.0.113.5",
        ),
    )
    backup = nas_service.NasConfigBackups.create(
        db_session,
        nas_service.NasConfigBackupCreate(
            nas_device_id=nas.id,
            config_content="/export verbose",
        ),
    )
    backup.created_at = datetime.now(UTC) - timedelta(hours=2)

    olt = OLTDevice(name="OLT Searchable", mgmt_ip="203.0.113.8")
    db_session.add(olt)
    db_session.flush()
    db_session.add(
        OltConfigBackup(
            olt_device_id=olt.id,
            backup_type=OltConfigBackupType.manual,
            file_path="olt/searchable.txt",
            file_size_bytes=2048,
            created_at=datetime.now(UTC) - timedelta(hours=1),
        )
    )
    db_session.commit()

    filtered = core_devices_views.backup_overview_page_data(
        db_session,
        search="203.0.113.5",
        sort="last_backup_desc",
    )
    assert filtered["stats"]["total"] == 1
    assert filtered["rows"][0]["device_name"] == "NAS Searchable"


def test_queue_backup_failure_notification_queues_notification(db_session, monkeypatch):
    values = {
        "alert_notifications_enabled": True,
        "alert_notifications_default_recipient": "noc@example.com",
        "alert_notifications_default_channel": "email",
    }

    def _fake_resolve(_db, _domain, key):
        return values.get(key)

    monkeypatch.setattr(backup_alerts_service.settings_spec, "resolve_value", _fake_resolve)

    queued = backup_alerts_service.queue_backup_failure_notification(
        db_session,
        device_kind="olt",
        device_name="OLT Alpha",
        device_ip="198.51.100.1",
        error_message="timeout",
        run_type="scheduled",
    )
    assert queued is True
    db_session.commit()

    items = db_session.query(Notification).all()
    assert len(items) == 1
    assert items[0].channel == NotificationChannel.email
    assert items[0].status == NotificationStatus.queued
    assert items[0].recipient == "noc@example.com"
    assert "Backup Failure" in (items[0].subject or "")


def test_queue_backup_failure_notification_no_recipient_noop(db_session, monkeypatch):
    values = {
        "alert_notifications_enabled": True,
        "alert_notifications_default_recipient": "",
        "alert_notifications_default_channel": "email",
    }

    def _fake_resolve(_db, _domain, key):
        return values.get(key)

    monkeypatch.setattr(backup_alerts_service.settings_spec, "resolve_value", _fake_resolve)

    queued = backup_alerts_service.queue_backup_failure_notification(
        db_session,
        device_kind="nas",
        device_name="NAS Beta",
        device_ip="203.0.113.10",
        error_message="auth failed",
        run_type="manual",
    )
    assert queued is False
    assert db_session.query(Notification).count() == 0


def test_list_olt_backups_orders_desc_and_filters(db_session):
    olt = OLTDevice(name="OLT Backups", mgmt_ip="198.51.100.40")
    db_session.add(olt)
    db_session.flush()

    old = OltConfigBackup(
        olt_device_id=olt.id,
        backup_type=OltConfigBackupType.auto,
        file_path="olt/old.txt",
        file_size_bytes=100,
        created_at=datetime.now(UTC) - timedelta(days=2),
    )
    recent = OltConfigBackup(
        olt_device_id=olt.id,
        backup_type=OltConfigBackupType.manual,
        file_path="olt/recent.txt",
        file_size_bytes=200,
        created_at=datetime.now(UTC) - timedelta(hours=2),
    )
    db_session.add_all([old, recent])
    db_session.commit()

    rows = web_network_olts_service.list_olt_backups(db_session, olt_id=str(olt.id))
    assert [row.file_path for row in rows] == ["olt/recent.txt", "olt/old.txt"]

    filtered = web_network_olts_service.list_olt_backups(
        db_session,
        olt_id=str(olt.id),
        start_at=datetime.now(UTC) - timedelta(hours=12),
    )
    assert [row.file_path for row in filtered] == ["olt/recent.txt"]


def test_olt_backup_file_resolution_and_preview(db_session, monkeypatch, tmp_path: Path):
    monkeypatch.setenv("OLT_BACKUP_DIR", str(tmp_path))
    olt = OLTDevice(name="OLT Files", mgmt_ip="198.51.100.41")
    db_session.add(olt)
    db_session.flush()

    relative = "device1/snapshot.txt"
    full = tmp_path / relative
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text("line1\nline2\nline3\n")

    backup = OltConfigBackup(
        olt_device_id=olt.id,
        backup_type=OltConfigBackupType.auto,
        file_path=relative,
        file_size_bytes=full.stat().st_size,
    )
    db_session.add(backup)
    db_session.commit()

    resolved = web_network_olts_service.backup_file_path(backup)
    assert resolved == full.resolve()
    preview = web_network_olts_service.read_backup_preview(backup, limit_chars=8)
    assert preview == "line1\nli"

    bad = OltConfigBackup(
        olt_device_id=olt.id,
        backup_type=OltConfigBackupType.auto,
        file_path="../escape.txt",
        file_size_bytes=10,
    )
    with pytest.raises(HTTPException):
        web_network_olts_service.backup_file_path(bad)


def test_compare_olt_backups_returns_diff(db_session, monkeypatch, tmp_path: Path):
    monkeypatch.setenv("OLT_BACKUP_DIR", str(tmp_path))
    olt = OLTDevice(name="OLT Compare", mgmt_ip="198.51.100.42")
    db_session.add(olt)
    db_session.flush()

    path1 = tmp_path / "compare" / "b1.txt"
    path2 = tmp_path / "compare" / "b2.txt"
    path1.parent.mkdir(parents=True, exist_ok=True)
    path1.write_text("line-a\nline-b\n")
    path2.write_text("line-a\nline-c\n")

    b1 = OltConfigBackup(
        olt_device_id=olt.id,
        backup_type=OltConfigBackupType.auto,
        file_path="compare/b1.txt",
        file_size_bytes=path1.stat().st_size,
    )
    b2 = OltConfigBackup(
        olt_device_id=olt.id,
        backup_type=OltConfigBackupType.manual,
        file_path="compare/b2.txt",
        file_size_bytes=path2.stat().st_size,
    )
    db_session.add_all([b1, b2])
    db_session.commit()

    _, _, diff = web_network_olts_service.compare_olt_backups(
        db_session,
        str(b1.id),
        str(b2.id),
    )
    assert isinstance(diff["unified_diff"], str)
    assert diff["added_lines"] >= 1
    assert diff["removed_lines"] >= 1


def test_compare_olt_backups_rejects_cross_olt(db_session):
    olt1 = OLTDevice(name="OLT 1", mgmt_ip="198.51.100.43")
    olt2 = OLTDevice(name="OLT 2", mgmt_ip="198.51.100.44")
    db_session.add_all([olt1, olt2])
    db_session.flush()
    b1 = OltConfigBackup(
        olt_device_id=olt1.id,
        backup_type=OltConfigBackupType.auto,
        file_path="x1.txt",
        file_size_bytes=1,
    )
    b2 = OltConfigBackup(
        olt_device_id=olt2.id,
        backup_type=OltConfigBackupType.auto,
        file_path="x2.txt",
        file_size_bytes=1,
    )
    db_session.add_all([b1, b2])
    db_session.commit()

    with pytest.raises(HTTPException):
        web_network_olts_service.compare_olt_backups(
            db_session,
            str(b1.id),
            str(b2.id),
        )


def test_test_olt_connection_and_test_backup(db_session, monkeypatch, tmp_path: Path):
    monkeypatch.setenv("OLT_BACKUP_DIR", str(tmp_path))
    olt = OLTDevice(name="OLT Test Actions", mgmt_ip="198.51.100.50")
    db_session.add(olt)
    db_session.commit()

    monkeypatch.setattr(
        web_network_olts_service,
        "fetch_running_config",
        lambda _olt: "sysName = OLT\nsysDescr = Device\n",
    )

    ok, message = web_network_olts_service.test_olt_connection(db_session, str(olt.id))
    assert ok is True
    assert "successful" in message.lower()

    backup, backup_message = web_network_olts_service.run_test_backup(db_session, str(olt.id))
    assert backup is not None
    assert "successfully" in backup_message.lower()
    assert backup.backup_type == OltConfigBackupType.manual
    assert (tmp_path / backup.file_path).exists()


def test_test_olt_connection_handles_missing_ip(db_session):
    olt = OLTDevice(name="OLT Missing IP", mgmt_ip=None)
    db_session.add(olt)
    db_session.commit()

    ok, message = web_network_olts_service.test_olt_connection(db_session, str(olt.id))
    assert ok is False
    assert "management ip" in message.lower()


def test_ip_pool_create_rejects_overlapping_cidr(db_session):
    existing, err = web_network_ip_service.create_ip_pool(
        db_session,
        {
            "name": "Pool-A",
            "ip_version": "ipv4",
            "cidr": "10.0.0.0/24",
            "gateway": None,
            "dns_primary": None,
            "dns_secondary": None,
            "notes": None,
            "is_active": True,
        },
    )
    assert err is None
    assert existing is not None

    created, error = web_network_ip_service.create_ip_pool(
        db_session,
        {
            "name": "Pool-B",
            "ip_version": "ipv4",
            "cidr": "10.0.0.128/25",
            "gateway": None,
            "dns_primary": None,
            "dns_secondary": None,
            "notes": None,
            "is_active": True,
        },
    )
    assert created is None
    assert error is not None
    assert "overlaps existing pool" in error.lower()


def test_ip_pool_update_rejects_overlapping_cidr(db_session):
    first, err1 = web_network_ip_service.create_ip_pool(
        db_session,
        {
            "name": "Pool-1",
            "ip_version": "ipv4",
            "cidr": "172.16.0.0/24",
            "gateway": None,
            "dns_primary": None,
            "dns_secondary": None,
            "notes": None,
            "is_active": True,
        },
    )
    second, err2 = web_network_ip_service.create_ip_pool(
        db_session,
        {
            "name": "Pool-2",
            "ip_version": "ipv4",
            "cidr": "172.16.1.0/24",
            "gateway": None,
            "dns_primary": None,
            "dns_secondary": None,
            "notes": None,
            "is_active": True,
        },
    )
    assert err1 is None and err2 is None
    assert first is not None and second is not None

    updated, changes, error = web_network_ip_service.update_ip_pool(
        db_session,
        pool_id=str(second.id),
        values={
            "name": "Pool-2",
            "ip_version": "ipv4",
            "cidr": "172.16.0.128/25",
            "gateway": None,
            "dns_primary": None,
            "dns_secondary": None,
            "notes": None,
            "is_active": True,
        },
    )
    assert updated is None
    assert changes is None
    assert error is not None
    assert "overlaps existing pool" in error.lower()


def test_import_ip_pools_csv_creates_valid_rows_and_reports_errors(db_session):
    csv_text = (
        "name,cidr,ip_version,gateway,is_active\n"
        "Import A,10.20.0.0/24,ipv4,10.20.0.1,true\n"
        "Import Bad,,ipv4,10.20.1.1,true\n"
    )
    result = web_network_ip_service.import_ip_pools_csv(
        db_session,
        csv_text=csv_text,
        default_ip_version="ipv4",
    )
    assert result["total_rows"] == 2
    assert len(result["created"]) == 1
    assert len(result["errors"]) == 1
    assert "cidr block is required" in str(result["errors"][0]["error"]).lower()


def test_import_ip_pools_csv_reports_overlap_errors(db_session):
    created, error = web_network_ip_service.create_ip_pool(
        db_session,
        {
            "name": "Existing",
            "ip_version": "ipv4",
            "cidr": "10.30.0.0/24",
            "gateway": None,
            "dns_primary": None,
            "dns_secondary": None,
            "notes": None,
            "is_active": True,
        },
    )
    assert created is not None
    assert error is None

    csv_text = "name,cidr,ip_version\nOverlap,10.30.0.128/25,ipv4\n"
    result = web_network_ip_service.import_ip_pools_csv(
        db_session,
        csv_text=csv_text,
        default_ip_version="ipv4",
    )
    assert len(result["created"]) == 0
    assert len(result["errors"]) == 1
    assert "overlaps existing pool" in str(result["errors"][0]["error"]).lower()


def test_create_ip_block_rejects_overlap_and_outside_pool(db_session):
    pool, pool_error = web_network_ip_service.create_ip_pool(
        db_session,
        {
            "name": "Block Pool",
            "ip_version": "ipv4",
            "cidr": "10.40.0.0/24",
            "gateway": None,
            "dns_primary": None,
            "dns_secondary": None,
            "notes": None,
            "is_active": True,
        },
    )
    assert pool is not None
    assert pool_error is None

    block_ok, err_ok = web_network_ip_service.create_ip_block(
        db_session,
        {
            "pool_id": str(pool.id),
            "cidr": "10.40.0.0/25",
            "notes": None,
            "is_active": True,
        },
    )
    assert block_ok is not None
    assert err_ok is None

    block_overlap, err_overlap = web_network_ip_service.create_ip_block(
        db_session,
        {
            "pool_id": str(pool.id),
            "cidr": "10.40.0.64/26",
            "notes": None,
            "is_active": True,
        },
    )
    assert block_overlap is None
    assert err_overlap is not None
    assert "overlaps existing block" in err_overlap.lower()

    block_outside, err_outside = web_network_ip_service.create_ip_block(
        db_session,
        {
            "pool_id": str(pool.id),
            "cidr": "10.41.0.0/24",
            "notes": None,
            "is_active": True,
        },
    )
    assert block_outside is None
    assert err_outside is not None
    assert "must be inside pool cidr" in err_outside.lower()


def test_parse_ip_pool_form_supports_fallback_flag():
    form = {
        "name": "Fallback Pool",
        "ip_version": "ipv4",
        "cidr": "10.50.0.0/24",
        "is_fallback": "true",
        "is_active": "true",
    }
    parsed = web_network_ip_service.parse_ip_pool_form(form)
    assert parsed["is_fallback"] is True
    assert parsed["is_active"] is True


def test_create_ip_pool_normalizes_fallback_notes(db_session):
    created, error = web_network_ip_service.create_ip_pool(
        db_session,
        {
            "name": "Fallback Tagged",
            "ip_version": "ipv4",
            "cidr": "10.51.0.0/24",
            "gateway": None,
            "dns_primary": None,
            "dns_secondary": None,
            "notes": "overflow pool",
            "is_fallback": True,
            "is_active": True,
        },
    )
    assert error is None
    assert created is not None
    assert created.notes is not None
    assert "[fallback]" in created.notes.lower()
    assert "overflow pool" in created.notes


def test_build_ip_pools_data_filters_fallback_and_standard(db_session):
    fallback, fallback_err = web_network_ip_service.create_ip_pool(
        db_session,
        {
            "name": "Fallback Only",
            "ip_version": "ipv4",
            "cidr": "10.52.0.0/24",
            "gateway": None,
            "dns_primary": None,
            "dns_secondary": None,
            "notes": "fallback note",
            "is_fallback": True,
            "is_active": True,
        },
    )
    standard, standard_err = web_network_ip_service.create_ip_pool(
        db_session,
        {
            "name": "Standard Only",
            "ip_version": "ipv4",
            "cidr": "10.53.0.0/24",
            "gateway": None,
            "dns_primary": None,
            "dns_secondary": None,
            "notes": None,
            "is_fallback": False,
            "is_active": True,
        },
    )
    assert fallback_err is None and standard_err is None
    assert fallback is not None and standard is not None

    fallback_state = web_network_ip_service.build_ip_pools_data(db_session, pool_type="fallback")
    fallback_names = {pool.name for pool in fallback_state["pools"]}
    assert fallback_names == {"Fallback Only"}
    assert fallback_state["stats"]["fallback_pools"] == 1

    standard_state = web_network_ip_service.build_ip_pools_data(db_session, pool_type="standard")
    standard_names = {pool.name for pool in standard_state["pools"]}
    assert "Standard Only" in standard_names
    assert "Fallback Only" not in standard_names


def test_build_ip_pools_data_tracks_ipv6_utilization(db_session):
    pool, error = web_network_ip_service.create_ip_pool(
        db_session,
        {
            "name": "IPv6 Util Pool",
            "ip_version": "ipv6",
            "cidr": "2001:db8:abcd::/126",
            "gateway": None,
            "dns_primary": None,
            "dns_secondary": None,
            "notes": None,
            "is_fallback": False,
            "is_active": True,
        },
    )
    assert error is None
    assert pool is not None

    network_service.ipv6_addresses.create(
        db_session,
        IPv6AddressCreate(address="2001:db8:abcd::1", pool_id=pool.id, is_reserved=False),
    )
    network_service.ipv6_addresses.create(
        db_session,
        IPv6AddressCreate(address="2001:db8:abcd::2", pool_id=pool.id, is_reserved=False),
    )

    state = web_network_ip_service.build_ip_pools_data(db_session, pool_type="all")
    util = state["pool_utilization"][str(pool.id)]
    assert util["total"] == 4
    assert util["used"] == 2
    assert util["percent"] == 50


def test_pool_form_snapshot_from_model_parses_metadata(db_session):
    pool, error = web_network_ip_service.create_ip_pool(
        db_session,
        {
            "name": "Meta IPv6 Pool",
            "ip_version": "ipv6",
            "cidr": "2001:db8:ffff::/64",
            "gateway": None,
            "dns_primary": None,
            "dns_secondary": None,
            "notes": "main fabric",
            "location": "Abuja POP",
            "category": "Production",
            "network_type": "Infrastructure",
            "router": "Core-v6-1",
            "is_fallback": True,
            "is_active": True,
        },
    )
    assert error is None
    assert pool is not None

    snapshot = web_network_ip_service.pool_form_snapshot_from_model(pool)
    assert snapshot["location"] == "Abuja POP"
    assert snapshot["category"] == "Production"
    assert snapshot["network_type"] == "Infrastructure"
    assert snapshot["router"] == "Core-v6-1"
    assert snapshot["is_fallback"] is True
    assert snapshot["notes"] == "main fabric"


def test_build_ipv6_networks_data_filters_and_sorts(db_session):
    pool_a, err_a = web_network_ip_service.create_ip_pool(
        db_session,
        {
            "name": "IPv6 Abuja",
            "ip_version": "ipv6",
            "cidr": "2001:db8:a::/64",
            "gateway": None,
            "dns_primary": None,
            "dns_secondary": None,
            "notes": None,
            "location": "Abuja",
            "category": "Production",
            "network_type": "EndNet",
            "router": "R1",
            "is_fallback": False,
            "is_active": True,
        },
    )
    pool_b, err_b = web_network_ip_service.create_ip_pool(
        db_session,
        {
            "name": "IPv6 Kano",
            "ip_version": "ipv6",
            "cidr": "2001:db8:b::/64",
            "gateway": None,
            "dns_primary": None,
            "dns_secondary": None,
            "notes": None,
            "location": "Kano",
            "category": "Dev",
            "network_type": "Infrastructure",
            "router": "R2",
            "is_fallback": False,
            "is_active": True,
        },
    )
    assert err_a is None and err_b is None
    assert pool_a is not None and pool_b is not None

    filtered = web_network_ip_service.build_ipv6_networks_data(
        db_session,
        location="Abuja",
        category="Production",
        sort_by="title",
        sort_dir="asc",
    )
    assert len(filtered["networks"]) == 1
    assert filtered["networks"][0]["pool"].name == "IPv6 Abuja"

    sorted_state = web_network_ip_service.build_ipv6_networks_data(
        db_session,
        sort_by="title",
        sort_dir="desc",
    )
    names = [item["pool"].name for item in sorted_state["networks"]]
    assert names == sorted(names, reverse=True)


def test_build_ipv4_networks_data_filters_sorts_and_utilization(db_session):
    pool_a, err_a = web_network_ip_service.create_ip_pool(
        db_session,
        {
            "name": "IPv4 Abuja",
            "ip_version": "ipv4",
            "cidr": "10.70.0.0/24",
            "gateway": "10.70.0.1",
            "dns_primary": None,
            "dns_secondary": None,
            "notes": None,
            "location": "Abuja",
            "category": "Production",
            "network_type": "EndNet",
            "router": "RTR-A",
            "usage_type": "Static",
            "allow_network_broadcast": False,
            "is_fallback": False,
            "is_active": True,
        },
    )
    pool_b, err_b = web_network_ip_service.create_ip_pool(
        db_session,
        {
            "name": "IPv4 Kano",
            "ip_version": "ipv4",
            "cidr": "10.71.0.0/24",
            "gateway": "10.71.0.1",
            "dns_primary": None,
            "dns_secondary": None,
            "notes": None,
            "location": "Kano",
            "category": "Dev",
            "network_type": "Infrastructure",
            "router": "RTR-B",
            "usage_type": "Dynamic/DHCP",
            "allow_network_broadcast": True,
            "is_fallback": False,
            "is_active": True,
        },
    )
    assert err_a is None and err_b is None
    assert pool_a is not None and pool_b is not None

    network_service.ipv4_addresses.create(
        db_session,
        IPv4AddressCreate(address="10.70.0.10", pool_id=pool_a.id, is_reserved=False),
    )
    network_service.ipv4_addresses.create(
        db_session,
        IPv4AddressCreate(address="10.70.0.11", pool_id=pool_a.id, is_reserved=False),
    )

    filtered = web_network_ip_service.build_ipv4_networks_data(
        db_session,
        location="Abuja",
        category="Production",
        network_type="EndNet",
        sort_by="title",
        sort_dir="asc",
    )
    assert len(filtered["networks"]) == 1
    net = filtered["networks"][0]
    assert net["pool"].name == "IPv4 Abuja"
    assert net["subnet_mask"] == "255.255.255.0"
    assert net["utilization"]["used"] == 2
    assert net["usage_type"] == "Static"

    sorted_state = web_network_ip_service.build_ipv4_networks_data(
        db_session,
        sort_by="title",
        sort_dir="desc",
    )
    names = [item["pool"].name for item in sorted_state["networks"]]
    assert names == sorted(names, reverse=True)


def test_build_ipv4_network_detail_data_exposes_assignment_rows(db_session, subscriber):
    pool, err = web_network_ip_service.create_ip_pool(
        db_session,
        {
            "name": "IPv4 Detail",
            "ip_version": "ipv4",
            "cidr": "10.72.0.0/29",
            "gateway": "10.72.0.1",
            "dns_primary": None,
            "dns_secondary": None,
            "notes": None,
            "location": "Abuja",
            "category": "Production",
            "network_type": "EndNet",
            "router": "RTR-D",
            "usage_type": "Static",
            "allow_network_broadcast": False,
            "is_fallback": False,
            "is_active": True,
        },
    )
    assert err is None
    assert pool is not None

    ip_record = network_service.ipv4_addresses.create(
        db_session,
        IPv4AddressCreate(address="10.72.0.2", pool_id=pool.id, is_reserved=False),
    )
    reserved = network_service.ipv4_addresses.create(
        db_session,
        IPv4AddressCreate(address="10.72.0.3", pool_id=pool.id, is_reserved=True),
    )
    network_service.ip_assignments.create(
        db_session,
        IPAssignmentCreate(
            account_id=subscriber.id,
            ip_version="ipv4",
            ipv4_address_id=ip_record.id,
            prefix_length=29,
            gateway="10.72.0.1",
            dns_primary="8.8.8.8",
            dns_secondary="8.8.4.4",
        ),
    )

    data = web_network_ip_service.build_ipv4_network_detail_data(
        db_session,
        pool_id=str(pool.id),
        limit=16,
    )
    assert data is not None
    statuses = {row["ip_address"]: row["status"] for row in data["ip_rows"]}
    assert statuses["10.72.0.2"] == "assigned"
    assert statuses["10.72.0.3"] == "reserved"
    assert "10.72.0.4" in statuses and statuses["10.72.0.4"] == "available"
    assert data["usage_type"] == "Static"
    assert data["allow_network_broadcast"] is False
    assert reserved is not None


def test_parse_ipv6_network_form_maps_to_pool_payload():
    values = web_network_ip_service.parse_ipv6_network_form(
        {
            "title": "Abuja IPv6",
            "network": "2001:db8:1::",
            "prefix_length": "48",
            "comment": "Core aggregation",
            "location": "Abuja",
            "category": "Production",
            "network_type": "EndNet",
            "usage_type": "Static",
            "router": "RTR-v6",
            "gateway": "2001:db8:1::1",
            "dns_primary": "2001:4860:4860::8888",
            "dns_secondary": "2001:4860:4860::8844",
            "is_active": "true",
        }
    )
    assert values["name"] == "Abuja IPv6"
    assert values["ip_version"] == "ipv6"
    assert values["cidr"] == "2001:db8:1::/48"
    assert values["usage_type"] == "Static"
    assert values["is_active"] is True


def test_create_ipv6_network_from_dedicated_form_values(db_session):
    values = web_network_ip_service.parse_ipv6_network_form(
        {
            "title": "Kano IPv6",
            "network": "2001:db8:2::",
            "prefix_length": "56",
            "comment": "Distribution",
            "location": "Kano",
            "category": "Dev",
            "network_type": "EndNet",
            "usage_type": "Dynamic/DHCP",
            "router": "RTR-v6-kano",
            "is_active": "true",
        }
    )
    pool, err = web_network_ip_service.create_ip_pool(db_session, values)
    assert err is None
    assert pool is not None
    snapshot = web_network_ip_service.pool_form_snapshot_from_model(pool)
    assert snapshot["ip_version"]["value"] == "ipv6"
    assert snapshot["cidr"] == "2001:db8:2::/56"
    assert snapshot["usage_type"] == "Dynamic/DHCP"
    assert snapshot["location"] == "Kano"


def test_build_dual_stack_data_groups_ipv4_and_ipv6_by_subscriber_location(
    db_session,
    subscriber,
):
    address = Address(
        subscriber_id=subscriber.id,
        address_type=AddressType.service,
        address_line1="10 Main Street",
        city="Abuja",
        region="FCT",
        is_primary=True,
    )
    db_session.add(address)
    db_session.commit()
    db_session.refresh(address)

    pool4, err4 = web_network_ip_service.create_ip_pool(
        db_session,
        {
            "name": "Dual v4",
            "ip_version": "ipv4",
            "cidr": "10.60.0.0/24",
            "gateway": None,
            "dns_primary": None,
            "dns_secondary": None,
            "notes": None,
            "is_active": True,
        },
    )
    pool6, err6 = web_network_ip_service.create_ip_pool(
        db_session,
        {
            "name": "Dual v6",
            "ip_version": "ipv6",
            "cidr": "2001:db8:60::/64",
            "gateway": None,
            "dns_primary": None,
            "dns_secondary": None,
            "notes": None,
            "is_active": True,
        },
    )
    assert err4 is None and err6 is None
    assert pool4 is not None and pool6 is not None

    v4 = network_service.ipv4_addresses.create(
        db_session,
        IPv4AddressCreate(address="10.60.0.10", pool_id=pool4.id, is_reserved=False),
    )
    v6 = network_service.ipv6_addresses.create(
        db_session,
        IPv6AddressCreate(address="2001:db8:60::10", pool_id=pool6.id, is_reserved=False),
    )

    network_service.ip_assignments.create(
        db_session,
        IPAssignmentCreate(
            account_id=subscriber.id,
            service_address_id=address.id,
            ip_version="ipv4",
            ipv4_address_id=v4.id,
            is_active=True,
        ),
    )
    network_service.ip_assignments.create(
        db_session,
        IPAssignmentCreate(
            account_id=subscriber.id,
            service_address_id=address.id,
            ip_version="ipv6",
            ipv6_address_id=v6.id,
            prefix_length=64,
            is_active=True,
        ),
    )

    state = web_network_ip_service.build_dual_stack_data(
        db_session,
        view_mode="location",
        location_query="abuja",
    )
    assert state["stats"]["total_records"] == 1
    assert state["stats"]["dual_stack_records"] == 1
    assert len(state["rows"]) == 1
    row = state["rows"][0]
    assert row["is_dual_stack"] is True
    assert row["ipv4_address"] == "10.60.0.10"
    assert row["ipv6_address"] == "2001:db8:60::10/64"
    assert "Abuja" in (row["location"] or "")
