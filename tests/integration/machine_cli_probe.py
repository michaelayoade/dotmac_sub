"""Explicit disposable-cluster probe for Sub's real machine-issuer session.

Run ``python -m tests.integration.machine_cli_probe parent`` only with
``TEST_DATABASE_URL``, a separate direct ``app_user`` ``DATABASE_URL``, and
``DOTMAC_MACHINE_PROBE_DISPOSABLE_CLUSTER=1`` on an isolated PostgreSQL cluster.
This is deliberately outside pytest's default collection: the parent observes
the migrated database read-only, then launches a fresh process using the actual
app.db.SessionLocal. The cluster already supplies the role and tenant row.
The child captures the one-time key in memory and emits only boolean results.
"""

from __future__ import annotations

import io
import json
import os
import secrets
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.engine import make_url

from scripts.ci.migrated_test_database import (
    parse_test_database_target,
    require_migrated_schema,
)

_MARKER = "DOTMAC_MACHINE_PROBE_DISPOSABLE_CLUSTER"
_EXPECTED = "DOTMAC_MACHINE_PROBE_EXPECTED_BACKEND"
_LABEL = "DOTMAC_MACHINE_PROBE_LABEL"
_SERVER_MARKER = "dotmac-sub-disposable-tests"
_OBSERVE_SQL = (
    "SELECT current_database(), inet_server_addr()::text, inet_server_port(), "
    "pg_postmaster_start_time(), session_user, current_user, "
    "rolsuper, rolbypassrls, "
    "(SELECT setting FROM pg_settings WHERE name = 'cluster_name'), "
    "(SELECT context FROM pg_settings WHERE name = 'cluster_name') "
    "FROM pg_roles WHERE rolname = current_user"
)


class ProbeRefusal(RuntimeError):
    """The disposable target or actual runtime connection is unsuitable."""


@dataclass(frozen=True)
class ConnectionObservation:
    database: str
    server_ip: str
    server_port: int
    postmaster_start: str
    session_user: str
    current_user: str
    superuser: bool
    bypassrls: bool
    cluster_name: str
    cluster_name_context: str

    @classmethod
    def from_row(cls, row: tuple) -> ConnectionObservation:
        (
            database,
            ip,
            port,
            started,
            session_user,
            current_user,
            superuser,
            bypassrls,
            cluster_name,
            cluster_name_context,
        ) = row
        if not ip or not port or not isinstance(started, datetime):
            raise ProbeRefusal("a TCP PostgreSQL backend identity is required")
        return cls(
            str(database),
            str(ip),
            int(port),
            started.astimezone(UTC).isoformat(),
            str(session_user),
            str(current_user),
            bool(superuser),
            bool(bypassrls),
            str(cluster_name),
            str(cluster_name_context),
        )

    @property
    def backend(self) -> tuple[str, str, int, str, str, str]:
        return (
            self.database,
            self.server_ip,
            self.server_port,
            self.postmaster_start,
            self.cluster_name,
            self.cluster_name_context,
        )


def require_marked_target(environ: dict[str, str]):
    if environ.get(_MARKER) != "1":
        raise ProbeRefusal("an explicitly marked disposable cluster is required")
    return parse_test_database_target(environ.get("TEST_DATABASE_URL"))


def require_server_marker(observed: ConnectionObservation) -> None:
    if (
        observed.cluster_name != _SERVER_MARKER
        or observed.cluster_name_context != "postmaster"
    ):
        raise ProbeRefusal(
            "server is not the postmaster-marked disposable test cluster"
        )


def require_issuance_ready(verified_connections: int) -> None:
    if verified_connections < 1:
        raise ProbeRefusal("runtime checkout was not verified before issuance")


def require_runtime_observation(
    observed: ConnectionObservation,
    expected_backend: tuple[str, str, int, str, str, str],
) -> None:
    require_server_marker(observed)
    if observed.backend != expected_backend:
        raise ProbeRefusal("runtime backend differs from the disposable oracle")
    if (
        observed.session_user != "app_user"
        or observed.current_user != "app_user"
        or observed.superuser
        or observed.bypassrls
    ):
        raise ProbeRefusal(
            "runtime connection is not a direct non-bypass app_user login"
        )


