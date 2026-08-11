from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.network.huawei_cli_response import HuaweiCliErrorCode
from app.services.network.olt_ssh_session import CommandResult


def test_build_autofind_command_all_and_port() -> None:
    from app.services.network.olt_ssh_ont.autofind import build_autofind_command

    assert build_autofind_command() == "display ont autofind all"
    assert build_autofind_command("0/2/11") == "display ont autofind 0/2/11"

    with pytest.raises(Exception):
        build_autofind_command("0/2")


def test_parse_autofind_output_uses_existing_huawei_parser() -> None:
    from app.services.network.olt_ssh_ont.autofind import parse_autofind_output

    output = Path("tests/fixtures/huawei/display_ont_autofind.txt").read_text()
    entries = parse_autofind_output(output)

    assert entries
    assert entries[0].fsp
    assert entries[0].serial_number


def test_query_ont_autofind_session_runs_display_command() -> None:
    from app.services.network.olt_ssh_ont.autofind import query_ont_autofind_session

    output = Path("tests/fixtures/huawei/display_ont_autofind.txt").read_text()

    class FakeSession:
        def __init__(self) -> None:
            self.calls = []

        def run_command(self, command, **kwargs):
            self.calls.append((command, kwargs))
            return CommandResult(success=True, output=output)

    session = FakeSession()
    entries = query_ont_autofind_session(session, port="0/2/11")

    assert entries
    assert session.calls == [
        (
            "display ont autofind 0/2/11",
            {"timeout_sec": 20, "slow_send": False},
        )
    ]


def test_query_ont_autofind_session_raises_on_olt_error() -> None:
    from app.services.network.olt_ssh_ont.autofind import (
        AutofindCommandError,
        query_ont_autofind_session,
    )

    class FakeSession:
        def run_command(self, *_args, **_kwargs):
            return CommandResult(
                success=False,
                output="% Parameter error",
                error_code=HuaweiCliErrorCode.PARAMETER_ERROR,
                message="bad",
            )

    with pytest.raises(AutofindCommandError, match="bad") as raised:
        query_ont_autofind_session(FakeSession())

    assert raised.value.error_code is HuaweiCliErrorCode.PARAMETER_ERROR


def test_scoped_unknown_command_falls_back_to_global_inventory(monkeypatch) -> None:
    from app.services.network.olt_ssh_ont import autofind

    output = Path("tests/fixtures/huawei/display_ont_autofind.txt").read_text()
    calls: list[str] = []

    class FakeSession:
        def run_command(self, command, **_kwargs):
            calls.append(command)
            if command != "display ont autofind all":
                return CommandResult(
                    success=False,
                    output="% Unknown command",
                    error_code=HuaweiCliErrorCode.UNKNOWN_COMMAND,
                    message="Error: unknown_command",
                )
            return CommandResult(success=True, output=output)

    @contextmanager
    def fake_olt_session(_olt):
        yield FakeSession()

    monkeypatch.setattr(autofind, "olt_session", fake_olt_session)
    olt = SimpleNamespace(
        name="Garki Huawei OLT",
        vendor="Huawei",
        model="MA5800-X2",
        firmware_version="V800R019C00",
        software_version=None,
    )

    ok, _message, entries = autofind.query_ont_autofind(olt, port="0/2/1")

    assert ok is True
    assert {entry.fsp for entry in entries} == {"0/2/1"}
    assert calls == [
        "display ont autofind 0/2/1",
        "display ont autofind all",
    ]


def test_global_unknown_command_retries_once_in_fresh_session(monkeypatch) -> None:
    from app.services.network.olt_ssh_ont import autofind

    output = Path("tests/fixtures/huawei/display_ont_autofind.txt").read_text()
    session_count = 0
    calls: list[str] = []

    class FakeSession:
        def __init__(self, *, reject: bool) -> None:
            self.reject = reject

        def run_command(self, command, **_kwargs):
            calls.append(command)
            if self.reject:
                return CommandResult(
                    success=False,
                    output="% Unknown command",
                    error_code=HuaweiCliErrorCode.UNKNOWN_COMMAND,
                    message="Error: unknown_command",
                )
            return CommandResult(success=True, output=output)

    @contextmanager
    def fake_olt_session(_olt):
        nonlocal session_count
        session_count += 1
        yield FakeSession(reject=session_count == 1)

    monkeypatch.setattr(autofind, "olt_session", fake_olt_session)
    olt = SimpleNamespace(
        name="Garki Huawei OLT",
        vendor="Huawei",
        model="MA5800-X2",
        firmware_version="V800R019C00",
        software_version=None,
    )

    ok, _message, entries = autofind.query_ont_autofind(olt)

    assert ok is True
    assert entries
    assert session_count == 2
    assert calls == ["display ont autofind all", "display ont autofind all"]


def test_global_non_grammar_error_is_not_retried(monkeypatch) -> None:
    from app.services.network.olt_ssh_ont import autofind

    session_count = 0

    class FakeSession:
        def run_command(self, _command, **_kwargs):
            return CommandResult(
                success=False,
                output="% Parameter error",
                error_code=HuaweiCliErrorCode.PARAMETER_ERROR,
                message="Error: parameter_error",
            )

    @contextmanager
    def fake_olt_session(_olt):
        nonlocal session_count
        session_count += 1
        yield FakeSession()

    monkeypatch.setattr(autofind, "olt_session", fake_olt_session)

    ok, message, entries = autofind.query_ont_autofind(SimpleNamespace())

    assert ok is False
    assert message == "Autofind query failed: Error: parameter_error"
    assert entries == []
    assert session_count == 1


def test_ma5608t_scoped_query_uses_global_command_and_filters_exact_fsp(
    monkeypatch,
) -> None:
    from app.services.network.olt_ssh_ont import autofind

    output = Path("tests/fixtures/huawei/display_ont_autofind.txt").read_text()

    class FakeSession:
        def __init__(self) -> None:
            self.calls = []

        def run_command(self, command, **kwargs):
            self.calls.append((command, kwargs))
            return CommandResult(success=True, output=output)

    session = FakeSession()

    @contextmanager
    def fake_olt_session(_olt):
        yield session

    monkeypatch.setattr(autofind, "olt_session", fake_olt_session)
    olt = SimpleNamespace(
        vendor="Huawei",
        model="MA5608T",
        firmware_version="V800R013C00",
        software_version=None,
    )

    ok, _message, entries = autofind.query_ont_autofind(olt, port="0/2/1")

    assert ok is True
    assert entries
    assert {entry.fsp for entry in entries} == {"0/2/1"}
    assert session.calls == [
        (
            "display ont autofind all",
            {"timeout_sec": 20, "slow_send": False},
        )
    ]

    ok, _message, entries = autofind.query_ont_autofind(olt, port="0/2/9")

    assert ok is True
    assert entries == []
    assert session.calls[-1] == (
        "display ont autofind all",
        {"timeout_sec": 20, "slow_send": False},
    )
