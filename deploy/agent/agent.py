"""Pull, verify, observe and report; never moves the checkout or config."""

from __future__ import annotations

import argparse
import logging
import sys
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import uuid4

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from . import contracts as wire
from .hostio import Config, Execution, HostIO, Snapshot, digest, now, protected
from .state import State, atomic, single_instance

LOG = logging.getLogger("dotmac.deploy_agent")


@dataclass
class Signer:
    key_id: str
    deployment_ref: str
    private_key: Ed25519PrivateKey

    def sign(self, body: bytes) -> bytes:
        return self.private_key.sign(body)

    @classmethod
    def load(cls, config: Config) -> Signer:
        key = serialization.load_pem_private_key(
            config.host_key_file.read_bytes(), password=None
        )
        if not isinstance(key, Ed25519PrivateKey):
            raise ValueError("Ed25519_host_key_required")
        return cls(config.host_key_id, config.deployment_ref, key)


@dataclass(frozen=True)
class Keyring:
    generation: int
    keys: dict[str, bytes]

    @classmethod
    def load(cls, config: Config, state: State) -> Keyring:
        value = wire.read_json(config.keyring_file.read_bytes())
        wire.exact(value, {"keyring_generation", "keys"})
        generation = value["keyring_generation"]
        wire.require(type(generation) is int and cast(int, generation) > 0)
        generation = cast(int, generation)
        wire.require(isinstance(value["keys"], list))
        keys = {}
        fingerprints = set()
        key_ids = set()
        for item in cast(list[dict[str, object]], value["keys"]):
            wire.exact(item, {"key_id", "public_key_b64", "revoked"})
            wire.identifier(item["key_id"])
            wire.require(type(item["revoked"]) is bool)
            key_id = cast(str, item["key_id"])
            public = wire.unb64(cast(str, item["public_key_b64"]))
            wire.require(
                len(public) == 32
                and item["key_id"] not in key_ids
                and public not in fingerprints
            )
            fingerprints.add(public)
            key_ids.add(item["key_id"])
            if not item["revoked"]:
                keys[key_id] = public
        generation_file = state.directory / "keyring-generation"
        previous = int(generation_file.read_text()) if generation_file.exists() else 0
        wire.require(generation >= previous)
        fingerprint_file = state.directory / "keyring-fingerprint"
        fingerprint = wire.digest(value)
        if generation == previous and fingerprint_file.exists():
            wire.require(fingerprint_file.read_text() == fingerprint)
        atomic(fingerprint_file, fingerprint.encode())
        atomic(generation_file, str(value["keyring_generation"]).encode())
        return cls(generation, keys)


@dataclass(frozen=True)
class PollResult:
    disposition: str
    reason: str | None = None
    report_id: str | None = None
    executor_spawned: bool = False