def _parent() -> int:
    target = require_marked_target(dict(os.environ))
    runtime_raw = os.environ.get("DATABASE_URL")
    if not runtime_raw:
        raise ProbeRefusal("direct app_user DATABASE_URL is required")
    runtime_url = make_url(runtime_raw)
    if (
        runtime_url.username != "app_user"
        or runtime_url.database != target.database_name
    ):
        raise ProbeRefusal("runtime URL must name app_user and the marked database")

    engine = sa.create_engine(target.url)
    label = f"machine-639-probe-{uuid4().hex}"
    try:
        with engine.connect() as connection:
            oracle = ConnectionObservation.from_row(
                connection.execute(sa.text(_OBSERVE_SQL)).one()
            )
            require_server_marker(oracle)
            if oracle.database != target.database_name:
                raise ProbeRefusal("oracle is not connected to the marked database")
        require_migrated_schema(engine)

        child_env = os.environ.copy()
        child_env["DATABASE_URL"] = runtime_raw
        child_env[_EXPECTED] = json.dumps(oracle.backend)
        child_env[_LABEL] = label
        completed = subprocess.run(
            [sys.executable, "-m", "tests.integration.machine_cli_probe", "child"],
            env=child_env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if completed.returncode != 0:
            raise ProbeRefusal("runtime child refused or failed")
        result = json.loads(completed.stdout)
        if result != {"ok": True, "issued": 1, "authenticated": 1}:
            raise ProbeRefusal(
                "runtime child did not prove issuance and authentication"
            )
        print(json.dumps(result, sort_keys=True))
        return 0
    finally:
        engine.dispose()


def _child() -> int:
    target = require_marked_target(dict(os.environ))
    expected_data = json.loads(os.environ.get(_EXPECTED, "null"))
    if not isinstance(expected_data, list) or len(expected_data) != 6:
        raise ProbeRefusal("missing oracle backend identity")
    expected = tuple(expected_data)
    if expected[0] != target.database_name or not os.environ.get(_LABEL):
        raise ProbeRefusal("child target or cleanup identity is missing")

    # app.db constructs its canonical engine from DATABASE_URL on import.
    # Attach the live DBAPI checkout guard before the first checkout; every
    # connection used by the unpatched SessionLocal must pass it before SQL.
    from app.db import SessionLocal

    runtime_engine = SessionLocal.kw["bind"]
    if runtime_engine.pool.checkedout():
        raise ProbeRefusal("runtime engine was used before its guard was installed")
    verified_connections = 0

    @sa.event.listens_for(runtime_engine, "checkout")
    def _guard(dbapi_connection, _record, _proxy):
        nonlocal verified_connections
        with dbapi_connection.cursor() as cursor:
            cursor.execute(_OBSERVE_SQL)
            observed = ConnectionObservation.from_row(cursor.fetchone())
        dbapi_connection.rollback()
        require_runtime_observation(observed, expected)
        verified_connections += 1

    from app.services.operator_tenant import operator_tenant_id

    with SessionLocal() as db:
        if db.scalar(sa.text("SELECT current_setting('app.current_tenant')")) != str(
            operator_tenant_id()
        ):
            raise ProbeRefusal("Sub's operator tenant hook did not run")
    require_issuance_ready(verified_connections)

    from dotmac_kernel import machine_auth

    from scripts.machine_credentials import issue

    held = secrets.token_urlsafe(32)
    machine_auth.get_secret = lambda _name: held
    issue.get_secret = lambda _name: held
    issue.install_secret_source = lambda: ()
    issue.settings = SimpleNamespace(accepted_source_applications="dotmac_erp")

    attempted = False
    try:
        raw_output, metadata_output = io.StringIO(), io.StringIO()
        with redirect_stdout(raw_output), redirect_stderr(metadata_output):
            require_issuance_ready(verified_connections)
            attempted = True
            status = issue.main(
                [
                    "--label",
                    os.environ[_LABEL],
                    "--source-application",
                    "dotmac_erp",
                    "--scope",
                    "billing:invoice:read",
                ]
            )
        raw = raw_output.getvalue().strip()
        if status != 0 or not raw or raw_output.getvalue().count("\n") != 1:
            raise ProbeRefusal("issuer did not produce exactly one key")

        with SessionLocal() as db:
            if db.scalar(
                sa.text("SELECT current_setting('app.current_tenant')")
            ) != str(operator_tenant_id()):
                raise ProbeRefusal("authentication session lacks operator scope")
            principal = machine_auth.authenticate_machine(db, raw)
            if (
                principal.application != "dotmac_erp"
                or principal.tenant_id != operator_tenant_id()
            ):
                raise ProbeRefusal("attributed credential did not authenticate")
    finally:
        if attempted:
            from dotmac_kernel.machine_models import MachineCredential

            with SessionLocal() as db:
                db.execute(
                    sa.delete(MachineCredential).where(
                        MachineCredential.tenant_id == operator_tenant_id(),
                        MachineCredential.label == os.environ[_LABEL],
                    )
                )
                db.commit()
    print(json.dumps({"ok": True, "issued": 1, "authenticated": 1}, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    requested = argv if argv is not None else sys.argv[1:]
    try:
        if requested == ["parent"]:
            return _parent()
        if requested == ["child"]:
            return _child()
        raise ProbeRefusal("choose parent or child")
    except Exception:
        # Connection errors and exception reprs may carry URL credentials or
        # key material. The primary receives a boolean refusal, never those.
        print('{"ok": false}', file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
