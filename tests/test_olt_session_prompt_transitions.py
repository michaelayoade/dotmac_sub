"""Huawei session prompts follow the actual CLI mode, without live devices."""

from __future__ import annotations

import re
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.services.network import olt_ssh, olt_ssh_session
from app.services.network.olt_batched_mgmt import (
    BatchedMgmtSpec,
    build_management_command_batch,
    execute_batched_management_setup,
)
from app.services.network.olt_ssh_ont.autofind import query_ont_autofind_session
from app.services.network.olt_ssh_session import CliMode, OltSession
from app.services.network.olt_vendor_adapters import get_olt_adapter


class Shell:
    def __init__(self, host: str) -> None:
        self.host = host
        self.prompt = host + ">"
        self.sent: list[str] = []
        self.queue: list[str] = []
        self.closed = False
        self.eof_received = False
        self.reject_interface = False
        self.reject_exit = False
        self.drop_interface_prompt = False
        self.autofind_pending = False

    def settimeout(self, value: float) -> None:
        pass

    def send(self, value: str) -> int:
        self.sent.append(value)
        command = value.strip()
        if command == "enable":
            self.prompt = self.host + "#"
        elif command == "config":
            self.prompt = self.host + "(config)#"
        elif command.startswith("interface gpon "):
            if self.reject_interface:
                self.queue.append("\r\n% Parameter error\r\n" + self.prompt)
                return len(value)
            self.prompt = self.host + "(config-if-gpon-" + command.split()[-1] + ")#"
            if self.drop_interface_prompt:
                return len(value)
        elif command == "quit":
            if self.reject_exit:
                self.queue.append("\r\n% Parameter error\r\n" + self.prompt)
                return len(value)
            self.prompt = self.host + (
                "(config)#" if "config-if" in self.prompt else "#"
            )
        elif command == "return":
            self.prompt = self.host + "#"
        elif command.startswith("display ont autofind"):
            self.queue.append("\r\n{ <cr>||<K> }:")
            self.autofind_pending = True
            return len(value)
        elif not command and self.autofind_pending:
            self.autofind_pending = False
            fixture = Path(__file__).parent / "fixtures/huawei/display_ont_autofind.txt"
            body = fixture.read_text().split("\n", 1)[1]
            self.queue.append(body + "\r\n" + self.prompt)
            return len(value)
        self.queue.append("\r\n" + self.prompt)
        return len(value)

    def recv(self, size: int) -> bytes:
        # An exhausted fake is an EOF, never a real-time wait in a unit test.
        return self.queue.pop(0).encode() if self.queue else b""


@pytest.fixture
def atomic_send(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        olt_ssh_session,
        "_send",
        lambda olt, channel, command, **kwargs: channel.send(command + "\n"),
    )


@pytest.mark.parametrize("model", ["MA5600", "MA5608T", "MA5800-X2"])
def test_initialization_and_autofind_use_privileged_prompt(
    model: str,
    monkeypatch: pytest.MonkeyPatch,
    atomic_send: None,
) -> None:
    # Hostname deliberately differs from the model identifier.
    shell = Shell("test-shelf")
    transport = Mock()
    olt = SimpleNamespace(name="test", vendor="Huawei", model=model)
    policy = get_olt_adapter(olt).get_ssh_policy(model=model)
    assert policy.paging_off_command is None
    policy = replace(policy, prompt_regex=r"(?:^|\r?\n)test\-shelf>\s*$")
    monkeypatch.setattr(olt_ssh_session, "require", lambda effect: None)
    monkeypatch.setattr(olt_ssh, "_open_shell", lambda olt: (transport, shell, policy))
    with olt_ssh_session.olt_session(olt) as session:
        assert session.current_mode == CliMode.ENABLE
        assert re.search(session.prompt_regex, "test-shelf#")
        assert not re.search(session.prompt_regex, "test-shelf>")
        entries = query_ont_autofind_session(session)
        assert len(entries) == 2
        assert {entry.fsp for entry in entries} == {"0/2/1"}
        assert {entry.model for entry in entries} == {"EG8145V5", "EG8145X6"}
        assert shell.sent == ["enable\n", "display ont autofind all\n", "\n"]
    transport.close.assert_called_once()


