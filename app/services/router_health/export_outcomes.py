"""Classify a failed config export into a stored failure class (never the text)."""

from __future__ import annotations

import re
import socket

from paramiko.ssh_exception import (
    AuthenticationException,
    BadHostKeyException,
    NoValidConnectionsError,
    SSHException,
)

from app.services.router_health.contracts import ConfigExportFailureClass
from app.services.router_management.config_export import (
    RouterConfigExportEmptyError,
    RouterConfigExportNoAuthError,
)

# paramiko's RejectPolicy raises "Server 'h' not found in known_hosts"; accept
# the spaced spelling too so a wording change cannot silently reclassify it.
_UNKNOWN_HOST_RE = re.compile(r"not found in known[ _]hosts", re.IGNORECASE)


def _classify_one(exc: BaseException) -> ConfigExportFailureClass | None:
    if isinstance(exc, BadHostKeyException):
        return ConfigExportFailureClass.host_key_changed
    if isinstance(exc, AuthenticationException):
        return ConfigExportFailureClass.auth_failed
    if isinstance(exc, RouterConfigExportNoAuthError):
        return ConfigExportFailureClass.no_auth_configured
    if isinstance(exc, RouterConfigExportEmptyError):
        return ConfigExportFailureClass.empty_export
    if isinstance(exc, SSHException) and _UNKNOWN_HOST_RE.search(str(exc)) is not None:
        return ConfigExportFailureClass.host_key_unknown
    if isinstance(
        exc,
        NoValidConnectionsError
        | socket.timeout
        | TimeoutError
        | ConnectionError
        | OSError,
    ):
        return ConfigExportFailureClass.unreachable
    return None


def classify_export_failure(exc: BaseException) -> ConfigExportFailureClass:
    """Classify from the underlying cause first, then the wrapper itself."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        found = _classify_one(current)
        if found is not None:
            return found
        current = current.__cause__ or current.__context__
    return ConfigExportFailureClass.other
