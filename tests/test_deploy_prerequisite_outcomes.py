"""The prerequisite leg's three outcomes, executed rather than read.

On 2026-08-31 two release candidates died and production was left
unprovisioned because ``run_database_prerequisite_bootstrap`` returned 0 in two
completely different situations: when the contract already held, and when no
credential existed to repair it. Every existing deploy test reads the script as
text, so none of them could have noticed.

These run the real bash, with the surrounding deploy stubbed out, and assert on
behaviour: what it exits with, what it says, and — for the already-satisfied
case — that it writes nothing at all.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DEPLOY_SH = ROOT / "scripts/deploy.sh"

START = "# --- Module database prerequisites"
END = "verify_database_prerequisites() {"

HARNESS = """
set -uo pipefail

log() { printf '\\n==> %s\\n' "$*"; }
env_value() {
  # Only BOOTSTRAP_DATABASE_URL matters here; the fixture drives it.
  if [[ "$1" == "BOOTSTRAP_DATABASE_URL" ]]; then
    printf '%s\\n' "$FAKE_ENV_BOOTSTRAP_URL"
  else
    printf '%s\\n' ""
  fi
}

# Stand-in for `docker compose`: record the argv, succeed. Recording is what
# lets a test prove the already-satisfied path wrote NOTHING.
compose_stub() { printf '%s\\n' "$*" >>"$COMPOSE_CALLS"; }

IMAGE="ghcr.io/example/app@sha256:deadbeef"
FULL_SHA="0000000000000000000000000000000000000000"

%(leg)s

# Overridden AFTER the leg: the leg defines its own real versions.
COMPOSE=(compose_stub)
module_prerequisites_satisfied() {
  compose_stub "verify-only"
  [[ "$FAKE_SATISFIED" == "1" ]]
}

