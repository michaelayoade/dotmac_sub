from __future__ import annotations

from app.models.network import OltCard, OltCardPort, OLTDevice, OltShelf
from app.services.network import olt_hardware_discovery as discovery


class FakeZabbixClient:
    def __init__(self, values: dict[str, dict[str, str]]) -> None:
        self.values = values

    def get_snmp_items(self, host_ids: list[str], oid: str, limit: int = 100000):
        return [
            {
                "itemid": f"{oid}.{index}",
                "hostid": host_ids[0],
                "name": f"SNMP {oid}.{index}",
                "key_": f"snmp[{oid}.{index}]",
                "snmp_oid": f"{oid}.{index}" if index else oid,
                "lastvalue": value,
            }
            for index, value in self.values.get(oid, {}).items()
        ]

    def get_items(self, **_kwargs):
        return []


def test_olt_hardware_discovery_requires_zabbix_host(db_session, monkeypatch):
    monkeypatch.setattr(discovery, "zabbix_configured", lambda: True)
    olt = OLTDevice(name="OLT-No-Zabbix", is_active=True)
    db_session.add(olt)
    db_session.commit()

    ok, message, stats = discovery.discover_olt_hardware(db_session, olt)

    assert ok is False
    assert message == "OLT is not linked to a Zabbix host"
    assert stats == {}


def test_olt_hardware_discovery_reads_entity_mib_from_zabbix(db_session, monkeypatch):
    monkeypatch.setattr(discovery, "zabbix_configured", lambda: True)

    values = {
        discovery._SYS_DESCR: {
            "": "Huawei Versatile Routing Platform Software VRP (R) V800R021C10SPC100"
        },
        discovery._ENT_CLASS: {"1": "3", "2": "9", "3": "10"},
        discovery._ENT_CONTAINED_IN: {"1": "0", "2": "1", "3": "2"},
        discovery._ENT_NAME: {
            "1": "Frame 0",
            "2": "Board 1 GPON",
            "3": "Port 0/1/1",
        },
        discovery._ENT_DESCR: {
            "1": "MA5800 chassis",
            "2": "GPBD board",
            "3": "GPON port",
        },
        discovery._ENT_MODEL: {"2": "GPBD"},
        discovery._ENT_SERIAL: {"1": "CHASSIS123", "2": "CARD123"},
        discovery._ENT_HW_REV: {"2": "A"},
        discovery._ENT_FW_REV: {"2": "V1"},
    }
    monkeypatch.setattr(
        discovery.ZabbixClient,
        "from_env",
        staticmethod(lambda: FakeZabbixClient(values)),
    )

    olt = OLTDevice(
        name="OLT-Zabbix-Hardware",
        vendor="Huawei",
        is_active=True,
        zabbix_host_id="10101",
    )
    db_session.add(olt)
    db_session.commit()

    ok, message, stats = discovery.discover_olt_hardware(db_session, olt)

    assert ok is True
    assert message == "Discovered 3 new, updated 0 existing"
    assert stats["shelves_created"] == 1
    assert stats["cards_created"] == 1
    assert stats["ports_created"] == 1
    assert olt.software_version == "V800R021C10SPC100"

    shelf = db_session.query(OltShelf).filter_by(olt_id=olt.id).one()
    assert shelf.serial_number == "CHASSIS123"
    card = db_session.query(OltCard).filter_by(shelf_id=shelf.id).one()
    assert card.model == "GPBD"
    assert card.serial_number == "CARD123"
    port = db_session.query(OltCardPort).filter_by(card_id=card.id).one()
    assert port.port_number == 1
