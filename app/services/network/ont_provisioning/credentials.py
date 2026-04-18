"""Credential-safe provisioning helpers."""

from __future__ import annotations

# Keywords to mask in command strings for safe logging
# Includes various case variants and credential-related terms
_CREDENTIAL_KEYWORDS = (
    # Core - lowercase, title, uppercase
    "password",
    "Password",
    "PASSWORD",
    "secret",
    "Secret",
    "SECRET",
    # Keys and tokens
    "key",
    "Key",
    "KEY",
    "token",
    "Token",
    "TOKEN",
    # Auth
    "authorization",
    "credential",
    # WiFi
    "passphrase",
    "psk",
    "PSK",
    "pre-shared-key",
    # PPPoE
    "pppoe-password",
    "ppp-password",
    # SNMP
    "community",
    "Community",
)


def mask_credentials(cmd: str) -> str:
    """Mask credential values in OLT CLI command strings for safe logging."""
    for keyword in _CREDENTIAL_KEYWORDS:
        idx = cmd.find(f" {keyword} ")
        if idx == -1:
            continue
        prefix = cmd[: idx + len(keyword) + 2]
        rest = cmd[idx + len(keyword) + 2 :]
        next_space = rest.find(" ")
        if next_space == -1:
            cmd = prefix + "********"
        else:
            cmd = prefix + "********" + rest[next_space:]
    return cmd

