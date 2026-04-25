"""Tests for OLT observed-state caching and persistence."""

from datetime import UTC, datetime

from app.models.network import OLTDevice
from app.services.network.olt_ssh_profiles import Tr069ServerProfile
from app.services.olt_observed_state_adapter import get_tr069_profiles_for_olt


def test_get_tr069_profiles_for_olt_persists_live_result(db_session, monkeypatch):
    olt = OLTDevice(
        name="OLT Profiles",
        hostname="olt-profiles",
        mgmt_ip="10.0.0.10",
        snmp_enabled=False,
        netconf_enabled=False,
    )
    db_session.add(olt)
    db_session.commit()
    profile = Tr069ServerProfile(
        profile_id=2,
        name="DotMac-ACS",
        acs_url="http://acs.example/cwmp",
        acs_username="acs",
        inform_interval=3600,
        binding_count=4,
    )

    monkeypatch.setattr(
        "app.services.olt_observed_state_adapter._read_redis_json",
        lambda _key: None,
    )
    monkeypatch.setattr(
        "app.services.olt_observed_state_adapter._write_redis_json",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "app.services.network.olt_ssh_profiles.get_tr069_server_profiles",
        lambda _olt: (True, "Found 1 profile", [profile]),
    )

    result = get_tr069_profiles_for_olt(db_session, olt)

    assert result.ok is True
    assert result.source == "live"
    assert result.data[0].profile_id == 2
    assert olt.tr069_profiles_snapshot_at is not None
    assert olt.tr069_profiles_snapshot["profiles"][0]["name"] == "DotMac-ACS"


def test_get_tr069_profiles_for_olt_falls_back_to_db_snapshot(db_session, monkeypatch):
    fetched_at = datetime(2026, 4, 20, 10, 0, tzinfo=UTC)
    olt = OLTDevice(
        name="OLT Cached Profiles",
        hostname="olt-cached-profiles",
        mgmt_ip="10.0.0.11",
        snmp_enabled=False,
        netconf_enabled=False,
        tr069_profiles_snapshot={
            "fetched_at": fetched_at.isoformat(),
            "profiles": [{"profile_id": 7, "name": "Cached ACS"}],
        },
        tr069_profiles_snapshot_at=fetched_at,
    )
    db_session.add(olt)
    db_session.commit()

    monkeypatch.setattr(
        "app.services.olt_observed_state_adapter._read_redis_json",
        lambda _key: None,
    )
    monkeypatch.setattr(
        "app.services.network.olt_ssh_profiles.get_tr069_server_profiles",
        lambda _olt: (False, "SSH timeout", []),
    )

    result = get_tr069_profiles_for_olt(db_session, olt)

    assert result.ok is True
    assert result.source == "db"
    assert result.stale is True
    assert result.data[0].profile_id == 7
    assert "SSH timeout" in result.message


