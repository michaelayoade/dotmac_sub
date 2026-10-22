"""Finite Seabone staging install. Importing/planning never changes a host."""

from __future__ import annotations

import fcntl
import grp
import hashlib
import os
import pwd
import re
import socket
import stat
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class ProvisionError(RuntimeError):
    """Public error codes never contain subprocess output or private bytes."""


class Target(Enum):
    SEABONE = "Seabone"
    TEST = "hp-server"


class Asset(Enum):
    AGENT = "/opt/dotmac-deploy-agent/release/agent.pyz"
    BUNDLE = "/opt/dotmac-deploy-agent/release/agent.bundle.json"
    LAUNCHER = "/opt/dotmac-deploy-agent/launcher.sh"
    PIN = "/etc/dotmac-deploy-agent/agent.sha256"
    CONFIG = "/etc/dotmac-deploy-agent/config.toml"
    KEYRING = "/etc/dotmac-deploy-agent/signer-keyring.json"
    UNIT = "/etc/systemd/system/dotmac-deploy-agent.service"
    COSIGN = "/usr/local/bin/cosign"
    GH = "/usr/bin/gh"
    DOCKER = "/usr/bin/docker"
    GIT = "/usr/bin/git"
    BASH = "/usr/bin/bash"
    FLOCK = "/usr/bin/flock"
    RESOLVER = "/opt/dotmac-egress/smartdns"
    DNS_CONFIG = "/etc/dotmac-egress/smartdns.conf"
    NFT_CONFIG = "/etc/dotmac-egress/dotmac-egress.nft"


@dataclass(frozen=True)
class VerifiedArtifact:
    """Pinned bytes plus independently authenticated public release manifest.

    Caller supplies the reviewed signer key and detached Ed25519 signature over
    the exact statement. No download, mutable version or self-update fallback.
    """

    asset: Asset
    content: bytes = field(repr=False)
    sha256: str
    source: str
    revision: str
    signer_public: bytes
    statement_signature: bytes = field(repr=False)

    def verify(self, trusted_signer: bytes) -> None:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        if (
            self.signer_public != trusted_signer
            or not re.fullmatch(r"[0-9a-f]{64}", self.sha256)
            or hashlib.sha256(self.content).hexdigest() != self.sha256
            or not re.fullmatch(r"https://[A-Za-z0-9./_@+-]+", self.source)
            or not re.fullmatch(r"[0-9a-f]{40,64}", self.revision)
        ):
            raise ProvisionError("artifact_pin_invalid")
        statement = "\n".join(
            (self.asset.value, self.sha256, self.source, self.revision)
        ).encode()
        try:
            Ed25519PublicKey.from_public_bytes(trusted_signer).verify(
                self.statement_signature, statement
            )
        except (ValueError, InvalidSignature):
            raise ProvisionError("artifact_signature_invalid") from None


