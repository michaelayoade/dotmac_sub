"""``--verify-only`` and ``--repair`` against the migrator/owner role graph.

A scripted connection stands in for PostgreSQL so the direction rules are
checked without a database: the NEW edge is app_admin -> member of dotmac_app
(INHERIT, SET, no ADMIN); the old edge (dotmac_app member of app_admin) is drift
that repair removes and never creates.
"""

from __future__ import annotations

from typing import Any

import pytest
from psycopg import sql

from scripts import bootstrap_outbox_dispatcher_roles as bootstrap


def _render(composable: sql.Composable) -> str:
    """Render without a connection (libpq quoting needs one): identifiers are
    double-quoted by hand, which is enough for the plain role names used here."""
    if isinstance(composable, sql.Composed):
        return "".join(_render(part) for part in composable)
    if isinstance(composable, sql.Identifier):
        return "".join(f'"{part}"' for part in composable._obj)
    return composable.as_string(None)


class _Result:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows

    def fetchone(self) -> tuple[Any, ...]:
        return self._rows[0]


class FakeConn:
    """Models only the catalog facts the contract reads."""

    def __init__(
        self,
        *,
        new_edge: bool,
        old_edge: bool,
        admin: bool = False,
        inherit: bool = True,
        set_option: bool = True,
        reachers: tuple[tuple[str, str], ...] = (),
        roles: tuple[str, ...] = (
            "app_admin",
            "dotmac_app",
            "outbox_dispatcher",
            "platform_outbox_dispatcher",
        ),
    ) -> None:
        self.new_edge = new_edge
        self.old_edge = old_edge
        self.admin = admin
        self.inherit = inherit
        self.set_option = set_option
        self.reachers = reachers
        self.roles = set(roles)
        self.statements: list[str] = []

    def execute(self, query: Any, params: Any = None) -> _Result:
        if isinstance(query, sql.Composable):
            text = _render(query)
            self.statements.append(text)
            self._apply(text)
            return _Result([])
        if "FROM pg_roles a" in query:
            return _Result(list(self.reachers))
        if "rolcanlogin" in query:
            return _Result(
                [
                    (name, True, False, False)
                    for name in sorted(self.roles & set(params[0]))
                    if name.endswith("dispatcher")
                ]
            )
        if "SELECT rolname FROM pg_roles" in query:
            return _Result([(name,) for name in sorted(self.roles & set(params[0]))])
        if "pg_has_role" in query and "'USAGE'" in query:
            return _Result([(self.new_edge and self.inherit,)])
        if "pg_has_role" in query and "'SET'" in query:
            return _Result([(self.new_edge and self.set_option,)])
        if "pg_has_role" in query and "'MEMBER'" in query:
            return _Result([(self.old_edge,)])
        if "admin_option" in query:
            return _Result([(self.new_edge and self.admin,)])
        if "has_schema_privilege" in query:
            return _Result([(True,)])
        raise AssertionError(f"unscripted query: {query}")

    def _apply(self, text: str) -> None:
        if text == 'REVOKE "app_admin" FROM "dotmac_app"':
            self.old_edge = False
        elif text.startswith('GRANT "dotmac_app" TO "app_admin"'):
            self.new_edge, self.inherit, self.set_option = True, True, True
        elif text.startswith('REVOKE ADMIN OPTION FOR "dotmac_app" FROM "app_admin"'):
            self.admin = False
        else:  # pragma: no cover - any other statement is a contract breach
            raise AssertionError(f"unexpected statement: {text}")


def test_verify_accepts_the_new_direction(capsys: pytest.CaptureFixture[str]) -> None:
    conn = FakeConn(new_edge=True, old_edge=False)
    assert bootstrap.verify(conn) == 0  # type: ignore[arg-type]
    assert capsys.readouterr().err == ""


def test_verify_reports_the_old_direction_as_drift(
    capsys: pytest.CaptureFixture[str],
) -> None:
    conn = FakeConn(new_edge=False, old_edge=True)
    assert bootstrap.verify(conn) == 1  # type: ignore[arg-type]
    err = capsys.readouterr().err
    assert "app_admin is not a member of dotmac_app with INHERIT" in err
    assert "dotmac_app is a member of app_admin" in err


