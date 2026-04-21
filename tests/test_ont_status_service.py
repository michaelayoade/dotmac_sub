from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.models.network import OntStatusSource, OntUnit, OnuOnlineStatus
from app.services import network as network_service
from app.services.network.ont_status import (
    OntAcsStatus,
    apply_resolved_status_for_model,
    apply_status_snapshot,
    ont_has_acs_management,
    reconcile_device_state,
    resolve_effective_last_seen_at,
    resolve_acs_online_window_minutes_for_model,
    resolve_ont_status_for_model,
    resolve_ont_status_snapshot,
)


def test_resolve_ont_status_snapshot_prefers_olt_online() -> None:
    snapshot = resolve_ont_status_snapshot(
        olt_status=OnuOnlineStatus.online,
        acs_last_inform_at=datetime.now(UTC) - timedelta(hours=2),
        managed=True,
    )

    assert snapshot.acs_status == OntAcsStatus.stale
    assert snapshot.effective_status == OnuOnlineStatus.online
    assert snapshot.effective_status_source == OntStatusSource.olt


def test_resolve_ont_status_snapshot_uses_recent_acs_for_unknown_olt() -> None:
    snapshot = resolve_ont_status_snapshot(
        olt_status=OnuOnlineStatus.unknown,
        acs_last_inform_at=datetime.now(UTC),
        managed=True,
    )

    assert snapshot.acs_status == OntAcsStatus.online
    assert snapshot.effective_status == OnuOnlineStatus.online
    assert snapshot.effective_status_source == OntStatusSource.acs


def test_resolve_ont_status_snapshot_marks_unmanaged_when_no_acs() -> None:
    snapshot = resolve_ont_status_snapshot(
        olt_status=OnuOnlineStatus.unknown,
        acs_last_inform_at=None,
        managed=False,
    )

    assert snapshot.acs_status == OntAcsStatus.unmanaged
    # Unknown OLT and no ACS means no observed status, not confirmed offline.
    assert snapshot.effective_status == OnuOnlineStatus.unknown
    assert snapshot.effective_status_source == OntStatusSource.derived


def test_apply_status_snapshot_updates_ont_fields() -> None:
    ont = OntUnit(serial_number="ONT-STATUS-1")
    now = datetime.now(UTC)
    snapshot = resolve_ont_status_snapshot(
        olt_status=OnuOnlineStatus.unknown,
        acs_last_inform_at=now,
        managed=True,
        now=now,
    )

    apply_status_snapshot(ont, snapshot)

    assert ont.acs_status == OntAcsStatus.online
    assert ont.acs_last_inform_at == now
    assert ont.effective_status == OnuOnlineStatus.online
    assert ont.effective_status_source == OntStatusSource.acs
    assert ont.status_resolved_at == now


def test_apply_resolved_status_for_model_persists_effective_snapshot() -> None:
    ont = OntUnit(
        serial_number="ONT-STATUS-RESOLVED",
        online_status=OnuOnlineStatus.online,
    )
    now = datetime.now(UTC)

    snapshot = apply_resolved_status_for_model(ont, now=now)

    assert snapshot.effective_status == OnuOnlineStatus.online
    assert ont.effective_status == OnuOnlineStatus.online
    assert ont.effective_status_source == OntStatusSource.olt
    assert ont.status_resolved_at == now


def test_resolve_acs_online_window_minutes_for_model_uses_acs_interval() -> None:
    ont = SimpleNamespace(
        tr069_acs_server=SimpleNamespace(periodic_inform_interval=3600),
        olt_device=None,
    )

    assert resolve_acs_online_window_minutes_for_model(ont) == 65


def test_resolve_ont_status_for_model_respects_hourly_inform_policy() -> None:
    now = datetime.now(UTC)
    ont = SimpleNamespace(
        online_status=OnuOnlineStatus.unknown,
        tr069_acs_server_id="acs-1",
        tr069_acs_server=SimpleNamespace(periodic_inform_interval=3600),
        olt_device=None,
        acs_last_inform_at=now - timedelta(minutes=50),
    )

    snapshot = resolve_ont_status_for_model(ont, now=now)

    assert snapshot.acs_status == OntAcsStatus.online
    assert snapshot.effective_status == OnuOnlineStatus.online
    assert snapshot.effective_status_source == OntStatusSource.acs


def test_resolve_ont_status_for_model_treats_olt_acs_as_managed() -> None:
    now = datetime.now(UTC)
    ont = SimpleNamespace(
        online_status=OnuOnlineStatus.unknown,
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

    assert snapshot.acs_status == OntAcsStatus.unknown
    # Unknown OLT and no recent ACS inform means no observed status.
    assert snapshot.effective_status == OnuOnlineStatus.unknown
    assert snapshot.effective_status_source == OntStatusSource.derived


def test_reconcile_device_state_prefers_recent_acs_over_olt_offline(
    db_session,
) -> None:
    now = datetime.now(UTC)
    ont = OntUnit(
        serial_number="ONT-RECON-ACS",
        online_status=OnuOnlineStatus.offline,
        acs_last_inform_at=now,
    )
    db_session.add(ont)
    db_session.commit()
    db_session.refresh(ont)

    result = reconcile_device_state(db_session, ont.id, now=now)

    assert result.conflict is True
    assert result.reason == "acs_recent_inform_overrides_olt_offline"
    assert result.authoritative_source == OntStatusSource.acs
    assert result.recommended_action == "refresh_olt_status"
    assert ont.effective_status == OnuOnlineStatus.online
    assert ont.effective_status_source == OntStatusSource.acs


def test_reconcile_device_state_prefers_olt_online_over_stale_acs(
    db_session,
) -> None:
    now = datetime.now(UTC)
    ont = OntUnit(
        serial_number="ONT-RECON-OLT",
        online_status=OnuOnlineStatus.online,
        acs_last_inform_at=now - timedelta(hours=2),
    )
    db_session.add(ont)
    db_session.commit()
    db_session.refresh(ont)

    result = reconcile_device_state(db_session, ont.id, now=now)

    assert result.conflict is True
    assert result.reason == "olt_online_overrides_stale_acs"
    assert result.authoritative_source == OntStatusSource.olt
    assert result.recommended_action == "send_connection_request"
    assert ont.effective_status == OnuOnlineStatus.online
    assert ont.effective_status_source == OntStatusSource.olt


def test_resolve_effective_last_seen_at_prefers_newer_acs_inform() -> None:
    now = datetime.now(UTC)
    ont = SimpleNamespace(
        last_seen_at=now - timedelta(days=2),
        acs_last_inform_at=now - timedelta(minutes=1),
    )

    assert resolve_effective_last_seen_at(ont) == now - timedelta(minutes=1)


def test_list_advanced_filters_by_persisted_effective_status(db_session) -> None:
    ont = OntUnit(
        serial_number="ONT-EFFECTIVE-ONLINE",
        is_active=True,
        online_status=OnuOnlineStatus.offline,
        effective_status=OnuOnlineStatus.online,
    )
    db_session.add(ont)
    db_session.commit()

    rows, total = network_service.ont_units.list_advanced(
        db_session,
        online_status="online",
        limit=50,
        offset=0,
    )

    assert total == 1
    assert [item.serial_number for item in rows] == ["ONT-EFFECTIVE-ONLINE"]
