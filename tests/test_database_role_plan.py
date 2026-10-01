"""Pure plan contract; these tests do not import a runtime or connect to SQL."""

from __future__ import annotations

import json
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from app.database_role_plan import (
    AuthorityPolicy,
    CatalogObject,
    CatalogSnapshot,
    ColumnGrant,
    DefaultAcl,
    EvidenceCompleteness,
    Grant,
    Membership,
    NamedColumnGrant,
    NamedGrant,
    ObjectPolicy,
    OperationalBootstrapMembership,
    PlanInputError,
    RolePosture,
    SourceAdminPrincipal,
    TypeName,
    compile_plan,
    decode_catalog,
    decode_policy,
    object_identity,
)
from scripts.plan_database_authority import main


def _object(kind: str, name: str, subkind: str | None, owner: str) -> CatalogObject:
    return CatalogObject(
        kind=kind,  # type: ignore[arg-type] -- fixture covers four literal kinds
        schema=None if kind == "schema" else "public",
        name=name,
        subkind=subkind,
        argument_types=(),
        owner=owner,
        extension_owned=False,
        columns=("id", "total") if kind == "relation" and subkind != "S" else (),
        acl_grants=(),
        column_grants=(),
        effective_privileges=(
            Grant("PUBLIC", (), ()),
            Grant("app_user", (), ()),
            Grant("platform_api", (), ()),
        ),
    )


def _reviewed_inputs() -> tuple[CatalogSnapshot, AuthorityPolicy]:
    objects = (
        _object("schema", "public", None, "pg_database_owner"),
        _object("relation", "invoice", "r", "legacy_writer"),
        _object("relation", "invoice_seq", "S", "legacy_writer"),
        _object("type", "invoice_state", "e", "legacy_writer"),
        _object("routine", "invoice_total", "f", "legacy_writer"),
    )
    catalog = CatalogSnapshot(
        schema_version=1,
        database="sub_review",
        database_owner="postgres",
        database_acl_grants=(
            Grant("postgres", ("CONNECT", "CREATE", "TEMPORARY"), ("CREATE",)),
            Grant("PUBLIC", ("CONNECT", "TEMPORARY"), ()),
            Grant("dotmac_schema_bootstrap", ("CREATE",), ()),
        ),
        scope_schemas=("public",),
        evidence=EvidenceCompleteness(
            True, True, True, True, True, True, True, True, True, True
        ),
        roles=(
            RolePosture("postgres", True, True, True, True, True, True, True),
            RolePosture("legacy_writer", True, True, False, False, False, False, False),
            RolePosture("app_admin", True, True, True, False, False, False, False),
            RolePosture("app_user", True, True, False, False, False, False, False),
            RolePosture("platform_api", True, True, False, False, False, False, False),
            RolePosture(
                "dotmac_schema_bootstrap", True, False, False, False, False, False, True
            ),
        ),
        memberships=(
            Membership("dotmac_schema_bootstrap", "app_admin", False, True, False),
        ),
        default_acls=(),
        objects=objects,
    )
    grants = {
        "public": (NamedGrant("app_user", ("USAGE",), None),),
        "invoice": (NamedGrant("app_user", ("SELECT", "UPDATE"), None),),
        "invoice_seq": (NamedGrant("app_user", ("SELECT", "USAGE"), None),),
        "invoice_state": (),
        "invoice_total": (
            NamedGrant("PUBLIC", ("EXECUTE",), "reviewed legacy routine contract"),
        ),
    }
    policy = AuthorityPolicy(
        schema_version=1,
        database="sub_review",
        target_owner="app_admin",
        runtime_role="app_user",
        expected_database_owner="postgres",
        scope_schemas=("public",),
        approved_source_owners=("legacy_writer", "pg_database_owner"),
        source_admin_principals=(
            SourceAdminPrincipal("postgres", "database superuser observation"),
        ),
        operational_bootstrap_memberships=(
            OperationalBootstrapMembership(
                "dotmac_schema_bootstrap",
                "app_admin",
                "managed schema bootstrap contract",
            ),
        ),
        objects=tuple(
            ObjectPolicy(
                object_identity(item),
                "named" if grants[item.name] else "none",
                grants[item.name],
                (),
            )
            for item in objects
        ),
    )
    return catalog, policy


def _blocked(catalog: CatalogSnapshot, policy: AuthorityPolicy, fragment: str) -> None:
    plan = compile_plan(catalog, policy)
    assert plan.status == "blocked"
    assert plan.statements == ()
    assert any(fragment in reason for reason in plan.blocked_reasons), (
        plan.blocked_reasons
    )


