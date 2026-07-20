from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.models.network import DeviceStatus, OLTDevice, OntUnit, OnuOnlineStatus
from app.services.network import ont_status_refresh
from app.services.queue_adapter import QueueDispatchResult

NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)


def _olt(db_session, *, name: str, vendor: str, **values) -> OLTDevice:
    olt = OLTDevice(
        name=name,
        vendor=vendor,
        is_active=True,
        status=DeviceStatus.active,
        **values,
    )
    db_session.add(olt)
    db_session.flush()
    return olt


def _stale_online_ont(
    db_session,
    *,
    serial: str,
    olt: OLTDevice,
    is_active: bool = True,
) -> OntUnit:
    ont = OntUnit(
        serial_number=serial,
        olt_device_id=olt.id,
        olt_status=OnuOnlineStatus.online,
        olt_status_seen_at=NOW - timedelta(hours=1),
        last_seen_at=NOW - timedelta(hours=1),
        is_active=is_active,
    )
    db_session.add(ont)
    db_session.flush()
    return ont


def test_stale_huawei_onts_queue_one_bulk_olt_refresh(db_session, monkeypatch):
    olt = _olt(db_session, name="Stale Huawei OLT", vendor="Huawei")
    ont_a = _stale_online_ont(db_session, serial="HWTCREFRESH001", olt=olt)
    ont_b = _stale_online_ont(db_session, serial="HWTCREFRESH002", olt=olt)
    queued: list[str] = []

    monkeypatch.setattr(
        ont_status_refresh,
        "_claim_refresh_window",
        lambda olt_id, **_kwargs: True,
    )

    def fake_queue(olt_id: str, *, source: str) -> QueueDispatchResult:
        queued.append(olt_id)
        assert source == "network.ont_status_refresh"
        return QueueDispatchResult(
            queued=True,
            task_name="app.tasks.ont_runtime_status.refresh_huawei_olt_status",
            queue="ingestion",
        )

    monkeypatch.setattr(ont_status_refresh, "queue_huawei_olt_status_poll", fake_queue)

    result = ont_status_refresh.request_stale_ont_status_refreshes(
        db_session, [ont_a, ont_b], now=NOW
    )

    assert result.stale_onts == 2
    assert result.queued_olts == 1
    assert queued == [str(olt.id)]


def test_recently_polled_huawei_olt_is_not_queued(db_session, monkeypatch):
    olt = _olt(
        db_session,
        name="Recently Polled Huawei OLT",
        vendor="Huawei",
        last_poll_at=NOW - timedelta(seconds=30),
    )
    ont = _stale_online_ont(db_session, serial="HWTCRECENT001", olt=olt)
    queued: list[str] = []
    monkeypatch.setattr(
        ont_status_refresh,
        "queue_huawei_olt_status_poll",
        lambda olt_id, **_kwargs: queued.append(olt_id),
    )

    result = ont_status_refresh.request_stale_ont_status_refreshes(
        db_session, [ont], now=NOW, cooldown_seconds=120
    )

    assert result.queued_olts == 0
    assert result.suppressed_recent_poll == 1
    assert queued == []


def test_recent_refresh_request_suppresses_duplicate_queue(db_session, monkeypatch):
    olt = _olt(db_session, name="Duplicate Huawei OLT", vendor="Huawei")
    ont = _stale_online_ont(db_session, serial="HWTCDUPE001", olt=olt)
    queued: list[str] = []
    monkeypatch.setattr(
        ont_status_refresh,
        "_claim_refresh_window",
        lambda olt_id, **_kwargs: False,
    )
    monkeypatch.setattr(
        ont_status_refresh,
        "queue_huawei_olt_status_poll",
        lambda olt_id, **_kwargs: queued.append(olt_id),
    )

    result = ont_status_refresh.request_stale_ont_status_refreshes(
        db_session, [ont], now=NOW
    )

    assert result.queued_olts == 0
    assert result.suppressed_recent_request == 1
    assert queued == []


def test_uisp_managed_ont_does_not_queue_huawei_refresh(db_session, monkeypatch):
    olt = _olt(
        db_session,
        name="UISP OLT",
        vendor="ubiquiti",
        uisp_device_id="uisp-olt-1",
    )
    ont = _stale_online_ont(db_session, serial="UBNTREFRESH001", olt=olt)
    queued: list[str] = []
    monkeypatch.setattr(
        ont_status_refresh,
        "queue_huawei_olt_status_poll",
        lambda olt_id, **_kwargs: queued.append(olt_id),
    )

    result = ont_status_refresh.request_stale_ont_status_refreshes(
        db_session, [ont], now=NOW
    )

    assert result.queued_olts == 0
    assert result.skipped_non_huawei == 1
    assert queued == []


def test_inactive_stale_ont_does_not_request_unrepairable_poll(db_session, monkeypatch):
    olt = _olt(db_session, name="Inactive ONT Huawei OLT", vendor="Huawei")
    ont = _stale_online_ont(
        db_session,
        serial="HWTCINACTIVE001",
        olt=olt,
        is_active=False,
    )
    queued: list[str] = []
    monkeypatch.setattr(
        ont_status_refresh,
        "queue_huawei_olt_status_poll",
        lambda olt_id, **_kwargs: queued.append(olt_id),
    )

    result = ont_status_refresh.request_stale_ont_status_refreshes(
        db_session, [ont], now=NOW
    )

    assert result.stale_onts == 0
    assert result.skipped_inactive_ont == 1
    assert queued == []


def test_suppressed_candidates_do_not_consume_successful_admission_cap(
    db_session, monkeypatch
):
    onts: list[OntUnit] = []
    olt_ids: list[str] = []
    for index in range(4):
        olt = _olt(
            db_session,
            name=f"Admission Cap Huawei OLT {index}",
            vendor="Huawei",
        )
        olt_ids.append(str(olt.id))
        onts.append(
            _stale_online_ont(
                db_session,
                serial=f"HWTCCAP{index:08d}",
                olt=olt,
            )
        )

    monkeypatch.setattr(
        ont_status_refresh,
        "_claim_refresh_window",
        lambda olt_id, **_kwargs: olt_id not in set(olt_ids[:2]),
    )
    queued: list[str] = []

    def fake_queue(olt_id: str, **_kwargs) -> QueueDispatchResult:
        queued.append(olt_id)
        return QueueDispatchResult(
            queued=True,
            task_name="app.tasks.ont_runtime_status.refresh_huawei_olt_status",
            queue="ingestion",
        )

    monkeypatch.setattr(
        ont_status_refresh,
        "queue_huawei_olt_status_poll",
        fake_queue,
    )

    result = ont_status_refresh.request_stale_ont_status_refreshes(
        db_session,
        onts,
        now=NOW,
        max_olts=2,
    )

    assert result.queued_olts == 2
    assert result.suppressed_recent_request == 2
    assert queued == olt_ids[2:]
