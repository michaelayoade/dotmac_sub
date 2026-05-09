from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "huawei"


def _load_fixture(name: str) -> str:
    return (FIXTURES_DIR / name).read_text()


def test_parse_active_alarms_key_value_blocks() -> None:
    from app.services.network.olt_ssh_diagnostics import parse_active_alarms

    alarms = parse_active_alarms(_load_fixture("display_alarm_active_all.txt"))

    assert len(alarms) == 2
    assert alarms[0].sequence == 1024
    assert alarms[0].alarm_id == 0x2E11
    assert alarms[0].severity == "major"
    assert alarms[0].name == "The ONT is offline"
    assert alarms[0].source == "FrameID:0, SlotID:2, PortID:11, ONT ID:13"
    assert alarms[0].raised_at == datetime(2026, 5, 8, 9, 29, 14)
    assert alarms[1].severity == "warning"


def test_parse_active_alarms_line_fallback() -> None:
    from app.services.network.olt_ssh_diagnostics import parse_active_alarms

    alarms = parse_active_alarms(
        """
        2026-05-08 09:29:14 Alarm ID 0x2e11 Major FrameID:0 SlotID:2 ONT offline
        """
    )

    assert len(alarms) == 1
    assert alarms[0].alarm_id == 0x2E11
    assert alarms[0].severity == "major"
    assert alarms[0].raised_at == datetime(2026, 5, 8, 9, 29, 14)


def test_parse_ont_traffic_stats() -> None:
    from app.services.network.olt_ssh_diagnostics import parse_ont_traffic

    stats = parse_ont_traffic(
        _load_fixture("display_ont_traffic.txt"),
        fsp="0/2/11",
        ont_id=13,
    )

    assert stats.fsp == "0/2/11"
    assert stats.ont_id == 13
    assert stats.upstream_bytes == 123456789
    assert stats.downstream_bytes == 987654321
    assert stats.upstream_packets == 12345
    assert stats.downstream_packets == 54321
    assert stats.upstream_rate_kbps == 1024
    assert stats.downstream_rate_kbps == 2048.5


def test_parse_ont_optical_info() -> None:
    from app.services.network.olt_ssh_diagnostics import parse_ont_optical_info

    info = parse_ont_optical_info(
        _load_fixture("display_ont_optical_info.txt"),
        fsp="0/2/11",
        ont_id=13,
    )

    assert info.fsp == "0/2/11"
    assert info.ont_id == 13
    assert info.rx_power_dbm == -20.13
    assert info.tx_power_dbm == 2.41
    assert info.olt_rx_power_dbm == -21.55
    assert info.temperature_c == 42.0
    assert info.voltage_v == 3.31
    assert info.bias_current_ma == 15.2


def test_get_active_alarms_runs_display_command(monkeypatch) -> None:
    from app.services.network import olt_ssh_diagnostics

    sent: list[str] = []

    class FakeChannel:
        def send(self, command: str) -> None:
            sent.append(command)

    class FakeTransport:
        def close(self) -> None:
            sent.append("close")

    def fake_run(_channel, command, **_kwargs):
        sent.append(command)
        if command == "display alarm active all":
            return _load_fixture("display_alarm_active_all.txt")
        return ""

    monkeypatch.setattr(
        "app.services.network.olt_ssh._open_shell",
        lambda _olt: (FakeTransport(), FakeChannel(), None),
    )
    monkeypatch.setattr(
        "app.services.network.olt_ssh._read_until_prompt",
        lambda *_args, **_kwargs: "",
    )
    monkeypatch.setattr("app.services.network.olt_ssh._run_huawei_cmd", fake_run)

    ok, message, alarms = olt_ssh_diagnostics.get_active_alarms(
        SimpleNamespace(name="OLT 1")
    )

    assert ok is True
    assert message == "Found 2 active alarm(s)"
    assert sent[:3] == [
        "enable\n",
        "screen-length 0 temporary\n",
        "display alarm active all",
    ]
    assert sent[-1] == "close"
    assert alarms[0].name == "The ONT is offline"


