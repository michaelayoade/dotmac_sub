from types import SimpleNamespace

import pytest

from app.models.network import DeviceStatus
from app.services import zabbix_host_sync
from app.services.zabbix import ZabbixClientError


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


class _DisableFakeClient:
    def __init__(self, fail_host_id: str | None = None) -> None:
        self.disabled: list[tuple[str, int]] = []
        self._fail_host_id = fail_host_id

    def update_host(self, host_id, status=None, **_kwargs):  # noqa: ANN001
        if host_id == self._fail_host_id:
            raise ZabbixClientError("zabbix down")
        self.disabled.append((host_id, status))
        return True


def _stale_device(zabbix_host_id):  # noqa: ANN001
    return SimpleNamespace(
        id=f"dev-{zabbix_host_id}",
        zabbix_host_id=zabbix_host_id,
        zabbix_last_sync_at=None,
    )


def test_disable_stale_hosts_disables_and_counts() -> None:
    client = _DisableFakeClient()
    db = SimpleNamespace(flush=lambda: None)
    rows = [_stale_device("100"), _stale_device(None), _stale_device("300")]

    count = zabbix_host_sync._disable_stale_hosts(
        db, client, device_label="olt", stale_rows=rows
    )

    # The device without a zabbix_host_id is skipped; the other two are disabled.
    assert count == 2
    assert client.disabled == [("100", 1), ("300", 1)]


def test_disable_stale_hosts_tolerates_client_error() -> None:
    client = _DisableFakeClient(fail_host_id="100")
    db = SimpleNamespace(flush=lambda: None)
    rows = [_stale_device("100"), _stale_device("300")]

    count = zabbix_host_sync._disable_stale_hosts(
        db, client, device_label="nas", stale_rows=rows
    )

    # The failing host is logged and skipped; the healthy one still disables.
    assert count == 1
    assert client.disabled == [("300", 1)]


class _SyncFakeClient:
    def __init__(self) -> None:
        self.update_host_kwargs: dict | None = None

    def get_or_create_host_group(self, _name):  # noqa: ANN001
        return "5"

    def get_templates(self, name=None, limit=1000):  # noqa: ANN001
        return [{"templateid": "9"}]

    def update_host(self, **kwargs):
        self.update_host_kwargs = kwargs
        return True

    def get_hosts(self, host_id=None, **_kwargs):  # noqa: ANN001
        return [
            {
                "interfaces": [
                    {
                        "interfaceid": "35",
                        "type": "2",
                        "main": "1",
                        "details": {},
                    }
                ]
            }
        ]

    def update_host_interface(self, _interface_id, **_kwargs):  # noqa: ANN001
        return True


def test_sync_olt_update_path_re_enables_host() -> None:
    client = _SyncFakeClient()
    db = SimpleNamespace(flush=lambda: None)
    olt = SimpleNamespace(
        id="bd2dbc50-90db-4f03-8670-8dc708053f06",
        status=DeviceStatus.active,
        is_active=True,
        zabbix_host_id="10687",
        hostname="olt-a",
        name="OLT A",
        mgmt_ip="172.20.100.18",
        vendor="huawei",
        model="MA5800",
        serial_number="SN1",
        firmware_version="1.0",
        snmp_port=161,
        snmp_version="v2c",
        snmp_bulk_enabled=False,
        snmp_bulk_max_repetitions=1,
        snmp_ro_community=None,
        zabbix_last_sync_at=None,
    )

    zabbix_host_sync.sync_olt_to_zabbix(db, olt, client=client)

    # An active device's host must be asserted enabled so a prior disable is
    # undone on reactivation.
    assert client.update_host_kwargs is not None
    assert client.update_host_kwargs["status"] == 0


class _AdoptFakeClient:
    def __init__(self, *, tag_hosts=None, update_error=None) -> None:
        self._tag_hosts = tag_hosts or []
        self._update_error = update_error
        self.created = False
        self.update_status = None

    def get_hosts_by_tag(self, _tag, _value, limit=2):  # noqa: ANN001
        return self._tag_hosts

    def update_host(self, host_id, status=None, **_kwargs):  # noqa: ANN001
        if self._update_error is not None:
            raise self._update_error
        self.update_status = status
        return True

    def create_host(self, **_kwargs):
        self.created = True
        return "NEW999"


def _call_ensure(client, *, stored_host_id):  # noqa: ANN001
    return zabbix_host_sync._create_or_update_host(
        client,
        dotmac_id="d1",
        stored_host_id=stored_host_id,
        host_name="olt-a",
        display_name="OLT A",
        group_id="5",
        template_ids=None,
        interface_ip="1.2.3.4",
        tags=[],
        inventory={},
        log_prefix="olt",
    )


def test_create_or_update_host_adopts_tagged_host() -> None:
    client = _AdoptFakeClient(tag_hosts=[{"hostid": "777"}])

    host_id = _call_ensure(client, stored_host_id=None)

    # An untracked but tagged host is adopted and updated, not duplicated.
    assert host_id == "777"
    assert client.created is False
    assert client.update_status == 0


def test_create_or_update_host_creates_when_adoption_ambiguous() -> None:
    client = _AdoptFakeClient(tag_hosts=[{"hostid": "1"}, {"hostid": "2"}])

    host_id = _call_ensure(client, stored_host_id=None)

    # Two tagged hosts is ambiguous; don't guess, create a fresh one.
    assert host_id == "NEW999"
    assert client.created is True


def test_create_or_update_host_recreates_on_missing_host() -> None:
    client = _AdoptFakeClient(
        update_error=ZabbixClientError(
            "Zabbix API error: No permissions to referred object or it does not exist!"
        )
    )

    host_id = _call_ensure(client, stored_host_id="STALE")

    # A stale id whose host was deleted out-of-band is recreated.
    assert host_id == "NEW999"
    assert client.created is True


def test_create_or_update_host_reraises_other_errors() -> None:
    client = _AdoptFakeClient(
        update_error=ZabbixClientError("Zabbix API error: invalid parameter")
    )

    with pytest.raises(ZabbixClientError):
        _call_ensure(client, stored_host_id="X")
    assert client.created is False
