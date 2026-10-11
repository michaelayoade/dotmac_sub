"""A failed pre-swap migration must put the .env image pin back.

deploy.sh pins the candidate image in the host .env, then runs a one-off
`alembic upgrade heads` while the OLD app still serves. Before this fix a
failed migration left .env pinned to the new image: the guarded restore path is
(correctly) refused by the payment-email floor, so any later container recreate
would start the new image on the un-migrated schema.

These run the real bash for ``pin_and_migrate`` with the surrounding deploy
stubbed, and assert on the resulting .env and on which paths were taken.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DEPLOY_SH = ROOT / "scripts/deploy.sh"

OLD_IMAGE = "registry.example.test/app@sha256:" + "a" * 64
NEW_IMAGE = "registry.example.test/app@sha256:" + "b" * 64
DB_URL = "postgresql://dotmac:s3cret-not-real@db.example.test:5432/dotmac_sub?sslmode=disable"
OLD_SHA = "1" * 40
NEW_SHA = "2" * 40

HARNESS = """
set -uo pipefail
log() { printf '\\n==> %s\\n' "$*"; }
env_value() { grep "^$1=" .env | head -1 | cut -d= -f2-; }
@@ENV_HELPERS@@
@@REPIN@@
@@PIN_AND_MIGRATE@@
CALLS="$PWD/calls"
: >"$CALLS"
cleanup_children() { echo cleanup_children >>"$CALLS"; }
install_deploy_interrupt_trap() { trap 'echo ORIGINAL_TRAP >>"$CALLS"' INT TERM HUP; echo trap_restored >>"$CALLS"; }
# Anything on the guarded rollback path must never be reached.
restore_prev() { echo restore_prev >>"$CALLS"; return 1; }
previous_image_rollback_allowed() { echo guard >>"$CALLS"; return 1; }
APP_CONTAINER=dotmac_sub_app
docker() { echo "docker $*" >>"$CALLS"; printf '%s\\n' "${RUNNING_IMAGE-$OLD_IMAGE}"; }
# Heads read #1 is before the migration, read #2 after it.
database_heads() {
  local n; n="$(cat "$PWD/head-reads" 2>/dev/null || echo 0)"
  echo $((n + 1)) >"$PWD/head-reads"
  if ((n == 0)); then
    [[ -n "${HEADS_BEFORE-}" ]] || return 1
    printf '%s\\n' ${HEADS_BEFORE}
  else
    [[ -n "${HEADS_AFTER-}" ]] || return 1
    printf '%s\\n' ${HEADS_AFTER}
  fi
}
run_migrations() {
  echo "migrate image=$(env_value APP_IMAGE)" >>"$CALLS"
  if [[ -n "${INTERRUPT-}" ]]; then kill -INT $$; sleep 1; fi
  return "$MIGRATE_RC"
}
if [[ -n "${FAIL_GIT_SHA_WRITE-}" ]]; then
  _real_set_env_value="$(declare -f set_env_value)"
  set_env_value() {
    if [[ "$1" == GIT_SHA && "$2" == "$NEW_SHA" ]]; then return 1; fi
    _real_set_env_value_body "$@"
  }
  eval "${_real_set_env_value/set_env_value/_real_set_env_value_body}"
fi
compose_up() { echo up >>"$CALLS"; }
COMPOSE=(compose_up)

