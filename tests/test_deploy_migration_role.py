"""Migrations run as app_admin, executed rather than read.

The Starter kernel contract (module_database_roles.v1) makes ``app_admin``
the permanent LOGIN BYPASSRLS role for offline/migration work, and backfill
migrations fail closed for any role subject to RLS. The 2026-10-20 deploy
only passed inside a temporary BYPASSRLS grant because ``run_migrations``
inherited the restricted runtime ``DATABASE_URL``.

These run the real bash leg with compose stubbed: the preflight must refuse
every malformed credential shape by name, and the migration container must
receive the pgpass mount and the admin URL — never the runtime URL.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEPLOY_SH = ROOT / "scripts/deploy.sh"

START = "# --- Migration role"
END = "# --- Module database prerequisites"

HARNESS = """
set -uo pipefail

log() { printf '\\n==> %s\\n' "$*"; }
env_value() { printf '%s\\n' ""; }

compose_stub() { printf '%s\\n' "$*" >>"$COMPOSE_CALLS"; }
COMPOSE=(compose_stub)
MIGRATION_MAX_ATTEMPTS=1
MIGRATION_RETRY_SECONDS=0

@@LEG@@

if migration_admin_credential_available; then
  echo "PREFLIGHT=ok"
  run_migrations
  echo "MIGRATE_RC=$?"
else
  echo "PREFLIGHT=refused"
