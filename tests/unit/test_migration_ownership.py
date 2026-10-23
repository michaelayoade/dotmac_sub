"""Migration ownership hand-back, planned without a database.

The migrator (app_admin, BYPASSRLS) creates what a migration creates; the
runtime owner (dotmac_app) must own it before the transaction commits.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.migration_ownership import (
    MigrationOwnershipError,
    MigratorPosture,
    OwnedObject,
    hand_back_new_objects,
    leftovers,
    load_definer_allowlist,
    migrator_violations,
    plan_hand_back,
    unowned_bypass_violation,
)

ROOT = Path(__file__).resolve().parents[2]
ALLOWLIST = ROOT / "alembic" / "definer_owner_allowlist.txt"
RELAY_CLAIM = "public.claim_outbox_batch(text, integer, integer)"
ALLOWED = frozenset({RELAY_CLAIM})


def table(name: str, oid: int, kind: str = "r") -> OwnedObject:
    return OwnedObject("pg_class", oid, kind, f"public.{name}", f"public.{name}")


def index(name: str, oid: int) -> OwnedObject:
    return OwnedObject(
        "pg_class", oid, "i", f"public.{name}", f"public.{name}", follows_parent=True
    )


def func(
    name: str, oid: int, *, secdef: bool = False, signature: str | None = None
) -> OwnedObject:
    return OwnedObject(
        "pg_proc",
        oid,
        "f",
        f"public.{name}()",
        signature or f"public.{name}()",
        secdef=secdef,
    )


def plan(*objects: OwnedObject, allowlist: frozenset[str] = frozenset()):
    return plan_hand_back(objects, owner="dotmac_app", definer_allowlist=allowlist)


def test_new_table_is_handed_back_and_its_index_follows() -> None:
    result = plan(table("invoices", 1), index("ix_invoices", 2))
    assert result.refusals == ()
    assert result.statements == ('ALTER TABLE public.invoices OWNER TO "dotmac_app"',)


def test_view_matview_foreign_table_and_partitioned_table_use_their_own_kind() -> None:
    result = plan(
        table("v_open", 1, "v"),
        table("mv_totals", 2, "m"),
        table("ft_ext", 3, "f"),
        table("events", 4, "p"),
    )
    assert result.statements == (
        'ALTER TABLE public.events OWNER TO "dotmac_app"',
        'ALTER FOREIGN TABLE public.ft_ext OWNER TO "dotmac_app"',
        'ALTER MATERIALIZED VIEW public.mv_totals OWNER TO "dotmac_app"',
        'ALTER VIEW public.v_open OWNER TO "dotmac_app"',
    )


def test_view_over_force_rls_tables_must_not_stay_with_the_bypass_role() -> None:
    """A view runs with its owner's privileges and RLS posture."""
    result = plan(table("v_tenant_totals", 7, "v"))
    assert result.statements == (
        'ALTER VIEW public.v_tenant_totals OWNER TO "dotmac_app"',
    )


def test_owned_and_identity_sequences_are_skipped_standalone_is_handed_back() -> None:
    owned_by_column = OwnedObject(
        "pg_class",
        10,
        "S",
        "public.t_id_seq",
        "public.t_id_seq",
        follows_parent=True,
    )
    standalone = table("invoice_number_seq", 11, "S")
    result = plan(owned_by_column, standalone)
    assert result.statements == (
        'ALTER SEQUENCE public.invoice_number_seq OWNER TO "dotmac_app"',
    )


def test_enum_domain_range_types_are_handed_back_but_array_and_row_types_follow() -> (
    None
):
    kinds = {"e": "TYPE", "r": "TYPE", "d": "DOMAIN"}
    objs = [
        OwnedObject("pg_type", i, kind, f"public.t_{kind}", f"public.t_{kind}")
        for i, kind in enumerate(kinds, 1)
    ]
    objs.append(
        OwnedObject(
            "pg_type", 99, "b", "public._t_e", "public._t_e", follows_parent=True
        )
    )
    result = plan(*objs)
    assert result.refusals == ()
    assert set(result.statements) == {
        'ALTER DOMAIN public.t_d OWNER TO "dotmac_app"',
        'ALTER TYPE public.t_e OWNER TO "dotmac_app"',
        'ALTER TYPE public.t_r OWNER TO "dotmac_app"',
    }


