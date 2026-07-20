from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from app.models.forwarding_topology import ForwardingControlObservation
from app.models.network_monitoring import DeviceInterface, NetworkDevice, PopSite
from app.models.router_management import Router
from app.services import control_registry
from app.services.network.forwarding_observation_collector import (
    RouterOSForwardingObservationReader,
    collect_forwarding_control_observations,
)
from app.services.network.forwarding_topology import (
    execute_forwarding_topology_decision,
    preview_forwarding_topology_decision,
    propose_forwarding_topology_decision,
    reconcile_forwarding_topology,
    review_forwarding_topology_decision,
)

NOW = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)


class FakeReader:
    def __init__(self, *, sessions, addresses, routes):
        self.sessions = sessions
        self.addresses = addresses
        self.route_rows = routes
        self.route_requests: list[str] = []

    def bgp_sessions(self, _router):
        return self.sessions

    def interface_addresses(self, _router, *, family):
        return self.addresses.get(family, [])

    def routes(self, _router, *, prefix):
        self.route_requests.append(prefix)
        return self.route_rows.get(prefix, [])


def _border_target(db):
    site = PopSite(name=f"Border site {uuid.uuid4()}")
    device = NetworkDevice(
        name=f"Border {uuid.uuid4()}",
        pop_site=site,
        is_active=True,
    )
    interface = DeviceInterface(device=device, name="sfp-sfpplus1")
    db.add_all([site, device, interface])
    db.flush()
    router = Router(
        name=f"Router {uuid.uuid4()}",
        hostname=f"router-{uuid.uuid4()}",
        management_ip="192.0.2.254",
        rest_api_username="test",
        rest_api_password="test",
        network_device_id=device.id,
        is_active=True,
    )
    db.add(router)
    db.flush()
    path_key = f"border:test:{uuid.uuid4()}"
    payload = {
        "configuration_intent_ref": f"routeros-intent:{path_key}",
        "configuration_owner": "network.routeros_sot",
        "downstream_device_id": str(device.id),
        "downstream_interface_id": str(interface.id),
        "downstream_pop_site_id": str(site.id),
        "downstream_role": "border",
        "next_hop_ip": "192.0.2.1",
        "path_key": path_key,
        "path_kind": "border_peer",
        "peer_asn": 64520,
        "peer_ip": "192.0.2.2",
        "preference": 100,
        "route_prefix": "0.0.0.0/0",
        "vrf_name": "main",
    }
    preview = preview_forwarding_topology_decision(
        db,
        action="declare",
        declaration=payload,
        path_key=path_key,
        reason="collector test",
        proposed_by="test:proposer",
    )
    decision = propose_forwarding_topology_decision(
        db,
        action="declare",
        declaration=payload,
        path_key=path_key,
        reason="collector test",
        proposed_by="test:proposer",
        expected_decision_sha256=preview.decision_sha256,
        commit=False,
    )
    review_forwarding_topology_decision(
        db,
        decision.id,
        action="approve",
        reviewed_by="test:reviewer",
        review_notes="exact collector fixture",
        expected_decision_sha256=decision.decision_sha256,
        commit=False,
    )
    execute_forwarding_topology_decision(
        db,
        decision.id,
        executed_by="test:executor",
        expected_decision_sha256=decision.decision_sha256,
        commit=False,
    )
    return device, interface, router


def _reader(*, route_next_hop="192.0.2.1", established="true"):
    return FakeReader(
        sessions=[
            {
                ".id": "*B1",
                "established": established,
                "local.address": "198.51.100.2",
                "remote.address": "192.0.2.2",
                "remote.as": "64520",
                "routing-table": "main",
            }
        ],
        addresses={
            4: [
                {
                    ".id": "*A1",
                    "address": "198.51.100.2/30",
                    "disabled": "false",
                    "interface": "sfp-sfpplus1",
                    "invalid": "false",
                }
            ]
        },
        routes={
            "0.0.0.0/0": [
                {
                    ".id": "*R1",
                    "active": "true",
                    "disabled": "false",
                    "dst-address": "0.0.0.0/0",
                    "filtered": "false",
                    "immediate-gw": f"{route_next_hop}%sfp-sfpplus1",
                    "routing-table": "main",
                    "unreachable": "false",
                }
            ]
        },
    )