IMAGE="$NEW_IMAGE"; FULL_SHA="$NEW_SHA"
PREV_IMAGE="$(pinned_image)"; PREV_IMAGE_PRESENT=1
PREV_GIT_SHA="$(pinned_git_sha)"; PREV_GIT_SHA_PRESENT=1
pin_and_migrate
echo "RC=$?"
"""


def _slice(source: str, start: str, end: str) -> str:
    return source[source.index(start) : source.index(end)]


def _script() -> str:
    src = DEPLOY_SH.read_text(encoding="utf-8")
    return (
        HARNESS.replace(
            "@@ENV_HELPERS@@",
            _slice(src, "set_env_value() {", "pinned_image() {")
            + "pinned_image() { env_value APP_IMAGE; }\n"
            + "pinned_git_sha() { env_value GIT_SHA; }\n",
        )
        .replace("@@REPIN@@", _slice(src, "repin_prev() {", "previous_image_rollback_allowed() {"))
        .replace(
            "@@PIN_AND_MIGRATE@@",
            _slice(src, "read_alembic_heads() {", "# --- End pre-swap pin and migration"),
        )
    )


def _run(
    tmp_path: Path,
    *,
    migrate_rc: int,
    backup_mode: str = "full",
    extra_env: dict[str, str] | None = None,
) -> tuple[str, str, list[str], dict[str, str]]:
    (tmp_path / ".env").write_text(
        f"OTHER=keep\nAPP_IMAGE={OLD_IMAGE}\nGIT_SHA={OLD_SHA}\n"
        f"DATABASE_URL={DB_URL}\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        ["bash", "-c", _script()],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env={
            **os.environ,
            "PWD": str(tmp_path),
            "MIGRATE_RC": str(migrate_rc),
            "NEW_IMAGE": NEW_IMAGE,
            "NEW_SHA": NEW_SHA,
            "BACKUP_MODE": backup_mode,
            "OLD_IMAGE": OLD_IMAGE,
            "HEADS_BEFORE": "670_a",
            "HEADS_AFTER": "670_a",
            **(extra_env or {}),
        },
    )
    env = dict(
        line.split("=", 1)
        for line in (tmp_path / ".env").read_text(encoding="utf-8").splitlines()
    )
    calls = (tmp_path / "calls").read_text(encoding="utf-8").splitlines()
    return result.stdout, result.stderr, calls, env


def test_failed_migration_restores_the_running_pin(tmp_path: Path) -> None:
    out, err, calls, env = _run(tmp_path, migrate_rc=1)
    assert "RC=1" in out
    assert env == {
        "OTHER": "keep",
        "APP_IMAGE": OLD_IMAGE,
        "GIT_SHA": OLD_SHA,
        "DATABASE_URL": DB_URL,
    }
    # the migration really ran against the candidate pin, then it was undone
    assert f"migrate image={NEW_IMAGE}" in calls
    assert f"Restored .env pin to {OLD_IMAGE}" in err
    assert "matches the running dotmac_sub_app image" in err
    assert "heads measured unchanged (670_a)" in err
    assert "no rollback path was used" in err
    assert "s3cret-not-real" not in out + err


def test_trap_is_restored_after_the_migration_window(tmp_path: Path) -> None:
    for rc in (0, 1):
        _, _, calls, _ = _run(tmp_path, migrate_rc=rc)
        assert "trap_restored" in calls


def test_first_deploy_message_does_not_claim_a_running_image(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(f"DATABASE_URL={DB_URL}\n", encoding="utf-8")
    script = _script().replace(
        'PREV_IMAGE="$(pinned_image)"; PREV_IMAGE_PRESENT=1',
        'PREV_IMAGE=""; PREV_IMAGE_PRESENT=0',
    ).replace(
        'PREV_GIT_SHA="$(pinned_git_sha)"; PREV_GIT_SHA_PRESENT=1',
        'PREV_GIT_SHA=""; PREV_GIT_SHA_PRESENT=0',
    )
    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env={
            **os.environ,
            "PWD": str(tmp_path),
            "MIGRATE_RC": "1",
            "NEW_IMAGE": NEW_IMAGE,
            "NEW_SHA": NEW_SHA,
            "BACKUP_MODE": "full",
            "OLD_IMAGE": "",
            "RUNNING_IMAGE": "",
            "HEADS_BEFORE": "670_a",
            "HEADS_AFTER": "670_a",
        },
    )
    assert "RC=1" in result.stdout
    assert "no previous pin and no running dotmac_sub_app container" in result.stderr
    assert "matches the running" not in result.stderr


def test_database_mismatch_is_unreadable_and_keeps_the_pin(tmp_path: Path) -> None:
    out, err, calls, env = _run(
        tmp_path, migrate_rc=1, extra_env={"DB_BACKUP_DB_NAME": "some_other_db"}
    )
    assert "RC=1" in out
    assert env["APP_IMAGE"] == NEW_IMAGE
    assert "targets database 'some_other_db' but alembic uses 'dotmac_sub'" in err
    assert "Do not recreate containers; repair forward" in err
    assert "s3cret-not-real" not in out + err
    assert "restore_prev" not in calls


def test_matching_backup_db_name_is_accepted(tmp_path: Path) -> None:
    out, _, _, env = _run(
        tmp_path, migrate_rc=1, extra_env={"DB_BACKUP_DB_NAME": "dotmac_sub"}
    )
    assert "RC=1" in out
    assert env["APP_IMAGE"] == OLD_IMAGE


def test_interrupt_with_unchanged_heads_repins_and_exits_130(tmp_path: Path) -> None:
    out, err, calls, env = _run(
        tmp_path, migrate_rc=0, extra_env={"INTERRUPT": "1"}
    )
    assert "RC=" not in out  # the trap exits the shell
    assert env["APP_IMAGE"] == OLD_IMAGE and env["GIT_SHA"] == OLD_SHA
    assert "heads measured unchanged" in err
    assert "cleanup_children" in calls
    assert "restore_prev" not in calls and "guard" not in calls and "up" not in calls


def test_interrupt_with_advanced_heads_keeps_the_pin(tmp_path: Path) -> None:
    out, err, calls, env = _run(
        tmp_path,
        migrate_rc=0,
        extra_env={"INTERRUPT": "1", "HEADS_AFTER": "670_b"},
    )
    assert "RC=" not in out
    assert env["APP_IMAGE"] == NEW_IMAGE and env["GIT_SHA"] == NEW_SHA
    assert "Do not recreate containers; repair forward" in err
    assert "restore_prev" not in calls and "guard" not in calls and "up" not in calls


def test_interrupt_exit_status_is_130(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        f"APP_IMAGE={OLD_IMAGE}\nGIT_SHA={OLD_SHA}\nDATABASE_URL={DB_URL}\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        ["bash", "-c", _script()],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env={
            **os.environ,
            "PWD": str(tmp_path),
            "MIGRATE_RC": "0",
            "NEW_IMAGE": NEW_IMAGE,
            "NEW_SHA": NEW_SHA,
            "BACKUP_MODE": "full",
            "OLD_IMAGE": OLD_IMAGE,
            "HEADS_BEFORE": "670_a",
            "HEADS_AFTER": "670_a",
            "INTERRUPT": "1",
        },
    )
    assert result.returncode == 130


def test_database_heads_query_is_time_bounded_and_timeout_is_unreadable(
    tmp_path: Path,
) -> None:
    src = DEPLOY_SH.read_text(encoding="utf-8")
    fn = _slice(src, "database_heads() {", "candidate_heads() {")
    (tmp_path / ".env").write_text(f"DATABASE_URL={DB_URL}\n", encoding="utf-8")
    script = f"""
