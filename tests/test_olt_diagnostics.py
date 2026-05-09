from __future__ import annotations

from types import SimpleNamespace

from app.services.network.olt_ssh_diagnostics import (
    AlarmEntry,
    OntTrafficStats,
    OpticalInfo,
)
from app.services.network.parsers.loader import OntInfoEntry


def _online_info() -> OntInfoEntry:
    return OntInfoEntry(
        fsp="0/2/11",
        ont_id=13,
        serial_number="4857544306351E9C",
        control_flag="active",
        run_state="online",
        config_state="normal",
        match_state="match",
    )


def test_diagnostic_snapshot_aggregates_readonly_sources(monkeypatch) -> None:
    from app.services.network import olt_diagnostics

    olt = SimpleNamespace(name="Garki")
    service_port = SimpleNamespace(index=27, vlan_id=203, ont_id=13)
    alarm = AlarmEntry(
        alarm_id=0x2E11,
        severity="major",
        source="FrameID:0, SlotID:2, PortID:11, ONT ID:13",
        name="The ONT is offline",
    )

    monkeypatch.setattr(
        "app.services.network.olt_ssh_diagnostics.get_ont_info",
        lambda *_args, **_kwargs: (True, "ok", _online_info()),
    )
    monkeypatch.setattr(
        "app.services.network.olt_ssh_diagnostics.get_ont_optical_info",
        lambda *_args, **_kwargs: (
            True,
            "ok",
            OpticalInfo(fsp="0/2/11", ont_id=13, rx_power_dbm=-20.0),
        ),
    )
    monkeypatch.setattr(
        "app.services.network.olt_ssh_diagnostics.get_ont_traffic_stats",
        lambda *_args, **_kwargs: (
            True,
            "ok",
            OntTrafficStats(
                fsp="0/2/11",
                ont_id=13,
                upstream_bytes=100,
                downstream_bytes=200,
            ),
        ),
    )
    monkeypatch.setattr(
        "app.services.network.olt_ssh_service_ports.get_service_ports_for_ont",
        lambda *_args, **_kwargs: (True, "ok", [service_port]),
    )
    monkeypatch.setattr(
        "app.services.network.olt_ssh_diagnostics.get_active_alarms",
        lambda *_args, **_kwargs: (True, "ok", [alarm]),
    )

    snapshot = olt_diagnostics.get_ont_diagnostic_snapshot(olt, "0/2/11", 13)

    assert snapshot.ont_info is not None
    assert snapshot.optical_info is not None
    assert snapshot.traffic_stats is not None
    assert snapshot.service_ports == [service_port]
    assert snapshot.active_alarms == [alarm]
    assert snapshot.offline_reason == "The ONT is offline"
    assert snapshot.diagnosis == "Active alarm indicates: The ONT is offline."
    assert snapshot.warnings == []


def test_diagnostic_snapshot_prefers_ont_info_down_cause(monkeypatch) -> None:
    from app.services.network import olt_diagnostics

    down_info = _online_info()
    down_info.run_state = "offline"
    down_info.last_down_cause = "LOS"

    monkeypatch.setattr(
        "app.services.network.olt_ssh_diagnostics.get_ont_info",
        lambda *_args, **_kwargs: (True, "ok", down_info),
    )
    monkeypatch.setattr(
        "app.services.network.olt_ssh_diagnostics.get_ont_optical_info",
        lambda *_args, **_kwargs: (True, "ok", None),
    )
    monkeypatch.setattr(
        "app.services.network.olt_ssh_diagnostics.get_ont_traffic_stats",
        lambda *_args, **_kwargs: (True, "ok", None),
    )
    monkeypatch.setattr(
        "app.services.network.olt_ssh_service_ports.get_service_ports_for_ont",
        lambda *_args, **_kwargs: (True, "ok", []),
    )
    monkeypatch.setattr(
        "app.services.network.olt_ssh_diagnostics.get_active_alarms",
        lambda *_args, **_kwargs: (True, "ok", []),
    )

    snapshot = olt_diagnostics.get_ont_diagnostic_snapshot(
        SimpleNamespace(name="Garki"),
        "0/2/11",
        13,
    )

    assert snapshot.offline_reason == "LOS"
    assert snapshot.diagnosis == "ONT is not online: LOS."


def test_diagnostic_snapshot_reports_low_optical_power(monkeypatch) -> None:
    from app.services.network import olt_diagnostics

    monkeypatch.setattr(
        "app.services.network.olt_ssh_diagnostics.get_ont_info",
        lambda *_args, **_kwargs: (True, "ok", _online_info()),
    )
    monkeypatch.setattr(
        "app.services.network.olt_ssh_diagnostics.get_ont_optical_info",
        lambda *_args, **_kwargs: (
            True,
            "ok",
            OpticalInfo(fsp="0/2/11", ont_id=13, rx_power_dbm=-29.2),
        ),
    )
    monkeypatch.setattr(
        "app.services.network.olt_ssh_diagnostics.get_ont_traffic_stats",
        lambda *_args, **_kwargs: (True, "ok", None),
    )
    monkeypatch.setattr(
        "app.services.network.olt_ssh_service_ports.get_service_ports_for_ont",
        lambda *_args, **_kwargs: (True, "ok", [SimpleNamespace(index=27)]),
    )
    monkeypatch.setattr(
        "app.services.network.olt_ssh_diagnostics.get_active_alarms",
        lambda *_args, **_kwargs: (True, "ok", []),
    )

    snapshot = olt_diagnostics.get_ont_diagnostic_snapshot(
        SimpleNamespace(name="Garki"),
        "0/2/11",
        13,
    )

    assert snapshot.offline_reason == "low_optical_power"
    assert snapshot.diagnosis == "Low ONT RX optical power: -29.20 dBm."


def test_diagnostic_snapshot_keeps_partial_failures_as_warnings(monkeypatch) -> None:
    from app.services.network import olt_diagnostics

    monkeypatch.setattr(
        "app.services.network.olt_ssh_diagnostics.get_ont_info",
        lambda *_args, **_kwargs: (False, "timeout", None),
    )
    monkeypatch.setattr(
        "app.services.network.olt_ssh_diagnostics.get_ont_optical_info",
        lambda *_args, **_kwargs: (True, "ok", None),
    )
    monkeypatch.setattr(
        "app.services.network.olt_ssh_diagnostics.get_ont_traffic_stats",
        lambda *_args, **_kwargs: (True, "ok", None),
    )
    monkeypatch.setattr(
        "app.services.network.olt_ssh_service_ports.get_service_ports_for_ont",
        lambda *_args, **_kwargs: (False, "service-port read failed", []),
    )
    monkeypatch.setattr(
        "app.services.network.olt_ssh_diagnostics.get_active_alarms",
        lambda *_args, **_kwargs: (True, "ok", []),
    )

    snapshot = olt_diagnostics.get_ont_diagnostic_snapshot(
        SimpleNamespace(name="Garki"),
        "0/2/11",
        13,
    )

    assert snapshot.ont_info is None
    assert snapshot.service_ports == []
    assert snapshot.offline_reason == "missing_service_ports"
    assert "ONT info: timeout" in snapshot.warnings
    assert "Service ports: service-port read failed" in snapshot.warnings