@dataclass(frozen=True)
class InstallPlan:
    target: Target
    artifacts: tuple[VerifiedArtifact, ...]
    trusted_manifest_signer: bytes

    def validate(self) -> None:
        if (
            not isinstance(self.target, Target)
            or len(self.trusted_manifest_signer) != 32
        ):
            raise ProvisionError("plan_identity_invalid")
        assets = [item.asset for item in self.artifacts]
        required = {
            Asset.AGENT,
            Asset.BUNDLE,
            Asset.LAUNCHER,
            Asset.PIN,
            Asset.CONFIG,
            Asset.KEYRING,
            Asset.UNIT,
            Asset.COSIGN,
            Asset.GH,
            Asset.DOCKER,
            Asset.GIT,
            Asset.BASH,
            Asset.FLOCK,
        }
        if set(assets) != required or len(assets) != len(required):
            raise ProvisionError("complete_signed_agent_inputs_required")
        for item in self.artifacts:
            item.verify(self.trusted_manifest_signer)
        import tomllib

        config = tomllib.loads(
            next(a.content for a in self.artifacts if a.asset is Asset.CONFIG).decode()
        )
        if (
            config.get("environment") != "staging"
            or config.get("allow_test_input") is not False
        ):
            raise ProvisionError("staging_normal_poll_required")
        if (
            config.get("host_key_file") != str(KEY)
            or config.get("host") != "dotmac-sub-staging"
        ):
            raise ProvisionError("host_binding_invalid")
        expected = next(a.sha256 for a in self.artifacts if a.asset is Asset.AGENT)
        pin = next(a.content for a in self.artifacts if a.asset is Asset.PIN)
        if pin != f"{expected}  agent.pyz\n".encode():
            raise ProvisionError("agent_pin_mismatch")
        from .hostio import Config

        try:
            for name in (
                "deploy_dir",
                "state_dir",
                "keyring_file",
                "host_key_file",
                "agent_lock",
                "deploy_lock",
                "gh_config_dir",
                "docker_config_dir",
            ):
                if name in config:
                    config[name] = Path(config[name])
            parsed = Config(**config)
            if parsed.artifact_digest != f"sha256:{expected}":
                raise ProvisionError("configured_agent_digest_mismatch")
        except (TypeError, ValueError):
            raise ProvisionError("agent_configuration_invalid") from None


KEY = Path("/etc/dotmac-deploy-agent/host-private.pem")
LOCK = Path("/run/dotmac-staging-provision.lock")


def host_guard(target: Target) -> None:
    if os.geteuid() != 0 or socket.gethostname() != target.value:
        raise ProvisionError("enrolled_root_host_required")


def protected(path: Path, *, uid: int = 0, directory: bool = False) -> None:
    metadata = path.lstat()
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if (
        not expected(metadata.st_mode)
        or metadata.st_uid != uid
        or metadata.st_mode & 0o022
    ):
        raise ProvisionError("foreign_or_unprotected_path")


def parents(path: Path) -> None:
    for parent in reversed(path.parents):
        protected(parent, directory=True)


def command(argv: tuple[str, ...], *, timeout: int = 30) -> bytes:
    try:
        return subprocess.run(
            argv,
            check=True,
            capture_output=True,
            timeout=timeout,
            env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"},
        ).stdout
    except (OSError, subprocess.SubprocessError):
        raise ProvisionError("host_operation_failed") from None


@dataclass(frozen=True)
class PublicReadback:
    target: Target
    host_key_sha256: str
    installed: tuple[tuple[Asset, str], ...]
    activation_required: bool = True


class CapturedInstall:
    """Rollback only files created by this transaction, unchanged since capture."""

    def __init__(self) -> None:
        self.created: list[tuple[Path, str]] = []
        self.directories: list[Path] = []

    def directory(self, path: Path) -> None:
        if path.exists() or path.is_symlink():
            protected(path, directory=True)
            parents(path)
            return
        if not path.parent.exists():
            self.directory(path.parent)
        parents(path)
        path.mkdir(mode=0o755)
        self.directories.append(path)

    def install(self, path: Path, content: bytes, *, mode: int = 0o644) -> None:
        self.directory(path.parent)
        parents(path)
        if path.exists() or path.is_symlink():
            protected(path)
            if (
                path.read_bytes() != content
                or stat.S_IMODE(path.stat().st_mode) != mode
            ):
                raise ProvisionError("existing_file_conflict")
            return
        descriptor = os.open(
            path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode
        )
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            # Capture even an interrupted write; rollback still checks its exact bytes.
            self.created.append((path, hashlib.sha256(path.read_bytes()).hexdigest()))

    def rollback(self) -> None:
        for path, digest in reversed(self.created):
            parents(path)
            protected(path)
            if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise ProvisionError("rollback_owned_file_changed")
            path.unlink()
        for path in reversed(self.directories):
            protected(path, directory=True)
            try:
                path.rmdir()
            except OSError:
                pass  # Never recursively remove a directory or somebody else's files.


