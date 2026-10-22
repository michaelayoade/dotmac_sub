"""Protected staging enrollment and read-only access to the existing CP ledger."""

from __future__ import annotations

import os
import re
import stat
import subprocess
import time
from pathlib import Path
from typing import Literal, TypedDict, cast
from uuid import UUID

from deploy.agent import contracts as c
from deploy.agent.hostio import Config, HostIO
from deploy.gate0.evidence import raw_digest

Tool = Literal[
    "systemctl", "journalctl", "auditctl", "ausearch", "git", "pgrep", "dig", "nft"
]
TOOLS = {
    "systemctl",
    "journalctl",
    "auditctl",
    "ausearch",
    "git",
    "pgrep",
    "dig",
    "nft",
}
ENROLLMENT = Path("/etc/dotmac-gate0/enrollment.json")
RUNNER = "actions.runner.michaelayoade-dotmac_sub.Seabone-proof1.service"


class ToolPin(TypedDict):
    path: c.BoundedText
    sha256: c.Digest


class Enrollment(TypedDict):
    deployment_ref: c.BoundedText
    config_sha256: c.Digest
    unit_sha256: c.Digest
    launcher_sha256: c.Digest
    runner_unit_sha256: c.Digest
    runner_settings_sha256: c.Digest
    tools: dict[Tool, ToolPin]
    cp_public_b64: c.BoundedText
    host_public_b64: c.BoundedText
    ledger_origin: c.BoundedText
    ledger_config_sha256: c.Digest
    proof_source_revision: c.Revision
    nft_element_time_unit: Literal["seconds", "milliseconds"]
    nft_unit_fixture_sha256: c.Digest
    module_sha256: dict[c.BoundedText, c.Digest]


class NftUnitFixture(TypedDict):
    schema: Literal["dotmac.nft-element-unit/1"]
    tool_sha256: c.Digest
    unit: Literal["seconds", "milliseconds"]
    known_timeout_ms: int
    observed_timeout_literal: int
    source_revision: c.Revision
    source_sha256: c.Digest
    verification_run_id: c.Identifier


MODULES = {
    "deploy/gate0/proof1_observation.py",
    "scripts/verify_github_release.py",
    "scripts/__init__.py",
    "deploy/agent/__init__.py",
    "deploy/gate0/evidence.py",
    "deploy/gate0/host_evidence.py",
    "deploy/gate0/proof2_refusals.py",
    "deploy/gate0/proof34_transition.py",
    "deploy/gate0/run_proofs.py",
    "deploy/agent/contracts.py",
    "deploy/agent/agent.py",
    "deploy/agent/hostio.py",
    "deploy/agent/state.py",
    "deploy/egress/render.py",
}


def protected_path(path: Path, *, directory: bool = False) -> None:
    c.require(path.is_absolute())
    for parent in (path, *path.parents):
        info = parent.lstat()
        c.require(
            not stat.S_ISLNK(info.st_mode)
            and info.st_uid == 0
            and not info.st_mode & 0o022
        )
    info = path.stat()
    c.require(stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))


