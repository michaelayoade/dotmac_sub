"""Finite staging refusal ceremony against the installed daemon, never Agent.poll."""

from __future__ import annotations

import json
import pwd
import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import uuid4

from deploy.agent import contracts as c
from deploy.agent.hostio import Config, now
from deploy.agent.state import atomic
from deploy.gate0.evidence import (
    HostObservation,
    RefusalEvidence,
    host_healthy,
    raw_digest,
)
from deploy.gate0.host_evidence import EnrolledHost, protected_path

UNIT = "dotmac-deploy-agent.service"
CONFIG = Path("/etc/dotmac-deploy-agent/config.toml")
DROPIN = Path("/run/systemd/system/dotmac-deploy-agent.service.d/90-gate0.conf")
INPUT = Path("/etc/dotmac-deploy-agent/gate0-desired.json")
TEMP_CONFIG = Path("/etc/dotmac-deploy-agent/gate0-config.toml")
LAUNCHER = "/opt/dotmac-deploy-agent/launcher.sh"


class DaemonProof(EnrolledHost):
    def __init__(self, config: Config):
        super().__init__(config)
        c.require(Config.load(CONFIG) == config and not config.allow_test_input)
        c.require(self.run(["systemctl", "is-active", UNIT]) == "active")
        self.audit_key = "dotmac_g0_" + uuid4().hex[:16]
        self.normal_poll = ("", "", "")
        self.last_invocation = self.invocation()

    def control(self, action: str, *, capture_invocation: bool = True) -> None:
        c.require(action in ("stop", "start", "restart", "daemon-reload"))
        c.require(capture_invocation or action == "stop")
        if action == "stop" and capture_invocation:
            state = self.result(["systemctl", "is-active", UNIT])
            c.require(
                (state.returncode, state.stdout.strip())
                in ((0, "active"), (3, "inactive"))
            )
            if state.returncode == 0:
                self.last_invocation = self.invocation()
        self.run(
            ["systemctl", action] + ([] if action == "daemon-reload" else [UNIT]),
            timeout=300,
        )

    def invocation(self) -> str:
        value = self.run(
            ["systemctl", "show", UNIT, "--property=InvocationID", "--value"]
        )
        c.require(re.fullmatch(r"[0-9a-f]{32}", value) is not None)
        return value

    def cursor(self) -> str:
        raw = self.run(
            ["journalctl", "-u", UNIT, "-n", "0", "--show-cursor", "--no-pager"]
        )
        cursor = raw.removeprefix("-- cursor: ")
        c.require(bool(cursor) and len(cursor) <= 2048)
        return cursor

    def audit_count(self) -> int:
        result = self.result(["ausearch", "-k", self.audit_key, "--raw"])
        c.require(
            result.returncode == 0
            or result.returncode == 1
            and "<no matches>" in result.stdout
        )
        uid = pwd.getpwnam("dotmac-deploy").pw_uid
        pids = set()
        for line in result.stdout.splitlines():
            if line.startswith("type=SYSCALL ") and re.search(
                r"\seuid=" + str(uid) + r"(?:\s|$)", line
            ):
                match = re.search(r"\spid=(\d+)", line)
                c.require(match is not None)
                pids.add(match[1])
        return len(pids)

    def observe(self) -> HostObservation:
        snapshot = self.host.observe()
        return {
            "observed_at": now(),
            "observed": cast(c.Observed, snapshot.observed()),
            "health": cast(c.Health, snapshot.health),
            "executor_count": self.audit_count(),
        }

    @contextmanager
    def audit(self) -> Iterator[None]:
        executor_path = self.config.deploy_dir / "scripts/deploy_staging.sh"
        c.require(executor_path.is_file() and not executor_path.is_symlink())
        executor = str(executor_path)
        c.require(self.audit_key not in self.run(["auditctl", "-l"]))
        self.run(["auditctl", "-w", executor, "-p", "r", "-k", self.audit_key])
        try:
            yield
        finally:
            self.run(["auditctl", "-W", executor, "-p", "r", "-k", self.audit_key])

    @contextmanager
    def override(self, raw: bytes, *, ancestor: str | None = None) -> Iterator[None]:
        c.require(
            not any(
                path.exists() or path.is_symlink()
                for path in (DROPIN, INPUT, TEMP_CONFIG)
            )
        )
        parent = DROPIN.parent
        protected_path(parent if parent.exists() else parent.parent, directory=True)
        original = CONFIG.read_bytes()
        text = original.decode()
        c.require(len(re.findall(r"(?m)^allow_test_input\s*=\s*false\s*$", text)) == 1)
        head = self.run(["git", "rev-parse", "HEAD"])
        branch = self.run(["git", "rev-parse", "--abbrev-ref", "HEAD"])
        c.require(
            not self.run(["git", "status", "--porcelain", "--untracked-files=no"])
        )
        if ancestor is not None:
            snapshot = self.host.observe()
            c.require(
                snapshot.healthy()
                and snapshot.running_revision_label == head
                and ancestor != head
            )
            self.run(["git", "merge-base", "--is-ancestor", ancestor, head])
        self.control("stop")
        original_invocation = fixture_invocation = self.last_invocation
        created_parent = not parent.exists()
        owned: dict[Path, bytes] = {}
        try:
            if ancestor is not None:
                self.run(["git", "checkout", "--detach", ancestor])
            parent.mkdir(parents=True, exist_ok=True)
            protected_path(parent, directory=True)
            atomic(INPUT, raw)
            owned[INPUT] = raw
            INPUT.chmod(0o644)
            atomic(
                TEMP_CONFIG,
                re.sub(
                    r"(?m)^allow_test_input\s*=\s*false\s*$",
                    "allow_test_input = true",
                    text,
                ).encode(),
            )
            owned[TEMP_CONFIG] = TEMP_CONFIG.read_bytes()
            TEMP_CONFIG.chmod(0o644)
            atomic(
                DROPIN,
                (
                    "[Service]\nExecStart=\nExecStart="
                    + LAUNCHER
                    + " --config "
                    + str(TEMP_CONFIG)
                    + " --desired-file "
                    + str(INPUT)
                    + "\n"
                ).encode(),
            )
            owned[DROPIN] = DROPIN.read_bytes()
            self.control("daemon-reload")
            self.control("start")
            fixture_invocation = self.invocation()
            c.require(fixture_invocation != original_invocation)
            yield
        finally:
            self.control("stop", capture_invocation=False)
            foreign = False
            for path, expected in owned.items():
                if (
                    path.is_symlink()
                    or not path.is_file()
                    or path.read_bytes() != expected
                ):
                    foreign = True
                else:
                    path.unlink()
            if created_parent and parent.exists() and not any(parent.iterdir()):
                parent.rmdir()
            c.require(CONFIG.read_bytes() == original)
            if ancestor is not None:
                self.run(
                    ["git", "checkout", "--detach" if branch == "HEAD" else branch]
                    + ([head] if branch == "HEAD" else [])
                )
                c.require(self.run(["git", "rev-parse", "HEAD"]) == head)
            self.control("daemon-reload")
            c.require(not foreign)
            normal_raw = self.host.fetch()
            c.require(normal_raw is not None)
            normal = c.validate_desired(
                c.verify(
                    c.DESIRED,
                    cast(c.Envelope, c.read_json(normal_raw)),
                    self.enrollment["cp_public_b64"],
                )
            )
            c.require(
                (
                    self.config.state_dir
                    / "nonces"
                    / (raw_digest(normal["nonce"].encode())[7:] + ".json")
                ).is_file()
            )
            cursor = self.cursor()
            self.control("start")
            c.require(self.invocation() != fixture_invocation)
            c.require(self.run(["systemctl", "is-active", UNIT]) == "active")
            command = self.run(
                ["systemctl", "show", UNIT, "--property=ExecStart", "--value"]
            )
            c.require("--desired-file" not in command and "launcher.sh" in command)
            end = self.wait_refusal(cursor, "nonce_replay")
            self.normal_poll = cursor, end, now()

    def wait_refusal(self, cursor: str, code: str) -> str:
        deadline = time.monotonic() + 150
        while time.monotonic() < deadline:
            journal = self.run(
                [
                    "journalctl",
                    "-u",
                    UNIT,
                    "--after-cursor=" + cursor,
                    "-o",
                    "json",
                    "--no-pager",
                ]
            )
            invocation = self.invocation()
            entries = [json.loads(line) for line in journal.splitlines()]
            matched = [
                entry
                for entry in entries
                if entry.get("_SYSTEMD_INVOCATION_ID") == invocation
                and re.search(
                    r"\breason=" + re.escape(code) + r"\b", entry.get("MESSAGE", "")
                )
            ]
            if matched:
                return cast(str, matched[-1]["__CURSOR"])
            time.sleep(1)
        raise ValueError("daemon_observation_timeout")


