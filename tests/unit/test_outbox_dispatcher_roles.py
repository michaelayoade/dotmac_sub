"""Pure role-contract tests for the explicit outbox bootstrap."""

from __future__ import annotations

from pathlib import Path

import pytest
from psycopg import sql
from sqlalchemy.engine import make_url

from app.commercial_module_prereqs import COMMERCIAL_BOOTSTRAP_ROLE_CONTRACT
from app.outbox_dispatcher_roles import (
    HISTORICAL_557_RELAY_OWNERSHIP_CONTRACT,
    OUTBOX_RELAY_OWNERSHIP_CONTRACT,
    RELAY_DISPATCHER_CONTRACT,
    RelayOwnershipObservation,
    relay_dispatcher_violations,
    relay_ownership_violations,
)
from scripts import bootstrap_outbox_dispatcher_roles as bootstrap_script
from scripts.ci import bootstrap_test_database_prereqs as ci_bootstrap


@pytest.mark.parametrize("posture_drift", [False, True])
def test_disposable_login_password_setup_requires_guard_and_exact_role_posture(
    posture_drift: bool,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    statements: list[sql.Composable] = []
    order: list[str] = []

    class _Connection:
        def __enter__(self) -> _Connection:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, statement: sql.Composable) -> None:
            statements.append(statement)

    monkeypatch.setattr(
        ci_bootstrap.psycopg, "connect", lambda *_a, **_k: _Connection()
    )
    monkeypatch.setattr(
        ci_bootstrap,
        "_require_disposable_cluster",
        lambda *_a, **_k: order.append("guard"),
    )
    monkeypatch.setattr(
        ci_bootstrap,
        "bootstrap_commercial_module_prereqs",
        lambda *_a, **_k: 0,
    )
    monkeypatch.setattr(ci_bootstrap, "_bootstrap_test_schema_login", lambda *_a: 0)
    monkeypatch.setattr(ci_bootstrap, "_bootstrap_outbox_url", lambda *_a, **_k: 0)
    observed = {
        role: COMMERCIAL_BOOTSTRAP_ROLE_CONTRACT[role].authority_posture
        for role in ("app_user", "app_admin")
    }
    if posture_drift:
        observed["app_user"] = (True, True, False, False, False)
    monkeypatch.setattr(ci_bootstrap, "observe_roles", lambda *_a: observed)
    password = "fixture apostrophe: O'Reilly"
    url = make_url("postgresql+psycopg://postgres@localhost/dotmac_sub_test").set(
        password=password
    )

    result = ci_bootstrap.bootstrap_disposable_database(url, label="test")

    assert order == ["guard"]
    role_statements = [
        statement for statement in statements if "ALTER ROLE" in repr(statement)
    ]
    if posture_drift:
        assert result == 2
        assert role_statements == []
    else:
        assert result == 0
        assert len(role_statements) == 2
        for statement, role in zip(role_statements, ("app_user", "app_admin")):
            parts = list(statement)
            assert [type(part) for part in parts] == [sql.SQL, sql.Literal]
            assert parts[0] == sql.SQL(f"ALTER ROLE {role} PASSWORD ")
            assert parts[1].as_string(None) == "'fixture apostrophe: O''Reilly'"
    output = capsys.readouterr()
    assert password not in output.out + output.err


class _MarkerResult:
    def __init__(self, row: tuple[str, str] | None) -> None:
        self.row = row

    def fetchone(self) -> tuple[str, str] | None:
        return self.row


class _MarkerOnlyConnection:
    def __init__(self, row: tuple[str, str] | None) -> None:
        self.row = row
        self.calls: list[str] = []

    def execute(self, statement: str) -> _MarkerResult:
        self.calls.append(statement)
        assert statement == (
            "SELECT setting, context FROM pg_settings WHERE name = 'cluster_name'"
        )
        return _MarkerResult(self.row)


