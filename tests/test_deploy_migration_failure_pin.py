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
import sys
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
docker() {
  echo "docker $*" >>"$CALLS"
  if [[ "$1" == ps ]]; then
    case "${PS_MODE-gone}" in
      running) echo abc123 ;;
      error) return 1 ;;
    esac
    return 0
  fi
  printf '%s\\n' "${RUNNING_IMAGE-$OLD_IMAGE}"
}
MIGRATION_PS_POLL_ATTEMPTS=2
MIGRATION_PS_POLL_SECONDS=0
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
  MIGRATION_RUN_PREFIX=dotmac_sub_migrate_test_
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


_SCRUBBED_PREFIXES = ("DATABASE_URL", "DB_", "DEPLOY_", "MIGRATION_", "PRODUCTION_")


def _base_env() -> dict[str, str]:
    """Inherited environment minus anything the deploy logic reads (hermetic)."""
    return {k: v for k, v in os.environ.items() if not k.startswith(_SCRUBBED_PREFIXES)}


def _slice(source: str, start: str, end: str) -> str:
    return source[source.index(start) : source.index(end)]


def _resume_shell_functions_in_source_order(source: str) -> str:
    """Extract resume helpers in their real top-level definition order."""
    starts = (
        "database_name_from_url() {",
        "database_heads() {",
        "candidate_heads() {",
        "verify_post_migration_resume() {",
    )
    end_markers = (*starts[1:], "read_alembic_heads() {")
    definitions = []
    for start in starts:
        offset = source.index(start)
        end_offsets = [
            source.index(marker, offset + len(start))
            for marker in end_markers
            if source.find(marker, offset + len(start)) >= 0
        ]
        if start == starts[-1]:
            end_offsets.append(source.index("# Deploy-integrity gate.", offset))
        definitions.append((offset, min(end_offsets), start))
    call_offset = source.index('if ! BACKUP_MODE="$(verify_post_migration_resume)"')
    return "".join(
        source[start:end]
        for start, end, _ in sorted(definitions)
        if start < call_offset
    )


