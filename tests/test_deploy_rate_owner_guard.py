"""The deploy refuses a release that needs the rate owner while its flag is off.

PR-2 derives every rate through the rate owner and a production process will not
start with the flag off. The check must therefore run before the backup and the
``.env`` image pin, so a refused restore can never leave ``.env`` pinned to an
image that cannot start.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DEPLOY_SH = ROOT / "scripts/deploy.sh"
START = "verify_rate_owner_enabled() {"
END = "# Print the database name from a postgres URL, or fail."

HARNESS = """
set -uo pipefail
log() { printf '\\n==> %s\\n' "$*"; }
compose_stub() { printf '%s\\n' "$*" >>"$CALLS"; return "$FAKE_RC"; }
COMPOSE=(compose_stub)
IMAGE="registry.example/app@sha256:deadbeef"
FULL_SHA="0000000000000000000000000000000000000000"
@@FN@@
verify_rate_owner_enabled
echo "RC=$?"
"""


def _run(tmp_path: Path, rc: int) -> tuple[str, list[str]]:
    source = DEPLOY_SH.read_text(encoding="utf-8")
    fn = source[source.index(START) : source.index(END)]
    calls = tmp_path / "calls"
    calls.write_text("")
    result = subprocess.run(
        ["bash", "-c", HARNESS.replace("@@FN@@", fn)],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin", "CALLS": str(calls), "FAKE_RC": str(rc)},
    )
    return result.stdout + result.stderr, calls.read_text().splitlines()


def test_flag_off_refuses_with_the_clear_message(tmp_path):
    out, calls = _run(tmp_path, 3)
    assert "RC=1" in out
    assert (
        "PR-2 requires the rate owner switched on first (run the PR-1c switch)" in out
    )
    assert len(calls) == 1
    assert "scripts.check_rate_owner_flag" in calls[0]


def test_flag_on_passes_silently(tmp_path):
    out, _calls = _run(tmp_path, 0)
    assert "RC=0" in out
    assert "DEPLOY REFUSED" not in out


def test_the_check_runs_before_any_write_capable_step():
    source = DEPLOY_SH.read_text(encoding="utf-8")
    call = source.index("\nverify_rate_owner_enabled || exit 1\n")
    assert call < source.index("\nrun_database_prerequisite_bootstrap\n")
    assert call < source.index("\nverify_database_prerequisites\n")
    assert call < source.index("# If this script is killed mid-backup")
    assert call < source.index('set_env_value APP_IMAGE "${IMAGE}"', call)


def test_the_deploy_invokes_the_check_as_a_module():
    """``python scripts/x.py`` would not put the repo root on sys.path."""
    source = DEPLOY_SH.read_text(encoding="utf-8")
    assert "python -m scripts.check_rate_owner_flag" in source
    assert "scripts/check_rate_owner_flag.py" not in source


def _container_like(*argv: str) -> subprocess.CompletedProcess[str]:
    """Run from the image workdir with no PYTHONPATH and an unreachable database."""
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in {"PYTHONPATH", "DATABASE_URL"} and not k.startswith("PYTEST")
    }
    env["DATABASE_URL"] = "postgresql+psycopg://u:p@127.0.0.1:9/none"
    return subprocess.run(
        [sys.executable, *argv],
        capture_output=True,
        text=True,
        cwd=ROOT,
        env=env,
        timeout=120,
    )


def test_the_module_invocation_resolves_the_app_and_fails_closed_on_the_db():
    result = _container_like("-m", "scripts.check_rate_owner_flag")
    assert result.returncode == 4, result.stderr
    # Imports resolved (the failure is the database, not a missing module).
    assert "unreadable:" in result.stderr
    assert "ModuleNotFoundError" not in result.stderr
    assert "u:p@" not in result.stdout + result.stderr


def test_a_plain_script_run_cannot_import_the_app_and_exits_4_with_the_class_only():
    """The container's sys.path: this is why the deploy must use ``-m``."""
    result = _container_like("scripts/check_rate_owner_flag.py")
    assert result.returncode == 4
    assert result.stderr.strip() == ("rate owner flag unreadable: ModuleNotFoundError")


class _Row:
    def __init__(self, value_json, value_text):
        self.value_json, self.value_text = value_json, value_text


class _Session:
    def __init__(self, row):
        self._row = row

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, _statement):
        row = self._row
        return type("R", (), {"first": lambda _s: row})()

    def rollback(self):
        pass


@pytest.mark.parametrize(
    ("row", "code"),
    [
        (_Row(True, None), 0),
        (_Row(None, "true"), 0),
        (_Row(False, None), 3),
        (_Row(None, "false"), 3),
        (None, 3),
    ],
)
def test_the_check_script_exit_codes(monkeypatch, row, code):
    from app import db as app_db

    monkeypatch.setattr(app_db, "SessionLocal", lambda: _Session(row))
    from scripts import check_rate_owner_flag

    assert check_rate_owner_flag.main() == code


def test_the_check_selects_only_the_value_columns():
    source = (ROOT / "scripts/check_rate_owner_flag.py").read_text(encoding="utf-8")
    assert "select(DomainSetting.value_json, DomainSetting.value_text)" in source
