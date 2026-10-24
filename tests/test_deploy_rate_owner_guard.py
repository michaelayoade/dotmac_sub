"""The deploy refuses a release that needs the rate owner while its flag is off.

PR-2 derives every rate through the rate owner and a production process will not
start with the flag off. The check must therefore run before the backup and the
``.env`` image pin, so a refused restore can never leave ``.env`` pinned to an
image that cannot start.
"""

from __future__ import annotations

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
    assert "check_rate_owner_flag.py" in calls[0]


def test_flag_on_passes_silently(tmp_path):
    out, _calls = _run(tmp_path, 0)
    assert "RC=0" in out
    assert "DEPLOY REFUSED" not in out


def test_the_check_runs_before_the_backup_and_the_env_pin():
    source = DEPLOY_SH.read_text(encoding="utf-8")
    call = source.index("\nverify_rate_owner_enabled || exit 1\n")
    assert call > source.index("\nverify_database_prerequisites\n")
    assert call < source.index("# If this script is killed mid-backup")
    assert call < source.index('set_env_value APP_IMAGE "${IMAGE}"', call)


class _Session:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def rollback(self):
        pass


@pytest.mark.parametrize(("flag", "code"), [(True, 0), (False, 3), (None, 3)])
def test_the_check_script_exit_codes(monkeypatch, capsys, flag, code):
    from app import db as app_db
    from app.services import access_rate_switch

    monkeypatch.setattr(app_db, "SessionLocal", lambda: _Session())
    monkeypatch.setattr(access_rate_switch, "flag_from_row", lambda _db: flag)
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import check_rate_owner_flag
    finally:
        sys.path.pop(0)
    assert check_rate_owner_flag.main() == code


def test_an_unreadable_flag_is_not_treated_as_on(monkeypatch, capsys):
    from app import db as app_db
    from app.services import access_rate_switch

    def boom(_db):
        raise RuntimeError("secret-looking detail")

    monkeypatch.setattr(app_db, "SessionLocal", lambda: _Session())
    monkeypatch.setattr(access_rate_switch, "flag_from_row", boom)
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import check_rate_owner_flag
    finally:
        sys.path.pop(0)
    assert check_rate_owner_flag.main() == 4
    assert "secret-looking detail" not in capsys.readouterr().err