def test_collector_submits_exact_expiring_bgp_and_route_facts(db_session):
    device, interface, _ = _border_target(db_session)
    reader = _reader()

    result = collect_forwarding_control_observations(
        db_session,
        reader=reader,
        observed_at=NOW,
        ttl_seconds=900,
        collector_run_id="collector-run-exact",
    )

    observations = (
        db_session.query(ForwardingControlObservation)
        .order_by(ForwardingControlObservation.source_type)
        .all()
    )
    assert result["observations_submitted"] == 2
    assert result["failures"] == []
    assert reader.route_requests == ["0.0.0.0/0"]
    assert {row.source_type for row in observations} == {
        "bgp_peer",
        "routing_table",
    }
    assert {row.device_id for row in observations} == {device.id}
    assert {row.interface_id for row in observations} == {interface.id}
    assert {row.vrf_name for row in observations} == {"main"}
    assert {row.collector for row in observations} == {"routeros:forwarding-control-v1"}
    assert all(row.expires_at > row.observed_at for row in observations)
    report = reconcile_forwarding_topology(db_session, as_of=NOW)
    assert report.state_counts["agreement"] == 1
    assert sum(report.state_counts.values()) == 1


def test_collector_preserves_conflicting_next_hop_as_drift_evidence(db_session):
    _border_target(db_session)

    collect_forwarding_control_observations(
        db_session,
        reader=_reader(route_next_hop="192.0.2.9"),
        observed_at=NOW,
        collector_run_id="collector-run-drift",
    )

    report = reconcile_forwarding_topology(db_session, as_of=NOW)
    assert report.state_counts["drift"] == 1
    assert sum(report.state_counts.values()) == 1
    assert report.declarations[0]["evidence"]["routing_table"][
        "conflict_observation_ids"
    ]


def test_collector_replay_is_idempotent_and_excludes_unrelated_peers(db_session):
    _border_target(db_session)
    reader = _reader()
    reader.sessions.append(
        {
            ".id": "*B2",
            "established": "true",
            "local.address": "198.51.100.2",
            "remote.address": "203.0.113.2",
            "remote.as": "64521",
            "routing-table": "main",
        }
    )

    first = collect_forwarding_control_observations(
        db_session,
        reader=reader,
        observed_at=NOW,
        collector_run_id="collector-run-replay",
    )
    second = collect_forwarding_control_observations(
        db_session,
        reader=reader,
        observed_at=NOW,
        collector_run_id="collector-run-replay",
    )

    assert first["skipped"]["bgp_outside_declaration_scope"] == 1
    assert second["observation_ids"] == first["observation_ids"]
    assert db_session.query(ForwardingControlObservation).count() == 2


def test_collector_never_fuzzily_matches_interface_names(db_session):
    _border_target(db_session)
    reader = _reader()
    reader.addresses[4][0]["interface"] = "SFP-SFPPLUS1"
    reader.route_rows["0.0.0.0/0"][0]["immediate-gw"] = "192.0.2.1%SFP-SFPPLUS1"

    result = collect_forwarding_control_observations(
        db_session,
        reader=reader,
        observed_at=NOW,
        collector_run_id="collector-run-case-mismatch",
    )

    assert result["observations_submitted"] == 0
    assert result["skipped"] == {
        "invalid_bgp_evidence": 1,
        "invalid_route_evidence": 1,
    }
    assert db_session.query(ForwardingControlObservation).count() == 0
    report = reconcile_forwarding_topology(db_session, as_of=NOW)
    assert report.state_counts["missing_observation"] == 1
    assert sum(report.state_counts.values()) == 1


def test_collector_ignores_cached_bgp_and_inactive_routes(db_session):
    _border_target(db_session)
    reader = _reader(established="false")
    reader.route_rows["0.0.0.0/0"][0]["active"] = "false"

    result = collect_forwarding_control_observations(
        db_session,
        reader=reader,
        observed_at=NOW,
        collector_run_id="collector-run-inactive",
    )

    assert result["observations_submitted"] == 0
    assert result["skipped"] == {
        "bgp_not_established": 1,
        "route_not_active": 1,
    }


