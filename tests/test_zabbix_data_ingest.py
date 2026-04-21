from app.models.network import OLTDevice, OntUnit
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
