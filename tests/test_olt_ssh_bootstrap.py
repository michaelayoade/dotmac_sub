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

    monkeypatch.setattr(olt_ssh.socket, "create_connection", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(olt_ssh, "Transport", _fake_transport)
    monkeypatch.setattr(olt_ssh, "resolve_policy", lambda _olt: SimpleNamespace(
        key="huawei",
        kex=(),
        host_key_types=(),
        ciphers=(),
        macs=(),
        prompt_regex=r"[>#]\s*$",
    ))
    monkeypatch.setattr(olt_ssh, "decrypt_credential", lambda _value: "secret")
    monkeypatch.setattr(olt_ssh, "_apply_preferred_algorithms", lambda *_args, **_kwargs: None)
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

    monkeypatch.setattr(olt_ssh.socket, "create_connection", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(olt_ssh, "Transport", _fake_transport)
    monkeypatch.setattr(olt_ssh, "resolve_policy", lambda _olt: SimpleNamespace(
        key="huawei",
        kex=(),
        host_key_types=(),
        ciphers=(),
        macs=(),
        prompt_regex=r"[>#]\s*$",
    ))
    monkeypatch.setattr(olt_ssh, "decrypt_credential", lambda _value: "secret")
    monkeypatch.setattr(olt_ssh, "_apply_preferred_algorithms", lambda *_args, **_kwargs: None)
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