def test_routeros_reader_uses_only_filtered_get_requests(monkeypatch):
    router = Router(
        name="Reader router",
        hostname="reader-router",
        management_ip="192.0.2.254",
        rest_api_username="test",
        rest_api_password="test",
    )
    calls: list[tuple[str, str]] = []

    def execute(_router, method, path, **_kwargs):
        calls.append((method, path))
        return []

    monkeypatch.setattr(
        "app.services.network.forwarding_observation_collector."
        "RouterConnectionService.execute",
        execute,
    )
    reader = RouterOSForwardingObservationReader()

    reader.bgp_sessions(router)
    reader.interface_addresses(router, family=4)
    reader.interface_addresses(router, family=6)
    reader.routes(router, prefix="0.0.0.0/0")

    assert {method for method, _ in calls} == {"GET"}
    assert calls[-1][1].startswith("/routing/route?")
    assert "dst-address=0.0.0.0%2F0" in calls[-1][1]
    assert all("/print" not in path for _, path in calls)


def test_collection_control_is_fail_closed(db_session):
    assert (
        control_registry.is_enabled(
            db_session, "network.forwarding_observation_collection"
        )
        is False
    )


def test_task_is_registered_exported_and_routed():
    import app.tasks as tasks
    from app.celery_app import celery_app

    task_name = (
        "app.tasks.forwarding_control_observations."
        "run_forwarding_control_observation_poll"
    )
    assert task_name in celery_app.tasks
    assert celery_app.conf.task_routes[task_name] == {"queue": "ingestion"}
    assert "run_forwarding_control_observation_poll" in tasks.__all__
    assert hasattr(tasks, "run_forwarding_control_observation_poll")


def test_disabled_task_does_not_touch_router_transport(monkeypatch):
    from app.tasks import forwarding_control_observations as task_module

    class FakeDb:
        closed = False

        def close(self):
            self.closed = True

    db = FakeDb()
    monkeypatch.setattr(task_module.db_session_adapter, "create_session", lambda: db)
    monkeypatch.setattr(
        task_module.control_registry, "is_enabled", lambda *_args: False
    )
    monkeypatch.setattr(
        task_module,
        "collect_forwarding_control_observations",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("collector must remain gated")
        ),
    )
    monkeypatch.setattr(task_module, "store_task_stats", lambda *_args: None)

    result: dict[str, Any] = task_module.run_forwarding_control_observation_poll()

    assert result == {
        "control": "network.forwarding_observation_collection",
        "status": "disabled",
    }
    assert db.closed is True


def test_enabled_task_keeps_expiry_beyond_two_collection_intervals(monkeypatch):
    from app.tasks import forwarding_control_observations as task_module

    class FakeDb:
        committed = False
        closed = False

        def commit(self):
            self.committed = True

        def rollback(self):
            raise AssertionError("successful collection must not roll back")

        def close(self):
            self.closed = True

    db = FakeDb()
    captured: dict[str, object] = {}

    def resolve_value(_db, _domain, key):
        return {
            "forwarding_control_observation_interval_seconds": 600,
            "forwarding_control_observation_ttl_seconds": 300,
        }[key]

    def collect(_db, *, ttl_seconds):
        captured["ttl_seconds"] = ttl_seconds
        return {"observations_submitted": 0}

    monkeypatch.setattr(task_module.db_session_adapter, "create_session", lambda: db)
    monkeypatch.setattr(task_module.control_registry, "is_enabled", lambda *_args: True)
    monkeypatch.setattr(task_module.settings_spec, "resolve_value", resolve_value)
    monkeypatch.setattr(task_module, "collect_forwarding_control_observations", collect)
    monkeypatch.setattr(task_module, "store_task_stats", lambda *_args: None)

    result = task_module.run_forwarding_control_observation_poll()

    assert captured == {"ttl_seconds": 1200}
    assert result["status"] == "collected"
    assert db.committed is True
    assert db.closed is True