def test_plain_function_is_handed_back() -> None:
    assert plan(func("helper", 5)).statements == (
        'ALTER ROUTINE public.helper() OWNER TO "dotmac_app"',
    )


def test_unlisted_security_definer_is_refused() -> None:
    result = plan(func("sneaky", 6, secdef=True))
    assert result.statements == ()
    assert result.refusals == (
        "SECURITY DEFINER function public.sneaky() is not in the definer allowlist",
    )


def test_listed_security_definer_stays_with_the_migrator() -> None:
    claim = func("claim_outbox_batch", 8, secdef=True, signature=RELAY_CLAIM)
    result = plan(claim, allowlist=ALLOWED)
    assert result.statements == () and result.refusals == ()


def test_aggregates_and_unknown_catalogs_are_refused_not_skipped() -> None:
    aggregate = OwnedObject("pg_proc", 3, "a", "public.agg(int)", "public.agg(int)")
    schema = OwnedObject("pg_namespace", 4, "", "pg_namespace:4", "pg_namespace:4")
    composite = OwnedObject("pg_class", 5, "c", "public.rowtype", "public.rowtype")
    result = plan(aggregate, schema, composite)
    assert result.statements == ()
    assert len(result.refusals) == 3


def test_extension_members_and_temp_objects_are_ignored() -> None:
    ext = OwnedObject(
        "pg_class", 1, "r", "public.spatial_ref_sys", "x", extension_member=True
    )
    temp = OwnedObject("pg_class", 2, "r", "pg_temp_3.scratch", "x", temporary=True)
    assert plan(ext, temp) == plan()


def test_identifiers_are_quoted_in_the_owner_name() -> None:
    result = plan_hand_back(
        [table("a", 1)], owner='odd"owner', definer_allowlist=frozenset()
    )
    assert result.statements == ('ALTER TABLE public.a OWNER TO "odd""owner"',)


def test_leftovers_name_only_new_non_allowlisted_objects() -> None:
    before = [func("claim_outbox_batch", 8, secdef=True, signature=RELAY_CLAIM)]
    after = [
        *before,
        table("late", 20),
        func("claim_outbox_batch2", 21, secdef=True, signature="public.x()"),
        func("claim_outbox_batch", 22, secdef=True, signature=RELAY_CLAIM),
    ]
    assert leftovers(before, after, definer_allowlist=ALLOWED) == (
        "pg_class public.late",
        "pg_proc public.claim_outbox_batch2()",
    )


def test_shipped_allowlist_is_exactly_the_four_relay_definers() -> None:
    assert load_definer_allowlist(ALLOWLIST) == frozenset(
        {
            "public.claim_outbox_batch(text, integer, integer)",
            "public.settle_outbox_event(uuid, text, text, timestamp with time zone, "
            "integer, text)",
            "public.claim_platform_outbox_batch(text, integer, integer)",
            "public.settle_platform_outbox_event(uuid, text, text, "
            "timestamp with time zone, integer, text)",
        }
    )


class _Rows:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def __iter__(self):
        return iter(self._rows)


class FakeConnection:
    """Replays two snapshots and records the ALTERs that run in between."""

    def __init__(
        self, snapshots: list[list[OwnedObject]], *, remaining: list[OwnedObject] | None
    ) -> None:
        self.snapshots = snapshots
        self.executed: list[str] = []

    def execute(self, _statement: object, _params: object = None) -> _Rows:
        snap = self.snapshots.pop(0)
        return _Rows(
            [
                (
                    o.catalog,
                    o.oid,
                    o.kind,
                    o.ident,
                    o.signature,
                    o.secdef,
                    o.follows_parent,
                    o.extension_member,
                    o.temporary,
                )
                for o in snap
            ]
        )

    def exec_driver_sql(self, statement: str) -> None:
        self.executed.append(statement)


def test_hand_back_issues_the_plan_and_proves_nothing_is_left() -> None:
    before = [table("old", 1)]
    created = [table("new", 2), index("ix_new", 3)]
    conn = FakeConnection([[*before, *created], before], remaining=None)
    count = hand_back_new_objects(
        conn, owner="dotmac_app", before=before, definer_allowlist=frozenset()
    )
    assert count == 1
    assert conn.executed == ['ALTER TABLE public.new OWNER TO "dotmac_app"']


