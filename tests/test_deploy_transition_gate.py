"""Exercise the real executor CLI with inert transports and real file locks."""

from __future__ import annotations

import fcntl
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from scripts.deploy_transition_gate import (
    PredecessorOutcome,
    PredecessorQuery,
    check_predecessor,
)

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "ghcr.io/michaelayoade/dotmac_sub"
PREDECESSOR = f"{REPOSITORY}@sha256:{'a' * 64}"
TARGET = f"{REPOSITORY}@sha256:{'b' * 64}"

# These stubs never contact a daemon. fcntl provides the same flock lock
# semantics on Linux and macOS, including fd9's shared open-file description.
STUB = r"""
import fcntl, os, pathlib, sys
name = pathlib.Path(sys.argv[0]).name
root = pathlib.Path(os.environ["DEPLOY_DIR"])
def record(event):
    with (root / "events").open("a") as stream:
        stream.write(event + "\n")
def locked():
    with (root / "lock").open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            record("locked")
            return
    record("UNLOCKED")
    sys.exit(99)
if name == "flock":
    try:
        fcntl.flock(int(sys.argv[-1]), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(1)
elif name == "pgrep":
    sys.exit(1)
elif name == "grep":
    if any("GITHUB_DEPLOY_GATE_TOKEN" in arg for arg in sys.argv[1:]):
        record("token_read")
    os.execv("/usr/bin/grep", ["grep", *sys.argv[1:]])
elif name == "nginx":
    locked()
    record("existing_engine_path")
    sys.exit(79)
elif name == "docker":
    locked()
    if sys.argv[1:] == ["inspect", "dotmac_sub_app", "--format",
                         "{{.State.Running}}|{{.Config.Image}}"]:
        record("inspect")
        value = (root / "observation").read_text()
        if value == "unreadable":
            sys.exit(1)
        print(value)
    else:
        record("MUTATION:" + " ".join(sys.argv[1:]))
        sys.exit(98)
"""


@dataclass(frozen=True)
class ExecutorHarness:
    path: Path
    environ: dict[str, str]

    def pin(self, image: str, *, dotenv_expected: str | None = None) -> None:
        content = (
            "APP_ENV=staging\nSERVER_NAME=dotmac-sub-staging\n"
            f"APP_IMAGE={image}\nGIT_SHA=fixture\n"
        )
        if dotenv_expected is not None:
            content += f"DEPLOY_EXPECTED_FROM_IMAGE={dotenv_expected}\n"
        (self.path / ".env").write_text(content)

    def observe(self, value: str) -> None:
        (self.path / "observation").write_text(value)

    def run(self, expected: str | None) -> subprocess.CompletedProcess[str]:
        environ = self.environ.copy()
        if expected is not None:
            environ["DEPLOY_EXPECTED_FROM_IMAGE"] = expected
        return subprocess.run(
            ["bash", str(ROOT / "scripts/deploy.sh"), TARGET.split("@", 1)[1]],
            env=environ,
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )

    def events(self) -> list[str]:
        path = self.path / "events"
        return path.read_text().splitlines() if path.exists() else []


@pytest.fixture
def executor(tmp_path: Path) -> ExecutorHarness:
    binary = tmp_path / "bin"
    binary.mkdir()
    for name in ("flock", "pgrep", "grep", "docker", "nginx"):
        path = binary / name
        path.write_text(f"#!{sys.executable}\n{STUB}")
        path.chmod(0o755)
    # Deliberately pass only this fixture's process inputs, never host secrets.
    environ = {
        "PATH": f"{binary}:/usr/bin:/bin",
        "DEPLOY_DIR": str(tmp_path),
        "REPO_DIR": str(ROOT),
        "PYTHON_BIN": sys.executable,
        "DEPLOY_LOCK_FILE": str(tmp_path / "lock"),
        "REQUIRE_PROXY_HANDOFF": "1",
        "HOME": str(tmp_path),
    }
    harness = ExecutorHarness(tmp_path, environ)
    harness.pin(PREDECESSOR)
    harness.observe(f"true|{PREDECESSOR}")
    return harness


@pytest.mark.parametrize("expected", [None, PREDECESSOR])
def test_absent_or_matching_predecessor_reaches_existing_engine_path(
    executor: ExecutorHarness, expected: str | None
) -> None:
    result = executor.run(expected)
    assert result.returncode != 0  # inert nginx deliberately stops this test
    events = executor.events()
    assert "existing_engine_path" in events
    assert "token_read" in events
    assert "UNLOCKED" not in events
    if expected is None:
        assert "inspect" not in events
    else:
        assert events.index("inspect") < events.index("token_read")


@pytest.mark.parametrize(
    "expected,observation,pin,code",
    [
        ("", f"true|{PREDECESSOR}", PREDECESSOR, PredecessorOutcome.INVALID),
        (
            "sha256:" + "a" * 64,
            f"true|{PREDECESSOR}",
            PREDECESSOR,
            PredecessorOutcome.INVALID,
        ),
        (
            PREDECESSOR.upper(),
            f"true|{PREDECESSOR}",
            PREDECESSOR,
            PredecessorOutcome.INVALID,
        ),
        (
            PREDECESSOR.replace(REPOSITORY, "other/repo"),
            f"true|{PREDECESSOR}",
            PREDECESSOR,
            PredecessorOutcome.INVALID,
        ),
        (PREDECESSOR, "unreadable", PREDECESSOR, PredecessorOutcome.MISMATCH),
        (PREDECESSOR, f"false|{PREDECESSOR}", PREDECESSOR, PredecessorOutcome.MISMATCH),
        (PREDECESSOR, f"true|{TARGET}", PREDECESSOR, PredecessorOutcome.MISMATCH),
        (PREDECESSOR, f"true|{PREDECESSOR}", TARGET, PredecessorOutcome.MISMATCH),
    ],
)
def test_refusal_precedes_credentials_and_all_mutation(
    executor: ExecutorHarness,
    expected: str,
    observation: str,
    pin: str,
    code: PredecessorOutcome,
) -> None:
    executor.pin(pin)
    executor.observe(observation)
    before = (executor.path / ".env").read_bytes()
    assert (
        check_predecessor(PredecessorQuery(expected, REPOSITORY, pin, observation))
        is code
    )
    result = executor.run(expected)
    assert result.returncode == 1
    assert code.value in result.stderr
    assert executor.events() == ["locked", "inspect"]
    assert (executor.path / ".env").read_bytes() == before
    assert "rollback" not in result.stderr.lower()


def test_dotenv_cannot_enable_guard(executor: ExecutorHarness) -> None:
    executor.pin(PREDECESSOR, dotenv_expected="invalid")
    result = executor.run(None)
    assert "existing_engine_path" in executor.events()
    assert "inspect" not in executor.events()
    assert "invalid_from_image_precondition" not in result.stderr


def test_competing_deploy_moves_predecessor_under_lock(
    executor: ExecutorHarness,
) -> None:
    # A prior observation matched. Another executor wins the lock and reaches
    # the target before this stale transition can start.
    with (executor.path / "lock").open("a") as owner:
        fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        busy = executor.run(PREDECESSOR)
        assert "another deploy already holds" in busy.stderr
        assert executor.events() == []
        executor.pin(TARGET)
        executor.observe(f"true|{TARGET}")
    result = executor.run(PREDECESSOR)
    assert result.returncode == 1
    assert PredecessorOutcome.MISMATCH.value in result.stderr
    assert executor.events() == ["locked", "inspect"]