def _invoice_policy(
    policy: AuthorityPolicy, catalog: CatalogSnapshot, *grants: NamedColumnGrant
) -> AuthorityPolicy:
    invoice_identity = object_identity(catalog.objects[1])
    return replace(
        policy,
        objects=tuple(
            replace(item, column_grants=grants)
            if item.identity == invoice_identity
            else item
            for item in policy.objects
        ),
    )


def test_reviewed_named_grants_and_ownership_have_ordered_bound_plan() -> None:
    catalog, policy = _reviewed_inputs()
    plan = compile_plan(catalog, policy)
    assert plan.status == "ready_for_review"
    assert plan.blocked_reasons == ()
    assert plan.statements == (
        'ALTER SCHEMA "public" OWNER TO "app_admin";',
        'GRANT USAGE ON SCHEMA "public" TO "app_user";',
        'ALTER TYPE "public"."invoice_state" OWNER TO "app_admin";',
        'ALTER TABLE "public"."invoice" OWNER TO "app_admin";',
        'GRANT SELECT, UPDATE ON TABLE "public"."invoice" TO "app_user";',
        'ALTER SEQUENCE "public"."invoice_seq" OWNER TO "app_admin";',
        'GRANT SELECT, USAGE ON SEQUENCE "public"."invoice_seq" TO "app_user";',
        'ALTER FUNCTION "public"."invoice_total"() OWNER TO "app_admin";',
        'GRANT EXECUTE ON ROUTINE "public"."invoice_total"() TO PUBLIC;',
    )
    assert len(plan.plan_sha256) == 64
    assert not any(
        "ALTER DATABASE" in sql or "CREATE ROLE" in sql or "ALTER ROLE" in sql
        for sql in plan.statements
    )


def test_public_schema_owner_requires_explicit_approved_transfer() -> None:
    catalog, policy = _reviewed_inputs()
    _blocked(
        catalog,
        replace(policy, approved_source_owners=("legacy_writer",)),
        "unexpected source owner",
    )
    assert (
        'ALTER SCHEMA "public" OWNER TO "app_admin";'
        in compile_plan(catalog, policy).statements
    )
    owner_acl = replace(
        catalog.objects[0],
        acl_grants=(
            Grant("pg_database_owner", ("CREATE", "USAGE"), ("CREATE", "USAGE")),
        ),
    )
    catalog = replace(catalog, objects=(owner_acl, *catalog.objects[1:]))
    assert compile_plan(catalog, policy).status == "ready_for_review"


def test_exact_existing_column_acl_is_approved_without_regrant() -> None:
    catalog, policy = _reviewed_inputs()
    invoice = replace(
        catalog.objects[1],
        column_grants=(ColumnGrant("id", "app_user", ("UPDATE",), ()),),
    )
    catalog = replace(
        catalog, objects=(catalog.objects[0], invoice, *catalog.objects[2:])
    )
    policy = _invoice_policy(
        policy, catalog, NamedColumnGrant("id", "app_user", ("UPDATE",), None)
    )
    plan = compile_plan(catalog, policy)
    assert plan.status == "ready_for_review"
    assert not any("GRANT UPDATE (" in statement for statement in plan.statements)


def test_new_reviewed_column_grant_only_emits_missing_right_and_binds_digest() -> None:
    catalog, policy = _reviewed_inputs()
    invoice = replace(
        catalog.objects[1],
        column_grants=(ColumnGrant("id", "app_user", ("UPDATE",), ()),),
    )
    catalog = replace(
        catalog, objects=(catalog.objects[0], invoice, *catalog.objects[2:])
    )
    existing = _invoice_policy(
        policy, catalog, NamedColumnGrant("id", "app_user", ("UPDATE",), None)
    )
    expanded = _invoice_policy(
        policy,
        catalog,
        NamedColumnGrant("id", "app_user", ("SELECT", "UPDATE"), None),
    )
    plan = compile_plan(catalog, expanded)
    assert plan.status == "ready_for_review"
    assert (
        'GRANT SELECT ("id") ON TABLE "public"."invoice" TO "app_user";'
        in plan.statements
    )
    assert plan.plan_sha256 != compile_plan(catalog, existing).plan_sha256
    assert plan.policy_sha256 != compile_plan(catalog, existing).policy_sha256