healthy = host_healthy


def corrupt_signature(raw: bytes, public: bytes) -> bytes:
    envelope = cast(c.Envelope, c.read_json(raw))
    c.verify(c.DESIRED, envelope, public)
    signature = bytearray(c.unb64(envelope["signature_b64"]))
    signature[0] ^= 1
    return c.canonical_json({**envelope, "signature_b64": c.b64(bytes(signature))})


def refusal(proof: DaemonProof, signed: bytes, code: str) -> RefusalEvidence:
    c.require(code in ("bad_signature", "nonce_replay", "rollback_digest"))
    doc = c.validate_desired(
        c.verify(
            c.DESIRED,
            cast(c.Envelope, c.read_json(signed)),
            proof.enrollment["cp_public_b64"],
        ),
        now=datetime.now(UTC),
    )
    c.require(doc["deployment_ref"] == proof.config.deployment_ref)
    raw = (
        corrupt_signature(signed, c.unb64(proof.enrollment["cp_public_b64"]))
        if code == "bad_signature"
        else signed
    )
    nonce_path = (
        proof.config.state_dir
        / "nonces"
        / (raw_digest(doc["nonce"].encode())[7:] + ".json")
    )
    c.require(
        nonce_path.exists() if code == "nonce_replay" else not nonce_path.exists()
    )
    if code == "nonce_replay":
        c.require(nonce_path.read_bytes() == c.canonical_json(doc))
    report_path = proof.config.state_dir / "outbox" / nonce_path.name
    original_report = report_path.read_bytes() if code == "nonce_replay" else None
    with proof.audit():
        before, cursor = proof.observe(), proof.cursor()
        with proof.override(
            raw, ancestor=doc["source_revision"] if code == "rollback_digest" else None
        ):
            end = proof.wait_refusal(cursor, code)
            after = proof.observe()
            report_raw = report_path.read_bytes()
            report = c.validate_applied(
                c.verify(
                    c.APPLIED,
                    cast(c.Envelope, c.read_json(report_raw)),
                    proof.enrollment["host_public_b64"],
                )
            )
            c.require(report["desired"]["nonce"] == doc["nonce"])
            c.require(
                report_raw == original_report and report["disposition"] == "applied"
                if code == "nonce_replay"
                else report["disposition"] == "refused" and report["reason"] == code
            )
        if code == "nonce_replay":
            c.require(
                nonce_path.read_bytes() == c.canonical_json(doc)
                and report_path.read_bytes() == original_report
            )
        after_cleanup = proof.observe()
        c.require(
            after_cleanup["observed"] == before["observed"] and healthy(after_cleanup)
        )
        c.require(after_cleanup["executor_count"] == before["executor_count"])
        c.require(
            healthy(before)
            and healthy(after)
            and before["observed"] == after["observed"]
        )
        c.require(before["executor_count"] == after["executor_count"])
    commit = proof.published_report(report["report_id"], report_raw, signed)
    return cast(
        RefusalEvidence,
        {
            "case": code,
            "submitted_sha256": raw_digest(raw),
            "nonce": doc["nonce"],
            "report_id": report["report_id"],
            "report_sha256": raw_digest(report_raw),
            "classification": "accepted",
            "expected_code": code,
            "observed_code": code,
            "journal_start": cursor,
            "journal_end": end,
            "normal_poll_journal_start": proof.normal_poll[0],
            "normal_poll_journal_end": proof.normal_poll[1],
            "cleanup_observed_at": proof.normal_poll[2],
            "ledger_commit": commit,
            "before": before,
            "after": after,
        },
    )
