from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.models.network import OntStatusSource, OntUnit, OnuOnlineStatus
from app.services import network as network_service
from app.services.network.ont_status import (
    apply_acs_inform_observation,
    apply_olt_status_observation,
    apply_status_snapshot,
    ont_has_acs_management,
    reconcile_ont_state,
    resolve_acs_online_window_minutes_for_model,
    resolve_effective_last_seen_at,
    resolve_ont_status_for_model,
    resolve_ont_status_snapshot,
)


def test_snapshot_preserves_raw_olt_online_observation() -> None:
    now = datetime.now(UTC)
    snapshot = resolve_ont_status_snapshot(
        olt_status=OnuOnlineStatus.online,
        acs_last_inform_at=now - timedelta(hours=2),
        now=now,
    )

    assert snapshot.olt_status == OnuOnlineStatus.online
    assert snapshot.olt_status_seen_at == now
    assert snapshot.acs_last_inform_at == now - timedelta(hours=2)
    assert snapshot.last_seen_at == now


def test_snapshot_preserves_recent_acs_inform_without_status_override() -> None:
    now = datetime.now(UTC)
    snapshot = resolve_ont_status_snapshot(
        olt_status=OnuOnlineStatus.offline,
        acs_last_inform_at=now,
        now=now,
    )

    assert snapshot.olt_status == OnuOnlineStatus.offline
    assert snapshot.olt_status_seen_at is None
    assert snapshot.acs_last_inform_at == now
    assert snapshot.last_seen_at == now


def test_snapshot_offline_keeps_no_last_seen_without_acs() -> None:
    now = datetime.now(UTC)
    snapshot = resolve_ont_status_snapshot(
        olt_status=OnuOnlineStatus.offline,
        acs_last_inform_at=None,
        now=now,
    )

    assert snapshot.olt_status == OnuOnlineStatus.offline
    assert snapshot.olt_status_seen_at is None
    assert snapshot.last_seen_at is None


def test_stale_acs_does_not_change_raw_olt_online_status() -> None:
    now = datetime.now(UTC)
    snapshot = resolve_ont_status_snapshot(
        olt_status=OnuOnlineStatus.online,
        acs_last_inform_at=now - timedelta(hours=2),
        now=now,
    )

    assert snapshot.olt_status == OnuOnlineStatus.online
    assert snapshot.last_seen_at == now


def test_apply_status_snapshot_updates_explicit_fields() -> None:
    now = datetime.now(UTC)
    ont = OntUnit(serial_number="ONT-STATUS-1")
    snapshot = resolve_ont_status_snapshot(
        olt_status=OnuOnlineStatus.online,
        acs_last_inform_at=now,
        now=now,
    )

    apply_status_snapshot(ont, snapshot)

    assert ont.olt_status == OnuOnlineStatus.online
    assert ont.olt_status_seen_at == now
    assert ont.acs_last_inform_at == now
    assert ont.last_seen_at == now


def test_apply_observations_persist_raw_olt_and_acs_state() -> None:
    now = datetime.now(UTC)
    ont = OntUnit(serial_number="ONT-OBSERVE")

    apply_olt_status_observation(ont, OnuOnlineStatus.offline, now=now)
    assert ont.olt_status == OnuOnlineStatus.offline
    assert ont.olt_status_seen_at == now

    apply_acs_inform_observation(ont, now=now + timedelta(minutes=1))
    assert ont.acs_last_inform_at == now + timedelta(minutes=1)
    assert ont.last_seen_at == now + timedelta(minutes=1)


def test_resolve_acs_online_window_minutes_for_model_uses_acs_interval() -> None:
    ont = SimpleNamespace(
        tr069_acs_server=SimpleNamespace(periodic_inform_interval=3600),
        olt_device=None,
    )

    assert resolve_acs_online_window_minutes_for_model(ont) == 65


def test_resolve_ont_status_for_model_treats_olt_acs_as_managed() -> None:
    now = datetime.now(UTC)
    ont = SimpleNamespace(
        olt_status=OnuOnlineStatus.offline,
        olt_status_seen_at=None,
        tr069_acs_server_id=None,
        tr069_acs_server=None,
        olt_device=SimpleNamespace(
            tr069_acs_server_id="acs-on-olt",
            tr069_acs_server=SimpleNamespace(periodic_inform_interval=300),
        ),
        acs_last_inform_at=None,
    )

    assert ont_has_acs_management(ont) is True
    snapshot = resolve_ont_status_for_model(ont, now=now)

    assert snapshot.olt_status == OnuOnlineStatus.offline
    assert snapshot.last_seen_at is None


def test_reconcile_ont_state_keeps_monitoring_as_authority() -> None:
    now = datetime.now(UTC)
    ont = OntUnit(
        serial_number="ONT-RECON-ACS",
        olt_status=OnuOnlineStatus.offline,
        olt_status_seen_at=now,
        acs_last_inform_at=now,
    )

    result = reconcile_ont_state(ont, now=now)

    assert result.conflict is False
    assert result.authoritative_source == OntStatusSource.zabbix
    assert result.recommended_action is None


def test_resolve_effective_last_seen_at_prefers_newer_acs_inform() -> None:
    now = datetime.now(UTC)
    ont = SimpleNamespace(
        last_seen_at=now - timedelta(days=2),
        acs_last_inform_at=now - timedelta(minutes=1),
        olt_status_seen_at=now - timedelta(days=1),
    )

    assert resolve_effective_last_seen_at(ont) == now - timedelta(minutes=1)


def test_list_advanced_runtime_status_filter_degrades_to_offline(db_session) -> None:
    """The live runtime-status source was retired: every ONT reads offline, so
    the "online" filter matches nothing and the "offline" filter matches all."""
    ont = OntUnit(
        serial_number="ONT-RUNTIME-STATUS",
        is_active=True,
        olt_status=OnuOnlineStatus.online,
    )
    db_session.add(ont)
    db_session.commit()

    rows, total = network_service.ont_units.list_advanced(
        db_session,
        olt_status="online",
        limit=50,
        offset=0,
    )
    assert total == 0
    assert rows == []

    rows, total = network_service.ont_units.list_advanced(
        db_session,
        olt_status="offline",
        limit=50,
        offset=0,
    )
    assert total == 1
    assert [item.serial_number for item in rows] == ["ONT-RUNTIME-STATUS"]
