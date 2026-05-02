from datetime import UTC, datetime
from types import SimpleNamespace

from app.services import zabbix_ont_status


class _FakeZabbixClient:
    def __init__(self, items):
        self._items = items

    def get_items(self, **_kwargs):
        return self._items


def _olt():
    return SimpleNamespace(zabbix_host_id="10101")


def _ont(ont_id: str, external_id: str, **kwargs):
    return SimpleNamespace(id=ont_id, external_id=external_id, **kwargs)


def test_snapshot_marks_status_code_one_online(monkeypatch) -> None:
    monkeypatch.setattr(zabbix_ont_status, "zabbix_configured", lambda: True)
    walk_output = (
        ".1.3.6.1.4.1.2011.6.128.1.1.2.51.1.4.4194318336.5 = INTEGER: 1"
    )

    snapshot = zabbix_ont_status.get_olt_ont_snapshot_from_zabbix(
        _olt(),
        [_ont("ont-1", "0/1/0.5")],
        client=_FakeZabbixClient(
            [
                {
                    "key_": "ont.status.walk",
                    "lastvalue": walk_output,
                    "lastclock": "1714564800",
                }
            ]
        ),
    )

    assert snapshot["ont-1"].status == "online"
    assert snapshot["ont-1"].updated_at == datetime.fromtimestamp(1714564800, tz=UTC)


def test_snapshot_marks_missing_or_non_one_status_offline(monkeypatch) -> None:
    monkeypatch.setattr(zabbix_ont_status, "zabbix_configured", lambda: True)
    walk_output = (
        ".1.3.6.1.4.1.2011.6.128.1.1.2.51.1.4.4194318336.5 = INTEGER: 2"
    )

    snapshot = zabbix_ont_status.get_olt_ont_snapshot_from_zabbix(
        _olt(),
        [
            _ont("present", "0/1/0.5"),
            _ont("missing", "0/1/0.10"),
        ],
        client=_FakeZabbixClient(
            [
                {
                    "key_": "ont.status.walk",
                    "lastvalue": walk_output,
                    "lastclock": "1714564800",
                }
            ]
        ),
    )

    assert snapshot["present"].status == "offline"
    assert snapshot["missing"].status == "offline"


def test_snapshot_uses_valid_rx_signal_as_online(monkeypatch) -> None:
    monkeypatch.setattr(zabbix_ont_status, "zabbix_configured", lambda: True)
    walk_output = (
        ".1.3.6.1.4.1.2011.6.128.1.1.2.51.1.4.4194318336.5 = INTEGER: -2318"
    )

    snapshot = zabbix_ont_status.get_olt_ont_snapshot_from_zabbix(
        _olt(),
        [_ont("ont-1", "0/1/0.5")],
        client=_FakeZabbixClient(
            [
                {
                    "key_": "opt.rx.walk",
                    "lastvalue": walk_output,
                    "lastclock": "1714564800",
                }
            ]
        ),
    )

    assert snapshot["ont-1"].status == "online"
    assert snapshot["ont-1"].olt_rx_dbm == -23.18


def test_snapshot_matches_huawei_encoded_external_id(monkeypatch) -> None:
    monkeypatch.setattr(zabbix_ont_status, "zabbix_configured", lambda: True)
    walk_output = (
        ".1.3.6.1.4.1.2011.6.128.1.1.2.51.1.4.4194312448.0 = INTEGER: -2113"
    )

    snapshot = zabbix_ont_status.get_olt_ont_snapshot_from_zabbix(
        _olt(),
        [_ont("ont-1", "huawei:4194312448.0")],
        client=_FakeZabbixClient(
            [
                {
                    "key_": "opt.rx.walk",
                    "lastvalue": walk_output,
                    "lastclock": "1714564800",
                }
            ]
        ),
    )

    assert snapshot["ont-1"].status == "online"
    assert snapshot["ont-1"].olt_rx_dbm == -21.13


def test_snapshot_matches_numeric_external_id_with_board_and_port(monkeypatch) -> None:
    monkeypatch.setattr(zabbix_ont_status, "zabbix_configured", lambda: True)
    walk_output = (
        ".1.3.6.1.4.1.2011.6.128.1.1.2.51.1.4.4194320640.7 = INTEGER: -2060"
    )

    snapshot = zabbix_ont_status.get_olt_ont_snapshot_from_zabbix(
        _olt(),
        [_ont("ont-1", "7", board="0/2", port="1")],
        client=_FakeZabbixClient(
            [
                {
                    "key_": "opt.rx.walk",
                    "lastvalue": walk_output,
                    "lastclock": "1714564800",
                }
            ]
        ),
    )

    assert snapshot["ont-1"].status == "online"
    assert snapshot["ont-1"].olt_rx_dbm == -20.6