set -uo pipefail
env_value() {{ grep "^$1=" .env | head -1 | cut -d= -f2-; }}
DB_CONTAINER=pg
docker() {{ printf '%s\\n' "$*" >docker-args; return "${{DOCKER_RC}}"; }}
{fn}
database_heads; echo "RC=$?"
"""
    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env={**os.environ, "DOCKER_RC": "1"},  # psql exits 1 on a timeout
    )
    args = (tmp_path / "docker-args").read_text(encoding="utf-8")
    assert "PGOPTIONS=-c lock_timeout=5s -c statement_timeout=15s" in args
    assert "PGCONNECT_TIMEOUT=5" in args
    assert "RC=1" in result.stdout
    assert "s3cret-not-real" not in args + result.stdout + result.stderr


def test_failed_migration_never_touches_the_guarded_rollback_or_containers(
    tmp_path: Path,
) -> None:
    _, _, calls, _ = _run(tmp_path, migrate_rc=1)
    assert "restore_prev" not in calls
    assert "guard" not in calls
    assert "up" not in calls


def test_successful_migration_keeps_the_new_pin(tmp_path: Path) -> None:
    out, err, calls, env = _run(tmp_path, migrate_rc=0)
    assert "RC=0" in out
    assert env == {
        "OTHER": "keep",
        "APP_IMAGE": NEW_IMAGE,
        "GIT_SHA": NEW_SHA,
        "DATABASE_URL": DB_URL,
    }
    assert f"migrate image={NEW_IMAGE}" in calls
    assert "Restored" not in err


def test_post_migration_resume_keeps_the_new_pin_and_skips_migration(
    tmp_path: Path,
) -> None:
    out, _, calls, env = _run(
        tmp_path, migrate_rc=1, backup_mode="skip_production_post_migration_resume"
    )
    assert "RC=0" in out
    assert env["APP_IMAGE"] == NEW_IMAGE
    assert not any(c.startswith("migrate") for c in calls)


@pytest.mark.parametrize(
    "extra",
    [
        {"HEADS_AFTER": "670_b"},  # autocommit_block committed an earlier revision
        {"HEADS_BEFORE": ""},  # cannot prove the starting point
        {"HEADS_AFTER": ""},  # cannot prove the end point
    ],
)
def test_schema_possibly_advanced_leaves_the_pin_and_says_so(
    tmp_path: Path, extra: dict[str, str]
) -> None:
    out, err, calls, env = _run(tmp_path, migrate_rc=1, extra_env=extra)
    assert "RC=1" in out
    assert env["APP_IMAGE"] == NEW_IMAGE
    assert env["GIT_SHA"] == NEW_SHA
    assert "Alembic heads before:" in err and "Alembic heads after:" in err
    assert "Do not recreate containers; repair forward" in err
    assert "Restored" not in err
    assert "restore_prev" not in calls and "guard" not in calls and "up" not in calls


def test_a_drifted_running_image_is_called_out(tmp_path: Path) -> None:
    other = "registry.example.test/app@sha256:" + "c" * 64
    out, err, _, env = _run(tmp_path, migrate_rc=1, extra_env={"RUNNING_IMAGE": other})
    assert "RC=1" in out
    assert env["APP_IMAGE"] == OLD_IMAGE
    assert f"reports image '{other}'" in err
    assert "matches the running" not in err


def test_failed_git_sha_write_restores_the_half_updated_pin(tmp_path: Path) -> None:
    out, err, calls, env = _run(
        tmp_path, migrate_rc=0, extra_env={"FAIL_GIT_SHA_WRITE": "1"}
    )
    assert "RC=1" in out
    assert env == {
        "OTHER": "keep",
        "APP_IMAGE": OLD_IMAGE,
        "GIT_SHA": OLD_SHA,
        "DATABASE_URL": DB_URL,
    }
    assert "DEPLOY PIN WRITE FAILED" in err
    assert not any(c.startswith("migrate") for c in calls)


def test_deploy_exits_nonzero_and_does_not_enter_the_restore_trap() -> None:
    src = DEPLOY_SH.read_text(encoding="utf-8")
    tail = src[src.index("# --- End pre-swap pin and migration") :]
    assert tail.index("if ! pin_and_migrate; then") < tail.index("exit 1")
    block = tail[: tail.index("log \"Verifying database schema contracts\"")]
    assert "trap - ERR" in block and "exit 1" in block
    assert "restore_prev" not in block