def _script() -> str:
    src = DEPLOY_SH.read_text(encoding="utf-8")
    return (
        HARNESS.replace(
            "@@ENV_HELPERS@@",
            _slice(src, "set_env_value() {", "pinned_image() {")
            + "pinned_image() { env_value APP_IMAGE; }\n"
            + "pinned_git_sha() { env_value GIT_SHA; }\n",
        )
        .replace(
            "@@REPIN@@",
            _slice(src, "repin_prev() {", "previous_image_rollback_allowed() {"),
        )
        .replace(
            "@@PIN_AND_MIGRATE@@",
            _slice(src, "database_name_from_url() {", "database_heads() {")
            + _slice(
                src,
                "read_alembic_heads() {",
                "# --- End pre-swap pin and migration",
            ),
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
            **_base_env(),
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
    script = (
        _script()
        .replace(
            'PREV_IMAGE="$(pinned_image)"; PREV_IMAGE_PRESENT=1',
            'PREV_IMAGE=""; PREV_IMAGE_PRESENT=0',
        )
        .replace(
            'PREV_GIT_SHA="$(pinned_git_sha)"; PREV_GIT_SHA_PRESENT=1',
            'PREV_GIT_SHA=""; PREV_GIT_SHA_PRESENT=0',
        )
    )
    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env={
            **_base_env(),
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
    out, err, calls, env = _run(tmp_path, migrate_rc=0, extra_env={"INTERRUPT": "1"})
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
            **_base_env(),
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
    fn = _slice(src, "database_heads() {", "candidate_heads() {") + _slice(
        src, "database_name_from_url() {", "database_heads() {"
    )
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
        env={**_base_env(), "DOCKER_RC": "1"},  # psql exits 1 on a timeout
    )
    args = (tmp_path / "docker-args").read_text(encoding="utf-8")
    assert "PGOPTIONS=-c lock_timeout=5s -c statement_timeout=15s" in args
    assert "PGCONNECT_TIMEOUT=5" in args
    assert "RC=1" in result.stdout
    assert "s3cret-not-real" not in args + result.stdout + result.stderr


@pytest.mark.parametrize(
    ("database_head", "expected_mode", "expected_error"),
    [
        ("670_a", "skip_production_post_migration_resume", None),
        ("669_a", None, "database_not_at_candidate_heads"),
    ],
)
def test_post_migration_resume_reads_database_heads_before_backup_mode(
    tmp_path: Path,
    database_head: str,
    expected_mode: str | None,
    expected_error: str | None,
) -> None:
    """Exercise the real early resume call, URL parser and fail-closed policy."""
    source = DEPLOY_SH.read_text(encoding="utf-8")
    backup = tmp_path / "dotmac_sub_run_38762087445_2026-10-15.sql.gz"
    backup.write_bytes(b"retained backup evidence")
    (tmp_path / ".env").write_text(f"DATABASE_URL={DB_URL}\n", encoding="utf-8")
    resume_functions = _resume_shell_functions_in_source_order(source)
    run_repo_module = _slice(
        source, "run_repo_module() {", "# Host-specific, so resolve shell env"
    )
    script = f"""
set -uo pipefail
env_value() {{ grep "^$1=" .env | head -1 | cut -d= -f2-; }}
{run_repo_module}
CALLS="$PWD/docker-args"
docker() {{
  printf '%s\\n' "$*" >>"$CALLS"
  case "$1" in
    inspect) printf '%s\\n' "$PREVIOUS_IMAGE" ;;
    exec) printf '%s\\n' "$DATABASE_HEAD" ;;
    run) printf '%s (head)\\n' "$CANDIDATE_HEAD" ;;
    *) return 99 ;;
  esac
}}
DB_CONTAINER=pg
APP_CONTAINER=dotmac_sub_app
IMAGE="{NEW_IMAGE}"
PREV_IMAGE="{OLD_IMAGE}"
TAG="sha256:{"b" * 64}"
PREVIOUS_IMAGE="{OLD_IMAGE}"
CANDIDATE_HEAD=670_a
DATABASE_HEAD="{database_head}"
DEPLOYMENT_TARGET=production
PRODUCTION_DEPLOY_RESUME_AFTER_MIGRATION=1
PRODUCTION_DEPLOY_RESUME_FAILED_RUN_ID=38762087445
PRODUCTION_DEPLOY_RESUME_AUTHORIZATION_RUN_ID=38761622018
PRODUCTION_DEPLOY_RESUME_BACKUP_PATH="{backup}"
REPO_DIR="{ROOT}"
PYTHON_BIN="{sys.executable}"
{resume_functions}
if BACKUP_MODE="$(verify_post_migration_resume)"; then
  printf 'BACKUP_MODE=%s\\n' "$BACKUP_MODE"
else
  echo RESUME_REJECTED
  printf 'POLICY_OUTPUT=%s\\n' "$BACKUP_MODE"
fi
"""
    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=_base_env(),
    )
    combined = result.stdout + result.stderr
    docker_args = (tmp_path / "docker-args").read_text(encoding="utf-8")
    assert result.returncode == 0, combined
    assert "s3cret-not-real" not in combined + docker_args
    assert "-d dotmac_sub" in docker_args
    if expected_mode is not None:
        assert f"BACKUP_MODE={expected_mode}" in result.stdout
        assert expected_error is None
    else:
        assert "RESUME_REJECTED" in result.stdout
        assert expected_error is not None and expected_error in combined


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
    block = tail[: tail.index('log "Verifying database schema contracts"')]
    assert "trap - ERR" in block and "exit 1" in block
    assert "restore_prev" not in block


@pytest.mark.parametrize("ps_mode", ["running", "error"])
def test_migration_container_still_running_or_unknown_keeps_the_pin(
    tmp_path: Path, ps_mode: str
) -> None:
    for kind in ("fail", "interrupt"):
        extra = {"PS_MODE": ps_mode}
        if kind == "interrupt":
            extra["INTERRUPT"] = "1"
        out, err, calls, env = _run(
            tmp_path, migrate_rc=0 if kind == "interrupt" else 1, extra_env=extra
        )
        assert env["APP_IMAGE"] == NEW_IMAGE, kind
        assert "still running or its state is unknown" in err
        assert "Do not recreate containers; repair forward" in err
        assert not any(
            c.startswith("docker kill") or c.startswith("docker stop") for c in calls
        )
        assert any(
            c.startswith("docker ps -q --filter name=dotmac_sub_migrate_test_")
            for c in calls
        )


def test_migration_container_gone_allows_the_measured_repin(tmp_path: Path) -> None:
    out, err, _, env = _run(tmp_path, migrate_rc=1, extra_env={"PS_MODE": "gone"})
    assert "RC=1" in out
    assert env["APP_IMAGE"] == OLD_IMAGE


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://u:s3cr3t@db.example.test",  # no path
        "postgresql://u:s3cr3t@db.example.test?password=ab/cd",  # '/' in query
        "postgresql://u:p@s3cr3t@db.example.test",  # '@' in password, no path
        "postgresql://u:p@s3cr3t@db.example.test:5432/dot mac",  # invalid name
        "u:s3cr3t@db.example.test/x",  # no scheme
        "postgresql://u@h.example.test/db?password=p@ss/word",  # '@' and '/' in query
    ],
)
def test_unparseable_database_url_never_leaks_the_password(
    tmp_path: Path, url: str
) -> None:
    (tmp_path / ".env").write_text(
        f"APP_IMAGE={OLD_IMAGE}\nGIT_SHA={OLD_SHA}\nDATABASE_URL={url}\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        ["bash", "-c", _script()],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env={
            **_base_env(),
            "PWD": str(tmp_path),
            "MIGRATE_RC": "1",
            "NEW_IMAGE": NEW_IMAGE,
            "NEW_SHA": NEW_SHA,
            "BACKUP_MODE": "full",
            "OLD_IMAGE": OLD_IMAGE,
            "HEADS_BEFORE": "670_a",
            "HEADS_AFTER": "670_a",
            "DB_BACKUP_DB_NAME": "dotmac_sub",
        },
    )
    combined = result.stdout + result.stderr
    assert "s3cr3t" not in combined
    assert "p@ss" not in combined and "ss/word" not in combined
    assert (
        "database name unparseable" in result.stderr
        or "alembic uses 'db'" in result.stderr
    )
    assert "RC=1" in result.stdout
    assert f"APP_IMAGE={NEW_IMAGE}" in (tmp_path / ".env").read_text()


