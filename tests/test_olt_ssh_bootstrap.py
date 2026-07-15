from types import SimpleNamespace


class _FakeChannel:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def get_pty(self, **_kwargs) -> None:
        return None

    def invoke_shell(self) -> None:
        return None

    def send(self, value: str) -> None:
        self.sent.append(value)


class _FakeTransport:
    def __init__(self, _sock) -> None:
        self.authenticated = True
        self.channel = _FakeChannel()

    def get_security_options(self):
        return SimpleNamespace(
            kex=[],
            key_types=[],
            ciphers=[],
            digests=[],
        )

    def start_client(self, timeout: int) -> None:
        assert timeout == 20

    def auth_password(self, *, username: str, password: str) -> None:
        assert username == "dotmacsub"
        assert password == "secret"

    def is_authenticated(self) -> bool:
        return self.authenticated

    def open_session(self, timeout: int):
        assert timeout == 20
        return self.channel

    def close(self) -> None:
        return None


def test_open_shell_primes_prompt_with_newline_retry(monkeypatch) -> None:
    from app.services.network import olt_ssh

    transport_holder: dict[str, _FakeTransport] = {}
    read_calls: list[tuple[str, float]] = []

    def _fake_transport(sock):
        transport = _FakeTransport(sock)
        transport_holder["transport"] = transport
        return transport

    def _fake_read_until_prompt(_channel, prompt_regex: str, timeout_sec: float = 8.0):
        read_calls.append((prompt_regex, timeout_sec))
        if len(read_calls) == 1:
            return ""
        return "MA5800>"

    monkeypatch.setattr(
        olt_ssh.socket, "create_connection", lambda *_args, **_kwargs: object()
    )
    monkeypatch.setattr(olt_ssh, "Transport", _fake_transport)
    monkeypatch.setattr(
        olt_ssh,
        "resolve_policy",
        lambda _olt: SimpleNamespace(
            key="huawei",
            kex=(),
            host_key_types=(),
            ciphers=(),
            macs=(),
            prompt_regex=r"[>#]\s*$",
        ),
    )
    monkeypatch.setattr(olt_ssh, "decrypt_credential", lambda _value: "secret")
    monkeypatch.setattr(
        olt_ssh, "_apply_preferred_algorithms", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(olt_ssh, "_read_until_prompt", _fake_read_until_prompt)

    transport, channel, policy = olt_ssh._open_shell(
        SimpleNamespace(
            mgmt_ip="172.20.100.2",
            hostname=None,
            ssh_port=22,
            ssh_username="dotmacsub",
            ssh_password="enc",
        )
    )

    assert transport is transport_holder["transport"]
    assert channel is transport_holder["transport"].channel
    assert channel.sent == ["\n", "\n"]
    assert read_calls == [
        (r"[>#]\s*$", 8.0),
        (r"[>#]\s*$", 4.0),
    ]
    assert policy.prompt_regex == r"(?:^|\r?\n)MA5800>\s*$"


def test_open_shell_does_not_send_screen_length_before_return(monkeypatch) -> None:
    from app.services.network import olt_ssh

    transport_holder: dict[str, _FakeTransport] = {}

    def _fake_transport(sock):
        transport = _FakeTransport(sock)
        transport_holder["transport"] = transport
        return transport

    monkeypatch.setattr(
        olt_ssh.socket, "create_connection", lambda *_args, **_kwargs: object()
    )
    monkeypatch.setattr(olt_ssh, "Transport", _fake_transport)
    monkeypatch.setattr(
        olt_ssh,
        "resolve_policy",
        lambda _olt: SimpleNamespace(
            key="huawei",
            kex=(),
            host_key_types=(),
            ciphers=(),
            macs=(),
            prompt_regex=r"[>#]\s*$",
        ),
    )
    monkeypatch.setattr(olt_ssh, "decrypt_credential", lambda _value: "secret")
    monkeypatch.setattr(
        olt_ssh, "_apply_preferred_algorithms", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        olt_ssh,
        "_read_until_prompt",
        lambda *_args, **_kwargs: "MA5800#",
    )

    _transport, channel, _policy = olt_ssh._open_shell(
        SimpleNamespace(
            mgmt_ip="172.20.100.2",
            hostname=None,
            ssh_port=22,
            ssh_username="dotmacsub",
            ssh_password="enc",
        )
    )

    assert channel.sent == ["\n"]


def test_prepare_read_shell_tracks_privileged_prompt_and_paces_commands(
    monkeypatch,
) -> None:
    from app.services.network import olt_ssh

    channel = _FakeChannel()
    prompts: list[str] = []
    responses = iter(["\r\nboi-olt#", "screen-length 0 temporary\r\nboi-olt#"])

    def read(_channel, prompt_regex: str, timeout_sec: float = 8.0):
        prompts.append(prompt_regex)
        return next(responses)

    monkeypatch.setattr(olt_ssh, "_read_until_prompt", read)
    monkeypatch.setattr(olt_ssh.time, "sleep", lambda _: None)

    prompt = olt_ssh._prepare_huawei_read_shell(channel, r"(?:^|\r?\n)boi\-olt>\s*$")

    assert prompt == r"(?:^|\r?\n)boi\-olt\#\s*$"
    assert "".join(channel.sent) == "enable\nscreen-length 0 temporary\n"
    assert prompts[0] == r"(?:^|\r?\n)[^\r\n]*#\s*$"
    assert prompts[1].startswith(prompt)


def test_firmware_probe_uses_privileged_prompt_for_readback(monkeypatch) -> None:
    from app.services.network import olt_ssh

    channel = _FakeChannel()
    transport = SimpleNamespace(close=lambda: None)
    policy = SimpleNamespace(prompt_regex=r"(?:^|\r?\n)boi\-olt>\s*$")
    calls: list[tuple[str, str]] = []
    privileged_prompt = r"(?:^|\r?\n)boi\-olt\#\s*$"
    monkeypatch.setattr(
        olt_ssh,
        "_open_shell",
        lambda _olt: (transport, channel, policy),
    )
    monkeypatch.setattr(
        olt_ssh,
        "_prepare_huawei_read_shell",
        lambda _channel, _prompt: privileged_prompt,
    )

    def run_paged(_channel, command: str, prompt: str, *, timeout_sec: int):
        calls.append((command, prompt))
        assert timeout_sec == 30
        return "VERSION : MA5600V800R013C00\nboi-olt#"

    monkeypatch.setattr(olt_ssh, "_run_huawei_paged_cmd", run_paged)

    success, message, info = olt_ssh.get_firmware_info(SimpleNamespace(name="BOI"))

    assert success is True
    assert message == "Running: MA5600V800R013C00"
    assert info.current_version == "MA5600V800R013C00"
    assert calls == [("display version", privileged_prompt)]