class Agent:
    def __init__(
        self,
        config: Config,
        host: HostIO,
        state: State,
        signer: Signer,
        keyring: Keyring,
    ):
        self.result = PollResult("idle")
        self.spawned = False
        self.config, self.host, self.state, self.signer, self.keyring = (
            config,
            host,
            state,
            signer,
            keyring,
        )

    def identity(self) -> wire.HostIdentity:
        return cast(
            wire.HostIdentity,
            {
                name: getattr(self.config, name)
                for name in ("product", "environment", "host", "image_reference")
            },
        )

    def matches(self, doc: wire.DesiredState, snapshot: Snapshot) -> bool:
        image = f"{doc['image_reference']}@{doc['image_digest']}"
        return (
            snapshot.running_image == image
            and snapshot.pinned_image == image
            and snapshot.running_revision_label
            == doc["source_revision"]
            == snapshot.pinned_git_sha
            and snapshot.config_files == doc["config_files"]
        )

    def report(
        self,
        doc: wire.DesiredState,
        disposition: str,
        reason: str | None,
        snapshot: Snapshot,
        execution: Execution | None = None,
    ) -> None:
        payload = wire.validate_applied(
            {
                "schema": wire.APPLIED,
                "report_id": str(uuid4()),
                "deployment_ref": self.config.deployment_ref,
                "host_key_id": self.signer.key_id,
                "signer_keyring_generation": self.keyring.generation,
                "desired": {
                    key: doc[key]
                    for key in (
                        "nonce",
                        "plan_digest",
                        "image_digest",
                        "source_revision",
                        "config_digest",
                    )
                },
                "disposition": disposition,
                "reason": reason,
                "observed": snapshot.observed(),
                "health": snapshot.health,
                "deploy_receipt": {
                    "exit_code": execution.exit_code if execution else None,
                    "receipt_line": execution.receipt_line if execution else None,
                },
                "observed_at": now(),
                "agent": {
                    "version": self.config.version,
                    "artifact_digest": self.config.artifact_digest,
                },
            }
        )
        envelope = wire.canonical_json(
            wire.seal(wire.APPLIED, dict(payload), self.signer)
        )
        self.state.seal(doc["nonce"], envelope)
        self.result = PollResult(
            disposition, reason, payload["report_id"], self.spawned
        )
        LOG.info("terminal disposition=%s reason=%s", disposition, reason)

    def retry_outbox(self) -> None:
        for path in self.state.unsent():
            try:
                if self.host.post(path.read_bytes()):
                    self.state.acknowledge(path)
                    envelope = path.read_bytes()
                    report = wire.read_json(
                        wire.unb64(
                            cast(wire.Envelope, wire.read_json(envelope))["payload_b64"]
                        )
                    )
                    if report["disposition"] in ("applied", "already_applied"):
                        self.state.remember(envelope)
                else:
                    LOG.error("applied_state_unacknowledged")
            except (OSError, ValueError):
                LOG.warning("cp_unreachable")
                break

    def recover(self) -> None:
        for raw in self.state.interrupted():
            doc = wire.validate_desired(wire.read_json(raw))
            self.report(doc, "failed", "interrupted", self.host.observe())

    def known_healthy(self, doc: wire.DesiredState) -> bool:
        raw = self.state.last_healthy()
        if raw is None:
            return False
        public = self.signer.private_key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        previous = wire.validate_applied(
            wire.verify(wire.APPLIED, cast(wire.Envelope, wire.read_json(raw)), public)
        )
        return (
            previous["deployment_ref"] == self.config.deployment_ref
            and previous["disposition"] in ("applied", "already_applied")
            and all(
                previous["desired"].get(key) == doc.get(key)
                for key in ("image_digest", "source_revision", "config_digest")
            )
        )

    def poll(self, desired_override: bytes | None = None) -> None:
        self.result, self.spawned = PollResult("idle"), False
        self.recover()
        self.retry_outbox()
        raw = desired_override if desired_override is not None else self.host.fetch()
        if raw is None:
            self.result = PollResult("no_desired")
            return
        # Recover a structurally valid claim for refusal evidence only. It never
        # authorizes execution; invalid/unjoinable payloads are quarantined.
        doc: wire.DesiredState | None = None
        try:
            envelope = cast(wire.Envelope, wire.read_json(raw))
            candidate = wire.read_json(wire.unb64(envelope["payload_b64"]))
            doc = wire.validate_desired(candidate)
            key = self.keyring.keys.get(envelope["key_id"])
            if key is None:
                raise wire.ContractError("unknown_signer_key")
            verified = wire.verify(wire.DESIRED, envelope, key)
            doc = wire.validate_desired(
                verified, now=datetime.now(UTC), identity=self.identity()
            )
        except (wire.ContractError, KeyError, TypeError, ValueError) as error:
            code = (
                error.code
                if isinstance(error, wire.ContractError)
                else "schema_violation"
            )
            if doc is None or doc.get("deployment_ref") != self.config.deployment_ref:
                self.state.quarantine(raw)
                LOG.error("refused reason=%s unjoinable=true", code)
                self.result = PollResult("quarantined", code)
                return
            if self.state.consumed(doc["nonce"]):
                LOG.warning("refused reason=nonce_replay")
                self.result = PollResult("refused", "nonce_replay")
                return
            self.state.consume(doc["nonce"], wire.canonical_json(doc))
            self.report(doc, "refused", code, self.host.observe())
            self.retry_outbox()
            return
        if self.state.consumed(doc["nonce"]):
            LOG.warning("refused reason=nonce_replay")
            self.result = PollResult("refused", "nonce_replay")
            return
        before = self.host.observe()
        reason = self.host.preflight(
            doc["source_revision"], before.running_revision_label
        )
        if reason is None and before.config_files != doc["config_files"]:
            reason = "config_drift"
        if (
            reason is None
            and self.matches(doc, before)
            and before.healthy()
            and self.known_healthy(doc)
        ):
            self.state.consume(doc["nonce"], wire.canonical_json(doc))
            self.report(doc, "already_applied", None, before)
            self.retry_outbox()
            return
        if (
            reason is None
            and before.running_image
            != self.config.image_reference + "@" + doc["from_image_digest"]
        ):
            reason = "from_image_mismatch"
        if reason is None:
            reason = self.host.artifact(
                doc["image_reference"], doc["image_digest"], doc["source_revision"]
            )
        if reason == "registry_unreachable":
            LOG.warning("registry_unreachable")
            self.result = PollResult("retry", "registry_unreachable")
            return
        if reason is not None:
            self.state.consume(doc["nonce"], wire.canonical_json(doc))
            self.report(doc, "refused", reason, self.host.observe())
            self.retry_outbox()
            return
        if self.host.busy():
            LOG.info("executor_busy")
            self.result = PollResult("retry", "executor_busy")
            return
        try:
            wire.validate_desired(
                dict(doc), now=datetime.now(UTC), identity=self.identity()
            )
        except wire.ContractError as error:
            self.state.consume(doc["nonce"], wire.canonical_json(doc))
            self.report(doc, "refused", error.code, self.host.observe())
            self.retry_outbox()
            return
        self.state.consume(doc["nonce"], wire.canonical_json(doc))
        log = self.state.directory / (self.state.nonce_path(doc["nonce"]).stem + ".log")
        self.spawned = True
        execution = self.host.execute(
            doc["image_digest"], log, doc["from_image_digest"]
        )
        after = self.host.observe()
        if execution.interrupted:
            disposition, reason = "failed", "interrupted"
        elif execution.exit_code == 0:
            disposition, reason = (
                ("failed", "observed_mismatch")
                if not self.matches(doc, after)
                else (
                    ("applied", None)
                    if after.healthy()
                    else ("failed", "health_gate_failed")
                )
            )
        elif (
            before.healthy()
            and after.healthy()
            and before.running_image == after.running_image
            and before.running_revision_label == after.running_revision_label
            and before.pinned_image == after.pinned_image
            and before.pinned_git_sha == after.pinned_git_sha
        ):
            disposition, reason = "rolled_back", None
        else:
            disposition, reason = (
                "failed",
                "health_gate_failed" if self.matches(doc, after) else "executor_failed",
            )
        self.report(doc, disposition, reason, after, execution)
        self.retry_outbox()

    def run(self) -> None:
        delay = self.config.poll_seconds
        while True:
            try:
                self.poll()
                delay = (
                    min(delay * 2, 300)
                    if self.result.disposition == "retry"
                    else self.config.poll_seconds
                )
            except (OSError, ValueError, TimeoutError):
                LOG.exception("poll_failed")
                delay = min(delay * 2, 300)
            time.sleep(delay)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--answer-challenge", type=Path)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--desired-file", type=Path)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    config = Config.load(args.config)
    if (
        config.version != "0.1.0"
        or digest(Path(sys.argv[0]).read_bytes()) != config.artifact_digest
    ):
        raise ValueError("agent_artifact_mismatch")
    with single_instance(config.agent_lock):
        state = State(config.state_dir)
        if args.answer_challenge:
            signer = Signer.load(config)
            challenge = wire.validate_challenge(
                wire.read_json(args.answer_challenge.read_bytes()),
                now=datetime.now(UTC),
            )
            wire.require(
                challenge["key_id"] == signer.key_id
                and challenge["deployment_ref"] == signer.deployment_ref,
                "host_mismatch",
            )
            observed = HostIO(config).observe().running_image
            if not isinstance(observed, str) or not observed.startswith(
                config.image_reference + "@"
            ):
                raise wire.ContractError("host_mismatch")
            baseline = observed.split("@", 1)[1]
            print(
                wire.canonical_json(
                    wire.answer_challenge(
                        challenge, signer, baseline, now=datetime.now(UTC)
                    )
                ).decode()
            )
            return
        agent = Agent(
            config,
            HostIO(config),
            state,
            Signer.load(config),
            Keyring.load(config, state),
        )
        if args.desired_file and (not args.once or not config.allow_test_input):
            raise ValueError("test_input_not_enabled")
        if args.once:
            override = None
            if args.desired_file:
                protected(args.desired_file)
                override = args.desired_file.read_bytes()
                wire.require(len(override) <= 1024 * 1024)
            agent.poll(override)
            print(wire.canonical_json(asdict(agent.result)).decode())
        else:
            agent.run()


if __name__ == "__main__":
    main()
