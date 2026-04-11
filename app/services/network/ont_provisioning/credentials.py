"""Credential-safe provisioning helpers."""

from __future__ import annotations

_CREDENTIAL_KEYWORDS = ("password", "secret", "Password")


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

