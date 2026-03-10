"""OLT SSH connection helpers with model-specific transport policies."""

from __future__ import annotations

import logging
import re
import socket
from dataclasses import dataclass
from typing import Any

import paramiko
from paramiko.channel import Channel
from paramiko.ssh_exception import SSHException
from paramiko.transport import Transport

logger = logging.getLogger(__name__)

from app.models.network import OLTDevice
from app.services.credential_crypto import decrypt_credential


@dataclass(frozen=True)
class OltSshPolicy:
    key: str
    kex: tuple[str, ...]
    host_key_types: tuple[str, ...]
    ciphers: tuple[str, ...]
    macs: tuple[str, ...]
    prompt_regex: str = r"[>#]\s*$"
    version_command: str = "display version"


_HUAWEI_LEGACY_KEX = (
    "diffie-hellman-group-exchange-sha1",
    "diffie-hellman-group1-sha1",
)
_HUAWEI_HOST_KEYS = ("ssh-rsa",)
_HUAWEI_MACS = ("hmac-sha1",)

_POLICIES: dict[str, OltSshPolicy] = {
    "huawei_ma5608t": OltSshPolicy(
        key="huawei_ma5608t",
        kex=_HUAWEI_LEGACY_KEX,
        host_key_types=_HUAWEI_HOST_KEYS,
        ciphers=("aes128-cbc",),
        macs=_HUAWEI_MACS,
    ),
    "huawei_ma5800": OltSshPolicy(
        key="huawei_ma5800",
        kex=_HUAWEI_LEGACY_KEX,
        host_key_types=_HUAWEI_HOST_KEYS,
        ciphers=("aes256-ctr",),
        macs=_HUAWEI_MACS,
    ),
    "huawei_ma5600": OltSshPolicy(
        key="huawei_ma5600",
        kex=_HUAWEI_LEGACY_KEX,
        host_key_types=_HUAWEI_HOST_KEYS,
        ciphers=("aes128-cbc",),
        macs=_HUAWEI_MACS,
    ),
}


def _normalized(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").strip().lower())


def resolve_policy(olt: OLTDevice) -> OltSshPolicy:
    vendor = _normalized(olt.vendor)
    model = _normalized(olt.model)
    if vendor == "huawei":
        if "ma5608t" in model:
            return _POLICIES["huawei_ma5608t"]
        if "ma5800" in model:
            return _POLICIES["huawei_ma5800"]
        if "ma5600" in model:
            return _POLICIES["huawei_ma5600"]
    raise ValueError(f"No SSH driver policy found for vendor={olt.vendor!r}, model={olt.model!r}")


def _apply_preferred_algorithms(transport: Transport, policy: OltSshPolicy) -> None:
    opts = transport.get_security_options()
    opts.kex = list(policy.kex) + [item for item in opts.kex if item not in policy.kex]
    opts.key_types = list(policy.host_key_types) + [
        item for item in opts.key_types if item not in policy.host_key_types
    ]
    opts.ciphers = list(policy.ciphers) + [item for item in opts.ciphers if item not in policy.ciphers]
    opts.digests = list(policy.macs) + [item for item in opts.digests if item not in policy.macs]


def _read_until_prompt(channel: Channel, prompt_regex: str, timeout_sec: float = 8.0) -> str:
    compiled = re.compile(prompt_regex)
    buffer = ""
    channel.settimeout(0.8)
    while True:
        try:
            chunk = channel.recv(4096).decode("utf-8", errors="replace")
        except socket.timeout:
            if compiled.search(buffer):
                return buffer
            if timeout_sec <= 0:
                return buffer
            timeout_sec -= 0.8
            continue
        if not chunk:
            return buffer
        buffer += chunk
        if compiled.search(buffer):
            return buffer


def run_version_probe(olt: OLTDevice) -> tuple[str, str]:
    host = (olt.mgmt_ip or olt.hostname or "").strip()
    if not host:
        raise ValueError("Management IP or hostname is required")
    if not olt.ssh_username:
        raise ValueError("SSH username is required")
    if not olt.ssh_password:
        raise ValueError("SSH password is required")

    policy = resolve_policy(olt)
    password = decrypt_credential(olt.ssh_password)
    if not password:
        raise ValueError("SSH password could not be decrypted")

    port = int(olt.ssh_port or 22)
    sock = socket.create_connection((host, port), timeout=20)
    transport = Transport(sock)
    try:
        _apply_preferred_algorithms(transport, policy)
        transport.start_client(timeout=20)
        transport.auth_password(username=olt.ssh_username, password=password)
        if not transport.is_authenticated():
            raise RuntimeError("SSH authentication failed")
        channel = transport.open_session(timeout=20)
        channel.get_pty()
        channel.invoke_shell()
        _read_until_prompt(channel, policy.prompt_regex, timeout_sec=8)
        channel.send("screen-length 0 temporary\n")
        _read_until_prompt(channel, policy.prompt_regex, timeout_sec=8)
        channel.send(f"{policy.version_command}\n")
        output = _read_until_prompt(channel, policy.prompt_regex, timeout_sec=12)
        return policy.key, output
    finally:
        transport.close()


def test_connection(olt: OLTDevice) -> tuple[bool, str, str | None]:
    try:
        policy_key, output = run_version_probe(olt)
    except (SSHException, socket.error, OSError) as exc:
        return False, f"Connection failed: {type(exc).__name__}: {exc}", None
    except Exception as exc:
        logger.error("Unexpected error testing OLT %s connection: %s", olt.name, exc)
        return False, f"Unexpected error: {type(exc).__name__}", None
    if not output.strip():
        return False, "SSH connected but no CLI output returned", policy_key
    return True, "SSH connection test successful", policy_key
