"""Parametrized coverage for the single NAS enforcement failure classifier.

``classify_enforcement_failure`` (app/services/nas/enforcement_failure.py) is
the one place a raw transport exception becomes a typed
``EnforcementFailureClass`` plus a sanitized detail string (ADR-0017 §5).
Every case here also asserts the sanitized detail never carries the raw
RouterOS API credential embedded in some exception messages.
"""

from __future__ import annotations

import errno

import paramiko
import pytest
from routeros_api.exceptions import (
    RouterOsApiCommunicationError,
    RouterOsApiConnectionError,
)

from app.models.enforcement_application import EnforcementFailureClass
from app.services.nas.enforcement_failure import classify_enforcement_failure

_SECRET = "SECRET"

_CASES = [
    pytest.param(
        RouterOsApiCommunicationError(
            'Error "invalid user name or password (6)" executing command '
            f"b'/login =name=X =password={_SECRET} .tag=1'",
            b"invalid user name or password (6)",
        ),
        EnforcementFailureClass.auth_rejected,
        id="routeros_login_rejection_is_auth_rejected",
    ),
    pytest.param(
        RouterOsApiCommunicationError("no such item", b"x"),
        EnforcementFailureClass.command_failed,
        id="routeros_generic_communication_error_is_command_failed",
    ),
    pytest.param(
        RouterOsApiConnectionError("x"),
        EnforcementFailureClass.unreachable,
        id="routeros_connection_error_is_unreachable",
    ),
    pytest.param(
        TimeoutError(),
        EnforcementFailureClass.timeout,
        id="timeout_error_is_timeout",
    ),
    pytest.param(
        OSError(errno.EHOSTUNREACH, "x"),
        EnforcementFailureClass.unreachable,
        id="os_error_ehostunreach_is_unreachable",
    ),
    pytest.param(
        paramiko.AuthenticationException("x"),
        EnforcementFailureClass.auth_rejected,
        id="paramiko_authentication_exception_is_auth_rejected",
    ),
    pytest.param(
        Exception("the password field is required"),
        EnforcementFailureClass.command_failed,
        id="near_miss_password_mention_is_not_auth_rejected",
    ),
    pytest.param(
        Exception("... cannot log in ..."),
        EnforcementFailureClass.auth_rejected,
        id="bare_exception_cannot_log_in_message_is_auth_rejected",
    ),
]


@pytest.mark.parametrize("exc, expected_class", _CASES)
def test_classify_enforcement_failure(
    exc: BaseException, expected_class: EnforcementFailureClass
) -> None:
    failure_class, detail = classify_enforcement_failure(exc)

    assert failure_class is expected_class
    assert _SECRET not in detail
