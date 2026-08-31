"""Pure contract tests for commercial module database prerequisites."""

from __future__ import annotations

from app.commercial_module_prereqs import (
    COMMERCIAL_BOOTSTRAP_ROLE_CONTRACT,
    COMMERCIAL_MODULE_SCHEMA_CONTRACT,
    MODULE_DATABASE_ROLE_CONTRACT,
    ModuleSchemaObservation,
    commercial_bootstrap_role_violations,
    commercial_schema_violations,
    module_database_role_violations,
)


def test_commercial_module_schema_manifest_names_every_composed_module() -> None:
    expected = {
        "dotmac-billing": "mod_billing",
        "dotmac-collections": "mod_coll",
        "dotmac-payments": "mod_payments",
        "dotmac-service-orders": "mod_serviceorders",
        "dotmac-subscriptions": "mod_subscriptions",
    }
    actual = {
        item.distribution: item.schema for item in COMMERCIAL_MODULE_SCHEMA_CONTRACT
    }
    assert actual == expected
    assert all(
        item.owner_role == "dotmac_app" for item in COMMERCIAL_MODULE_SCHEMA_CONTRACT
    )
    assert all(
        item.usage_roles == ("app_admin", "app_user", "platform_api")
        for item in COMMERCIAL_MODULE_SCHEMA_CONTRACT
    )


def test_module_database_roles_match_the_rls_posture_contract() -> None:
    assert {
        role: item.posture for role, item in MODULE_DATABASE_ROLE_CONTRACT.items()
    } == {
        "app_admin": (True, True, False),
        "app_user": (True, False, False),
        "platform_api": (True, False, False),
    }
    assert (
        module_database_role_violations(
            {role: item.posture for role, item in MODULE_DATABASE_ROLE_CONTRACT.items()}
        )
        == ()
    )


def test_bootstrap_roles_include_the_schema_owner_without_broad_privilege() -> None:
    assert COMMERCIAL_BOOTSTRAP_ROLE_CONTRACT["dotmac_app"].posture == (
        True,
        False,
        False,
    )
    missing = commercial_bootstrap_role_violations({})
    assert any("dotmac_app" in violation for violation in missing)
    assert any("app_user" in violation for violation in missing)


def test_schema_contract_refuses_missing_public_and_usage_drift() -> None:
    good = {
        item.schema: ModuleSchemaObservation(
            owner_role=item.owner_role,
            public_privileges=(),
            usage_roles=item.usage_roles,
        )
        for item in COMMERCIAL_MODULE_SCHEMA_CONTRACT
    }
    assert commercial_schema_violations(good) == ()

    first = COMMERCIAL_MODULE_SCHEMA_CONTRACT[0]
    bad = dict(good)
    bad[first.schema] = ModuleSchemaObservation(
        owner_role="postgres",
        public_privileges=("USAGE",),
        usage_roles=("app_user",),
    )
    violations = commercial_schema_violations(bad)
    assert any("owned by 'postgres'" in violation for violation in violations)
    assert any("grants USAGE to PUBLIC" in violation for violation in violations)
    assert any(
        "does not grant USAGE to app_admin" in violation for violation in violations
    )
