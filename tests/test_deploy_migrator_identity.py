"""Executed refusals of the production migration identity in ``deploy.sh``.

The real bash runs with the surrounding deploy stubbed out. Each test asserts a
behaviour: what the deploy exits with, what it says, and that the migrator
credential reaches the Alembic one-off container and nothing else. Passwords in
these tests are synthetic and are asserted to never appear in output.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DEPLOY_SH = ROOT / "scripts/deploy.sh"

START = "# --- Migration identity (dotmac_sub_migrator)"
END = 'PREREQUISITE_OUTCOME="unknown"'

SYNTHETIC_PASSWORD = "synthetic-Passw0rd-for-tests-only"  # pragma: allowlist secret
MIGRATOR_URL = "postgresql://dotmac_sub_migrator@db.example.test:5432/sub"

HARNESS = """
set -uo pipefail
log() { printf '\\n==> %s\\n' "$*"; }
env_value() { printf '%s\\n' "${FAKE_ENV_VALUE_MIGRATOR_PGPASS:-}"; }
compose_stub() { printf '%s\\n' "$*" >>"$COMPOSE_CALLS"; }

@@REGION@@

COMPOSE=(compose_stub)
MIGRATION_MAX_ATTEMPTS=1
MIGRATION_RETRY_SECONDS=0
case "$ENTRY" in
  require) require_migrator_identity; echo "REQUIRE_RC=$?" ;;
  args) migration_identity_args; echo "ARGC=${#MIGRATION_IDENTITY_ARGS[@]}" ;;
  migrate) run_migrations; echo "MIGRATE_RC=$?" ;;
