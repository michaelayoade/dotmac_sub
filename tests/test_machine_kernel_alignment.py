"""Sub 639 must satisfy the installed Kernel a97 machine credential contract."""

from __future__ import annotations

import ast
import importlib.util
import inspect
import io
import textwrap
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from dotmac_kernel import machine_auth, source_applications
from dotmac_kernel.exceptions import UnauthorizedError
from dotmac_kernel.machine_models import MachineCredential
from dotmac_kernel.machine_rotation import issue_credential
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tests.integration import machine_cli_probe
from tests.integration.machine_cli_probe import (
    ConnectionObservation,
    ProbeRefusal,
    require_issuance_ready,
    require_marked_target,
    require_runtime_observation,
    require_server_marker,
)

_REVISION = (
    Path(__file__).resolve().parents[1]
    / "alembic/versions/639_machine_attribution_alignment.py"
)


def _postgres_upgrade_sql() -> str:
    spec = importlib.util.spec_from_file_location("sub_machine_638", _REVISION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == "639_machine_attribution"
    assert len(module.revision) <= 32
    assert module.down_revision == "638_payment_email_cutover"
    output = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql", opts={"as_sql": True, "output_buffer": output}
    )
    with Operations.context(context):
        module.upgrade()
    return output.getvalue()


def test_postgres_migration_matches_kernel_machine_model_contract():
    sql = _postgres_upgrade_sql()
    assert "audit_events" not in sql
    assert (
        "ALTER TABLE machine_credentials ADD COLUMN source_application VARCHAR(64)"
        in sql
    )
    assert "ADD COLUMN next_key_hash VARCHAR(120)" in sql
    assert "ADD COLUMN rotation_started_at TIMESTAMP WITH TIME ZONE" in sql
    assert "ADD COLUMN rotated_at TIMESTAMP WITH TIME ZONE" in sql
    for constraint in MachineCredential.__table__.constraints:
        if constraint.name and constraint.name.startswith(
            (
                "ck_machine_credentials_next_",
                "ck_machine_credentials_rotation_",
                "ck_machine_credentials_source_",
                "uq_machine_credentials_tenant_next_",
            )
        ):
            assert constraint.name in sql
            if isinstance(constraint, sa.CheckConstraint):
                # Alembic doubles LIKE's percent for offline SQL formatting.
                assert str(constraint.sqltext) in sql.replace("%%", "%")
    assert "UNIQUE (tenant_id, next_key_hash)" in sql
    assert "ix_machine_credentials_source_application" in sql
    assert "UPDATE machine_credentials" not in sql


@pytest.fixture
def kernel_machine_db(monkeypatch):
    monkeypatch.setattr(
        machine_auth, "get_secret", lambda name: "unit-test-held-material"
    )
    monkeypatch.setattr(
        source_applications,
        "_active_registry",
        source_applications.SourceApplicationRegistry(["dotmac_erp"]),
    )
    engine = sa.create_engine("sqlite+pysqlite:///:memory:")
    metadata = sa.MetaData()
    sa.Table("tenants", metadata, sa.Column("id", sa.Uuid(), primary_key=True))
    MachineCredential.__table__.to_metadata(metadata)
    metadata.create_all(engine)
    with Session(engine) as session:
        tenant_id = uuid4()
        session.execute(
            sa.text("INSERT INTO tenants (id) VALUES (:id)"), {"id": tenant_id.hex}
        )
        session.commit()
        yield session, tenant_id
    engine.dispose()


def test_kernel_a97_issuance_authentication_and_null_attribution_refusal(
    kernel_machine_db,
):
    session, tenant_id = kernel_machine_db
    credential, raw = issue_credential(
        session,
        tenant_id=tenant_id,
        label="erp-sync",
        source_application="dotmac_erp",
        scopes=["billing:invoice:read"],
    )
    session.commit()
    principal = machine_auth.authenticate_machine(session, raw)
    assert principal.application == "dotmac_erp"
    assert principal.has_scope("billing:invoice:read")
    assert not principal.has_scope("billing:invoice:write")
    credential.source_application = None
    session.commit()
    with pytest.raises(UnauthorizedError, match="machine credential is not valid"):
        machine_auth.authenticate_machine(session, raw)


