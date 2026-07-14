from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.models.network import OntUnit
from app.models.ont_observation import OntObservation
from app.services.network import ont_config_snapshots as snapshots_module
from app.services.network.ont_action_common import ActionResult
from app.services.network.ont_config_snapshots import (
    OntConfigSnapshots,
    snapshot_integrity_valid,
)


def _live_config_result() -> ActionResult:
    return ActionResult(
        success=True,
        message="Configuration retrieved.",
        data={
            "device_info": {"serial": "HWTC0001"},
            "wan": {"username": "customer", "password": "wan-secret"},
            "optical": {"rx_dbm": -19.5},
            "wifi": {"ssid": "CustomerNet", "key_passphrase": "wifi-secret"},
        },
    )


def _observation(ont: OntUnit, observed_at: datetime) -> OntObservation:
    return OntObservation(
        ont_unit_id=ont.id,
        last_reconciled_at=observed_at,
        last_reconcile_duration_ms=1234,
        mgmt_ip_pingable=True,
        olt_present=True,
        olt_match_state="match",
        olt_run_state="online",
        olt_mgmt_ip="10.0.0.20",
        olt_mgmt_vlan=100,
        olt_line_profile_id=40,
        olt_service_profile_id=41,
        olt_tr069_profile_id=7,
        olt_service_ports=[{"index": 10, "vlan": 203, "gem": 1}],
        acs_present=True,
        acs_last_inform_at=observed_at,
        acs_observed_software_version="V5R019",
        acs_observed_pppoe_username="customer",
        acs_observed_pppoe_enable=True,
        acs_observed_wan_vlan=203,
        acs_observed_nat_enabled=True,
        acs_observed_ssid="CustomerNet",
        acs_observed_wifi_enabled=True,
        acs_observed_ipv6_enabled=True,
    )


def test_capture_persists_composite_evidence_with_integrity(
    db_session, monkeypatch
) -> None:
    ont = OntUnit(
        serial_number="HWTC0001",
        vendor="Huawei",
        desired_config={"wan": {"password": "intent-secret"}},
    )
    db_session.add(ont)
    db_session.flush()
    observed_at = datetime(2026, 7, 14, 8, 0, tzinfo=UTC)
    db_session.add(_observation(ont, observed_at))
    db_session.flush()

    monkeypatch.setattr(
        snapshots_module, "get_running_config", lambda *_: _live_config_result()
    )
    monkeypatch.setattr(
        snapshots_module,
        "resolve_effective_ont_config",
        lambda *_: {
            "config_pack": SimpleNamespace(id="pack-1"),
            "assignment": SimpleNamespace(id="assignment-1"),
            "desired_config_keys": ["wan.password", "wan.mode"],
            "values": {
                "wan_mode": "pppoe",
                "pppoe_password": "intent-secret",
                "wan_vlan": 203,
            },
        },
    )

    snapshot = OntConfigSnapshots.capture(db_session, str(ont.id), label="before")

    assert snapshot.schema_version == 2
    assert snapshot.wan["password"] == "[redacted]"
    assert snapshot.wifi["key_passphrase"] == "[redacted]"
    assert snapshot.effective_config["values"]["pppoe_password"] == "[redacted]"
    assert snapshot.effective_config["config_pack_id"] == "pack-1"
    assert snapshot.observed_state["olt"]["line_profile_id"] == 40
    assert snapshot.observed_state["olt"]["service_ports"][0]["vlan"] == 203
    assert snapshot.provenance["acs_running_config"]["status"] == "live"
    assert snapshot.provenance["reconciler_observation"] == {
        "observed_at": observed_at.isoformat(),
        "status": "cached",
    }
    assert snapshot_integrity_valid(snapshot) is True

    snapshot.wan["username"] = "tampered"
    assert snapshot_integrity_valid(snapshot) is False


def test_capture_marks_missing_reconciler_observation(db_session, monkeypatch) -> None:
    ont = OntUnit(serial_number="HWTC0002", vendor="Huawei")
    db_session.add(ont)
    db_session.flush()
    monkeypatch.setattr(
        snapshots_module, "get_running_config", lambda *_: _live_config_result()
    )
    monkeypatch.setattr(
        snapshots_module,
        "resolve_effective_ont_config",
        lambda *_: {"desired_config_keys": [], "values": {}},
    )

    snapshot = OntConfigSnapshots.capture(db_session, str(ont.id))

    assert snapshot.observed_state == {"available": False, "acs": None, "olt": None}
    assert snapshot.provenance["reconciler_observation"] == {
        "observed_at": None,
        "status": "missing",
    }
    assert snapshot_integrity_valid(snapshot) is True


def test_capture_does_not_persist_when_live_acs_read_fails(
    db_session, monkeypatch
) -> None:
    ont = OntUnit(serial_number="HWTC0003", vendor="Huawei")
    db_session.add(ont)
    db_session.commit()
    monkeypatch.setattr(
        snapshots_module,
        "get_running_config",
        lambda *_: ActionResult(success=False, message="ACS unavailable"),
    )

    with pytest.raises(HTTPException, match="ACS unavailable"):
        OntConfigSnapshots.capture(db_session, str(ont.id))

    assert OntConfigSnapshots.list_for_ont(db_session, str(ont.id)) == []
