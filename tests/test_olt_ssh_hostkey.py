"""OLT SSH host-key trust on first use (D3 phase 1: record, enforce opt-in)."""

from __future__ import annotations

import importlib.util
import logging
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.models.network import OLTDevice
from app.services.network import olt_ssh, olt_ssh_hostkey, olt_ssh_metrics
from app.services.network.olt_ssh_limiter import LimiterConfig, set_config_override

ROOT = Path(__file__).resolve().parents[1]


class FakeKey:
    def __init__(self, blob: bytes, name: str = "ssh-rsa") -> None:
        self._blob = blob
        self._name = name

    def asbytes(self) -> bytes:
        return self._blob

    def get_name(self) -> str:
        return self._name


class FakeChannel:
    def get_pty(self, **_kw) -> None: ...
    def invoke_shell(self) -> None: ...


class KeyTransport:
    key: FakeKey = FakeKey(b"key-one")
    auth_calls = 0

    def __init__(self, _sock) -> None:
        self.channel = FakeChannel()

    def start_client(self, timeout: int) -> None: ...
    def get_remote_server_key(self):
        return type(self).key

    def auth_password(self, **_kw) -> None:
        type(self).auth_calls += 1

    def is_authenticated(self) -> bool:
        return True

    def open_session(self, timeout: int):
        return self.channel

    def is_active(self) -> bool:
        return True

    def close(self) -> None: ...


def _sha(blob: bytes) -> str:
    return olt_ssh_hostkey.observe(
        SimpleNamespace(get_remote_server_key=lambda: FakeKey(blob))
    ).sha256


def _device(stored: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        name="OLT-A",
        model="MA5608T",
        mgmt_ip="192.0.2.10",
        hostname=None,
        ssh_port=22,
        ssh_username="u",
        ssh_password="enc",
        olt_ssh_host_key_sha256=stored,
    )


@pytest.fixture
def env(monkeypatch):
    KeyTransport.key = FakeKey(b"key-one")
    KeyTransport.auth_calls = 0
    recorded: list[tuple] = []
    counted: list[str] = []
    mode = {"value": "record"}
    monkeypatch.setattr(olt_ssh, "Transport", KeyTransport)
    monkeypatch.setattr(olt_ssh.socket, "create_connection", lambda *a, **k: object())
    monkeypatch.setattr(
        olt_ssh,
        "resolve_policy",
        lambda _o: SimpleNamespace(prompt_regex=r"\S*[>#]\s*$"),
    )
    monkeypatch.setattr(olt_ssh, "decrypt_credential", lambda *_a, **_k: "s3cr3t-pw")
    monkeypatch.setattr(olt_ssh, "_apply_preferred_algorithms", lambda *a, **k: None)
    monkeypatch.setattr(olt_ssh, "_prime_shell_prompt", lambda ch, rx: "olt>")
    monkeypatch.setattr(olt_ssh, "_instrument_channel", lambda ch: None)
    monkeypatch.setattr(olt_ssh_hostkey, "get_mode", lambda: mode["value"])
    monkeypatch.setattr(
        olt_ssh_hostkey,
        "record_observation",
        lambda olt_id, obs, *, mode: recorded.append((olt_id, obs.sha256, mode)),
    )
    monkeypatch.setattr(
        olt_ssh_metrics, "record_host_key_mismatch", lambda olt: counted.append(olt)
    )
    set_config_override(LimiterConfig(enabled=False))
    yield SimpleNamespace(recorded=recorded, counted=counted, mode=mode)
    set_config_override(None)


def _connect(olt):
    return olt_ssh._connect_unlimited(olt, lease=None, priority_name="write")


def test_first_seen_is_recorded_without_warning(env, caplog):
    olt = _device()
    with caplog.at_level(logging.WARNING):
        _connect(olt)
    assert env.recorded == [(olt.id, _sha(b"key-one"), "record")]
    assert not [r for r in caplog.records if "MISMATCH" in r.getMessage()]
    assert env.counted == []


def test_same_key_no_warning(env, caplog):
    olt = _device(_sha(b"key-one"))
    with caplog.at_level(logging.WARNING):
        _connect(olt)
    assert not [
        r
        for r in caplog.records
        if r.levelno >= logging.WARNING and r.name.endswith("olt_ssh_hostkey")
    ]
    assert env.counted == []
    assert len(env.recorded) == 1  # last_seen refresh


def test_changed_key_warns_counts_records_and_still_connects(env, caplog):
    olt = _device(_sha(b"other-key"))
    with caplog.at_level(logging.WARNING):
        _connect(olt)
    assert [r for r in caplog.records if "MISMATCH" in r.getMessage()]
    assert env.counted == ["OLT-A"]
    assert env.recorded and KeyTransport.auth_calls == 1


def test_enforce_refuses_mismatch_before_sending_credentials(env):
    env.mode["value"] = "enforce"
    with pytest.raises(olt_ssh_hostkey.OltHostKeyRefused):
        _connect(_device(_sha(b"other-key")))
    assert KeyTransport.auth_calls == 0
    assert env.counted == ["OLT-A"]


def test_enforce_refuses_unknown_key(env):
    env.mode["value"] = "enforce"
    with pytest.raises(olt_ssh_hostkey.OltHostKeyRefused):
        _connect(_device(None))
    assert KeyTransport.auth_calls == 0


def test_enforce_accepts_matching_key(env):
    env.mode["value"] = "enforce"
    _connect(_device(_sha(b"key-one")))
    assert KeyTransport.auth_calls == 1


