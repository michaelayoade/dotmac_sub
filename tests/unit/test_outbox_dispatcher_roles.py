"""Pure role-contract tests for the explicit outbox bootstrap."""

from __future__ import annotations

from dataclasses import replace

from app.outbox_dispatcher_roles import (
    OUTBOX_RELAY_OWNERSHIP_CONTRACT,
    RELAY_DISPATCHER_CONTRACT,
    RoleGraphObservation,
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


GOOD_GRAPH = RoleGraphObservation(
    migrator_exists=True,
    owner_exists=True,
    migrator_inherits_owner=True,
    migrator_can_set_owner=True,
    migrator_admin_option=False,
    owner_reaches_migrator=False,
)
GOOD_SCHEMA = {"USAGE": True, "CREATE": True}


def test_relay_function_ownership_prerequisites_are_typed() -> None:
    contract = OUTBOX_RELAY_OWNERSHIP_CONTRACT
    # The migrator is app_admin and is a member of the runtime owner.
    assert contract.migration_role == "app_admin"
    assert contract.owner_role == "dotmac_app"
    assert contract.definer_role == "app_admin"
    assert contract.schema == "public"
    assert contract.schema_privileges == ("USAGE", "CREATE")
    assert (
        relay_ownership_violations(
            role_graph=GOOD_GRAPH, definer_schema_privileges=GOOD_SCHEMA
        )
        == ()
    )


def test_old_direction_membership_is_refused() -> None:
    """dotmac_app being a member of app_admin is the edge being removed."""
    graph = replace(GOOD_GRAPH, owner_reaches_migrator=True)
    violations = relay_ownership_violations(
        role_graph=graph, definer_schema_privileges=GOOD_SCHEMA
    )
    assert len(violations) == 1
    assert "dotmac_app is a member of app_admin" in violations[0]
    assert "old direction" in violations[0]


def test_missing_inherit_set_and_admin_option_are_each_named() -> None:
    graph = replace(
        GOOD_GRAPH,
        migrator_inherits_owner=False,
        migrator_can_set_owner=False,
        migrator_admin_option=True,
    )
    assert relay_ownership_violations(
        role_graph=graph, definer_schema_privileges=GOOD_SCHEMA
    ) == (
        "app_admin is not a member of dotmac_app with INHERIT",
        "app_admin cannot SET ROLE dotmac_app",
        "app_admin holds ADMIN OPTION on dotmac_app",
    )


def test_missing_roles_and_schema_privileges_are_refused() -> None:
    graph = replace(GOOD_GRAPH, migrator_exists=False, owner_exists=False)
    violations = relay_ownership_violations(
        role_graph=graph,
        definer_schema_privileges={"USAGE": True, "CREATE": False},
    )
    assert violations == (
        "database role 'app_admin' is missing",
        "database role 'dotmac_app' is missing",
        "app_admin lacks CREATE on schema public",
    )


def test_login_role_reaching_a_bypassrls_role_is_refused() -> None:
    graph = replace(GOOD_GRAPH, bypass_reachers=("dotmac_schema_bootstrap->app_admin",))
    violations = relay_ownership_violations(
        role_graph=graph, definer_schema_privileges=GOOD_SCHEMA
    )
    assert violations == (
        "login role path dotmac_schema_bootstrap->app_admin reaches a BYPASSRLS "
        "role; only app_admin may",
    )