class EnrolledHost:
    def __init__(self, config: Config):
        c.require(os.geteuid() == 0 and config.environment == "staging")
        protected_path(ENROLLMENT)
        self.enrollment = cast(Enrollment, c.read_json(ENROLLMENT.read_bytes()))
        c._shape(self.enrollment, Enrollment)
        c.require(
            self.enrollment["deployment_ref"] == config.deployment_ref
            and set(self.enrollment["tools"]) == TOOLS
        )
        c.require(set(self.enrollment["module_sha256"]) == MODULES)
        source = Path(__file__).resolve().parents[2]
        for namespace in ("deploy", "deploy/gate0", "deploy/egress"):
            c.require(not (source / namespace / "__init__.py").exists())
        for module, digest in self.enrollment["module_sha256"].items():
            protected_path(source / module)
            c.require(raw_digest((source / module).read_bytes()) == digest)
        for pin in self.enrollment["tools"].values():
            protected_path(Path(pin["path"]))
            c.require(raw_digest(Path(pin["path"]).read_bytes()) == pin["sha256"])
        fixture_path = Path("/etc/dotmac-gate0/nft-unit-fixture.json")
        self.verify(fixture_path, self.enrollment["nft_unit_fixture_sha256"])
        fixture = cast(NftUnitFixture, c.read_json(fixture_path.read_bytes()))
        c._shape(fixture, NftUnitFixture)
        factor = 1000 if fixture["unit"] == "seconds" else 1
        c.require(
            fixture["unit"] == self.enrollment["nft_element_time_unit"]
            and fixture["tool_sha256"] == self.enrollment["tools"]["nft"]["sha256"]
            and 0 < fixture["known_timeout_ms"] <= 300000
            and fixture["observed_timeout_literal"] * factor
            == fixture["known_timeout_ms"]
        )
        for key in ("cp_public_b64", "host_public_b64"):
            c.require(len(c.unb64(self.enrollment[key])) == 32)
        c.require(
            re.fullmatch(
                r"https://github.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?",
                self.enrollment["ledger_origin"],
            )
            is not None
        )
        self.config, self.host = config, HostIO(config)
        self.verify(
            Path("/etc/dotmac-deploy-agent/config.toml"),
            self.enrollment["config_sha256"],
        )
        self.verify(
            Path("/etc/systemd/system/dotmac-deploy-agent.service"),
            self.enrollment["unit_sha256"],
        )
        self.verify(
            Path("/opt/dotmac-deploy-agent/launcher.sh"),
            self.enrollment["launcher_sha256"],
        )
        self.verify(
            Path("/etc/systemd/system") / RUNNER, self.enrollment["runner_unit_sha256"]
        )

    def verify(self, path: Path, digest: str) -> None:
        protected_path(path)
        c.require(raw_digest(path.read_bytes()) == digest)

    def result(
        self, argv: list[str], *, timeout: int = 60
    ) -> subprocess.CompletedProcess[str]:
        c.require(argv[0] in self.enrollment["tools"])
        pin = self.enrollment["tools"][argv[0]]
        if argv[0] == "git":
            argv = [
                argv[0],
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "core.fsmonitor=false",
                *argv[1:],
            ]
        self.verify(Path(pin["path"]), pin["sha256"])
        try:
            result = subprocess.run(
                [pin["path"], *argv[1:]],
                cwd=self.config.deploy_dir,
                env=self.host.environment
                | {
                    "HOME": "/nonexistent",
                    "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_CONFIG_GLOBAL": "/dev/null",
                    "GIT_NO_REPLACE_OBJECTS": "1",
                    "GIT_GRAFT_FILE": "/dev/null",
                },
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise ValueError("proof_tool_unavailable") from None
        c.require(len(result.stdout) <= 1024 * 1024)
        return result

    def run(self, argv: list[str], *, timeout: int = 60) -> str:
        result = self.result(argv, timeout=timeout)
        c.require(result.returncode == 0, "host_mismatch")
        return result.stdout.strip()

    def published(self, records: dict[str, bytes]) -> str:
        ledger = Path("/var/lib/dotmac-gate0/ledger")
        protected_path(ledger, directory=True)
        self.verify(ledger / ".git/config", self.enrollment["ledger_config_sha256"])
        prefix = ["git", "-C", str(ledger)]
        c.require(
            self.run(prefix + ["remote", "get-url", "origin"])
            == self.enrollment["ledger_origin"]
        )
        deadline = time.monotonic() + 150
        while time.monotonic() < deadline:

            def remaining() -> int:
                seconds = int(deadline - time.monotonic())
                c.require(seconds > 0)
                return min(30, seconds)

            self.run(
                prefix
                + [
                    "fetch",
                    "--no-tags",
                    "origin",
                    "refs/heads/main:refs/remotes/origin/main",
                ],
                timeout=remaining(),
            )
            commit = self.run(
                prefix + ["rev-parse", "refs/remotes/origin/main"], timeout=remaining()
            )
            c.sha(commit, prefix=False)
            remote = self.run(
                prefix + ["ls-remote", "origin", "refs/heads/main"], timeout=remaining()
            ).split()[0]
            if remote == commit:
                matched = True
                for path, raw in records.items():
                    c.require(
                        re.fullmatch(
                            r"(?:desired|applied)/dotmac_sub/staging/[A-Za-z0-9_.-]+/[A-Za-z0-9_-]+\.json",
                            path,
                        )
                        is not None
                    )
                    result = self.result(
                        prefix + ["show", commit + ":" + path], timeout=remaining()
                    )
                    matched &= (
                        result.returncode == 0
                        and c.canonical_json(c.read_json(result.stdout.encode())) == raw
                    )
                if matched:
                    return commit
            time.sleep(1)
        raise ValueError("published_ledger_readback_timeout")

    def published_report(
        self, report_id: str, raw: bytes, desired_raw: bytes | None = None
    ) -> str:
        c.require(str(UUID(report_id)) == report_id)
        records = {
            "applied/" + self.config.deployment_ref + "/" + report_id + ".json": raw
        }
        if desired_raw is not None:
            envelope = cast(c.Envelope, c.read_json(desired_raw))
            doc = c.validate_desired(
                c.verify(c.DESIRED, envelope, self.enrollment["cp_public_b64"])
            )
            records[
                "desired/" + self.config.deployment_ref + "/" + doc["nonce"] + ".json"
            ] = desired_raw
        return self.published(records)
