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
# Anything on the guarded rollback path must never be reached.
restore_prev() { echo restore_prev >>"$CALLS"; return 1; }
previous_image_rollback_allowed() { echo guard >>"$CALLS"; return 1; }
run_migrations() { echo "migrate image=$(env_value APP_IMAGE)" >>"$CALLS"; return "$MIGRATE_RC"; }
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
            _slice(src, "pin_and_migrate() {", "# --- End pre-swap pin and migration"),
        )
    )


def _run(tmp_path: Path, *, migrate_rc: int, backup_mode: str = "full") -> tuple[str, str, list[str], dict[str, str]]:
    (tmp_path / ".env").write_text(
        f"OTHER=keep\nAPP_IMAGE={OLD_IMAGE}\nGIT_SHA={OLD_SHA}\n", encoding="utf-8"
    )
    result = subprocess.run(
        ["bash", "-c", _script()],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env={
            **os.environ,
            "MIGRATE_RC": str(migrate_rc),
            "NEW_IMAGE": NEW_IMAGE,
            "NEW_SHA": NEW_SHA,
            "BACKUP_MODE": backup_mode,
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
    assert env == {"OTHER": "keep", "APP_IMAGE": OLD_IMAGE, "GIT_SHA": OLD_SHA}
    # the migration really ran against the candidate pin, then it was undone
    assert calls == [f"migrate image={NEW_IMAGE}"]
    assert f"Restored .env pin to the running image {OLD_IMAGE}" in err
    assert "alembic head unchanged" in err
    assert "no rollback path was used" in err


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
    assert env == {"OTHER": "keep", "APP_IMAGE": NEW_IMAGE, "GIT_SHA": NEW_SHA}
    assert calls == [f"migrate image={NEW_IMAGE}"]
    assert "Restored" not in err


def test_post_migration_resume_keeps_the_new_pin_and_skips_migration(
    tmp_path: Path,
) -> None:
    out, _, calls, env = _run(
        tmp_path, migrate_rc=1, backup_mode="skip_production_post_migration_resume"
    )
    assert "RC=0" in out
    assert env["APP_IMAGE"] == NEW_IMAGE
    assert calls == []


def test_deploy_exits_nonzero_and_does_not_enter_the_restore_trap() -> None:
    src = DEPLOY_SH.read_text(encoding="utf-8")
    tail = src[src.index("# --- End pre-swap pin and migration") :]
    assert tail.index("if ! pin_and_migrate; then") < tail.index("exit 1")
    block = tail[: tail.index("log \"Verifying database schema contracts\"")]
    assert "trap - ERR" in block and "exit 1" in block
    assert "restore_prev" not in block