def test_first_column_grant_uses_catalog_proved_column_without_object_grant() -> None:
    catalog, policy = _reviewed_inputs()
    invoice_identity = object_identity(catalog.objects[1])
    policy = replace(
        policy,
        objects=tuple(
            replace(
                item,
                grants=(),
                column_grants=(
                    NamedColumnGrant("total", "app_user", ("SELECT",), None),
                ),
            )
            if item.identity == invoice_identity
            else item
            for item in policy.objects
        ),
    )
    plan = compile_plan(catalog, policy)
    assert plan.status == "ready_for_review"
    assert (
        'GRANT SELECT ("total") ON TABLE "public"."invoice" TO "app_user";'
        in plan.statements
    )
    assert not any(
        'GRANT SELECT, UPDATE ON TABLE "public"."invoice"' in sql
        for sql in plan.statements
    )


def test_column_acl_excess_unknown_column_role_privilege_and_kind_block() -> None:
    catalog, policy = _reviewed_inputs()
    invoice = replace(
        catalog.objects[1],
        column_grants=(ColumnGrant("id", "app_user", ("SELECT", "UPDATE"), ()),),
    )
    catalog = replace(
        catalog, objects=(catalog.objects[0], invoice, *catalog.objects[2:])
    )
    approved = _invoice_policy(
        policy, catalog, NamedColumnGrant("id", "app_user", ("UPDATE",), None)
    )
    _blocked(catalog, approved, "excess or unexplained column ACL")
    for grant, reason in (
        (NamedColumnGrant("unknown", "app_user", ("SELECT",), None), "unknown column"),
        (NamedColumnGrant("id", "postgres", ("SELECT",), None), "unsafe posture"),
        (NamedColumnGrant("id", "app_user", ("DELETE",), None), "unknown privilege"),
        (NamedColumnGrant("id", "PUBLIC", ("SELECT",), None), "lacks justification"),
    ):
        _blocked(catalog, _invoice_policy(policy, catalog, grant), reason)
    sequence = replace(
        catalog.objects[2],
        column_grants=(ColumnGrant("id", "app_user", ("UPDATE",), ()),),
    )
    wrong_kind_catalog = replace(
        catalog,
        objects=(
            catalog.objects[0],
            catalog.objects[1],
            sequence,
            *catalog.objects[3:],
        ),
    )
    _blocked(wrong_kind_catalog, policy, "unsupported column-grant relation kind")


def test_grant_options_require_complete_direct_evidence_and_no_delegation() -> None:
    catalog, policy = _reviewed_inputs()
    _blocked(
        replace(
            catalog, evidence=replace(catalog.evidence, grant_options_complete=False)
        ),
        policy,
        "grant_options_complete evidence",
    )
    delegated = replace(
        catalog.objects[1],
        acl_grants=(Grant("app_user", ("SELECT",), ("SELECT",)),),
    )
    _blocked(
        replace(catalog, objects=(catalog.objects[0], delegated, *catalog.objects[2:])),
        policy,
        "non-owner direct grant options",
    )
    owner_entry = replace(
        catalog.objects[1],
        acl_grants=(Grant("legacy_writer", ("SELECT",), ("SELECT",)),),
    )
    owner_catalog = replace(
        catalog,
        objects=(catalog.objects[0], owner_entry, *catalog.objects[2:]),
    )
    owner_plan = compile_plan(owner_catalog, policy)
    assert owner_plan.status == "ready_for_review"
    no_option_owner = replace(
        owner_entry,
        acl_grants=(Grant("legacy_writer", ("SELECT",), ()),),
    )
    no_option_catalog = replace(
        catalog,
        objects=(catalog.objects[0], no_option_owner, *catalog.objects[2:]),
    )
    assert (
        owner_plan.catalog_sha256
        != compile_plan(no_option_catalog, policy).catalog_sha256
    )
    column_option = replace(
        catalog.objects[1],
        column_grants=(ColumnGrant("id", "app_user", ("UPDATE",), ("UPDATE",)),),
    )
    _blocked(
        replace(
            catalog, objects=(catalog.objects[0], column_option, *catalog.objects[2:])
        ),
        policy,
        "column ACL grant options require separate review",
    )


def test_digest_binds_catalog_policy_and_order_but_ignores_input_order() -> None:
    catalog, policy = _reviewed_inputs()
    first = compile_plan(catalog, policy)
    reordered = compile_plan(
        replace(catalog, objects=tuple(reversed(catalog.objects))),
        replace(policy, objects=tuple(reversed(policy.objects))),
    )
    assert first.plan_sha256 == reordered.plan_sha256
    modified = replace(
        policy,
        objects=tuple(
            replace(item, grants=(NamedGrant("app_user", ("SELECT",), None),))
            if item.identity == object_identity(catalog.objects[1])
            else item
            for item in policy.objects
        ),
    )
    assert compile_plan(catalog, modified).plan_sha256 != first.plan_sha256
    assert (
        compile_plan(replace(catalog, database="other"), policy).plan_sha256
        != first.plan_sha256
    )