esac
"""


def _region() -> str:
    source = DEPLOY_SH.read_text(encoding="utf-8")
    return source[source.index(START) : source.index(END)]


def _me() -> str:
    return subprocess.run(
        ["id", "-un"], capture_output=True, text=True, check=True
    ).stdout.strip()


class Env:
    """One synthetic deploy directory."""

    def __init__(self, tmp_path: Path) -> None:
        self.dir = tmp_path
        self.pgpass = tmp_path / "migrator.pgpass"
        self.write_pgpass()
        self.env_file = tmp_path / ".env"
        self.env_file.write_text("APP_ENV=production\n", encoding="utf-8")
        self.calls = tmp_path / "compose-calls"
        self.calls.write_text("", encoding="utf-8")
        self.vars: dict[str, str] = {
            "DEPLOYMENT_TARGET": "production",
            "MIGRATOR_PGPASS": str(self.pgpass),
            "MIGRATOR_URL": MIGRATOR_URL,
            "MIGRATOR_PGPASS_OWNER": _me(),
        }

    def write_pgpass(self, text: str | None = None, mode: int = 0o400) -> None:
        self.pgpass.unlink(missing_ok=True)
        self.pgpass.write_text(
            text
            if text is not None
            else f"db.example.test:5432:sub:dotmac_sub_migrator:{SYNTHETIC_PASSWORD}\n",
            encoding="utf-8",
        )
        self.pgpass.chmod(mode)

    def append_env(self, line: str) -> None:
        with self.env_file.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def run(self, entry: str, **extra: str) -> tuple[int, str]:
        script = HARNESS.replace("@@REGION@@", _region())
        env = {
            **{k: v for k, v in os.environ.items() if k != "MIGRATION_DATABASE_URL"},
            "ENTRY": entry,
            "COMPOSE_CALLS": str(self.calls),
            **self.vars,
            **extra,
        }
        result = subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
            cwd=self.dir,
            env=env,
            check=False,
        )
        return result.returncode, result.stdout + result.stderr

    def compose_calls(self) -> list[str]:
        return [
            line for line in self.calls.read_text(encoding="utf-8").splitlines() if line
        ]


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def _refused(code: int, output: str, needle: str) -> None:
    assert code != 0, output
    assert "DEPLOY REFUSED" in output
    assert needle in output, output
    assert SYNTHETIC_PASSWORD not in output, "a credential value was printed"


def test_a_correctly_provisioned_host_passes(env: Env) -> None:
    code, output = env.run("require")
    assert code == 0, output
    assert "REQUIRE_RC=0" in output
    assert SYNTHETIC_PASSWORD not in output


def test_staging_keeps_its_existing_path_untouched(env: Env) -> None:
    env.env_file.write_text("MIGRATION_DATABASE_URL=anything\n", encoding="utf-8")
    env.pgpass.unlink()
    code, output = env.run("require", DEPLOYMENT_TARGET="staging")
    assert code == 0, output
    code, output = env.run("args", DEPLOYMENT_TARGET="staging")
    assert code == 0
    assert "ARGC=0" in output


def test_migrator_url_in_env_is_refused(env: Env) -> None:
    env.append_env("MIGRATION_DATABASE_URL=postgresql://x@h/db")
    code, output = env.run("require")
    _refused(
        code, output, "MIGRATION_DATABASE_URL is set in the deploy directory's .env"
    )


def test_exported_migration_url_is_refused(env: Env) -> None:
    code, output = env.run("require", MIGRATION_DATABASE_URL="postgresql://x@h/db")
    _refused(code, output, "exported in the deploy environment")


def test_inline_migrator_password_in_env_is_refused(env: Env) -> None:
    env.append_env("SOMETHING=postgresql://dotmac_sub_migrator:hunter2@h/db")
    code, output = env.run("require")
    _refused(code, output, "inline dotmac_sub_migrator password")
    assert "hunter2" not in output


def test_pgpassfile_in_env_is_refused(env: Env) -> None:
    env.append_env("PGPASSFILE=/etc/dotmac/sub/migrator.pgpass")
    code, output = env.run("require")
    _refused(code, output, "PGPASSFILE is set in .env")


def test_the_pgpass_password_appearing_in_env_is_refused(env: Env) -> None:
    env.append_env(f"UNRELATED_KEY={SYNTHETIC_PASSWORD}")
    code, output = env.run("require")
    _refused(code, output, "password also appears in .env")


@pytest.mark.parametrize(
    ("url", "needle"),
    [
        ("", "MIGRATOR_URL is not set"),
        (
            "postgresql://dotmac_sub_migrator:pw@db.example.test/sub",
            "inline password",
        ),
        (
            "postgresql://dotmac_app@db.example.test/sub",
            "user is not dotmac_sub_migrator",
        ),
        ("postgresql://db.example.test/sub", "carries no user"),
    ],
)
def test_a_bad_migrator_url_is_refused(env: Env, url: str, needle: str) -> None:
    code, output = env.run("require", MIGRATOR_URL=url)
    _refused(code, output, needle)


def test_missing_credential_file_is_refused(env: Env) -> None:
    env.pgpass.unlink()
    code, output = env.run("require")
    _refused(code, output, "does not exist")


def test_group_readable_credential_file_is_refused(env: Env) -> None:
    env.write_pgpass(mode=0o640)
    code, output = env.run("require")
    _refused(code, output, "has mode 640; expected 400")


def test_wrongly_owned_credential_file_is_refused(env: Env) -> None:
    code, output = env.run("require", MIGRATOR_PGPASS_OWNER="root-not-me")
    _refused(code, output, "expected root-not-me")


def test_empty_credential_file_is_refused(env: Env) -> None:
    env.write_pgpass(text="")
    code, output = env.run("require")
    _refused(code, output, "is empty")


def test_credential_file_without_a_migrator_entry_is_refused(env: Env) -> None:
    env.write_pgpass(text=f"h:5432:sub:dotmac_app:{SYNTHETIC_PASSWORD}\n")
    code, output = env.run("require")
    _refused(code, output, "has no entry for dotmac_sub_migrator")


def test_symlinked_credential_file_is_refused(env: Env, tmp_path: Path) -> None:
    target = tmp_path / "real.pgpass"
    target.write_text(
        f"h:5432:sub:dotmac_sub_migrator:{SYNTHETIC_PASSWORD}\n", encoding="utf-8"
    )
    target.chmod(0o400)
    env.pgpass.unlink()
    env.pgpass.symlink_to(target)
    code, output = env.run("require")
    _refused(code, output, "is a symlink")


def test_directory_in_place_of_credential_file_is_refused(env: Env) -> None:
    env.pgpass.unlink()
    env.pgpass.mkdir()
    code, output = env.run("require")
    _refused(code, output, "is not a regular file")


def test_every_failed_check_is_reported_not_just_the_first(env: Env) -> None:
    env.append_env("MIGRATION_DATABASE_URL=postgresql://x@h/db")
    env.write_pgpass(mode=0o644)
    code, output = env.run("require", MIGRATOR_URL="")
    _refused(code, output, "MIGRATION_DATABASE_URL is set")
    assert "MIGRATOR_URL is not set" in output
    assert "has mode 644" in output


# --- The credential reaches only the Alembic one-off container ---------------


def test_the_alembic_container_alone_receives_the_migrator_flags(env: Env) -> None:
    code, output = env.run("migrate")
    assert code == 0, output
    calls = env.compose_calls()
    assert len(calls) == 1
    call = calls[0]
    assert "app alembic upgrade heads" in call
    assert "-e PGPASSFILE=/run/secrets/migrator.pgpass" in call
    assert f"-v {env.pgpass}:/run/secrets/migrator.pgpass:ro" in call
    assert f"-e MIGRATION_DATABASE_URL={MIGRATOR_URL}" in call
    assert "-e MIGRATION_SCHEMA_OWNER=dotmac_app" in call
    assert "-e MIGRATION_PRODUCTION_MODE=1" in call
    # The URL is passwordless and the secret never reaches argv.
    assert SYNTHETIC_PASSWORD not in call
    assert SYNTHETIC_PASSWORD not in output


def test_staging_migration_receives_no_migrator_flags(env: Env) -> None:
    code, output = env.run("migrate", DEPLOYMENT_TARGET="staging")
    assert code == 0, output
    (call,) = env.compose_calls()
    for flag in ("PGPASSFILE", "MIGRATION_DATABASE_URL", "MIGRATION_PRODUCTION_MODE"):
        assert flag not in call