def test_get_ont_optical_info_runs_profiled_display_command(monkeypatch) -> None:
    from app.services.network import olt_ssh_diagnostics

    sent: list[str] = []

    class FakeChannel:
        def send(self, command: str) -> None:
            sent.append(command)

    class FakeTransport:
        def close(self) -> None:
            sent.append("close")

    def fake_run(_channel, command, **_kwargs):
        sent.append(command)
        if command == "display ont optical-info 0/2 11 13":
            return _load_fixture("display_ont_optical_info.txt")
        return ""

    monkeypatch.setattr(
        "app.services.network.olt_ssh._open_shell",
        lambda _olt: (FakeTransport(), FakeChannel(), None),
    )
    monkeypatch.setattr(
        "app.services.network.olt_ssh._read_until_prompt",
        lambda *_args, **_kwargs: "",
    )
    monkeypatch.setattr("app.services.network.olt_ssh._run_huawei_cmd", fake_run)

    ok, message, info = olt_ssh_diagnostics.get_ont_optical_info(
        SimpleNamespace(
            name="OLT 1",
            model="MA5800-X2",
            firmware_version="V100R019C11",
            software_version=None,
        ),
        "0/2/11",
        13,
    )

    assert ok is True
    assert message == "ONT optical info read"
    assert info is not None
    assert info.rx_power_dbm == -20.13
    assert sent[:3] == [
        "enable\n",
        "screen-length 0 temporary\n",
        "display ont optical-info 0/2 11 13",
    ]
    assert sent[-1] == "close"


def test_get_ont_info_runs_profiled_display_command(monkeypatch) -> None:
    from app.services.network import olt_ssh_diagnostics

    sent: list[str] = []

    class FakeChannel:
        def send(self, command: str) -> None:
            sent.append(command)

    class FakeTransport:
        def close(self) -> None:
            sent.append("close")

    def fake_run(_channel, command, **_kwargs):
        sent.append(command)
        if command == "display ont info 0/2 11 13":
            return _load_fixture("display_ont_info.txt")
        return ""

    monkeypatch.setattr(
        "app.services.network.olt_ssh._open_shell",
        lambda _olt: (FakeTransport(), FakeChannel(), None),
    )
    monkeypatch.setattr(
        "app.services.network.olt_ssh._read_until_prompt",
        lambda *_args, **_kwargs: "",
    )
    monkeypatch.setattr("app.services.network.olt_ssh._run_huawei_cmd", fake_run)

    ok, message, info = olt_ssh_diagnostics.get_ont_info(
        SimpleNamespace(
            name="OLT 1",
            model="MA5800-X2",
            firmware_version="V100R019C11",
            software_version=None,
        ),
        "0/2/11",
        13,
    )

    assert ok is True
    assert message == "ONT info read"
    assert info is not None
    assert sent[:3] == [
        "enable\n",
        "screen-length 0 temporary\n",
        "display ont info 0/2 11 13",
    ]
    assert sent[-1] == "close"


def test_get_ont_traffic_stats_runs_display_command(monkeypatch) -> None:
    from app.services.network import olt_ssh_diagnostics

    sent: list[str] = []

    class FakeChannel:
        def send(self, command: str) -> None:
            sent.append(command)

    class FakeTransport:
        def close(self) -> None:
            sent.append("close")

    def fake_run(_channel, command, **_kwargs):
        sent.append(command)
        if command == "display ont traffic 0/2/11 13":
            return _load_fixture("display_ont_traffic.txt")
        return ""

    monkeypatch.setattr(
        "app.services.network.olt_ssh._open_shell",
        lambda _olt: (FakeTransport(), FakeChannel(), None),
    )
    monkeypatch.setattr(
        "app.services.network.olt_ssh._read_until_prompt",
        lambda *_args, **_kwargs: "",
    )
    monkeypatch.setattr("app.services.network.olt_ssh._run_huawei_cmd", fake_run)

    ok, message, stats = olt_ssh_diagnostics.get_ont_traffic_stats(
        SimpleNamespace(name="OLT 1"),
        "0/2/11",
        13,
    )

    assert ok is True
    assert message == "ONT traffic stats read"
    assert stats is not None
    assert stats.downstream_bytes == 987654321
    assert sent[:3] == [
        "enable\n",
        "screen-length 0 temporary\n",
        "display ont traffic 0/2/11 13",
    ]
    assert sent[-1] == "close"