def local_key(uid: int, gid: int) -> str:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    parents(KEY)
    if KEY.exists() or KEY.is_symlink():
        protected(KEY, uid=uid)
        if stat.S_IMODE(KEY.stat().st_mode) != 0o600:
            raise ProvisionError("private_key_mode_invalid")
        key = serialization.load_pem_private_key(KEY.read_bytes(), password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise ProvisionError("private_key_type_invalid")
    else:
        key = Ed25519PrivateKey.generate()
        descriptor = os.open(
            KEY, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(descriptor, "wb") as stream:
            os.fchown(stream.fileno(), uid, gid)
            stream.write(
                key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                )
            )
            stream.flush()
            os.fsync(stream.fileno())
    public = key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return hashlib.sha256(public).hexdigest()


def apply(plan: InstallPlan) -> PublicReadback:
    plan.validate()
    host_guard(plan.target)
    interpreter = Path("/opt/dotmac-deploy-agent/venv/bin/python")
    # Existing reviewed runtime is ready; never reinstall or silently use OS Python.
    parents(interpreter)
    resolved = interpreter.resolve(strict=True)
    parents(resolved)
    protected(resolved)
    command(
        (
            str(interpreter),
            "-c",
            "import sys,cryptography,cffi,pycparser; "
            "assert sys.version_info[:3] == (3,12,15); "
            "assert cryptography.__version__.split('.')[0] == '50'; "
            "assert cffi.__version__.startswith('2.1.'); assert pycparser.__version__ == '3.0'",
        )
    )
    parents(LOCK)
    descriptor = os.open(LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "a+b") as lock:
        protected(LOCK)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        transaction = CapturedInstall()
        try:
            try:
                account = pwd.getpwnam("dotmac-deploy")
            except KeyError:
                command(
                    (
                        "/usr/sbin/useradd",
                        "--system",
                        "--user-group",
                        "--groups",
                        "docker",
                        "--no-create-home",
                        "--home-dir",
                        "/var/lib/dotmac-deploy-agent",
                        "--shell",
                        "/usr/sbin/nologin",
                        "dotmac-deploy",
                    )
                )
                account = pwd.getpwnam("dotmac-deploy")
            if (
                account.pw_uid == 0
                or account.pw_shell != "/usr/sbin/nologin"
                or account.pw_dir != "/var/lib/dotmac-deploy-agent"
                or grp.getgrnam("docker").gr_gid
                not in os.getgrouplist(account.pw_name, account.pw_gid)
            ):
                raise ProvisionError("agent_account_conflict")
            for artifact in plan.artifacts:
                executable = artifact.asset in {
                    Asset.LAUNCHER,
                    Asset.COSIGN,
                    Asset.GH,
                    Asset.DOCKER,
                    Asset.GIT,
                    Asset.BASH,
                    Asset.FLOCK,
                }
                transaction.install(
                    Path(artifact.asset.value),
                    artifact.content,
                    mode=0o755 if executable else 0o644,
                )
            release = "/opt/dotmac-deploy-agent/release/"
            command(
                (
                    Asset.COSIGN.value,
                    "verify-blob",
                    "--bundle",
                    release + "agent.bundle.json",
                    "--certificate-identity",
                    "https://github.com/michaelayoade/dotmac_sub/.github/workflows/deploy-agent-release.yml@refs/heads/main",
                    "--certificate-oidc-issuer",
                    "https://token.actions.githubusercontent.com",
                    release + "agent.pyz",
                )
            )
            fingerprint = local_key(account.pw_uid, account.pw_gid)
            installed = tuple(
                (a.asset, hashlib.sha256(Path(a.asset.value).read_bytes()).hexdigest())
                for a in plan.artifacts
            )
            return PublicReadback(plan.target, fingerprint, installed)
        except BaseException:
            transaction.rollback()
            raise
