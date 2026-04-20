"""Credential-safe provisioning helpers."""

from __future__ import annotations

import re

_CREDENTIAL_KEYWORD_RE = re.compile(
    r"(?P<prefix>(?:^|\s|[=/,;])"
    r"(?:"
    r"password|passwd|pwd|secret|token|api[-_]?key|key|authorization|"
    r"credential|passphrase|psk|pre[-_]?shared[-_]?key|"
    r"pppoe[-_]?password|ppp[-_]?password|community"
    r")"
    r"(?:\s+|=|:))"
    r"(?P<quote>['\"]?)"
    r"(?P<value>[^'\"\s,;]+)"
    r"(?P=quote)",
    re.IGNORECASE,
)

_QUOTED_CREDENTIAL_RE = re.compile(
    r"(?P<prefix>(?:"
    r"password|passwd|pwd|secret|token|api[-_]?key|authorization|"
    r"passphrase|psk|pre[-_]?shared[-_]?key|community"
    r")(?:\s+|=|:))"
    r"(?P<quote>['\"])(?P<value>.*?)(?P=quote)",
    re.IGNORECASE,
)


def mask_credentials(cmd: str) -> str:
    """Mask credential values in OLT CLI command strings for safe logging."""
    masked = _QUOTED_CREDENTIAL_RE.sub(
        lambda match: f"{match.group('prefix')}{match.group('quote')}********{match.group('quote')}",
        cmd,
    )
    return _CREDENTIAL_KEYWORD_RE.sub(
        lambda match: f"{match.group('prefix')}{match.group('quote')}********{match.group('quote')}",
        masked,
    )
