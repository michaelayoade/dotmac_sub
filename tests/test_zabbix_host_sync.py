from types import SimpleNamespace

from app.services import zabbix_host_sync


class _FakeZabbixClient:
    def __init__(self) -> None:
        self.updated_details = None

    def get_hosts(self, host_id=None, **_kwargs):  # noqa: ANN001
        assert host_id == "10687"
        return [
            {
                "interfaces": [
                    {
                        "interfaceid": "35",
                        "type": "2",
                        "main": "1",
                        "details": {
                            "version": "2",
                            "bulk": "0",
                            "community": "{$SNMP_COMMUNITY}",
                        },
                    }
                ]
            }
        ]

    def update_host_interface(self, _interface_id, **kwargs):  # noqa: ANN001
        self.updated_details = kwargs["details"]
        return True


def _olt(snmp_ro_community: str):
    return SimpleNamespace(
        id="bd2dbc50-90db-4f03-8670-8dc708053f06",
        zabbix_host_id="10687",
        mgmt_ip="172.20.100.18",
        snmp_port=161,
        snmp_version="v2c",
        snmp_bulk_enabled=False,
        snmp_bulk_max_repetitions=1,
        snmp_ro_community=snmp_ro_community,
    )


def test_sync_olt_snmp_interface_replaces_macro_with_olt_community(monkeypatch) -> None:
    monkeypatch.setattr(
        zabbix_host_sync,
        "decrypt_credential",
        lambda value: "resolved-community" if value == "enc:ok" else value,
    )
    client = _FakeZabbixClient()

    zabbix_host_sync._sync_olt_snmp_interface(client, _olt("enc:ok"))

    assert client.updated_details["community"] == "resolved-community"
    assert client.updated_details["bulk"] == "0"
    assert client.updated_details["max_repetitions"] == "1"


def test_sync_olt_snmp_interface_preserves_existing_community_when_unreadable(
    monkeypatch,
) -> None:
    def _raise(_value):  # noqa: ANN001
        raise ValueError("invalid token")

    monkeypatch.setattr(zabbix_host_sync, "decrypt_credential", _raise)
    client = _FakeZabbixClient()

    zabbix_host_sync._sync_olt_snmp_interface(client, _olt("enc:bad"))

    assert client.updated_details["community"] == "{$SNMP_COMMUNITY}"
