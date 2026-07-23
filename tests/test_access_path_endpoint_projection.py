"""Serving access endpoint projection and subscriber topology trace (G1).

These cover the projection layer: CustomerPath already resolves the endpoint,
and the bug this slice fixes is that AccessPathSummary dropped it and the
customer page fell back to the static provisioning NAS site.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from app.services.network.access_path import (
    resolve_subscription_topology_trace,
    summarize_customer_path,
)
from app.services.topology.customer_path import CustomerPath


@dataclass
class _Asset:
    """Minimal stand-in for an ORM asset: the projection only reads attributes."""

    id: uuid.UUID
    name: str | None = None
    serial_number: str | None = None
    port_number: int | None = None
    model: str | None = None
    olt_status: str | None = None
    olt_status_seen_at: datetime | None = None
    last_seen_at: datetime | None = None
    onu_rx_signal_dbm: float | None = None
    olt_rx_signal_dbm: float | None = None


@dataclass
class _Subscription:
    id: uuid.UUID
    subscriber_id: uuid.UUID


def _subscription() -> _Subscription:
    return _Subscription(id=uuid.uuid4(), subscriber_id=uuid.uuid4())


def _fiber_path(**overrides) -> CustomerPath:
    defaults: dict = {
        "ont": _Asset(id=uuid.uuid4(), serial_number="UBNT58508c30"),
        "pon_port": _Asset(id=uuid.uuid4(), name="0/1/3", port_number=3),
        "access_device": _Asset(id=uuid.uuid4(), name="Gudu OLT"),
        "access_device_kind": "olt",
    }
    defaults.update(overrides)
    return CustomerPath(**defaults)


# ---------------------------------------------------------------------------
# Endpoint projection
# ---------------------------------------------------------------------------


def test_fiber_endpoint_display_names_olt_and_pon_port():
    """The channel's recurring question is "which cabinet?" — answer both parts."""

    summary = summarize_customer_path(_subscription(), _fiber_path())

    assert summary.endpoint_display == "Gudu OLT (0/1/3)"
    assert summary.pon_port_label == "0/1/3"
    assert summary.access_device_name == "Gudu OLT"
    assert summary.ont_serial == "UBNT58508c30"


def test_pon_label_falls_back_to_port_number_when_unnamed():
    path = _fiber_path(pon_port=_Asset(id=uuid.uuid4(), name=None, port_number=7))

    summary = summarize_customer_path(_subscription(), path)

    assert summary.pon_port_label == "7"
    assert summary.endpoint_display == "Gudu OLT (7)"


def test_wireless_endpoint_display_names_ap_and_basestation():
    path = CustomerPath(
        access_device=_Asset(id=uuid.uuid4(), name="D-LUGBE-3"),
        access_device_kind="ap",
        node=_Asset(id=uuid.uuid4(), name="D-LUGBE-3"),
        basestation=_Asset(id=uuid.uuid4(), name="Lugbe BTS"),
        radio=_Asset(id=uuid.uuid4(), name="CPE-1"),
    )

    summary = summarize_customer_path(_subscription(), path)

    assert summary.endpoint_display == "D-LUGBE-3 (Lugbe BTS)"
    assert summary.radio_name == "CPE-1"


def test_wireless_endpoint_does_not_repeat_identical_node_and_basestation():
    path = CustomerPath(
        access_device=_Asset(id=uuid.uuid4(), name="D-KARU-1"),
        access_device_kind="ap",
        node=_Asset(id=uuid.uuid4(), name="D-KARU-1"),
        basestation=_Asset(id=uuid.uuid4(), name="D-KARU-1"),
    )

    assert summarize_customer_path(_subscription(), path).endpoint_display == "D-KARU-1"


def test_endpoint_source_distinguishes_live_from_provisioned():
    """selfcare-vs-UISP escalations come from not knowing which one is shown."""

    provisioned = summarize_customer_path(_subscription(), _fiber_path())
    live = summarize_customer_path(_subscription(), _fiber_path(live_session=True))

    assert provisioned.endpoint_source == "provisioning"
    assert live.endpoint_source == "live_session"