@pytest.mark.parametrize(
    "marker",
    [None, ("", "postmaster"), ("dotmac-sub-disposable-tests", "user")],
)
def test_ci_historical_replay_refuses_unmarked_or_session_marker_before_grant(
    marker: tuple[str, str] | None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ci_bootstrap, "REPO_ROOT", tmp_path)
    conn = _MarkerOnlyConnection(marker)
    url = make_url("postgresql+psycopg://postgres@localhost:5432/dotmac_sub_test")
    environ = {
        "APP_ENV": "test",
        "TEST_DATABASE_URL": url.render_as_string(hide_password=False),
    }
    for key, value in environ.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(ci_bootstrap.DisposableClusterRefusal):
        ci_bootstrap._prepare_historical_557_replay(conn, url)  # type: ignore[arg-type]
    assert len(conn.calls) == 1


def test_ci_disposable_marker_requires_test_host_and_endpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ci_bootstrap, "REPO_ROOT", tmp_path)
    conn = _MarkerOnlyConnection(("dotmac-sub-disposable-tests", "postmaster"))
    url = make_url("postgresql+psycopg://postgres@localhost:5432/dotmac_sub_test")
    base = {"TEST_DATABASE_URL": url.render_as_string(hide_password=False)}
    for environ, candidate in (
        (base, url),
        ({**base, "APP_ENV": "production"}, url),
        ({**base, "APP_ENV": "test"}, url.set(host="elsewhere")),
    ):
        with pytest.raises(ci_bootstrap.DisposableClusterRefusal):
            ci_bootstrap._require_disposable_cluster(
                conn,
                candidate,
                environ=environ,  # type: ignore[arg-type]
            )
    assert conn.calls == []
    observed = ci_bootstrap._require_disposable_cluster(
        conn,
        url,
        environ={**base, "APP_ENV": "test"},  # type: ignore[arg-type]
    )
    assert observed == ci_bootstrap.DisposableClusterObservation(
        "dotmac-sub-disposable-tests", "postmaster"
    )


def test_ci_repeated_clone_bootstrap_uses_current_schema_owner_without_legacy_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class _Connection:
        def __enter__(self) -> _Connection:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(
        ci_bootstrap.psycopg, "connect", lambda *_args, **_kw: _Connection()
    )
    monkeypatch.setattr(
        ci_bootstrap,
        "_require_disposable_cluster",
        lambda *_args, **_kw: calls.append("guard"),
    )
    monkeypatch.setattr(
        ci_bootstrap,
        "observe_historical_membership",
        lambda _conn: True,
    )
    monkeypatch.setattr(
        ci_bootstrap,
        "observe_dispatchers",
        lambda _conn: RELAY_DISPATCHER_CONTRACT,
    )
    monkeypatch.setattr(
        ci_bootstrap,
        "ensure_current_schema_privileges",
        lambda *_args, **_kw: calls.append("schema"),
    )
    monkeypatch.setattr(
        ci_bootstrap,
        "bootstrap_outbox_dispatcher_roles",
        lambda *_args, **_kw: pytest.fail("operational bootstrap saw retired link"),
    )
    monkeypatch.setattr(
        ci_bootstrap,
        "_prepare_historical_557_replay",
        lambda *_args, **_kw: pytest.fail("repeat bootstrap regranted old membership"),
    )
    url = make_url("postgresql+psycopg://postgres@localhost:5432/dotmac_test_clone")
    assert ci_bootstrap._bootstrap_outbox_url(url, label="clone") == 0
    assert calls == ["guard", "schema"]