def test_mode_transitions_refresh_exact_prompt(atomic_send: None) -> None:
    shell = Shell("arbitrary-shelf")
    session = OltSession(Mock(), Mock(), shell, r"arbitrary\-shelf>\s*$")
    for transition, expected, mode in (
        (session._enter_enable_mode, "arbitrary-shelf#", CliMode.ENABLE),
        (session._enter_config_mode, "arbitrary-shelf(config)#", CliMode.CONFIG),
        (session.exit_config_mode, "arbitrary-shelf#", CliMode.ENABLE),
    ):
        transition()
        assert session.current_mode == mode
        assert re.search(session.prompt_regex, expected)
        assert not re.search(session.prompt_regex, "#")
        assert not re.search(session.prompt_regex, "other-shelf#")
        assert session.run_command("display version").success


def test_failed_enable_does_not_advance_mode(
    monkeypatch: pytest.MonkeyPatch,
    atomic_send: None,
) -> None:
    shell = Shell("shelf")
    session = OltSession(Mock(), Mock(), shell, r"shelf>\s*$")
    monkeypatch.setattr(olt_ssh, "_read_until_prompt", lambda *a, **k: "partial")
    with pytest.raises(TimeoutError):
        session._enter_enable_mode()
    assert session.current_mode == CliMode.USER
    assert session.prompt_regex == r"shelf>\s*$"


def test_per_read_timeout_reports_actual_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(olt_ssh, "_read_until_prompt", lambda *a, **k: "partial")
    with pytest.raises(TimeoutError, match=r"20s per-read budget \(total cap 120s\)"):
        olt_ssh._read_paged_response(
            Shell("shelf"), timeout_sec=120, read_timeout_sec=20
        )


def test_eof_is_not_reported_as_timeout() -> None:
    shell = Shell("shelf")
    shell.eof_received = True
    with pytest.raises(ConnectionError, match="channel closed"):
        olt_ssh._read_paged_response(shell)