def test_unresolved_path_reports_unresolved_not_blank():
    """A blank field is what sends the agent to the chat channel."""

    summary = summarize_customer_path(
        _subscription(), CustomerPath(gap="no_access_device")
    )

    assert summary.endpoint_source == "unresolved"
    assert summary.endpoint_display is None
    assert summary.gap == "no_access_device"


# ---------------------------------------------------------------------------
# Topology trace
# ---------------------------------------------------------------------------


def _trace_for(path: CustomerPath, monkeypatch, db_session, subscription):
    monkeypatch.setattr(
        "app.services.network.access_path.resolve_customer_path",
        lambda _db, _sub: path,
    )
    return resolve_subscription_topology_trace(db_session, subscription)


def test_trace_orders_active_path_and_excludes_passive_plant(
    monkeypatch, db_session, subscription
):
    upstream = [
        _Asset(id=uuid.uuid4(), name="Gudu Agg"),
        _Asset(id=uuid.uuid4(), name="Abuja BNG"),
    ]
    path = _fiber_path(upstream_chain=upstream)

    trace = _trace_for(path, monkeypatch, db_session, subscription)

    assert [node.kind for node in trace.nodes] == [
        "ont",
        "pon_port",
        "olt",
        "network_device",
        "network_device",
    ]
    assert [node.label for node in trace.nodes] == [
        "UBNT58508c30",
        "0/1/3",
        "Gudu OLT",
        "Gudu Agg",
        "Abuja BNG",
    ]
    assert trace.complete is True


def test_trace_reports_ont_state_from_stored_telemetry(
    monkeypatch, db_session, subscription
):
    seen_at = datetime(2026, 7, 23, 9, 0, tzinfo=UTC)
    path = _fiber_path(
        ont=_Asset(
            id=uuid.uuid4(),
            serial_number="UBNT1",
            olt_status="offline",
            olt_status_seen_at=seen_at,
            onu_rx_signal_dbm=-27.0,
        ),
        upstream_chain=[_Asset(id=uuid.uuid4(), name="BNG")],
    )

    ont_node = _trace_for(path, monkeypatch, db_session, subscription).nodes[0]

    assert ont_node.state == "down"
    assert ont_node.observed_at == seen_at
    assert ont_node.detail["onu_rx_signal_dbm"] == -27.0


def test_trace_state_is_unknown_without_observation(
    monkeypatch, db_session, subscription
):
    """access_path owns identity, not health: never claim "up" it cannot see."""

    path = _fiber_path(upstream_chain=[_Asset(id=uuid.uuid4(), name="BNG")])

    assert (
        _trace_for(path, monkeypatch, db_session, subscription).nodes[2].state
        == "unknown"
    )


def test_trace_breaks_when_upstream_is_unproven(monkeypatch, db_session, subscription):
    trace = _trace_for(_fiber_path(), monkeypatch, db_session, subscription)

    assert trace.complete is False
    assert [gap.code for gap in trace.breaks] == ["upstream.unproven"]
    assert trace.breaks[0].after_index == len(trace.nodes) - 1


def test_trace_breaks_when_path_has_a_gap(monkeypatch, db_session, subscription):
    path = _fiber_path(gap="ont_unassigned", upstream_chain=[_Asset(id=uuid.uuid4())])

    codes = [
        gap.code
        for gap in _trace_for(path, monkeypatch, db_session, subscription).breaks
    ]

    assert "path.ont_unassigned" in codes


def test_trace_with_no_equipment_is_a_break_not_an_empty_chain(
    monkeypatch, db_session, subscription
):
    trace = _trace_for(CustomerPath(), monkeypatch, db_session, subscription)

    assert trace.nodes == ()
    assert [gap.code for gap in trace.breaks] == ["path.unresolved"]
    assert trace.complete is False


def test_trace_serializes_for_api_and_template_consumers(
    monkeypatch, db_session, subscription
):
    path = _fiber_path(upstream_chain=[_Asset(id=uuid.uuid4(), name="BNG")])

    payload = _trace_for(path, monkeypatch, db_session, subscription).to_dict()

    assert payload["schema_version"] == 1
    assert payload["access_kind"] == "olt"
    assert [node["kind"] for node in payload["nodes"]][:3] == ["ont", "pon_port", "olt"]
