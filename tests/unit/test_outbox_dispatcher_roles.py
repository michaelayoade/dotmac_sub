"""Pure role-contract tests for the explicit outbox bootstrap."""

from __future__ import annotations

from app.outbox_dispatcher_roles import (
    RELAY_DISPATCHER_CONTRACT,
    relay_dispatcher_violations,
)


def test_both_dispatchers_are_non_privileged_login_roles() -> None:
    assert RELAY_DISPATCHER_CONTRACT == {
        "outbox_dispatcher": (True, False, False),
        "platform_outbox_dispatcher": (True, False, False),
    }
    assert relay_dispatcher_violations(RELAY_DISPATCHER_CONTRACT) == ()


def test_absent_non_login_bypass_and_superuser_postures_are_refused() -> None:
    observed = {
        "outbox_dispatcher": (False, False, False),
        "platform_outbox_dispatcher": (True, True, True),
    }
    violations = relay_dispatcher_violations(observed)
    assert any("outbox_dispatcher has" in violation for violation in violations)
    assert any(
        "platform_outbox_dispatcher has" in violation for violation in violations
    )

    missing = relay_dispatcher_violations({})
    assert len(missing) == 2
    assert all(violation.endswith("is missing") for violation in missing)