def test_every_app_object_needs_unique_explicit_classification() -> None:
    catalog, policy = _reviewed_inputs()
    extra = _object("relation", "unreviewed", "v", "legacy_writer")
    _blocked(
        replace(catalog, objects=(*catalog.objects, extra)),
        policy,
        "unclassified app object",
    )
    _blocked(
        replace(catalog, objects=(*catalog.objects, catalog.objects[1])),
        policy,
        "duplicate catalog object",
    )
    _blocked(
        catalog,
        replace(policy, objects=(*policy.objects, policy.objects[0])),
        "duplicate policy object",
    )
    _blocked(
        catalog, replace(policy, objects=policy.objects[1:]), "unclassified app object"
    )


def test_system_and_extension_exclusion_and_routine_quoting() -> None:
    catalog, policy = _reviewed_inputs()
    extension = replace(
        _object("relation", "extension_table", "r", "legacy_writer"),
        extension_owned=True,
    )
    system = replace(
        _object("relation", "pg_class", "r", "postgres"), schema="pg_catalog"
    )
    excluded = compile_plan(
        replace(catalog, objects=(*catalog.objects, extension, system)), policy
    )
    assert excluded.status == "ready_for_review"
    assert all(
        "extension_table" not in sql and "pg_class" not in sql
        for sql in excluded.statements
    )

    routine = replace(
        catalog.objects[4],
        name='quoted"name',
        argument_types=(TypeName("pg_catalog", "int4"),),
    )
    reviewed_routine = replace(policy.objects[4], identity=object_identity(routine))
    changed = compile_plan(
        replace(catalog, objects=(*catalog.objects[:4], routine)),
        replace(policy, objects=(*policy.objects[:4], reviewed_routine)),
    )
    assert changed.status == "ready_for_review"
    assert (
        'ALTER FUNCTION "public"."quoted""name"("pg_catalog"."int4") OWNER TO "app_admin";'
        in changed.statements
    )


def test_unexpected_owner_or_role_and_database_create_are_refused() -> None:
    catalog, policy = _reviewed_inputs()
    rogue = replace(catalog.objects[1], owner="rogue")
    with_rogue = replace(
        catalog,
        roles=(
            *catalog.roles,
            RolePosture("rogue", True, True, False, False, False, False, False),
        ),
        objects=(catalog.objects[0], rogue, *catalog.objects[2:]),
    )
    _blocked(with_rogue, policy, "unexpected source owner")
    _blocked(
        replace(catalog, database_owner="app_admin"), policy, "implicitly holds CREATE"
    )
    bad_role = tuple(
        replace(role, bypass_rls=True) if role.name == "app_user" else role
        for role in catalog.roles
    )
    _blocked(replace(catalog, roles=bad_role), policy, "app_user role posture")
    create_role = tuple(
        replace(role, database_create=True) if role.name == "app_admin" else role
        for role in catalog.roles
    )
    _blocked(
        replace(catalog, roles=create_role), policy, "effectively holds database CREATE"
    )
    for cluster_flag in ("can_create_database", "can_create_role"):
        cluster_role = tuple(
            replace(role, **{cluster_flag: True}) if role.name == "app_admin" else role
            for role in catalog.roles
        )
        _blocked(
            replace(catalog, roles=cluster_role),
            policy,
            "cluster role or database creation authority",
        )


def test_current_owner_acl_is_transferred_but_other_direct_grants_are_refused() -> None:
    catalog, policy = _reviewed_inputs()
    current_owner_acl = replace(
        catalog.objects[1],
        acl_grants=(Grant("legacy_writer", ("SELECT", "UPDATE"), ()),),
    )
    observed = replace(
        catalog,
        objects=(catalog.objects[0], current_owner_acl, *catalog.objects[2:]),
    )
    assert compile_plan(observed, policy).status == "ready_for_review"
    postgres_owner_acl = replace(
        current_owner_acl,
        owner="postgres",
        acl_grants=(Grant("postgres", ("SELECT",), ()),),
    )
    postgres_policy = replace(
        policy, approved_source_owners=(*policy.approved_source_owners, "postgres")
    )
    assert (
        compile_plan(
            replace(
                catalog,
                objects=(catalog.objects[0], postgres_owner_acl, *catalog.objects[2:]),
            ),
            postgres_policy,
        ).status
        == "ready_for_review"
    )
    unrelated_acl = replace(
        current_owner_acl,
        acl_grants=(Grant("postgres", ("SELECT",), ()),),
    )
    _blocked(
        replace(
            catalog,
            objects=(catalog.objects[0], unrelated_acl, *catalog.objects[2:]),
        ),
        policy,
        "excess direct ACL",
    )