def test_off_mode_does_nothing(env):
    env.mode["value"] = "off"
    _connect(_device(_sha(b"other-key")))
    assert env.recorded == [] and env.counted == []


def test_no_secret_or_key_blob_is_logged(env, caplog):
    with caplog.at_level(logging.DEBUG):
        _connect(_device(_sha(b"other-key")))
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "s3cr3t-pw" not in text
    obs = olt_ssh_hostkey.observe(
        SimpleNamespace(get_remote_server_key=lambda: FakeKey(b"key-one"))
    )
    assert obs.base64 not in text


def test_evaluate_only_enforce_refuses():
    obs = olt_ssh_hostkey.HostKeyObservation("ssh-rsa", "SHA256:x", "eA==")
    for mode in ("off", "record"):
        assert not olt_ssh_hostkey.evaluate(
            mode=mode, stored_sha256="SHA256:y", observed=obs
        ).refuse
    assert olt_ssh_hostkey.evaluate(
        mode="enforce", stored_sha256="SHA256:y", observed=obs
    ).refuse


def test_setting_registered_with_record_default():
    from app.services.settings_spec import SETTINGS_SPECS

    spec = next(s for s in SETTINGS_SPECS if s.key == "olt_ssh_host_key_enforcement")
    assert spec.default == "record" and spec.allowed == {"off", "record", "enforce"}


# ── persistence (owner path) ────────────────────────────────────────────────


@pytest.fixture
def owner_session(db_session, monkeypatch):
    @contextmanager
    def _session():
        yield db_session
        db_session.flush()

    monkeypatch.setattr(
        "app.services.db_session_adapter.db_session_adapter.session", _session
    )
    return db_session


def _real_olt(db):
    olt = OLTDevice(name=f"hk-{uuid.uuid4().hex[:8]}", is_active=True)
    db.add(olt)
    db.flush()
    return olt


def _obs(blob: bytes):
    return olt_ssh_hostkey.observe(
        SimpleNamespace(get_remote_server_key=lambda: FakeKey(blob))
    )


def test_record_first_seen_then_refresh_then_mismatch_keeps_key(owner_session):
    olt = _real_olt(owner_session)
    olt_ssh_hostkey.record_observation(olt.id, _obs(b"a"), mode="record")
    owner_session.refresh(olt)
    first = olt.olt_ssh_host_key_first_seen_at
    assert olt.olt_ssh_host_key_sha256 == _obs(b"a").sha256 and first
    olt_ssh_hostkey.record_observation(olt.id, _obs(b"a"), mode="record")
    owner_session.refresh(olt)
    assert olt.olt_ssh_host_key_first_seen_at == first
    assert olt.olt_ssh_host_key_last_mismatch_at is None
    olt_ssh_hostkey.record_observation(olt.id, _obs(b"b"), mode="record")
    owner_session.refresh(olt)
    assert olt.olt_ssh_host_key_sha256 == _obs(b"a").sha256  # never replaced
    assert olt.olt_ssh_host_key_last_mismatch_at is not None
    assert olt_ssh_hostkey.status_view(olt)["state"] == "mismatch"


def test_enforce_never_self_trusts_unknown_key(owner_session):
    olt = _real_olt(owner_session)
    olt_ssh_hostkey.record_observation(olt.id, _obs(b"a"), mode="enforce")
    owner_session.refresh(olt)
    assert olt.olt_ssh_host_key_sha256 is None


def test_retrust_clears_everything(owner_session):
    olt = _real_olt(owner_session)
    olt_ssh_hostkey.record_observation(olt.id, _obs(b"a"), mode="record")
    olt_ssh_hostkey.record_observation(olt.id, _obs(b"b"), mode="record")
    cleared = olt_ssh_hostkey.retrust(owner_session, olt)
    assert cleared["sha256"] == _obs(b"a").sha256
    assert olt.olt_ssh_host_key_sha256 is None
    assert olt.olt_ssh_host_key_last_mismatch_at is None


# ── migration ───────────────────────────────────────────────────────────────


class _Op:
    def __init__(self) -> None:
        self.added: list[str] = []
        self.dropped: list[str] = []

    def get_bind(self):
        return None

    def add_column(self, table, column):
        assert table == "olt_devices" and column.nullable
        self.added.append(column.name)

    def drop_column(self, table, name):
        assert table == "olt_devices"
        self.dropped.append(name)


def _migration(monkeypatch, existing: set[str]):
    path = ROOT / "alembic/versions/701_olt_ssh_host_key_record.py"
    spec = importlib.util.spec_from_file_location("mig_701", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    fake = _Op()
    module.op = fake
    monkeypatch.setattr(
        module.sa,
        "inspect",
        lambda _b: SimpleNamespace(
            get_columns=lambda _t: [{"name": n} for n in existing]
        ),
    )
    return module, fake


def test_migration_up_and_down(monkeypatch):
    module, fake = _migration(monkeypatch, set())
    assert module.revision == "701_olt_ssh_host_key_record"
    assert module.down_revision == "700_ont_service_config_drift_fields"
    module.upgrade()
    assert len(fake.added) == 5 and fake.added[0] == "olt_ssh_host_key_type"
    module, fake = _migration(monkeypatch, set(fake.added))
    module.upgrade()
    assert fake.added == []  # idempotent
    module.downgrade()
    assert len(fake.dropped) == 5
