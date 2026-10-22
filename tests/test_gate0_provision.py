"""Offline privilege, preservation and rollback checks; no host activation."""

import hashlib
import os
from dataclasses import replace
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from deploy.agent import provision as p
from deploy.egress.provision import ResolverPlan
from deploy.egress.provision import apply as resolver_apply
from deploy.egress.render import EgressConfiguration
from deploy.gate0 import proof1_runner as runner
from deploy.openbao.loopback_transport import LoopbackPlan


def artifact(asset: p.Asset = p.Asset.AGENT) -> tuple[p.VerifiedArtifact, bytes]:
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    body = b"public fixture"
    digest = hashlib.sha256(body).hexdigest()
    source, revision = "https://github.com/actions/runner", "a" * 40
    signature = key.sign("\n".join((asset.value, digest, source, revision)).encode())
    return p.VerifiedArtifact(
        asset, body, digest, source, revision, public, signature
    ), public


def test_authenticated_pin_cannot_change_asset_or_bytes() -> None:
    item, signer = artifact()
    item.verify(signer)
    for changed in (
        replace(item, content=b"tampered"),
        replace(item, asset=p.Asset.COSIGN),
        replace(item, signer_public=b"x" * 32),
    ):
        with pytest.raises(p.ProvisionError):
            changed.verify(signer)


def test_missing_inputs_fail_closed() -> None:
    with pytest.raises(p.ProvisionError, match="complete_signed"):
        p.InstallPlan(p.Target.SEABONE, (), b"x" * 32).validate()


def test_host_guard_requires_enrolled_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(p.os, "geteuid", lambda: 0)
    monkeypatch.setattr(p.socket, "gethostname", lambda: "production")
    with pytest.raises(p.ProvisionError, match="enrolled_root"):
        p.host_guard(p.Target.SEABONE)
    monkeypatch.setattr(p.socket, "gethostname", lambda: "Seabone")
    monkeypatch.setattr(p.os, "geteuid", lambda: 1000)
    with pytest.raises(p.ProvisionError):
        p.host_guard(p.Target.SEABONE)


def test_symlink_writable_and_foreign_paths_refused(tmp_path: Path) -> None:
    path = tmp_path / "file"
    path.write_bytes(b"original")
    path.chmod(0o666)
    with pytest.raises(p.ProvisionError):
        p.protected(path, uid=os.getuid())
    path.chmod(0o600)
    with pytest.raises(p.ProvisionError):
        p.protected(path, uid=os.getuid() + 1)
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises(p.ProvisionError):
        p.protected(link, uid=os.getuid())


@pytest.fixture
def local_files(monkeypatch: pytest.MonkeyPatch) -> None:
    original = p.protected
    monkeypatch.setattr(p, "parents", lambda path: None)
    monkeypatch.setattr(
        p,
        "protected",
        lambda path, uid=0, directory=False: original(
            path, uid=os.getuid(), directory=directory
        ),
    )


def test_capture_idempotence_and_rollback_preserve_foreign(
    tmp_path: Path, local_files: None
) -> None:
    foreign, owned = tmp_path / "foreign", tmp_path / "owned"
    foreign.write_bytes(b"keep")
    foreign.chmod(0o644)
    transaction = p.CapturedInstall()
    transaction.install(owned, b"owned")
    transaction.install(owned, b"owned")
    with pytest.raises(p.ProvisionError, match="existing_file_conflict"):
        transaction.install(foreign, b"replace")
    transaction.rollback()
    assert foreign.read_bytes() == b"keep" and not owned.exists()


def test_rollback_refuses_subsequent_mutation(
    tmp_path: Path, local_files: None
) -> None:
    path = tmp_path / "owned"
    transaction = p.CapturedInstall()
    transaction.install(path, b"original")
    path.write_bytes(b"changed")
    with pytest.raises(p.ProvisionError, match="rollback_owned_file_changed"):
        transaction.rollback()
    assert path.read_bytes() == b"changed"


def test_local_identity_preserved_and_never_in_public_result(
    tmp_path: Path, local_files: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "identity.pem"
    monkeypatch.setattr(p, "KEY", path)
    fingerprint = p.local_key(os.getuid(), os.getgid())
    original = path.read_bytes()
    assert p.local_key(os.getuid(), os.getgid()) == fingerprint
    assert path.read_bytes() == original and path.stat().st_mode & 0o777 == 0o600
    assert "PRIVATE" not in repr(p.PublicReadback(p.Target.SEABONE, fingerprint, ()))
    path.chmod(0o644)
    with pytest.raises(p.ProvisionError, match="private_key_mode"):
        p.local_key(os.getuid(), os.getgid())
    assert path.read_bytes() == original


def test_resolver_enforcement_never_installs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("deploy.egress.provision.host_guard", lambda target: None)
    item, signer = artifact(p.Asset.RESOLVER)
    plan = ResolverPlan(
        p.Target.SEABONE,
        item,
        signer,
        EgressConfiguration("control.example", "bao.example", True),
        item.revision,
    )
    with pytest.raises(p.ProvisionError, match="log_only"):
        resolver_apply(plan)


def test_errors_hide_subprocess_secret_sentinel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess

    def fail(*args: object, **kwargs: object) -> None:
        raise subprocess.CalledProcessError(1, "fixed", output=b"SECRET_SENTINEL")

    monkeypatch.setattr(p.subprocess, "run", fail)
    with pytest.raises(p.ProvisionError) as caught:
        p.command(("/usr/bin/false",))
    assert "SECRET_SENTINEL" not in str(caught.value)


def test_runner_stopped_state_requires_readback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner, "host_guard", lambda target: None)
    monkeypatch.setattr(runner, "parents", lambda path: None)
    monkeypatch.setattr(runner, "protected", lambda path: None)
    monkeypatch.setattr(Path, "read_bytes", lambda path: b"unit")
    monkeypatch.setattr(runner, "command", lambda argv: b"active\n")
    with pytest.raises(p.ProvisionError, match="stop_unproven"):
        runner.stop_before_proofs34(
            target=p.Target.SEABONE,
            receipt=runner.RegistrationReceipt(
                "michaelayoade/dotmac_sub",
                "dotmac-sub-staging",
                "Seabone-proof1",
                hashlib.sha256(b"unit").hexdigest(),
            ),
        )


def test_tunnel_rejects_unbounded_or_injected_plan() -> None:
    base = LoopbackPlan("observe.example", "proof", Path("/pin"), "a" * 64, "a" * 36)
    for invalid in (
        replace(base, lifetime_seconds=1801),
        replace(base, ssh_host="-oProxyCommand=x"),
        replace(base, port=True),
        replace(base, known_hosts=Path("relative")),
    ):
        with pytest.raises(RuntimeError):
            invalid.validate()
