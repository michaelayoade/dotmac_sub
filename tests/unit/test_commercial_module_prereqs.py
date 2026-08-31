"""Pure contract tests for composed module database prerequisites."""

from __future__ import annotations

import pytest

from app.commercial_module_prereqs import (
    COMMERCIAL_BOOTSTRAP_ROLE_CONTRACT,
    MODULE_DATABASE_ROLE_CONTRACT,
    PUBLIC_PROBE_ROLE,
    ModuleSchemaObservation,
    commercial_bootstrap_role_violations,
    commercial_schema_violations,
    composed_lineage_import_names,
    module_database_role_violations,
    module_schema_contract,
    module_schemas,
)


def _satisfied() -> dict[str, ModuleSchemaObservation]:
    return {
        item.schema: ModuleSchemaObservation(
            owner_role=item.owner_role,
            public_privileges=(),
            usage_roles=item.usage_roles,
            probe_privileges=(),
            probe_observed=True,
        )
        for item in module_schema_contract()
    }


def test_the_schema_set_is_derived_from_the_composed_lineages() -> None:
    """The whole point of the rewrite: no hand-kept list to go stale.

    `mod_inbox` reached production unprovisioned because three prose lists and
    one Python tuple each had to be remembered separately.  Deriving means
    composing a lineage cannot silently skip the prerequisite.
    """
    derived = {item.import_name: item.schema for item in module_schema_contract()}
    assert set(derived) == set(composed_lineage_import_names())
    assert derived == {
        "dotmac_billing": "mod_billing",
        "dotmac_collections": "mod_coll",
        "dotmac_inbox": "mod_inbox",
        "dotmac_payments": "mod_payments",
        "dotmac_service_orders": "mod_serviceorders",
        "dotmac_subscriptions": "mod_subscriptions",
    }
    assert module_schemas() == set(derived.values())
    assert all(item.owner_role == "dotmac_app" for item in module_schema_contract())
    assert all(
        item.usage_roles == ("app_admin", "app_user", "platform_api")
        for item in module_schema_contract()
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


def test_the_public_probe_role_cannot_log_in() -> None:
    """A measuring instrument, not an identity.

    If the probe could log in it would be one more credential to protect, and
    a tempting one: it is by construction the role with no privileges anywhere.
    """
    probe = COMMERCIAL_BOOTSTRAP_ROLE_CONTRACT[PUBLIC_PROBE_ROLE]
    assert probe.posture == (False, False, False)


def test_schema_contract_refuses_ownership_public_and_usage_drift() -> None:
    good = _satisfied()
    assert commercial_schema_violations(good) == ()

    first = module_schema_contract()[0]
    bad = dict(good)
    bad[first.schema] = ModuleSchemaObservation(
        owner_role="postgres",
        public_privileges=("USAGE",),
        usage_roles=("app_user",),
        probe_privileges=("USAGE",),
        probe_observed=True,
    )
    violations = commercial_schema_violations(bad)
    assert any("owned by 'postgres'" in violation for violation in violations)
    assert any("grants USAGE to PUBLIC" in violation for violation in violations)
    assert any(
        "does not grant USAGE to app_admin" in violation for violation in violations
    )
    assert any("is reachable by dotmac_public_probe" in v for v in violations)


def test_an_unobserved_probe_is_a_violation_not_a_pass() -> None:
    """The non-vacuity guard.

    `probe_privileges=()` means "asked, and the answer was no".  Without
    `probe_observed`, a missing probe role would produce the identical empty
    tuple and the PUBLIC-denial assertion would pass because nothing was ever
    checked — the exact failure mode this contract exists to prevent.
    """
    unprobed = {
        item.schema: ModuleSchemaObservation(
            owner_role=item.owner_role,
            public_privileges=(),
            usage_roles=item.usage_roles,
            probe_privileges=(),
            probe_observed=False,
        )
        for item in module_schema_contract()
    }
    violations = commercial_schema_violations(unprobed)
    assert violations, "an unprobed schema must not verify clean"
    assert all("PUBLIC denial is unproven" in v for v in violations)


def test_probe_reachability_is_reported_even_when_the_acl_row_is_absent() -> None:
    """Denial beats absence.

    A grant reaching PUBLIC through some path other than a `grantee = 0` ACL
    row on the schema itself is invisible to the catalog read but visible to
    the probe.  The probe must be the half that fails.
    """
    first = module_schema_contract()[0]
    observed = _satisfied()
    observed[first.schema] = ModuleSchemaObservation(
        owner_role=first.owner_role,
        public_privileges=(),
        usage_roles=first.usage_roles,
        probe_privileges=("CREATE",),
        probe_observed=True,
    )
    violations = commercial_schema_violations(observed)
    assert len(violations) == 1
    assert "is reachable by dotmac_public_probe" in violations[0]
    assert "CREATE" in violations[0]


@pytest.mark.parametrize("schema", sorted(module_schemas()))
def test_every_derived_schema_is_missing_when_absent(schema: str) -> None:
    observed = _satisfied()
    del observed[schema]
    violations = commercial_schema_violations(observed)
    assert f"schema {schema!r} is missing" in violations