fi
"""


def _leg() -> str:
    source = DEPLOY_SH.read_text(encoding="utf-8")
    return source[source.index(START) : source.index(END)]


def _run(
    tmp_path: Path, *, url: str, pgpass_mode: int | None
) -> tuple[str, str, list[str]]:
    calls = tmp_path / "compose-calls"
    calls.write_text("", encoding="utf-8")
    pgpass = tmp_path / "migration-admin.pgpass"
    if pgpass_mode is not None:
        pgpass.write_text(
            "db:5432:dotmac_sub:app_admin:s3cret-not-real\n", encoding="utf-8"
        )
        pgpass.chmod(pgpass_mode)
    result = subprocess.run(
        ["bash", "-c", HARNESS.replace("@@LEG@@", _leg())],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env={
            **os.environ,
            "COMPOSE_CALLS": str(calls),
            "MIGRATION_ADMIN_URL": url,
            "MIGRATION_ADMIN_PGPASS": str(pgpass),
            "MIGRATION_ADMIN_OWNER": os.environ.get("USER", ""),
        },
    )
    return result.stdout, result.stderr, calls.read_text(encoding="utf-8").splitlines()


GOOD_URL = "postgresql://app_admin@db:5432/dotmac_sub"


def test_valid_credential_runs_migrations_as_app_admin(tmp_path: Path) -> None:
    out, err, calls = _run(tmp_path, url=GOOD_URL, pgpass_mode=0o400)
    assert "PREFLIGHT=ok" in out
    assert "MIGRATE_RC=0" in out
    (call,) = calls
    assert "alembic upgrade heads" in call
    assert "PGPASSFILE=/run/secrets/migration-admin.pgpass" in call
    assert f"MIGRATION_DATABASE_URL={GOOD_URL}" in call
    # The migrator owns new objects; the runtime role is granted on them.
    assert "MIGRATION_RUNTIME_ROLE=dotmac_app" in call
    assert "MIGRATION_OBJECT_OWNER" not in call
    assert "s3cret-not-real" not in out + err + call


def test_missing_url_is_refused_by_name(tmp_path: Path) -> None:
    out, err, calls = _run(tmp_path, url="", pgpass_mode=0o400)
    assert "PREFLIGHT=refused" in out
    assert "MIGRATION_ADMIN_URL is not set" in err
    assert calls == []


def test_runtime_user_url_is_refused_by_name(tmp_path: Path) -> None:
    out, err, calls = _run(
        tmp_path, url="postgresql://dotmac_app@db:5432/dotmac_sub", pgpass_mode=0o400
    )
    assert "PREFLIGHT=refused" in out
    assert "connects as dotmac_app; migrations must run as app_admin" in err
    assert calls == []


def test_inline_password_is_refused_by_name(tmp_path: Path) -> None:
    out, err, calls = _run(
        tmp_path,
        url="postgresql://app_admin:hunter2@db:5432/dotmac_sub",
        pgpass_mode=0o400,
    )
    assert "PREFLIGHT=refused" in out
    assert "carries an inline password" in err
    assert "hunter2" not in err
    assert calls == []


def test_missing_pgpass_is_refused_by_name(tmp_path: Path) -> None:
    out, err, calls = _run(tmp_path, url=GOOD_URL, pgpass_mode=None)
    assert "PREFLIGHT=refused" in out
    assert "does not exist" in err
    assert calls == []


def test_group_readable_pgpass_is_refused_by_name(tmp_path: Path) -> None:
    out, err, calls = _run(tmp_path, url=GOOD_URL, pgpass_mode=0o440)
    assert "PREFLIGHT=refused" in out
    assert "has mode 440; expected 400" in err
    assert calls == []


# --- Identity check and runtime-grant wiring ------------------------------------

IDENTITY_HARNESS = """
set -uo pipefail
log() { printf '\\n==> %s\\n' "$*"; }
compose_stub() { printf '%s\\n' "$*" >>"$COMPOSE_CALLS"; return "${COMPOSE_RC:-0}"; }
COMPOSE=(compose_stub)
IMAGE=registry.invalid/app:candidate
FULL_SHA=0123456789abcdef
MIGRATION_ADMIN_PGPASS=/etc/dotmac/sub/migration-admin.pgpass
MIGRATION_ADMIN_PGPASS_MOUNT=/run/secrets/migration-admin.pgpass
MIGRATION_ADMIN_URL=postgresql://app_admin@db:5432/dotmac_sub
MIGRATION_MAX_ATTEMPTS=1
MIGRATION_RETRY_SECONDS=0
@@LEG@@
verify_migration_identity
echo "VERIFY_RC=$?"
"""


def _run_identity(tmp_path: Path, *, compose_rc: int = 0) -> tuple[str, list[str]]:
    calls = tmp_path / "compose-calls"
    calls.write_text("", encoding="utf-8")
    result = subprocess.run(
        ["bash", "-c", IDENTITY_HARNESS.replace("@@LEG@@", _leg())],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env={
            **os.environ,
            "COMPOSE_CALLS": str(calls),
            "COMPOSE_RC": str(compose_rc),
        },
    )
    return result.stdout, calls.read_text(encoding="utf-8").splitlines()


def test_identity_check_runs_the_read_only_script_with_the_migration_credential(
    tmp_path: Path,
) -> None:
    out, calls = _run_identity(tmp_path)
    assert "VERIFY_RC=0" in out
    (call,) = calls
    assert call.endswith("app python scripts/verify_migrator_identity.py")
    assert "PGPASSFILE=/run/secrets/migration-admin.pgpass" in call
    assert "MIGRATION_DATABASE_URL=postgresql://app_admin@db:5432/dotmac_sub" in call
    assert "MIGRATION_RUNTIME_ROLE=dotmac_app" in call
    assert "alembic" not in call


def test_identity_check_failure_propagates(tmp_path: Path) -> None:
    out, _calls = _run_identity(tmp_path, compose_rc=1)
    assert "VERIFY_RC=1" in out


def test_identity_check_precedes_the_pin_and_is_skipped_only_for_resume() -> None:
    source = DEPLOY_SH.read_text(encoding="utf-8")
    body = source[source.index("pin_and_migrate() {") :]
    assert body.index("verify_migration_identity") < body.index(
        'log "Pinning APP_IMAGE'
    )
    assert body.index("migration_admin_credential_available") < body.index(
        "verify_migration_identity"
    )
    refusal = body[body.index("verify_migration_identity") :]
    assert "DEPLOY REFUSED before pinning" in refusal[: refusal.index("Pinning")]


def test_runtime_role_is_fixed_not_environment_configurable() -> None:
    source = DEPLOY_SH.read_text(encoding="utf-8")
    assert '\nMIGRATION_RUNTIME_ROLE="dotmac_app"\n' in source
    assert "MIGRATION_RUNTIME_ROLE:-" not in source


def test_candidate_image_is_named_for_the_pre_pin_check() -> None:
    """Nothing is pinned yet, and the pinned image may predate the script."""
    source = DEPLOY_SH.read_text(encoding="utf-8")
    fn = source[source.index("verify_migration_identity() {") :]
    fn = fn[: fn.index("\n}\n")]
    assert 'APP_IMAGE="${IMAGE}" GIT_SHA="${FULL_SHA}"' in fn