run_database_prerequisite_bootstrap
echo "OUTCOME=${PREREQUISITE_OUTCOME}"
"""


def _leg() -> str:
    source = DEPLOY_SH.read_text(encoding="utf-8")
    return source[source.index(START) : source.index(END)]


def _run(
    tmp_path: Path, *, satisfied: bool, env: dict[str, str]
) -> tuple[int, str, list[str]]:
    calls = tmp_path / "compose-calls"
    calls.write_text("", encoding="utf-8")
    script = HARNESS % {"leg": _leg()}
    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env={
            **os.environ,
            "COMPOSE_CALLS": str(calls),
            "FAKE_SATISFIED": "1" if satisfied else "0",
            "FAKE_ENV_BOOTSTRAP_URL": env.pop("_env_bootstrap_url", ""),
            **env,
        },
    )
    return (
        result.returncode,
        result.stdout + result.stderr,
        [line for line in calls.read_text(encoding="utf-8").splitlines() if line],
    )


def _credential(tmp_path: Path, *, mode: int = 0o400) -> dict[str, str]:
    pgpass = tmp_path / "schema-bootstrap.pgpass"
    pgpass.write_text(
        "127.0.0.1:9001:dotmac_sub:dotmac_schema_bootstrap:x\n", encoding="utf-8"
    )
    pgpass.chmod(mode)
    return {
        "SCHEMA_BOOTSTRAP_PGPASS": str(pgpass),
        "SCHEMA_BOOTSTRAP_URL": "postgresql://dotmac_schema_bootstrap@127.0.0.1:9001/dotmac_sub",
        # The harness cannot chown to root; the fixed-owner check itself is
        # exercised by test_a_wrongly_owned_credential_is_refused below.
        "SCHEMA_BOOTSTRAP_OWNER": _current_user(),
    }


def _current_user() -> str:
    return subprocess.run(
        ["id", "-un"], capture_output=True, text=True, check=True
    ).stdout.strip()


def test_missing_credential_and_missing_schema_is_a_hard_refusal(
    tmp_path: Path,
) -> None:
    """State one: repair is required and impossible. The deploy must stop."""
    code, output, calls = _run(
        tmp_path,
        satisfied=False,
        env={
            "SCHEMA_BOOTSTRAP_PGPASS": str(tmp_path / "absent.pgpass"),
            "SCHEMA_BOOTSTRAP_URL": "",
        },
    )

    assert code != 0, "a blocked repair must not exit successfully"
    assert "DEPLOY REFUSED" in output
    assert "blocked" in output
    assert "does not exist" in output, "the refusal must name the failing check"
    assert "SCHEMA_BOOTSTRAP_URL is not set" in output
    assert not any("--repair-schemas" in call for call in calls)


def test_missing_credential_but_satisfied_schema_verifies_without_mutating(
    tmp_path: Path,
) -> None:
    """State two: nothing to do, so the absent credential is irrelevant.

    This is the case the old code got right by accident and the new code must
    keep getting right on purpose — refusing here would ground every ordinary
    deploy on a host with no bootstrap credential.
    """
    code, output, calls = _run(
        tmp_path,
        satisfied=True,
        env={
            "SCHEMA_BOOTSTRAP_PGPASS": str(tmp_path / "absent.pgpass"),
            "SCHEMA_BOOTSTRAP_URL": "",
        },
    )

    assert code == 0
    assert "OUTCOME=already_satisfied" in output
    assert "DEPLOY REFUSED" not in output
    assert not any("--repair-schemas" in call for call in calls), (
        "an already-satisfied database must not be written to"
    )


def test_a_held_credential_repairs_and_reports_repaired(tmp_path: Path) -> None:
    code, output, calls = _run(tmp_path, satisfied=False, env=_credential(tmp_path))

    assert code == 0, output
    assert "OUTCOME=repaired" in output
    assert any("--repair-schemas" in call for call in calls)
    assert any("PGPASSFILE" in call for call in calls)


def test_the_password_never_appears_in_the_repair_invocation(tmp_path: Path) -> None:
    """The credential is read by libpq from the file, never passed as argv."""
    _, output, calls = _run(tmp_path, satisfied=False, env=_credential(tmp_path))
    joined = " ".join(calls)
    assert "BOOTSTRAP_DATABASE_URL" in joined
    assert "@127.0.0.1:9001" in joined
    assert ":x@" not in joined, "a password leaked into the connection URL"


@pytest.mark.parametrize("mode", [0o444, 0o600, 0o640])
def test_a_loosely_permissioned_credential_is_refused(
    tmp_path: Path, mode: int
) -> None:
    """libpq refuses a group- or world-readable pgpass; say so before it does."""
    code, output, _ = _run(
        tmp_path, satisfied=False, env=_credential(tmp_path, mode=mode)
    )
    assert code != 0
    assert "expected 400" in output


def test_a_wrongly_owned_credential_is_refused(tmp_path: Path) -> None:
    env = _credential(tmp_path)
    env["SCHEMA_BOOTSTRAP_OWNER"] = "definitely-not-the-owner"
    code, output, _ = _run(tmp_path, satisfied=False, env=env)
    assert code != 0
    assert "is owned by" in output


def test_an_inline_password_in_the_url_is_refused(tmp_path: Path) -> None:
    """A URL password would land in argv, the environment and the log."""
    env = _credential(tmp_path)
    env["SCHEMA_BOOTSTRAP_URL"] = (
        "postgresql://dotmac_schema_bootstrap:secret@127.0.0.1:9001/dotmac_sub"
    )
    code, output, _ = _run(tmp_path, satisfied=False, env=env)
    assert code != 0
    assert "inline password" in output


def test_a_persisted_elevated_dsn_in_dot_env_is_refused(tmp_path: Path) -> None:
    """The standing-privilege trap, closed.

    `env_value` greps the deploy directory's `.env`, so the old
    `${BOOTSTRAP_DATABASE_URL:-$(env_value BOOTSTRAP_DATABASE_URL)}` would arm
    superuser-shaped auto-repair on EVERY deploy for as long as the line stayed
    in the file. That the file happened to be empty was the whole safety
    property, and nothing enforced it. Now something does.
    """
    code, output, calls = _run(
        tmp_path,
        satisfied=True,
        env={
            "_env_bootstrap_url": "postgresql://postgres:hunter2@127.0.0.1:9001/dotmac_sub",
            "SCHEMA_BOOTSTRAP_PGPASS": str(tmp_path / "absent.pgpass"),
            "SCHEMA_BOOTSTRAP_URL": "",
        },
    )

    assert code != 0
    assert "DEPLOY REFUSED" in output
    assert "deploy directory's .env" in output
    # Refused even though the contract was satisfied: a standing elevated DSN
    # is wrong regardless of whether this particular deploy needed repair.
    assert not any("--repair" in call for call in calls)


def test_an_environment_supplied_elevated_dsn_still_works(tmp_path: Path) -> None:
    """One deliberate invocation is still allowed - it is how provisioning runs.

    The guard above must distinguish "persisted in a file" from "passed for
    this one command", or it would break the operator path it is protecting.
    """
    code, output, calls = _run(
        tmp_path,
        satisfied=False,
        env={
            "BOOTSTRAP_DATABASE_URL": "postgresql://postgres@127.0.0.1:9001/dotmac_sub",
            "SCHEMA_BOOTSTRAP_PGPASS": str(tmp_path / "absent.pgpass"),
            "SCHEMA_BOOTSTRAP_URL": "",
        },
    )

    assert code == 0, output
    assert "OUTCOME=repaired" in output
    assert any("--repair" in call for call in calls)