def test_total_deadline_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = iter([0.0, 0.0, 121.0])
    monkeypatch.setattr(olt_ssh.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(olt_ssh, "_read_until_prompt", lambda *a, **k: "partial")
    with pytest.raises(TimeoutError, match="120s total deadline"):
        olt_ssh._read_paged_response(Shell("shelf"), timeout_sec=120)


def test_partial_response_is_never_success(
    monkeypatch: pytest.MonkeyPatch,
    atomic_send: None,
) -> None:
    shell = Shell("shelf")
    session = OltSession(Mock(), Mock(), shell, r"shelf#\s*$", CliMode.ENABLE)
    monkeypatch.setattr(olt_ssh, "_read_until_prompt", lambda *a, **k: "partial")
    assert not session.run_command("display version").success


def test_pager_and_ansi_handling_is_preserved(
    monkeypatch: pytest.MonkeyPatch,
    atomic_send: None,
) -> None:
    shell = Shell("shelf")
    session = OltSession(
        Mock(), Mock(), shell, r"(?:^|\r?\n)shelf\#\s*$", CliMode.ENABLE
    )
    responses = iter(
        [
            "\r\nfirst\r\n---- More ( Press 'Q' to break ) ----",
            "\x1b[31mlast\x1b[0m\r\nshelf#",
        ]
    )
    monkeypatch.setattr(olt_ssh, "_read_until_prompt", lambda *a, **k: next(responses))
    result = session.run_command("display version")
    assert result.success
    assert shell.sent == ["display version\n", " "]
    assert "first" in result.output and "last" in result.output
    assert "More" not in result.output and "\x1b" not in result.output


def test_final_prompt_wins_over_continuation_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shell = Shell("shelf")
    response = "\r\n{ <cr>||<K> }:\r\n---- More ----\r\nshelf#"
    monkeypatch.setattr(olt_ssh, "_read_until_prompt", lambda *a, **k: response)
    assert olt_ssh._read_paged_response(shell, r"(?:^|\r?\n)shelf\#\s*$") == response
    assert shell.sent == []


@pytest.mark.parametrize("mode", [CliMode.ENABLE, CliMode.CONFIG])
def test_failed_config_transition_keeps_prompt_and_mode(
    mode: CliMode,
    monkeypatch: pytest.MonkeyPatch,
    atomic_send: None,
) -> None:
    shell = Shell("shelf")
    previous = r"shelf\#\s*$" if mode == CliMode.ENABLE else r"shelf\(config\)\#\s*$"
    session = OltSession(Mock(), Mock(), shell, previous, mode)
    monkeypatch.setattr(olt_ssh, "_read_until_prompt", lambda *a, **k: "partial")
    transition = (
        session._enter_config_mode
        if mode == CliMode.ENABLE
        else session.exit_config_mode
    )
    with pytest.raises(RuntimeError):
        transition()
    assert session.current_mode == mode
    assert session.prompt_regex == previous


@pytest.mark.parametrize("start_mode", [CliMode.CONFIG, CliMode.INTERFACE])
def test_enable_requirement_unwinds_configuration(
    start_mode: CliMode, atomic_send: None
) -> None:
    shell = Shell("shelf")
    suffix = "(config)#" if start_mode == CliMode.CONFIG else "(config-if-gpon-0/1)#"
    shell.prompt = "shelf" + suffix
    pattern = r"(?:^|\r?\n)" + re.escape(shell.prompt) + r"\s*$"
    session = OltSession(
        SimpleNamespace(name="fake"), Mock(), shell, pattern, start_mode
    )
    assert session.run_command("display version", require_mode=CliMode.ENABLE).success
    assert session.current_mode == CliMode.ENABLE
    quits = 1 if start_mode == CliMode.CONFIG else 2
    assert shell.sent == ["quit\n"] * quits + ["display version\n"]


@pytest.mark.parametrize("failure", ["reject_interface", "drop_interface_prompt"])
def test_navigation_failure_blocks_later_writes(
    failure: str, atomic_send: None
) -> None:
    shell = Shell("shelf")
    setattr(shell, failure, True)
    session = OltSession(
        SimpleNamespace(name="fake"), Mock(), shell, r"(?:^|\r?\n)shelf>\s*$"
    )
    assert not session.run_command(
        "interface gpon 0/1", require_mode=CliMode.CONFIG
    ).success
    sent = list(shell.sent)
    assert not session.run_command(
        "ont ipconfig 0 1 dhcp", require_mode=CliMode.CONFIG
    ).success
    assert shell.sent == sent


@pytest.mark.parametrize("reject", [False, True])
def test_complete_batched_management_uses_real_session(
    reject: bool,
    monkeypatch: pytest.MonkeyPatch,
    atomic_send: None,
) -> None:
    shell = Shell("shelf")
    shell.reject_interface = reject
    session = OltSession(
        SimpleNamespace(name="fake"), Mock(), shell, r"(?:^|\r?\n)shelf>\s*$"
    )

    @contextmanager
    def connection(olt):
        yield session

    monkeypatch.setattr(olt_ssh_session, "olt_session", connection)
    spec = BatchedMgmtSpec(
        fsp="0/1/0",
        ont_id_on_olt=1,
        mgmt_vlan_tag=201,
        mgmt_gem_index=2,
        internet_config_ip_index=0,
        wan_config_profile_id=1,
        tr069_profile_id=1,
    )
    result = execute_batched_management_setup(SimpleNamespace(name="fake"), spec)
    assert result.success is not reject
    if reject:
        assert result.steps_failed == ["enter_interface_mode"]
        assert not any(value.startswith("ont ") for value in shell.sent)
    else:
        commands = [cmd + "\n" for cmd, step in build_management_command_batch(spec)]
        assert shell.sent == ["enable\n", "config\n"] + commands
        assert session.current_mode == CliMode.CONFIG
        assert re.search(session.prompt_regex, "shelf(config)#")


def test_generated_command_groups_execute_through_real_session(
    atomic_send: None,
) -> None:
    from app.services.network.olt_command_gen import (
        HuaweiCommandGenerator,
        OntProvisioningContext,
        ProvisioningSpec,
    )

    shell = Shell("shelf")
    session = OltSession(
        SimpleNamespace(name="fake"), Mock(), shell, r"(?:^|\r?\n)shelf>\s*$"
    )
    spec = ProvisioningSpec(
        mgmt_vlan_tag=201,
        internet_config_ip_index=0,
        wan_config_profile_id=1,
        tr069_profile_id=1,
    )
    context = OntProvisioningContext(frame=0, slot=1, port=0, ont_id=1)
    groups = (
        HuaweiCommandGenerator.generate_iphost_commands(spec, context)
        + HuaweiCommandGenerator.generate_internet_config_commands(spec, context)
        + HuaweiCommandGenerator.generate_wan_config_commands(spec, context)
        + HuaweiCommandGenerator.generate_tr069_binding_commands(spec, context)
    )
    assert groups
    for group in groups:
        results = session.run_commands(group.commands, require_mode=CliMode.CONFIG)
        assert len(results) == len(group.commands)
        assert all(result.success for result in results)
        assert session.current_mode == CliMode.CONFIG


@pytest.mark.parametrize("reject", [False, True])
def test_compensation_verifies_navigation_before_undo(
    reject: bool,
    monkeypatch: pytest.MonkeyPatch,
    atomic_send: None,
) -> None:
    from app.models.compensation_failure import CompensationStatus
    from app.services.network import compensation_retry

    shell = Shell("shelf")
    shell.reject_interface = reject
    session = OltSession(
        SimpleNamespace(name="fake"), Mock(), shell, r"(?:^|\r?\n)shelf>\s*$"
    )

    @contextmanager
    def connection(olt):
        yield session

    monkeypatch.setattr(compensation_retry, "olt_session", connection)
    failure = SimpleNamespace(
        status=CompensationStatus.pending,
        step_name="undo_iphost",
        olt_device_id="fake",
        interface_path="0/1",
        undo_commands=["undo ont ipconfig 0 1"],
        failure_count=0,
    )
    db = Mock()
    db.get.side_effect = [failure, SimpleNamespace(name="fake")]
    ok, message = compensation_retry.retry_compensation(db, "fake-failure")
    assert ok is not reject
    assert failure.status == (
        CompensationStatus.pending if reject else CompensationStatus.resolved
    )
    assert any(cmd.startswith("undo ") for cmd in shell.sent) is not reject
    db.commit.assert_called_once()


def test_return_restores_privileged_prompt(atomic_send: None) -> None:
    shell = Shell("shelf")
    session = OltSession(
        SimpleNamespace(name="fake"), Mock(), shell, r"(?:^|\r?\n)shelf>\s*$"
    )
    assert session.run_command(
        "interface gpon 0/1", require_mode=CliMode.CONFIG
    ).success
    assert session.run_command("return").success
    assert session.current_mode == CliMode.ENABLE
    assert session.run_command("display version").success


@pytest.mark.parametrize("failure", ["drop_interface_prompt", "reject_exit"])
def test_batch_fails_on_lost_navigation_or_rejected_exit(
    failure: str,
    monkeypatch: pytest.MonkeyPatch,
    atomic_send: None,
) -> None:
    shell = Shell("shelf")
    setattr(shell, failure, True)
    session = OltSession(
        SimpleNamespace(name="fake"), Mock(), shell, r"(?:^|\r?\n)shelf>\s*$"
    )

    @contextmanager
    def connection(olt):
        yield session

    monkeypatch.setattr(olt_ssh_session, "olt_session", connection)
    result = execute_batched_management_setup(
        SimpleNamespace(name="fake"),
        BatchedMgmtSpec(
            fsp="0/1/0", ont_id_on_olt=1, mgmt_vlan_tag=201, mgmt_gem_index=2
        ),
    )
    assert not result.success
    step = "exit_interface_mode" if failure == "reject_exit" else "enter_interface_mode"
    assert result.steps_failed == [step]
    sent = list(shell.sent)
    assert not session.run_command("display version").success
    assert shell.sent == sent


def test_device_rejection_at_clean_prompt_does_not_poison_session(
    monkeypatch: pytest.MonkeyPatch,
    atomic_send: None,
) -> None:
    shell = Shell("shelf")
    session = OltSession(
        SimpleNamespace(name="fake"), Mock(), shell, r"(?:^|\r?\n)shelf>\s*$"
    )
    session._enter_enable_mode()
    original = shell.send

    def send(value: str) -> int:
        if value.startswith("ont internet-config"):
            shell.sent.append(value)
            shell.queue.append("\r\n% Unknown command\r\nshelf#")
            return len(value)
        return original(value)

    monkeypatch.setattr(shell, "send", send)
    assert not session.run_command("ont internet-config 0 1 ip-index 0").success
    assert session.run_command("display version").success


def test_wrong_interface_prompt_is_not_accepted(
    monkeypatch: pytest.MonkeyPatch,
    atomic_send: None,
) -> None:
    shell = Shell("shelf")
    session = OltSession(
        SimpleNamespace(name="fake"), Mock(), shell, r"(?:^|\r?\n)shelf>\s*$"
    )
    session._enter_config_mode()
    monkeypatch.setattr(
        olt_ssh, "_read_until_prompt", lambda *a, **k: "\r\nshelf(config-if-gpon-0/2)#"
    )
    assert not session.run_command("interface gpon 0/1").success
    assert session.current_mode == CliMode.CONFIG


def test_global_config_command_exits_interface_first(atomic_send: None) -> None:
    shell = Shell("shelf")
    session = OltSession(
        SimpleNamespace(name="fake"), Mock(), shell, r"(?:^|\r?\n)shelf>\s*$"
    )
    assert session.run_command(
        "interface gpon 0/1", require_mode=CliMode.CONFIG
    ).success
    assert session.run_config_command("display version").success
    assert shell.sent[-2:] == ["quit\n", "display version\n"]
    assert session.current_mode == CliMode.CONFIG