def test_hand_back_raises_on_unlisted_definer_before_any_alter() -> None:
    conn = FakeConnection([[func("evil", 4, secdef=True)]], remaining=None)
    with pytest.raises(MigrationOwnershipError, match="cannot be handed back"):
        hand_back_new_objects(
            conn, owner="dotmac_app", before=[], definer_allowlist=frozenset()
        )
    assert conn.executed == []


def test_hand_back_raises_when_something_is_still_migrator_owned() -> None:
    new = table("stuck", 2)
    conn = FakeConnection([[new], [new]], remaining=None)
    with pytest.raises(MigrationOwnershipError, match="still owned by the migrator"):
        hand_back_new_objects(
            conn, owner="dotmac_app", before=[], definer_allowlist=frozenset()
        )


def good_posture(**overrides: Any) -> MigratorPosture:
    base: dict[str, Any] = {
        "current_user": "app_admin",
        "owner_exists": True,
        "bypassrls": True,
        "superuser": False,
        "inherits_owner": True,
        "can_set_owner": True,
        "owner_reaches_migrator": False,
    }
    base.update(overrides)
    return MigratorPosture(**base)


def test_the_contract_migrator_has_no_violations() -> None:
    assert migrator_violations(good_posture(), owner="dotmac_app") == ()


@pytest.mark.parametrize(
    ("override", "fragment"),
    [
        ({"current_user": "dotmac_app"}, "connected as the object owner"),
        ({"superuser": True}, "is a superuser"),
        ({"bypassrls": False}, "lacks BYPASSRLS"),
        ({"inherits_owner": False}, "with INHERIT"),
        ({"can_set_owner": False}, "cannot SET ROLE dotmac_app"),
        ({"owner_reaches_migrator": True}, "must not reach the migrator"),
        ({"owner_exists": False}, "does not exist"),
    ],
)
def test_each_identity_violation_is_named(
    override: dict[str, Any], fragment: str
) -> None:
    problems = migrator_violations(good_posture(**override), owner="dotmac_app")
    assert any(fragment in problem for problem in problems), problems


ENV = (ROOT / "alembic" / "env.py").read_text(encoding="utf-8")


def test_env_sets_lock_timeout_before_the_alembic_version_alter() -> None:
    body = ENV[ENV.index("def run_migrations_online") :]
    assert body.index("_set_migration_lock_timeout(connection)") < body.index(
        "ensure_alembic_version_table(connection)"
    )


def test_env_alters_alembic_version_only_when_the_width_differs() -> None:
    ensure = ENV[
        ENV.index("def ensure_alembic_version_table") : ENV.index("def include_object")
    ]
    assert "atttypmod" in ensure
    assert "if typmod != _VERSION_NUM_TYPMOD" in ensure
    assert ensure.index("atttypmod") < ensure.index("ALTER TABLE alembic_version")


def test_env_hands_back_inside_the_migration_transaction_and_before_commit() -> None:
    body = ENV[ENV.index("def run_migrations_online") :]
    # Bootstrap table: handed back before the intermediate commit.
    assert body.index("hand_back_new_objects(") < body.index("connection.commit()")
    # Migrations: snapshot, run, hand back, all inside begin_transaction.
    inside = body[body.index("with context.begin_transaction():") :]
    assert (
        inside.index("snapshot_owned_objects(connection)")
        < inside.index("context.run_migrations()")
        < inside.index("hand_back_new_objects(")
    )


def test_env_hand_back_is_opt_in_through_the_owner_variable() -> None:
    assert "OWNER_ENV_VAR" in ENV
    assert "if owner is not None" in ENV


def test_bypassrls_non_superuser_without_owner_variable_is_refused() -> None:
    message = unowned_bypass_violation(
        current_user="app_admin", bypassrls=True, superuser=False
    )
    assert message is not None
    assert "MIGRATION_OBJECT_OWNER" in message and "module_database_roles.v1" in message


def test_superuser_and_plain_roles_may_migrate_without_the_variable() -> None:
    for bypass, superuser in ((True, True), (False, False), (False, True)):
        assert (
            unowned_bypass_violation(
                current_user="x", bypassrls=bypass, superuser=superuser
            )
            is None
        )


def test_env_checks_unowned_bypass_before_any_ddl() -> None:
    body = ENV[ENV.index("def run_migrations_online") :]
    assert body.index("assert_owner_variable_set_for_bypass(connection)") < body.index(
        "ensure_alembic_version_table(connection)"
    )
