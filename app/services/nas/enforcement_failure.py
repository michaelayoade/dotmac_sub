"""One classifier for NAS enforcement transport failures (ADR-0017 §5).

Matches exception types first (``routeros_api``, ``paramiko``,
``socket``/``OSError``), then falls back to message-text matching for
transports (SSH shell-out, legacy drivers) that raise a bare ``Exception``
carrying only a vendor error string. The returned detail always passes through
``app.logging.sanitize_exception`` so a credential embedded in a RouterOS API
exception's text is never persisted.
"""

from __future__ import annotations

import errno as errno_module
import socket

from app.logging import sanitize_exception
from app.models.enforcement_application import EnforcementFailureClass

__all__ = ["classify_enforcement_failure"]

_UNREACHABLE_ERRNOS = frozenset(
    {errno_module.ECONNREFUSED, errno_module.EHOSTUNREACH, errno_module.ENETUNREACH}
)


def _routeros_exception_types() -> tuple[type[BaseException], ...]:
    try:
        from routeros_api.exceptions import (
            RouterOsApiCommunicationError,
            RouterOsApiConnectionError,
        )
    except ImportError:  # pragma: no cover - routeros_api is a hard dependency
        return ()
    return (RouterOsApiConnectionError, RouterOsApiCommunicationError)


def _paramiko_exception_types() -> tuple[type[BaseException], ...]:
    try:
        from paramiko.ssh_exception import (
            AuthenticationException,
            NoValidConnectionsError,
            SSHException,
        )
    except ImportError:  # pragma: no cover - paramiko is a hard dependency
        return ()
    return (AuthenticationException, NoValidConnectionsError, SSHException)


def _classify_by_type(exc: BaseException) -> EnforcementFailureClass | None:
    routeros_conn, routeros_comm = _routeros_exception_types() or (
        type(None),
        type(None),
    )
    paramiko_auth, paramiko_no_valid_conn, paramiko_ssh = (
        _paramiko_exception_types() or (type(None), type(None), type(None))
    )

    if isinstance(exc, paramiko_auth):
        return EnforcementFailureClass.auth_rejected
    if isinstance(exc, routeros_conn):
        return EnforcementFailureClass.unreachable
    if isinstance(exc, paramiko_no_valid_conn):
        return EnforcementFailureClass.unreachable
    if isinstance(exc, routeros_comm):
        # RouterOS reports a rejected /login as a communication-error TRAP,
        # not a distinct type: `Error "invalid user name or password (6)"
        # executing command b'/login ...'`. Matching the type alone would
        # file the exact failure this record exists for (a router whose API
        # user vanished) as a generic command failure.
        if _is_routeros_login_rejection(str(exc)):
            return EnforcementFailureClass.auth_rejected
        return EnforcementFailureClass.command_failed
    if isinstance(exc, socket.timeout | TimeoutError):
        return EnforcementFailureClass.timeout
    if isinstance(exc, ConnectionResetError | BrokenPipeError):
        return EnforcementFailureClass.command_failed
    if isinstance(exc, OSError) and exc.errno in _UNREACHABLE_ERRNOS:
        return EnforcementFailureClass.unreachable
    if isinstance(exc, paramiko_ssh):
        # A generic SSHException that isn't an auth or connection failure
        # (e.g. a channel-level protocol error mid-command).
        return EnforcementFailureClass.command_failed
    return None


_ROUTEROS_LOGIN_REJECTION_MARKERS = (
    "invalid user name or password",
    "cannot log in",
)


def _is_routeros_login_rejection(message: str) -> bool:
    lowered = message.lower()
    return any(marker in lowered for marker in _ROUTEROS_LOGIN_REJECTION_MARKERS)


def _classify_by_message(message: str) -> EnforcementFailureClass:
    lowered = message.lower()
    if _is_routeros_login_rejection(message):
        return EnforcementFailureClass.auth_rejected
    if "timed out" in lowered:
        return EnforcementFailureClass.timeout
    if "no route to host" in lowered:
        return EnforcementFailureClass.unreachable
    return EnforcementFailureClass.command_failed


def classify_enforcement_failure(
    exc: BaseException,
) -> tuple[EnforcementFailureClass, str]:
    """Classify a NAS enforcement transport failure into a typed class.

    Matches the exception's type first; falls back to matching its sanitized
    message text for drivers that raise a bare ``Exception``. The returned
    detail is always ``app.logging.sanitize_exception(exc)`` — the classifier
    never re-derives its own copy of the message.
    """

    detail = sanitize_exception(exc)
    failure_class = _classify_by_type(exc)
    if failure_class is None:
        failure_class = _classify_by_message(detail)
    return failure_class, detail
