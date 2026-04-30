from app.models.network import OLTDevice, OntUnit, OnuOnlineStatus
from app.services import zabbix_data_ingest


class _FakeZabbixClient:
    def __init__(self, items):
        self._items = items

    def get_items(self, **_kwargs):
        return self._items


def test_ingest_olt_signal_data_clears_stale_invalid_olt_rx(db_session) -> None:
    olt = OLTDevice(
        name="OLT-ZABBIX-SIGNAL",
        mgmt_ip="198.51.100.55",
        is_active=True,
        zabbix_host_id="10101",
    )
    db_session.add(olt)
    db_session.flush()

    ont = OntUnit(
        serial_number="ONT-ZABBIX-SIGNAL-001",
        is_active=True,
        olt_device_id=olt.id,
        external_id="0/1/0.5",
        olt_rx_signal_dbm=21474836.47,
    )
    db_session.add(ont)
    db_session.commit()

    walk_output = (
        ".1.3.6.1.4.1.2011.6.128.1.1.2.51.1.4.4194318336.5 = INTEGER: 2147483647"
    )
    updated = zabbix_data_ingest.ingest_olt_signal_data(
        db_session,
        olt,
        client=_FakeZabbixClient(
            [{"key_": "opt.rx.walk", "lastvalue": walk_output}]
        ),
    )

    db_session.refresh(ont)

    assert updated == 1
    assert ont.olt_rx_signal_dbm is None


def test_ingest_marks_missing_ont_offline(db_session) -> None:
    """ONT previously online but not in poll data should be marked offline."""
    olt = OLTDevice(
        name="OLT-OFFLINE-TEST",
        mgmt_ip="198.51.100.56",
        is_active=True,
        zabbix_host_id="10102",
    )
    db_session.add(olt)
    db_session.flush()

    # ONT that was online but won't appear in the poll
    ont_missing = OntUnit(
        serial_number="ONT-MISSING-001",
        is_active=True,
        olt_device_id=olt.id,
        external_id="0/1/0.10",
        olt_status=OnuOnlineStatus.online,
    )
    # ONT that will appear online in poll
    ont_present = OntUnit(
        serial_number="ONT-PRESENT-001",
        is_active=True,
        olt_device_id=olt.id,
        external_id="0/1/0.5",
        olt_status=OnuOnlineStatus.online,
    )
    db_session.add_all([ont_missing, ont_present])
    db_session.commit()

    # Poll only returns ont_present (external_id 0/1/0.5)
    walk_output = ".1.3.6.1.4.1.2011.6.128.1.1.2.51.1.4.4194318336.5 = INTEGER: -2000"
    updated = zabbix_data_ingest.ingest_olt_signal_data(
        db_session,
        olt,
        client=_FakeZabbixClient([{"key_": "opt.rx.walk", "lastvalue": walk_output}]),
    )

    db_session.refresh(ont_missing)
    db_session.refresh(ont_present)

    # ont_present should still be online, ont_missing should be offline
    assert ont_present.olt_status == OnuOnlineStatus.online
    assert ont_missing.olt_status == OnuOnlineStatus.offline
    assert ont_missing.consecutive_offline_polls == 1
    assert updated == 2  # 1 online + 1 missing


def test_ingest_detects_explicit_offline_status(db_session) -> None:
    """ONT with explicit offline status code should be marked offline."""
    olt = OLTDevice(
        name="OLT-STATUS-TEST",
        mgmt_ip="198.51.100.57",
        is_active=True,
        zabbix_host_id="10103",
    )
    db_session.add(olt)
    db_session.flush()

    ont = OntUnit(
        serial_number="ONT-STATUS-001",
        is_active=True,
        olt_device_id=olt.id,
        external_id="0/1/0.5",
        olt_status=OnuOnlineStatus.online,
    )
    db_session.add(ont)
    db_session.commit()

    # Status code 4 = offline (LOS)
    walk_output = ".1.3.6.1.4.1.2011.6.128.1.1.2.51.1.4.4194318336.5 = INTEGER: 4"
    updated = zabbix_data_ingest.ingest_olt_signal_data(
        db_session,
        olt,
        client=_FakeZabbixClient([{"key_": "ont.status.walk", "lastvalue": walk_output}]),
    )

    db_session.refresh(ont)

    assert ont.olt_status == OnuOnlineStatus.offline
    assert ont.consecutive_offline_polls == 1
    assert updated == 1
