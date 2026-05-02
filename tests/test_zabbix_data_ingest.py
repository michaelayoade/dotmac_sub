from datetime import UTC, datetime, timedelta

from app.models.network import OLTDevice, OntUnit, OnuOfflineReason, OnuOnlineStatus
from app.services.zabbix_data_ingest import ingest_olt_signal_data


class _FakeZabbixClient:
    def __init__(self, items):
        self._items = items

    def get_items(self, **_kwargs):
        return self._items


def _ifindex(snmp_slot: int, port: int) -> int:
    return 4194304000 + (snmp_slot * 8192) + (port * 256)


def _walk(ifindex: int, ont_index: int, value: int | float) -> str:
    return (
        ".1.3.6.1.4.1.2011.6.128.1.1.2.51.1.4."
        f"{ifindex}.{ont_index} = INTEGER: {value}"
    )


def _olt(db_session) -> OLTDevice:
    olt = OLTDevice(name="Zabbix OLT", zabbix_host_id="10101", is_active=True)
    db_session.add(olt)
    db_session.commit()
    return olt


def _ont(db_session, olt: OLTDevice, serial: str, **kwargs) -> OntUnit:
    ont = OntUnit(serial_number=serial, olt_device_id=olt.id, is_active=True, **kwargs)
    db_session.add(ont)
    db_session.commit()
    db_session.refresh(ont)
    return ont


def test_ingest_matches_full_fsp_external_id(db_session) -> None:
    olt = _olt(db_session)
    ont = _ont(db_session, olt, "ONT-FSP", external_id="0/1/0.5")

    updated = ingest_olt_signal_data(
        db_session,
        olt,
        client=_FakeZabbixClient(
            [{"key_": "opt.rx.walk", "lastvalue": _walk(_ifindex(7, 0), 5, -2318)}]
        ),
    )

    db_session.refresh(ont)
    assert updated == 1
    assert ont.olt_status == OnuOnlineStatus.online
    assert ont.olt_rx_signal_dbm == -23.18
    assert ont.last_sync_source == "zabbix_data_ingest"


def test_ingest_matches_huawei_encoded_external_id(db_session) -> None:
    olt = _olt(db_session)
    ifindex = _ifindex(4, 1)
    ont = _ont(db_session, olt, "ONT-HUAWEI", external_id=f"huawei:{ifindex}.7")

    updated = ingest_olt_signal_data(
        db_session,
        olt,
        client=_FakeZabbixClient(
            [{"key_": "opt.rx.walk", "lastvalue": _walk(ifindex, 7, -2113)}]
        ),
    )

    db_session.refresh(ont)
    assert updated == 1
    assert ont.olt_status == OnuOnlineStatus.online
    assert ont.olt_rx_signal_dbm == -21.13


def test_ingest_matches_numeric_external_id_with_board_and_port(db_session) -> None:
    olt = _olt(db_session)
    ont = _ont(
        db_session,
        olt,
        "HWTC600AC29C",
        external_id="7",
        board="0/2",
        port="1",
    )

    updated = ingest_olt_signal_data(
        db_session,
        olt,
        client=_FakeZabbixClient(
            [{"key_": "opt.rx.walk", "lastvalue": _walk(_ifindex(8, 1), 7, -2060)}]
        ),
    )

    db_session.refresh(ont)
    assert updated == 1
    assert ont.olt_status == OnuOnlineStatus.online
    assert ont.olt_rx_signal_dbm == -20.6


def test_ingest_clears_invalid_olt_rx_without_leaving_stale_signal(db_session) -> None:
    olt = _olt(db_session)
    ont = _ont(
        db_session,
        olt,
        "ONT-INVALID-RX",
        external_id="0/1/0.5",
        olt_rx_signal_dbm=-23.0,
    )

    updated = ingest_olt_signal_data(
        db_session,
        olt,
        client=_FakeZabbixClient(
            [
                {
                    "key_": "opt.rx.walk",
                    "lastvalue": _walk(_ifindex(7, 0), 5, 2147483647),
                }
            ]
        ),
    )

    db_session.refresh(ont)
    assert updated == 1
    assert ont.olt_status == OnuOnlineStatus.online
    assert ont.olt_rx_signal_dbm is None


def test_ingest_persists_explicit_los_status(db_session) -> None:
    olt = _olt(db_session)
    ont = _ont(db_session, olt, "ONT-LOS", external_id="0/1/0.5")

    updated = ingest_olt_signal_data(
        db_session,
        olt,
        client=_FakeZabbixClient(
            [{"key_": "ont.status.walk", "lastvalue": _walk(_ifindex(7, 0), 5, 4)}]
        ),
    )

    db_session.refresh(ont)
    assert updated == 1
    assert ont.olt_status == OnuOnlineStatus.offline
    assert ont.offline_reason == OnuOfflineReason.los


def test_ingest_marks_missing_previously_online_mapped_ont_offline(db_session) -> None:
    olt = _olt(db_session)
    now = datetime.now(UTC) - timedelta(minutes=5)
    present = _ont(db_session, olt, "ONT-PRESENT", external_id="0/1/0.5")
    missing = _ont(
        db_session,
        olt,
        "ONT-MISSING",
        external_id="0/1/0.6",
        olt_status=OnuOnlineStatus.online,
        olt_status_seen_at=now,
        last_seen_at=now,
    )

    updated = ingest_olt_signal_data(
        db_session,
        olt,
        client=_FakeZabbixClient(
            [{"key_": "opt.rx.walk", "lastvalue": _walk(_ifindex(7, 0), 5, -2318)}]
        ),
    )

    db_session.refresh(present)
    db_session.refresh(missing)
    assert updated == 2
    assert present.olt_status == OnuOnlineStatus.online
    assert missing.olt_status == OnuOnlineStatus.offline
    assert missing.offline_reason == OnuOfflineReason.los


def test_ingest_does_not_force_unmapped_online_ont_offline(db_session) -> None:
    olt = _olt(db_session)
    now = datetime.now(UTC) - timedelta(minutes=5)
    mapped = _ont(db_session, olt, "ONT-MAPPED", external_id="0/1/0.5")
    unmapped = _ont(
        db_session,
        olt,
        "ONT-UNMAPPED",
        external_id="not-parseable",
        olt_status=OnuOnlineStatus.online,
        olt_status_seen_at=now,
        last_seen_at=now,
    )

    updated = ingest_olt_signal_data(
        db_session,
        olt,
        client=_FakeZabbixClient(
            [{"key_": "opt.rx.walk", "lastvalue": _walk(_ifindex(7, 0), 5, -2318)}]
        ),
    )

    db_session.refresh(mapped)
    db_session.refresh(unmapped)
    assert updated == 1
    assert mapped.olt_status == OnuOnlineStatus.online
    assert unmapped.olt_status == OnuOnlineStatus.online