def test_materialized_view_has_explicit_select_only_privilege_contract() -> None:
    catalog, policy = _reviewed_inputs()
    view = _object("relation", "invoice_snapshot", "m", "legacy_writer")
    reviewed = ObjectPolicy(
        object_identity(view), "named", (NamedGrant("app_user", ("SELECT",), None),), ()
    )
    catalog = replace(catalog, objects=(*catalog.objects, view))
    policy = replace(policy, objects=(*policy.objects, reviewed))
    plan = compile_plan(catalog, policy)
    assert plan.status == "ready_for_review"
    assert (
        'ALTER MATERIALIZED VIEW "public"."invoice_snapshot" OWNER TO "app_admin";'
        in plan.statements
    )
    assert (
        'GRANT SELECT ON TABLE "public"."invoice_snapshot" TO "app_user";'
        in plan.statements
    )
    invalid = replace(reviewed, grants=(NamedGrant("app_user", ("UPDATE",), None),))
    _blocked(
        catalog,
        replace(policy, objects=(*policy.objects[:-1], invalid)),
        "policy has unknown privilege",
    )


def test_incomplete_or_excess_acl_membership_and_defaults_block() -> None:
    catalog, policy = _reviewed_inputs()
    _blocked(
        replace(catalog, evidence=replace(catalog.evidence, acl_complete=False)),
        policy,
        "acl_complete evidence",
    )
    _blocked(
        replace(catalog, evidence=replace(catalog.evidence, column_acl_complete=False)),
        policy,
        "column_acl_complete evidence",
    )
    public = replace(
        catalog.objects[1],
        effective_privileges=(
            Grant("PUBLIC", ("SELECT",), ()),
            Grant("app_user", (), ()),
            Grant("platform_api", (), ()),
        ),
    )
    _blocked(
        replace(catalog, objects=(catalog.objects[0], public, *catalog.objects[2:])),
        policy,
        "excess effective ACL",
    )
    excess = replace(catalog.objects[1], acl_grants=(Grant("PUBLIC", ("SELECT",), ()),))
    _blocked(
        replace(catalog, objects=(catalog.objects[0], excess, *catalog.objects[2:])),
        policy,
        "excess direct ACL",
    )
    column = replace(
        catalog.objects[1],
        column_grants=(ColumnGrant("total", "app_user", ("UPDATE",), ()),),
    )
    _blocked(
        replace(catalog, objects=(catalog.objects[0], column, *catalog.objects[2:])),
        policy,
        "column ACL",
    )
    _blocked(
        replace(
            catalog,
            memberships=(
                *catalog.memberships,
                Membership("app_user", "legacy_writer", True, True, False),
            ),
        ),
        policy,
        "membership",
    )
    _blocked(
        replace(
            catalog,
            memberships=(
                *catalog.memberships,
                Membership("legacy_writer", "app_admin", False, True, False),
            ),
        ),
        policy,
        "membership",
    )
    default = DefaultAcl("legacy_writer", "public", "table", "PUBLIC", ("SELECT",))
    _blocked(replace(catalog, default_acls=(default,)), policy, "default ACLs")


def test_platform_api_membership_blocks_even_without_desired_platform_grants() -> None:
    catalog, policy = _reviewed_inputs()
    assert all(
        grant.role != "platform_api"
        for item in policy.objects
        for grant in (*item.grants, *item.column_grants)
    )
    _blocked(
        replace(
            catalog,
            memberships=(
                *catalog.memberships,
                Membership("platform_api", "legacy_writer", False, True, False),
            ),
        ),
        policy,
        "protected role membership",
    )