@pytest.mark.parametrize(
    ("next_hash", "started", "source"),
    [
        ("sha256:weak", datetime(2026, 10, 1, tzinfo=UTC), "dotmac_erp"),
        ("hmac-sha256:other", None, "dotmac_erp"),
        ("hmac-sha256:current", datetime(2026, 10, 1, tzinfo=UTC), "dotmac_erp"),
        (None, None, " bad "),
    ],
)
def test_kernel_a97_schema_refuses_invalid_rotation_or_attribution(
    kernel_machine_db, next_hash, started, source
):
    session, tenant_id = kernel_machine_db
    values = {
        "id": uuid4(),
        "tenant_id": tenant_id,
        "label": str(uuid4()),
        "key_hash": "hmac-sha256:current",
        "scopes": ["billing:invoice:read"],
        "source_application": source,
        "next_key_hash": next_hash,
        "rotation_started_at": started,
    }
    with pytest.raises(IntegrityError):
        session.execute(sa.insert(MachineCredential.__table__).values(**values))
        session.flush()
    session.rollback()


def test_next_digest_uniqueness_is_tenant_scoped(kernel_machine_db):
    session, first_tenant = kernel_machine_db
    second_tenant = uuid4()
    session.execute(
        sa.text("INSERT INTO tenants (id) VALUES (:id)"), {"id": second_tenant.hex}
    )
    next_digest = "hmac-sha256:incoming"
    for tenant_id in (first_tenant, second_tenant):
        session.execute(
            sa.insert(MachineCredential.__table__).values(
                id=uuid4(),
                tenant_id=tenant_id,
                label=str(uuid4()),
                key_hash=f"hmac-sha256:{uuid4().hex}",
                next_key_hash=next_digest,
                rotation_started_at=datetime(2026, 10, 1, tzinfo=UTC),
                scopes=["billing:invoice:read"],
                source_application="dotmac_erp",
            )
        )
    session.commit()
    with pytest.raises(IntegrityError):
        session.execute(
            sa.insert(MachineCredential.__table__).values(
                id=uuid4(),
                tenant_id=first_tenant,
                label=str(uuid4()),
                key_hash=f"hmac-sha256:{uuid4().hex}",
                next_key_hash=next_digest,
                rotation_started_at=datetime(2026, 10, 1, tzinfo=UTC),
                scopes=["billing:invoice:read"],
                source_application="dotmac_erp",
            )
        )
    session.rollback()


def test_issuance_cli_refuses_undeclared_peer_before_secret_or_database(monkeypatch):
    from scripts.machine_credentials import issue

    monkeypatch.setattr(
        issue, "settings", SimpleNamespace(accepted_source_applications="")
    )
    monkeypatch.setattr(
        issue, "install_secret_source", lambda: pytest.fail("secret source reached")
    )
    with pytest.raises(source_applications.UndeclaredSourceApplicationError):
        issue.main(
            [
                "--label",
                "test",
                "--source-application",
                "dotmac_erp",
                "--scope",
                "billing:read",
            ]
        )


def test_issuance_cli_rejects_foreign_tenant_switch_before_install(monkeypatch):
    from scripts.machine_credentials import issue

    monkeypatch.setattr(
        issue, "install_secret_source", lambda: pytest.fail("secret source reached")
    )
    monkeypatch.setattr(issue, "SessionLocal", lambda: pytest.fail("database reached"))
    with pytest.raises(SystemExit) as failure:
        issue.main(
            [
                "--label",
                "test",
                "--source-application",
                "dotmac_erp",
                "--scope",
                "billing:read",
                "--tenant-slug",
                "foreign",
            ]
        )
    assert failure.value.code == 2


def test_issuance_cli_commits_shared_writer_and_shows_raw_key_once(
    kernel_machine_db, monkeypatch, capsys
):
    from scripts.machine_credentials import issue

    session, tenant_id = kernel_machine_db
    monkeypatch.setattr(
        issue, "settings", SimpleNamespace(accepted_source_applications="dotmac_erp")
    )
    monkeypatch.setattr(issue, "install_secret_source", lambda: ())
    monkeypatch.setattr(issue, "get_secret", lambda name: "unit-test-held-material")
    monkeypatch.setattr(issue, "SessionLocal", lambda: session)
    monkeypatch.setattr(
        issue, "operator_tenant", lambda db: SimpleNamespace(id=tenant_id)
    )
    args = [
        "--label",
        "erp-sync",
        "--source-application",
        "dotmac_erp",
        "--scope",
        "billing:invoice:read",
    ]
    assert issue.main(args) == 0
    output = capsys.readouterr()
    raw = output.out.strip()
    assert raw and output.out.count("\n") == 1
    assert raw not in output.err
    stored = session.scalar(sa.select(MachineCredential))
    assert stored is not None
    assert stored.source_application == "dotmac_erp"
    assert stored.key_hash == machine_auth.hash_machine_key(raw)
    assert raw not in stored.key_hash


