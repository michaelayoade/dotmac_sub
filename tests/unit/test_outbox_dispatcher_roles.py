"""Pure role-contract tests for the explicit outbox bootstrap."""

from __future__ import annotations

from app.outbox_dispatcher_roles import (
    OUTBOX_RELAY_OWNERSHIP_CONTRACT,
    RELAY_DISPATCHER_CONTRACT,
    relay_dispatcher_violations,
    relay_ownership_violations,
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


def test_relay_function_ownership_prerequisites_are_typed() -> None:
    assert OUTBOX_RELAY_OWNERSHIP_CONTRACT.migration_role == "dotmac_app"
    assert OUTBOX_RELAY_OWNERSHIP_CONTRACT.definer_role == "app_admin"
    assert OUTBOX_RELAY_OWNERSHIP_CONTRACT.schema == "public"
    assert OUTBOX_RELAY_OWNERSHIP_CONTRACT.schema_privileges == ("USAGE", "CREATE")
    assert (
        relay_ownership_violations(
            migration_role_is_definer_member=True,
            definer_schema_privileges={"USAGE": True, "CREATE": True},
        )
        == ()
    )


def test_relay_function_ownership_refuses_missing_membership_and_schema_privileges() -> (
    None
):
    violations = relay_ownership_violations(
        migration_role_is_definer_member=False,
        definer_schema_privileges={"USAGE": True, "CREATE": False},
    )

    assert violations == (
        "dotmac_app is not a member of app_admin",
        "app_admin lacks CREATE on schema public",
    )