def test_ci_fresh_bootstrap_prepares_history_only_after_operational_roles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class _Connection:
        def __enter__(self) -> _Connection:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(
        ci_bootstrap.psycopg, "connect", lambda *_args, **_kw: _Connection()
    )
    monkeypatch.setattr(
        ci_bootstrap,
        "_require_disposable_cluster",
        lambda *_args, **_kw: calls.append("guard"),
    )
    monkeypatch.setattr(
        ci_bootstrap,
        "observe_historical_membership",
        lambda _conn: False,
    )

    def operational(*_args: object, **_kwargs: object) -> int:
        calls.append("operational")
        return 0

    monkeypatch.setattr(ci_bootstrap, "bootstrap_outbox_dispatcher_roles", operational)
    monkeypatch.setattr(
        ci_bootstrap,
        "_prepare_historical_557_replay",
        lambda *_args: calls.append("historical"),
    )
    url = make_url("postgresql+psycopg://postgres@localhost:5432/dotmac_sub_test")
    assert ci_bootstrap._bootstrap_outbox_url(url, label="first") == 0
    assert calls == ["guard", "operational", "historical"]


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
    assert OUTBOX_RELAY_OWNERSHIP_CONTRACT.migration_role == "app_admin"
    assert OUTBOX_RELAY_OWNERSHIP_CONTRACT.definer_role == "app_admin"
    assert OUTBOX_RELAY_OWNERSHIP_CONTRACT.schema == "public"
    assert OUTBOX_RELAY_OWNERSHIP_CONTRACT.schema_privileges == ("USAGE", "CREATE")
    assert HISTORICAL_557_RELAY_OWNERSHIP_CONTRACT.migration_role == "dotmac_app"
    assert (
        relay_ownership_violations(
            RelayOwnershipObservation(
                True, True, True, {"USAGE": True, "CREATE": True}
            ),
        )
        == ()
    )


def test_relay_function_ownership_refuses_missing_membership_and_schema_privileges() -> (
    None
):
    violations = relay_ownership_violations(
        RelayOwnershipObservation(True, True, False, {"USAGE": True, "CREATE": False}),
        contract=HISTORICAL_557_RELAY_OWNERSHIP_CONTRACT,
    )

    assert violations == (
        "dotmac_app is not a member of app_admin",
        "app_admin lacks CREATE on schema public",
    )


def test_operational_self_owner_needs_no_membership_or_missing_role_grant() -> None:
    assert (
        relay_ownership_violations(
            RelayOwnershipObservation(
                True, True, False, {"USAGE": True, "CREATE": True}
            )
        )
        == ()
    )
    assert (
        relay_ownership_violations(
            RelayOwnershipObservation(
                False, False, False, {"USAGE": False, "CREATE": False}
            )
        )[0]
        == "database role 'app_admin' is missing"
    )


def test_default_repair_refuses_retired_link_before_any_write(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        bootstrap_script, "observe_historical_membership", lambda _conn: True
    )

    class NoWriteConnection:
        def execute(self, *_args: object) -> None:
            raise AssertionError("default repair reached catalog read or role write")

    assert (
        bootstrap_script.bootstrap(
            NoWriteConnection(),
            dry_run=False,
            repair=True,  # type: ignore[arg-type]
        )
        == 1
    )
    assert "retired dotmac_app membership" in capsys.readouterr().err


def test_default_repair_refuses_missing_app_admin_before_dispatcher_write(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        bootstrap_script, "observe_historical_membership", lambda _conn: False
    )
    monkeypatch.setattr(bootstrap_script, "observe", lambda _conn: {})
    monkeypatch.setattr(
        bootstrap_script,
        "observe_ownership",
        lambda _conn: RelayOwnershipObservation(
            False, False, False, {"USAGE": False, "CREATE": False}
        ),
    )

    class NoWriteConnection:
        def execute(self, *_args: object) -> None:
            raise AssertionError("default repair tried a role write")

    assert (
        bootstrap_script.bootstrap(
            NoWriteConnection(),
            dry_run=False,
            repair=True,  # type: ignore[arg-type]
        )
        == 1
    )
    assert "database role 'app_admin' is missing" in capsys.readouterr().err


def test_historical_replay_option_is_not_public(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        bootstrap_script.sys,
        "argv",
        [
            "bootstrap_outbox_dispatcher_roles.py",
            "--prepare-historical-replay",
        ],
    )
    with pytest.raises(SystemExit, match="2"):
        bootstrap_script.main()