def test_cli_probe_refuses_unmarked_cluster_before_connection():
    with pytest.raises(ProbeRefusal, match="marked disposable cluster"):
        require_marked_target(
            {"TEST_DATABASE_URL": "postgresql://localhost/dotmac_sub_test"}
        )


@pytest.mark.parametrize(
    "expected_backend",
    [
        (
            "other_test_database",
            "127.0.0.1",
            65440,
            "2026-10-01T00:00:00+00:00",
            "dotmac-sub-disposable-tests",
            "postmaster",
        ),
        (
            "dotmac_sub_test",
            "127.0.0.1",
            5432,
            "2026-10-01T00:00:00+00:00",
            "dotmac-sub-disposable-tests",
            "postmaster",
        ),
        (
            "dotmac_sub_test",
            "127.0.0.1",
            65440,
            "2026-10-02T00:00:00+00:00",
            "dotmac-sub-disposable-tests",
            "postmaster",
        ),
    ],
)
def test_cli_probe_refuses_live_backend_mismatch(expected_backend):
    observed = ConnectionObservation(
        "dotmac_sub_test",
        "127.0.0.1",
        65440,
        "2026-10-01T00:00:00+00:00",
        "app_user",
        "app_user",
        False,
        False,
        "dotmac-sub-disposable-tests",
        "postmaster",
    )
    with pytest.raises(ProbeRefusal, match="backend differs"):
        require_runtime_observation(observed, expected_backend)


@pytest.mark.parametrize(
    ("session_user", "current_user", "superuser", "bypassrls"),
    [
        ("postgres", "app_user", False, False),
        ("app_user", "postgres", False, False),
        ("app_user", "app_user", True, False),
        ("app_user", "app_user", False, True),
    ],
)
def test_cli_probe_refuses_non_login_or_bypass_role(
    session_user, current_user, superuser, bypassrls
):
    observed = ConnectionObservation(
        "dotmac_sub_test",
        "127.0.0.1",
        65440,
        "2026-10-01T00:00:00+00:00",
        session_user,
        current_user,
        superuser,
        bypassrls,
        "dotmac-sub-disposable-tests",
        "postmaster",
    )
    with pytest.raises(ProbeRefusal, match="direct non-bypass app_user"):
        require_runtime_observation(observed, observed.backend)


@pytest.mark.parametrize(
    ("cluster_name", "context"),
    [
        ("", "postmaster"),
        ("another-cluster", "postmaster"),
        ("dotmac-sub-disposable-tests", "user"),
    ],
)
def test_cli_probe_refuses_unmarked_or_mutable_server(cluster_name, context):
    observed = ConnectionObservation(
        "dotmac_sub_test",
        "127.0.0.1",
        65440,
        "2026-10-01T00:00:00+00:00",
        "app_user",
        "app_user",
        False,
        False,
        cluster_name,
        context,
    )
    with pytest.raises(ProbeRefusal, match="postmaster-marked disposable"):
        require_server_marker(observed)
    with pytest.raises(ProbeRefusal, match="postmaster-marked disposable"):
        require_runtime_observation(observed, observed.backend)


def test_cli_probe_requires_verified_checkout_before_issue():
    with pytest.raises(ProbeRefusal, match="before issuance"):
        require_issuance_ready(0)
    require_issuance_ready(1)

    def call_lines(function, name):
        tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
        return sorted(
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and (
                isinstance(node.func, ast.Name)
                and node.func.id == name
                or isinstance(node.func, ast.Attribute)
                and node.func.attr == name
            )
        )

    parent_marker = call_lines(machine_cli_probe._parent, "require_server_marker")
    parent_launch = call_lines(machine_cli_probe._parent, "run")
    child_gate = call_lines(machine_cli_probe._child, "require_issuance_ready")
    child_issue = call_lines(machine_cli_probe._child, "main")
    assert parent_marker and parent_launch and max(parent_marker) < min(parent_launch)
    assert len(child_gate) >= 2 and child_issue and max(child_gate) < min(child_issue)
