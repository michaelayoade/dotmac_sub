from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.models.network import OntStatusSource, OntUnit, OnuOnlineStatus
from app.services import network as network_service
from app.services.network.ont_status import (
    OntStatusInputs,
    apply_acs_inform_observation,
    apply_olt_status_observation,
    apply_status_snapshot,
    ont_has_acs_management,
    reconcile_ont_state,
    resolve_acs_online_window_minutes_for_model,
    resolve_effective_last_seen_at,
    resolve_ont_effective_status,
    resolve_ont_status_for_model,
    resolve_ont_status_snapshot,
)


def test_olt_online_immediately_marks_effective_online() -> None:
    now = datetime.now(UTC)
    snapshot = resolve_ont_status_snapshot(
        olt_status=OnuOnlineStatus.online,
        acs_last_inform_at=now - timedelta(hours=2),
        now=now,
    )

    assert snapshot.olt_status == OnuOnlineStatus.online
    assert snapshot.effective_status == OnuOnlineStatus.online
    assert snapshot.effective_status_source == OntStatusSource.olt


def test_recent_acs_inform_overrides_olt_offline() -> None:
    now = datetime.now(UTC)
    resolution = resolve_ont_effective_status(
        OntStatusInputs(
            olt_status=OnuOnlineStatus.offline,
            olt_seen_at=now,
            acs_last_inform_at=now,
            acs_online_window_minutes=15,
            consecutive_offline_polls=3,
        ),
        now=now,
    )

    assert resolution.effective_status == OnuOnlineStatus.online
    assert resolution.effective_status_source == OntStatusSource.acs


def test_olt_offline_requires_threshold_before_effective_offline() -> None:
    now = datetime.now(UTC)
    resolution = resolve_ont_effective_status(
        OntStatusInputs(
            olt_status=OnuOnlineStatus.offline,
            olt_seen_at=now,
            acs_last_inform_at=None,
            acs_online_window_minutes=15,
            consecutive_offline_polls=2,
        ),
        now=now,
    )

    assert resolution.effective_status == OnuOnlineStatus.offline
    assert resolution.effective_status_source == OntStatusSource.derived


def test_snapshot_offline_preserves_poll_threshold() -> None:
    now = datetime.now(UTC)

    below_threshold = resolve_ont_status_snapshot(
        olt_status=OnuOnlineStatus.offline,
        acs_last_inform_at=None,
        now=now,
        consecutive_offline_polls=2,
    )
    at_threshold = resolve_ont_status_snapshot(
        olt_status=OnuOnlineStatus.offline,
        acs_last_inform_at=None,
        now=now,
        consecutive_offline_polls=3,
    )

    assert below_threshold.effective_status == OnuOnlineStatus.offline
    assert below_threshold.effective_status_source == OntStatusSource.derived
    assert at_threshold.effective_status == OnuOnlineStatus.offline
    assert at_threshold.effective_status_source == OntStatusSource.olt


def test_stale_acs_does_not_override_olt_online() -> None:
    now = datetime.now(UTC)
    snapshot = resolve_ont_status_snapshot(
        olt_status=OnuOnlineStatus.online,
        acs_last_inform_at=now - timedelta(hours=2),
        now=now,
    )

    assert snapshot.effective_status == OnuOnlineStatus.online
    assert snapshot.effective_status_source == OntStatusSource.olt


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
    assert ont.effective_status == OnuOnlineStatus.online
    assert ont.effective_status_source == OntStatusSource.acs
    assert ont.last_seen_at == now


def test_apply_observations_persist_source_semantics() -> None:
    now = datetime.now(UTC)
    ont = OntUnit(serial_number="ONT-OBSERVE")

    apply_olt_status_observation(ont, OnuOnlineStatus.offline, now=now)
    assert ont.olt_status == OnuOnlineStatus.offline
    assert ont.consecutive_offline_polls == 1
    assert ont.effective_status == OnuOnlineStatus.offline

    apply_acs_inform_observation(ont, now=now + timedelta(minutes=1))
    assert ont.acs_last_inform_at == now + timedelta(minutes=1)
    assert ont.effective_status == OnuOnlineStatus.online
    assert ont.effective_status_source == OntStatusSource.acs


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
        consecutive_offline_polls=0,
    )

    assert ont_has_acs_management(ont) is True
    snapshot = resolve_ont_status_for_model(ont, now=now)

    assert snapshot.effective_status == OnuOnlineStatus.offline
    assert snapshot.effective_status_source == OntStatusSource.derived


def test_reconcile_ont_state_flags_recent_acs_over_olt_offline() -> None:
    now = datetime.now(UTC)
    ont = OntUnit(
        serial_number="ONT-RECON-ACS",
        olt_status=OnuOnlineStatus.offline,
        olt_status_seen_at=now,
        acs_last_inform_at=now,
        consecutive_offline_polls=3,
    )

    result = reconcile_ont_state(ont, now=now)

    assert result.conflict is True
    assert result.authoritative_source == OntStatusSource.acs
    assert result.recommended_action == "check_olt_polling_freshness"


def test_resolve_effective_last_seen_at_prefers_newer_acs_inform() -> None:
    now = datetime.now(UTC)
    ont = SimpleNamespace(
        last_seen_at=now - timedelta(days=2),
        acs_last_inform_at=now - timedelta(minutes=1),
        olt_status_seen_at=now - timedelta(days=1),
    )

    assert resolve_effective_last_seen_at(ont) == now - timedelta(minutes=1)


def test_list_advanced_filters_by_persisted_effective_status(db_session) -> None:
    ont = OntUnit(
        serial_number="ONT-EFFECTIVE-ONLINE",
        is_active=True,
        olt_status=OnuOnlineStatus.offline,
        effective_status=OnuOnlineStatus.online,
    )
    db_session.add(ont)
    db_session.commit()

    rows, total = network_service.ont_units.list_advanced(
        db_session,
        olt_status="online",
        limit=50,
        offset=0,
    )

    assert total == 1
    assert [item.serial_number for item in rows] == ["ONT-EFFECTIVE-ONLINE"]
