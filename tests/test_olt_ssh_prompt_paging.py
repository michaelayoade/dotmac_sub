"""Prompt, paging and dirty-channel handling for OLT SSH reads (G4 PR-B).

Prompt shapes follow tests/test_olt_ssh_bootstrap.py: ``boi-olt>`` before
``enable`` and ``boi-olt#`` after it. No real OLT is contacted.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from app.services.network import olt_ssh

USER_PROMPT = r"(?:^|\r?\n)boi\-olt>\s*$"
OLT = SimpleNamespace(id="olt-1", name="BOI")
OPTIONS_WITH_CR = "{ <cr>|gemport<K>|sort-by<K> }:"
OPTIONS_NO_CR = "{ frameid/slotid<S>|ontid<K> }:"


class ScriptedChannel:
    """Replies to ``enable`` / ``screen-length`` itself; the rest is scripted.

    ``replies`` holds one list of chunks per non-setup send (command, space or
    Enter), consumed in order.
    """

    def __init__(self, replies: list[list[str]] | None = None) -> None:
        self.sent: list[str] = []
        self.replies = list(replies or [])
        self.queue: list[str] = []
        self.closed = False

    def settimeout(self, _value: float) -> None:
        return None

    def send(self, value: str) -> None:
        self.sent.append(value)
        if value == "enable\n":
            self.queue.append("enable\r\nboi-olt#")
        elif value == "screen-length 0 temporary\n":
            self.queue.append("screen-length 0 temporary\r\nboi-olt#")
        elif self.replies:
            self.queue.extend(self.replies.pop(0))

    def recv(self, _size: int) -> bytes:
        if self.queue:
            return self.queue.pop(0).encode()
        time.sleep(0.01)
        raise TimeoutError


class FakePool:
    def __init__(self, channel: ScriptedChannel) -> None:
        self.channel = channel
        self.release_close_flags: list[bool] = []
        self._stats = {"errors": 0}

    def acquire(self, _olt):
        return SimpleNamespace(
            channel=self.channel, policy=SimpleNamespace(prompt_regex=USER_PROMPT)
        )

    def release(self, _conn, *, close: bool = False) -> None:
        self.release_close_flags.append(close)


class NullCache:
    def get(self, *_a, **_k):
        return None

    def set(self, *_a, **_k) -> None:
        return None


@pytest.fixture
def pool(monkeypatch):
    from app.services.network import olt_read_cache, olt_ssh_pool

    monkeypatch.setattr(olt_read_cache, "olt_cache", NullCache())
    monkeypatch.setattr(olt_ssh.time, "sleep", lambda _s: None)
    monkeypatch.setattr(olt_ssh, "_setup_prompt_timeout", lambda: 2)

    def install(channel: ScriptedChannel) -> FakePool:
        fake = FakePool(channel)
        monkeypatch.setattr(olt_ssh_pool, "ssh_pool", fake)
        return fake

    return install


CONFIG = (
    "display current-configuration\r\n"
    "#\r\nsysname boi-olt\r\n" + "interface gpon 0/1\r\n" * 5
)


def test_backup_completes_with_hash_prompt_and_paging(pool) -> None:
    channel = ScriptedChannel(
        replies=[
            [CONFIG + "---- More ( Press 'Q' to break ) ----"],
            ["\r\nreturn\r\nboi-olt#"],
        ]
    )
    fake = pool(channel)

    ok, message, config = olt_ssh.fetch_running_config_ssh(OLT)

    assert ok is True, message
    assert "sysname boi-olt" in config
    assert config.rstrip().endswith("return")
    # Paging disabled before the config read; pager answered with a space.
    assert channel.sent[:2] == [
        "enable\n",
        "display current-configuration\n",
    ]
    assert " " in channel.sent
    assert fake.release_close_flags == [False]


def test_backup_without_prompt_returns_failure_and_discards_channel(
    pool, monkeypatch
) -> None:
    monkeypatch.setattr(olt_ssh, "_BACKUP_COMMAND_IDLE_SECONDS", 0.2)
    channel = ScriptedChannel(replies=[[CONFIG]])  # output stops, no prompt
    fake = pool(channel)

    ok, message, _config = olt_ssh.fetch_running_config_ssh(OLT)

    assert ok is False
    assert fake.release_close_flags == [True]


def test_idle_reader_gives_up_after_silence() -> None:
    channel = ScriptedChannel()
    started = time.monotonic()
    out = olt_ssh._read_until_prompt_idle(
        channel, r"boi\-olt#\s*$", idle_timeout_sec=0.2, max_timeout_sec=5
    )
    assert out == ""
    assert time.monotonic() - started < 2


def test_cli_command_uses_privileged_prompt_and_pages(pool) -> None:
    channel = ScriptedChannel(
        replies=[
            ["display version\r\nline one\r\n---- More ( Press 'Q' to break ) ----"],
            ["line two\r\nboi-olt#"],
        ]
    )
    fake = pool(channel)

    ok, message, output = olt_ssh.run_cli_command(OLT, "display version")

    assert ok is True, message
    assert "line one" in output and "line two" in output
    assert channel.sent == [
        "enable\n",
        "display version\n",
        " ",
    ]
    assert fake.release_close_flags == [False]


def test_cli_command_answers_cr_prompt_with_enter_only(pool) -> None:
    channel = ScriptedChannel(
        replies=[
            [f"display service-port 0/1/1\r\n  {OPTIONS_WITH_CR}"],
            ["  1  100  common  gpon\r\nboi-olt#"],
        ]
    )
    fake = pool(channel)

    ok, message, output = olt_ssh.run_cli_command(OLT, "display service-port 0/1/1")

    assert ok is True, message
    assert "common  gpon" in output
    assert channel.sent[-1] == "\n"
    assert fake.release_close_flags == [False]


def test_cli_command_without_cr_option_fails_and_channel_is_discarded(pool) -> None:
    channel = ScriptedChannel(
        replies=[[f"display ont info\r\n  {OPTIONS_NO_CR}"]],
    )
    fake = pool(channel)

    ok, message, output = olt_ssh.run_cli_command(OLT, "display ont info")

    assert ok is False
    assert "needs more arguments" in message
    assert output == ""
    # Nothing was typed into the pending prompt.
    assert channel.sent == [
        "enable\n",
        "display ont info\n",
    ]
    assert fake.release_close_flags == [True]


def test_cli_command_never_answers_confirmation(pool) -> None:
    channel = ScriptedChannel(replies=[["display x\r\nAre you sure? (y/n)[n]:"]])
    fake = pool(channel)

    ok, message, _ = olt_ssh.run_cli_command(OLT, "display x")

    assert ok is False
    assert "confirmation" in message
    assert "y\n" not in channel.sent and "y" not in channel.sent
    assert fake.release_close_flags == [True]


def test_cli_command_prompt_never_returns_is_failure_not_success(pool) -> None:
    channel = ScriptedChannel(replies=[["display version\r\nsome output, no prompt"]])
    fake = pool(channel)

    ok, message, _ = olt_ssh.run_cli_command(OLT, "display version")

    assert ok is False
    assert fake.release_close_flags == [True]


def test_helper_times_out_when_prompt_never_returns() -> None:
    channel = ScriptedChannel(replies=[[]])
    with pytest.raises(olt_ssh.OltCliPromptError):
        olt_ssh._run_readonly_display_cmd(
            channel, "display version", prompt=r"boi\-olt#\s*$", timeout_sec=0.3
        )


def test_cr_answers_are_capped() -> None:
    channel = ScriptedChannel(replies=[[OPTIONS_WITH_CR]] * 10)
    with pytest.raises(olt_ssh.OltCliPromptError):
        olt_ssh._run_readonly_display_cmd(
            channel, "display x", prompt=r"boi\-olt#\s*$", timeout_sec=5
        )
    assert channel.sent.count("\n") == olt_ssh._MAX_CR_ANSWERS


@pytest.mark.parametrize(
    "command",
    ["undo service-port 1", "ont delete 0/1/1 1", "reboot", "configure terminal"],
)
def test_non_read_only_command_is_still_rejected(pool, command) -> None:
    channel = ScriptedChannel()
    fake = pool(channel)

    ok, message, output = olt_ssh.run_cli_command(OLT, command)

    assert ok is False
    assert output == ""
    assert channel.sent == []  # never connected, nothing typed
    assert fake.release_close_flags == []


def test_rollback_firmware_reuses_one_session(monkeypatch) -> None:
    opened: list[int] = []
    channel = ScriptedChannel()
    transport = SimpleNamespace(close=lambda: None)
    policy = SimpleNamespace(prompt_regex=USER_PROMPT)

    def open_shell(_olt):
        opened.append(1)
        return transport, channel, policy

    monkeypatch.setattr(olt_ssh, "_open_shell", open_shell)
    monkeypatch.setattr(
        olt_ssh, "_prepare_huawei_read_shell", lambda *_a, **_k: r"boi\-olt#\s*$"
    )
    monkeypatch.setattr(
        olt_ssh,
        "_run_huawei_paged_cmd",
        lambda *_a, **_k: "Current version : A\nStandby version : B\n",
    )
    monkeypatch.setattr(
        olt_ssh,
        "_parse_firmware_info",
        lambda _o: SimpleNamespace(
            has_dual_image=False, standby_version="", current_version="A"
        ),
    )

    ok, _message = olt_ssh.rollback_firmware(OLT)

    assert ok is False
    assert opened == [1]


@pytest.mark.parametrize(
    "command",
    [
        "display version\nsave",
        "display version\rsave",
        "display version; save",
        "display version\tx",
        "display version\x1b",
        "displayx",
        "showtime",
        "pingx 1.1.1.1",
    ],
)
def test_validator_rejects_chaining_and_loose_prefixes(command) -> None:
    from app.services.network.parsers.cli import validate_readonly_command

    assert validate_readonly_command(command)[0] is False


@pytest.mark.parametrize(
    "command",
    [
        "display version",
        "DISPLAY ont info 0/1/1 1",
        "display current-configuration | include ssh",
        "  show version ",
        "display",
    ],
)
def test_validator_accepts_normal_display_commands(command) -> None:
    from app.services.network.parsers.cli import validate_readonly_command

    assert validate_readonly_command(command)[0] is True


def test_api_request_rejects_newline_and_long_commands() -> None:
    from pydantic import ValidationError

    from app.schemas.network_olt_ops import OltCliCommandRequest

    OltCliCommandRequest(command="display version")
    for bad in ("display version\nsave", "display x; save", "d" * 201):
        with pytest.raises(ValidationError):
            OltCliCommandRequest(command=bad)


def test_pool_discards_channel_on_any_exception(pool) -> None:
    from app.services.network.olt_ssh_pool import pooled_ssh_connection

    fake = pool(ScriptedChannel())
    for exc in (RuntimeError("x"), ValueError("y"), KeyboardInterrupt()):
        with pytest.raises(type(exc)):
            with pooled_ssh_connection(OLT):
                raise exc
    assert fake.release_close_flags == [True, True, True]


def test_config_read_cap_defaults_to_60_and_backup_gets_300(pool, monkeypatch) -> None:
    caps: list[int] = []

    def paged(_ch, _cmd, prompt, *, timeout_sec, idle_timeout_sec=None):
        caps.append(timeout_sec)
        return "x" * 60 + "\nreturn\nboi-olt#"

    monkeypatch.setattr(olt_ssh, "_run_huawei_paged_cmd", paged)
    pool(ScriptedChannel())
    olt_ssh.fetch_running_config_ssh(OLT)
    with olt_ssh.backup_config_read_cap():
        olt_ssh.fetch_running_config_ssh(OLT)
    assert caps == [60, 300]