def test_declared_dba_effective_rights_on_other_owners_are_effective_only() -> None:
    catalog, policy = _reviewed_inputs()
    schema = replace(
        catalog.objects[0],
        effective_privileges=(
            *catalog.objects[0].effective_privileges,
            Grant("postgres", ("CREATE", "USAGE"), ()),
        ),
    )
    admin_owned = replace(
        catalog.objects[1],
        owner="app_admin",
        effective_privileges=(
            *catalog.objects[1].effective_privileges,
            Grant("postgres", ("SELECT", "UPDATE"), ()),
        ),
    )
    catalog = replace(catalog, objects=(schema, admin_owned, *catalog.objects[2:]))
    plan = compile_plan(catalog, policy)
    assert plan.status == "ready_for_review"
    assert not any('ALTER TABLE "public"."invoice" OWNER' in s for s in plan.statements)
    _blocked(
        catalog,
        replace(policy, source_admin_principals=()),
        "excess effective ACL for postgres",
    )
    assert (
        plan.policy_sha256
        != compile_plan(
            catalog, replace(policy, source_admin_principals=())
        ).policy_sha256
    )

    direct_extra = replace(
        schema,
        acl_grants=(Grant("postgres", ("CREATE",), ()),),
    )
    _blocked(
        replace(catalog, objects=(direct_extra, *catalog.objects[1:])),
        policy,
        "excess direct ACL for postgres",
    )
    column_extra = replace(
        admin_owned,
        column_grants=(ColumnGrant("id", "postgres", ("UPDATE",), ()),),
    )
    _blocked(
        replace(catalog, objects=(schema, column_extra, *catalog.objects[2:])),
        policy,
        "excess or unexplained column ACL",
    )
    delegated = replace(
        admin_owned,
        acl_grants=(Grant("postgres", ("SELECT",), ("SELECT",)),),
    )
    _blocked(
        replace(catalog, objects=(schema, delegated, *catalog.objects[2:])),
        policy,
        "non-owner direct grant options",
    )
    effective_option = replace(
        schema,
        effective_privileges=(
            *schema.effective_privileges[:-1],
            Grant("postgres", ("CREATE", "USAGE"), ("CREATE",)),
        ),
    )
    _blocked(
        replace(catalog, objects=(effective_option, *catalog.objects[1:])),
        policy,
        "effective ACL grant options are not direct evidence",
    )
    desired = replace(
        policy,
        objects=tuple(
            replace(
                item, grants=(*item.grants, NamedGrant("postgres", ("SELECT",), None))
            )
            if item.identity == object_identity(admin_owned)
            else item
            for item in policy.objects
        ),
    )
    _blocked(catalog, desired, "grant role has unsafe posture")


def test_explicit_public_type_and_routine_grants_cover_effective_roles_only() -> None:
    catalog, policy = _reviewed_inputs()
    legacy_admin = RolePosture(
        "dotmac_app", True, True, True, False, False, False, False
    )
    type_object = replace(
        catalog.objects[3],
        effective_privileges=(
            Grant("PUBLIC", ("USAGE",), ()),
            Grant("app_user", ("USAGE",), ()),
            Grant("platform_api", ("USAGE",), ()),
            Grant("dotmac_app", ("USAGE",), ()),
        ),
    )
    routine = replace(
        catalog.objects[4],
        effective_privileges=(
            Grant("PUBLIC", ("EXECUTE",), ()),
            Grant("app_user", ("EXECUTE",), ()),
            Grant("platform_api", ("EXECUTE",), ()),
            Grant("dotmac_app", ("EXECUTE",), ()),
        ),
    )
    catalog = replace(
        catalog,
        roles=(*catalog.roles, legacy_admin),
        objects=(*catalog.objects[:3], type_object, routine),
    )
    type_policy = replace(
        policy.objects[3],
        classification="named",
        grants=(NamedGrant("PUBLIC", ("USAGE",), "reviewed type compatibility"),),
    )
    policy = replace(
        policy, objects=(*policy.objects[:3], type_policy, policy.objects[4])
    )
    plan = compile_plan(catalog, policy)
    assert plan.status == "ready_for_review", plan.blocked_reasons
    assert 'GRANT USAGE ON TYPE "public"."invoice_state" TO PUBLIC;' in plan.statements
    assert not any('TO "dotmac_app"' in statement for statement in plan.statements)

    extra_effective = replace(
        catalog.objects[1],
        effective_privileges=(
            Grant("PUBLIC", ("SELECT",), ()),
            Grant("app_user", ("SELECT", "UPDATE"), ()),
            Grant("platform_api", ("SELECT",), ()),
        ),
    )
    extra_catalog = replace(
        catalog, objects=(catalog.objects[0], extra_effective, *catalog.objects[2:])
    )
    public_only = replace(
        policy.objects[1],
        grants=(NamedGrant("PUBLIC", ("SELECT",), "reviewed public table read"),),
    )
    public_policy = replace(
        policy, objects=(policy.objects[0], public_only, *policy.objects[2:])
    )
    _blocked(extra_catalog, public_policy, "excess effective ACL for app_user")
    direct_extra = replace(
        extra_effective, acl_grants=(Grant("app_user", ("SELECT",), ()),)
    )
    _blocked(
        replace(
            extra_catalog,
            objects=(catalog.objects[0], direct_extra, *catalog.objects[2:]),
        ),
        public_policy,
        "excess direct ACL for app_user",
    )


