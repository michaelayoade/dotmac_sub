"""Credential-safe provisioning helpers."""

from __future__ import annotations

import re

_CREDENTIAL_WORDS = (
    r"password|passwd|pwd|secret|token|api[-_]?key|key|authorization|"
    r"credential|passphrase|psk|pre[-_]?shared[-_]?key|"
    r"pppoe[-_]?password|ppp[-_]?password|community"
)

_JSON_CREDENTIAL_RE = re.compile(
    rf"(?P<prefix>(?P<field_quote>['\"])(?:{_CREDENTIAL_WORDS})(?P=field_quote)\s*:\s*)"
    r"(?P<quote>['\"])(?P<value>.*?)(?P=quote)",
    re.IGNORECASE,
)

_XML_CREDENTIAL_RE = re.compile(
    rf"(?P<prefix><(?P<tag>(?:{_CREDENTIAL_WORDS}))[^>]*>)"
    r"(?P<value>.*?)"
    r"(?P<suffix></(?P=tag)>)",
    re.IGNORECASE,
)

_AUTH_SCHEME_RE = re.compile(
    r"(?P<prefix>(?:^|\s|[=/,;]|--?)authorization(?:\s+|=|:)\s*"
    r"(?:bearer|basic|digest)\s+)"
    r"(?P<quote>['\"]?)"
    r"(?P<value>[^'\"\s,;]+)"
    r"(?P=quote)",
    re.IGNORECASE,
)

_QUALIFIED_CREDENTIAL_RE = re.compile(
    rf"(?P<prefix>(?:^|\s|[=/,;]|--?)(?:{_CREDENTIAL_WORDS})"
    r"(?:\s+|=|:)(?:(?:read|write|rw|ro)\s+)?"
    r"(?:cipher|simple|plain|encrypted|hash)\s+)"
    r"(?P<quote>['\"]?)"
    r"(?P<value>[^'\"\s,;]+)"
    r"(?P=quote)",
    re.IGNORECASE,
)

_CREDENTIAL_KEYWORD_RE = re.compile(
    rf"(?P<prefix>(?:^|\s|[=/,;]|--?)(?:{_CREDENTIAL_WORDS})"
    r"(?:\s+|=|:))"
    r"(?P<quote>['\"]?)"
    r"(?!(?:cipher|simple|plain|encrypted|hash|read|write|rw|ro)(?:['\"\s,;]|$))"
    r"(?P<value>[^'\"\s,;]+)"
    r"(?P=quote)",
    re.IGNORECASE,
)

_QUOTED_CREDENTIAL_RE = re.compile(
    rf"(?P<prefix>(?:{_CREDENTIAL_WORDS})(?:\s+|=|:))"
    r"(?P<quote>['\"])(?P<value>.*?)(?P=quote)",
    re.IGNORECASE,
)


def mask_credentials(cmd: str) -> str:
    """Mask credential values in OLT CLI command strings for safe logging."""
    masked = _JSON_CREDENTIAL_RE.sub(
        lambda match: f"{match.group('prefix')}{match.group('quote')}********{match.group('quote')}",
        cmd,
    )
    masked = _XML_CREDENTIAL_RE.sub(
        lambda match: f"{match.group('prefix')}********{match.group('suffix')}",
        masked,
    )
    for pattern in (
        _AUTH_SCHEME_RE,
        _QUALIFIED_CREDENTIAL_RE,
        _QUOTED_CREDENTIAL_RE,
        _CREDENTIAL_KEYWORD_RE,
    ):
        masked = pattern.sub(
            lambda match: f"{match.group('prefix')}{match.group('quote')}********{match.group('quote')}",
            masked,
        )
    return masked
