from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.models.network import OntStatusSource, OntUnit, OnuOnlineStatus
from app.services import network as network_service
from app.services.network.ont_status import (
    OntAcsStatus,
    apply_status_snapshot,
    ont_has_acs_management,
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
    # When OLT is unknown and no ACS, effective status is offline (can't confirm online)
    assert snapshot.effective_status == OnuOnlineStatus.offline
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
    # When OLT is unknown and ACS has no recent inform, effective status is offline
    assert snapshot.effective_status == OnuOnlineStatus.offline
    assert snapshot.effective_status_source == OntStatusSource.derived


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