@pytest.mark.parametrize(
    "name", ["legacy_writer", "app_user", "platform_api", "PUBLIC", "dotmac_app"]
)
def test_source_admin_declaration_rejects_ordinary_and_runtime_roles(name: str) -> None:
    catalog, policy = _reviewed_inputs()
    changed = replace(
        policy,
        source_admin_principals=(SourceAdminPrincipal(name, "synthetic review"),),
    )
    _blocked(
        catalog,
        changed,
        "forbidden principal"
        if name != "legacy_writer"
        else "lacks migration or DBA posture",
    )


def test_exact_operational_bootstrap_membership_and_database_acl_are_required() -> None:
    catalog, policy = _reviewed_inputs()
    assert compile_plan(catalog, policy).status == "ready_for_review"
    _blocked(
        catalog,
        replace(policy, operational_bootstrap_memberships=()),
        "exact operational bootstrap membership declaration",
    )
    _blocked(
        catalog,
        replace(
            policy,
            operational_bootstrap_memberships=(
                OperationalBootstrapMembership(
                    "dotmac_schema_bootstrap", "legacy_writer", "wrong role"
                ),
            ),
        ),
        "exact operational bootstrap membership declaration",
    )
    _blocked(
        replace(catalog, memberships=()),
        policy,
        "operational bootstrap membership is absent",
    )
    wrong_role = Membership(
        "dotmac_schema_bootstrap", "legacy_writer", False, True, False
    )
    _blocked(
        replace(catalog, memberships=(wrong_role,)),
        policy,
        "unexpected operational bootstrap membership",
    )
    for field, value in (
        ("inherits", True),
        ("set_option", False),
        ("admin_option", True),
    ):
        changed = replace(catalog.memberships[0], **{field: value})
        _blocked(
            replace(catalog, memberships=(changed,)),
            policy,
            "operational bootstrap membership options",
        )
    for field, value in (
        ("can_login", False),
        ("inherits", True),
        ("bypass_rls", True),
        ("superuser", True),
        ("can_create_database", True),
        ("can_create_role", True),
        ("database_create", False),
    ):
        bootstrap_roles = tuple(
            replace(role, **{field: value})
            if role.name == "dotmac_schema_bootstrap"
            else role
            for role in catalog.roles
        )
        _blocked(
            replace(catalog, roles=bootstrap_roles),
            policy,
            "operational bootstrap role posture",
        )
    _blocked(
        replace(catalog, database_acl_grants=catalog.database_acl_grants[:2]),
        policy,
        "bootstrap needs direct database CREATE",
    )
    delegated = replace(catalog.database_acl_grants[2], grant_options=("CREATE",))
    _blocked(
        replace(
            catalog, database_acl_grants=(*catalog.database_acl_grants[:2], delegated)
        ),
        policy,
        "bootstrap needs direct database CREATE without grant option",
    )
    public_create = replace(
        catalog.database_acl_grants[1], privileges=("CONNECT", "CREATE", "TEMPORARY")
    )
    _blocked(
        replace(
            catalog,
            database_acl_grants=(
                catalog.database_acl_grants[0],
                public_create,
                catalog.database_acl_grants[2],
            ),
        ),
        policy,
        "unreviewed direct database CREATE grant",
    )
    _blocked(
        replace(
            catalog, evidence=replace(catalog.evidence, database_acl_complete=False)
        ),
        policy,
        "database_acl_complete evidence",
    )
    historical = replace(
        catalog,
        roles=(
            *catalog.roles,
            RolePosture("dotmac_app", True, True, True, False, False, False, False),
        ),
        memberships=(
            *catalog.memberships,
            Membership("dotmac_app", "app_admin", False, True, False),
        ),
    )
    _blocked(historical, policy, "protected role membership")