def test_verify_reports_both_edges_present_and_admin_option(
    capsys: pytest.CaptureFixture[str],
) -> None:
    conn = FakeConn(new_edge=True, old_edge=True, admin=True)
    assert bootstrap.verify(conn) == 1  # type: ignore[arg-type]
    err = capsys.readouterr().err
    assert "holds ADMIN OPTION" in err
    assert "old direction" in err


def test_verify_reports_a_login_role_that_reaches_bypassrls(
    capsys: pytest.CaptureFixture[str],
) -> None:
    conn = FakeConn(
        new_edge=True,
        old_edge=False,
        reachers=(("dotmac_schema_bootstrap", "app_admin"),),
    )
    assert bootstrap.verify(conn) == 1  # type: ignore[arg-type]
    assert "dotmac_schema_bootstrap->app_admin" in capsys.readouterr().err


def test_repair_converges_old_direction_to_new_and_never_regrants_old() -> None:
    conn = FakeConn(new_edge=False, old_edge=True)
    assert bootstrap.bootstrap(conn, dry_run=False, repair=True) == 0  # type: ignore[arg-type]
    assert conn.statements == [
        'REVOKE "app_admin" FROM "dotmac_app"',
        'GRANT "dotmac_app" TO "app_admin" WITH INHERIT TRUE, SET TRUE',
    ]
    assert not conn.old_edge and conn.new_edge
    assert bootstrap.verify(conn) == 0  # type: ignore[arg-type]


def test_repair_is_a_no_op_when_the_new_direction_holds() -> None:
    conn = FakeConn(new_edge=True, old_edge=False)
    assert bootstrap.bootstrap(conn, dry_run=False, repair=True) == 0  # type: ignore[arg-type]
    assert conn.statements == []


def test_repair_drops_admin_option_without_touching_direction() -> None:
    conn = FakeConn(new_edge=True, old_edge=False, admin=True)
    assert bootstrap.bootstrap(conn, dry_run=False, repair=True) == 0  # type: ignore[arg-type]
    assert conn.statements == ['REVOKE ADMIN OPTION FOR "dotmac_app" FROM "app_admin"']


def test_repair_restores_a_missing_inherit_with_the_new_direction_only() -> None:
    conn = FakeConn(new_edge=True, old_edge=False, inherit=False)
    assert bootstrap.bootstrap(conn, dry_run=False, repair=True) == 0  # type: ignore[arg-type]
    assert conn.statements == [
        'GRANT "dotmac_app" TO "app_admin" WITH INHERIT TRUE, SET TRUE'
    ]


@pytest.mark.parametrize("old_edge", [True, False])
def test_no_repair_path_ever_grants_app_admin_to_dotmac_app(old_edge: bool) -> None:
    for new_edge in (True, False):
        conn = FakeConn(new_edge=new_edge, old_edge=old_edge)
        bootstrap.bootstrap(conn, dry_run=False, repair=True)  # type: ignore[arg-type]
        assert 'GRANT "app_admin" TO "dotmac_app"' not in conn.statements


def test_dry_run_changes_nothing() -> None:
    conn = FakeConn(new_edge=False, old_edge=True)
    assert bootstrap.bootstrap(conn, dry_run=True, repair=True) == 0  # type: ignore[arg-type]
    assert conn.statements == []
    assert conn.old_edge and not conn.new_edge


def test_drift_without_repair_is_refused_and_writes_nothing() -> None:
    conn = FakeConn(new_edge=False, old_edge=True)
    assert bootstrap.bootstrap(conn, dry_run=False, repair=False) == 1  # type: ignore[arg-type]
    assert conn.statements == []


def test_repair_refuses_but_does_not_revoke_a_bypass_reacher() -> None:
    conn = FakeConn(
        new_edge=True,
        old_edge=False,
        reachers=(("dotmac_schema_bootstrap", "app_admin"),),
    )
    assert bootstrap.bootstrap(conn, dry_run=False, repair=True) == 1  # type: ignore[arg-type]
    assert conn.statements == []


def test_missing_owner_role_is_skipped_not_granted(
    capsys: pytest.CaptureFixture[str],
) -> None:
    conn = FakeConn(
        new_edge=False,
        old_edge=False,
        roles=("app_admin", "outbox_dispatcher", "platform_outbox_dispatcher"),
    )
    assert bootstrap.bootstrap(conn, dry_run=False, repair=True) == 0  # type: ignore[arg-type]
    assert conn.statements == []
    assert "skipped role-graph repair" in capsys.readouterr().err