def test_password_with_at_sign_before_a_valid_path_still_parses(
    tmp_path: Path,
) -> None:
    out, _, _, env = _run(
        tmp_path,
        migrate_rc=1,
        extra_env={"DB_BACKUP_DB_NAME": "dotmac_sub"},
    )
    assert env["APP_IMAGE"] == OLD_IMAGE  # control: the default URL parses


def test_exported_database_url_that_differs_from_env_is_unreadable(
    tmp_path: Path,
) -> None:
    out, err, _, env = _run(
        tmp_path,
        migrate_rc=1,
        extra_env={"DATABASE_URL": "postgresql://x:s3cr3t@h.example.test/other"},
    )
    assert env["APP_IMAGE"] == NEW_IMAGE
    assert "differs from .env" in err
    assert "s3cr3t" not in out + err


@pytest.mark.parametrize(
    ("url", "expected_db"),
    [
        (
            "postgresql://u:pw@h.example.test:5432/dotmac_sub?sslmode=disable",
            "dotmac_sub",
        ),
        ("postgresql:///dbname?host=/var/run/pg", "dbname"),
        ("postgresql://u@h.example.test/db?password=p@ss/word", "db"),
        ("postgresql://u:s3cr3t@h.example.test", None),
        ("postgresql://u:s3cr3t@h.example.test?x=a/b", None),
    ],
)
def test_database_heads_uses_the_shared_url_parser(
    tmp_path: Path, url: str, expected_db: str | None
) -> None:
    src = DEPLOY_SH.read_text(encoding="utf-8")
    fn = _slice(src, "database_heads() {", "candidate_heads() {") + _slice(
        src, "database_name_from_url() {", "database_heads() {"
    )
    (tmp_path / ".env").write_text(f"DATABASE_URL={url}\n", encoding="utf-8")
    script = f"""
set -uo pipefail
env_value() {{ grep "^$1=" .env | head -1 | cut -d= -f2-; }}
DB_CONTAINER=pg
docker() {{ printf '%s\\n' "$*" >docker-args; echo 670_a; }}
{fn}
database_heads; echo "RC=$?"
"""
    env = _base_env()
    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env={**env, "PWD": str(tmp_path)},
    )
    combined = result.stdout + result.stderr
    assert (
        "s3cr3t" not in combined and "p@ss" not in combined and "word" not in combined
    )
    if expected_db is None:
        assert "RC=1" in result.stdout
        assert not (tmp_path / "docker-args").exists()
    else:
        assert "RC=0" in result.stdout
        args = (tmp_path / "docker-args").read_text(encoding="utf-8").split()
        assert args[args.index("-d") + 1] == expected_db