def test_unknown_privilege_missing_effective_role_and_unjustified_public_block() -> (
    None
):
    catalog, policy = _reviewed_inputs()
    unknown = replace(
        catalog.objects[1], acl_grants=(Grant("app_user", ("MAINTAIN",), ()),)
    )
    _blocked(
        replace(catalog, objects=(catalog.objects[0], unknown, *catalog.objects[2:])),
        policy,
        "unknown privilege",
    )
    missing = replace(
        catalog.objects[1], effective_privileges=(Grant("PUBLIC", (), ()),)
    )
    _blocked(
        replace(catalog, objects=(catalog.objects[0], missing, *catalog.objects[2:])),
        policy,
        "effective ACL evidence",
    )
    unjustified = tuple(
        replace(item, grants=(NamedGrant("PUBLIC", ("EXECUTE",), None),))
        if item.identity == object_identity(catalog.objects[4])
        else item
        for item in policy.objects
    )
    _blocked(
        catalog,
        replace(policy, objects=unjustified),
        "PUBLIC grant lacks justification",
    )


def test_json_decoder_refuses_missing_unknown_and_oversized_evidence() -> None:
    catalog, policy = _reviewed_inputs()
    source = json.loads(json.dumps(asdict(catalog)))
    source.pop("evidence")
    with pytest.raises(PlanInputError, match="missing or unknown fields"):
        decode_catalog(source)
    source = json.loads(json.dumps(asdict(catalog)))
    source["evidence"].pop("grant_options_complete")
    with pytest.raises(PlanInputError, match="missing or unknown fields"):
        decode_catalog(source)
    for section, field in (
        ("evidence", "database_acl_complete"),
        (None, "database_acl_grants"),
    ):
        source = json.loads(json.dumps(asdict(catalog)))
        (source if section is None else source[section]).pop(field)
        with pytest.raises(PlanInputError, match="missing or unknown fields"):
            decode_catalog(source)
    for section, field in (
        ("roles", "inherits"),
        ("memberships", "set_option"),
        ("memberships", "admin_option"),
    ):
        source = json.loads(json.dumps(asdict(catalog)))
        source[section][0].pop(field)
        with pytest.raises(PlanInputError, match="missing or unknown fields"):
            decode_catalog(source)
    source = json.loads(json.dumps(asdict(catalog)))
    source["objects"][0]["unmodeled"] = True
    with pytest.raises(PlanInputError, match="missing or unknown fields"):
        decode_catalog(source)
    source = json.loads(json.dumps(asdict(catalog)))
    source["objects"][1]["name"] = "x" * 64
    with pytest.raises(PlanInputError, match="identifier"):
        decode_catalog(source)
    source = json.loads(json.dumps(asdict(catalog)))
    source["objects"][1]["acl_grants"] = [
        {"grantee": "app_user", "privileges": ["SELECT"], "grant_options": ["UPDATE"]}
    ]
    with pytest.raises(PlanInputError, match="grant options exceed privileges"):
        decode_catalog(source)
    bad_policy = json.loads(json.dumps(asdict(policy)))
    bad_policy["objects"][0]["classification"] = "grant_everything"
    with pytest.raises(PlanInputError, match="unknown object policy"):
        decode_policy(bad_policy)
    for field in ("source_admin_principals", "operational_bootstrap_memberships"):
        bad_policy = json.loads(json.dumps(asdict(policy)))
        bad_policy.pop(field)
        with pytest.raises(PlanInputError, match="missing or unknown fields"):
            decode_policy(bad_policy)
    bad_policy = json.loads(json.dumps(asdict(policy)))
    bad_policy["source_admin_principals"][0]["justification"] = " "
    with pytest.raises(PlanInputError, match="bounded nonempty text"):
        decode_policy(bad_policy)


def test_json_cli_is_read_only_and_reports_blocked_without_statements(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    catalog, policy = _reviewed_inputs()
    catalog_file = tmp_path / "catalog.json"
    policy_file = tmp_path / "policy.json"
    catalog_file.write_text(json.dumps(asdict(catalog)), encoding="utf-8")
    policy_file.write_text(json.dumps(asdict(policy)), encoding="utf-8")
    assert main(["--catalog", str(catalog_file), "--policy", str(policy_file)]) == 0
    ready = json.loads(capsys.readouterr().out)
    assert ready["status"] == "ready_for_review"
    assert ready["statements"]
    broken = asdict(catalog)
    broken["evidence"]["objects_complete"] = False
    catalog_file.write_text(json.dumps(broken), encoding="utf-8")
    assert main(["--catalog", str(catalog_file), "--policy", str(policy_file)]) == 2
    blocked = json.loads(capsys.readouterr().out)
    assert blocked["status"] == "blocked"
    assert blocked["statements"] == []
