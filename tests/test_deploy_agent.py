"""Offline host seam checks: real Ed25519, fake host IO, durable restart."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from deploy.agent import agent as agent_module
from deploy.agent import contracts as wire
from deploy.agent.agent import Agent, Keyring, Signer
from deploy.agent.hostio import Config, Execution, HostIO, Snapshot
from deploy.agent.state import State, single_instance

DIGEST = "sha256:" + "a" * 64
PREVIOUS = "sha256:" + "b" * 64
REVISION = "c" * 40
FILES = {"docker-compose.yml": DIGEST, "docker-compose.override.yml": DIGEST}
IMAGE = "ghcr.io/michaelayoade/dotmac_sub"


def snapshot(image=PREVIOUS, healthy=True):
    health = {
        "live": {
            "url": "http://10.120.121.20:8001/health",
            "status": 200,
            "checked_at": "2026-10-21T12:00:00Z",
        },
        "ready": {
            "url": "http://10.120.121.20:8001/api/v1/health/ready",
            "status": 200 if healthy else 503,
            "body_status": "ready" if healthy else "failed",
            "checked_at": "2026-10-21T12:00:00Z",
        },
        "background": {
            "celery_inspect_ok": True,
            "containers": {
                "app": "running",
                "celery-worker": "running",
                "celery-beat": "running",
            },
            "restart_counts_zero": True,
        },
    }
    return Snapshot(
        IMAGE + "@" + image, REVISION, IMAGE + "@" + image, REVISION, FILES, (), health
    )


class FakeHost:
    def __init__(self):
        self.raw = None
        self.current = snapshot()
        self.executions = []
        self.posts = []
        self.ack = True
        self.locked = False
        self.error = None
        self.no_change = False
        self.preflight_reason = None

    def fetch(self):
        return self.raw

    def observe(self):
        return self.current

    def preflight(self, revision, running_revision):
        return self.preflight_reason

    def artifact(self, image, image_digest, source_revision):
        return self.error

    def busy(self):
        return self.locked

    def execute(self, image_digest, logfile, from_image_digest):
        self.executions.append(image_digest)
        if not self.no_change:
            self.current = snapshot(image_digest)
        return Execution(0, "DEPLOY RECEIPT: fake")

    def post(self, body):
        self.posts.append(body)
        return self.ack


@pytest.fixture
def harness(tmp_path):
    config = Config(
        "https://cp.invalid",
        tmp_path / "checkout",
        tmp_path / "state",
        tmp_path / "keys.json",
        tmp_path / "host.pem",
        "host-1",
        DIGEST,
        "0.1.0",
        DIGEST,
        agent_lock=tmp_path / "agent.lock",
        deploy_lock=tmp_path / "deploy.lock",
    )
    desired_signer = Signer(
        "desired-1", config.deployment_ref, Ed25519PrivateKey.generate()
    )
    host_signer = Signer("host-1", config.deployment_ref, Ed25519PrivateKey.generate())
    public = desired_signer.private_key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    host = FakeHost()
    state = State(config.state_dir)
    agent = Agent(config, host, state, host_signer, Keyring(1, {"desired-1": public}))

    def issue(**changes):
        current = datetime.now(UTC)
        doc = {
            "schema": wire.DESIRED,
            "product": config.product,
            "environment": config.environment,
            "host": config.host,
            "deployment_ref": config.deployment_ref,
            "image_reference": IMAGE,
            "image_digest": DIGEST,
            "source_revision": REVISION,
            "source_tree": "d" * 40,
            "product_manifest_digest": DIGEST,
            "build_run_id": 1,
            "evidence_run_id": 2,
            "config_files": FILES,
            "config_digest": wire.digest(FILES),
            "migration_policy": {"mode": "upgrade_heads"},
            "plan_digest": DIGEST,
            "approvals": [
                {"environment": "staging", "actor": "fixture actor", "run_id": 2}
            ],
            "nonce": wire.b64(bytes(range(32))),
            "issued_at": current.isoformat().replace("+00:00", "Z"),
            "expires_at": (current + timedelta(hours=4))
            .isoformat()
            .replace("+00:00", "Z"),
            "signer_key_id": desired_signer.key_id,
        }
        if "from_image_digest" in wire.DESIRED_FIELDS:
            doc["from_image_digest"] = PREVIOUS
        doc.update(changes)
        doc["plan_digest"] = wire.plan_digest(doc)
        host.raw = wire.canonical_json(wire.seal(wire.DESIRED, doc, desired_signer))
        return doc

    return agent, host, state, issue


def report(host):
    return wire.read_json(wire.unb64(wire.read_json(host.posts[-1])["payload_b64"]))


def test_apply_and_replay_after_restart(harness):
    agent, host, state, issue = harness
    doc = issue()
    agent.poll()
    assert host.executions == [DIGEST]
    assert report(host)["disposition"] == "applied"
    assert state.consumed(doc["nonce"])
    Agent(
        agent.config, host, State(state.directory), agent.signer, agent.keyring
    ).poll()
    assert host.executions == [DIGEST]


def test_exit_zero_is_not_truth(harness):
    agent, host, state, issue = harness
    issue()
    host.no_change = True
    agent.poll()
    assert report(host)["reason"] == "observed_mismatch"


def test_executor_busy_does_not_consume_nonce(harness):
    agent, host, state, issue = harness
    doc = issue()
    host.locked = True
    agent.poll()
    assert not host.executions and not host.posts
    assert not state.consumed(doc["nonce"])
    host.locked = False
    agent.poll()
    assert report(host)["disposition"] == "applied"


def test_sealed_retry_is_identical(harness):
    agent, host, state, issue = harness
    issue()
    host.ack = False
    agent.poll()
    first = host.posts[-1]
    agent.poll()
    assert host.posts[-1] == first
    assert len(host.executions) == 1
    host.ack = True
    agent.poll()
    assert not state.unsent()


def test_consumed_before_executor_and_restart_interrupted(harness):
    agent, host, state, issue = harness
    doc = issue()

    def crash(image_digest, logfile, from_image_digest):
        assert state.consumed(doc["nonce"])
        raise RuntimeError("simulated_process_crash")

    host.execute = crash
    with pytest.raises(RuntimeError):
        agent.poll()
    host.raw = None
    agent.poll()
    assert report(host)["reason"] == "interrupted"
    assert not host.executions


def test_noop_needs_prior_healthy_report(harness):
    agent, host, state, issue = harness
    issue()
    agent.poll()
    issue(nonce=wire.b64(b"n" * 32))
    agent.poll()
    assert report(host)["disposition"] == "already_applied"
    assert len(host.executions) == 1


@pytest.mark.parametrize(
    "reason",
    [
        "cosign_verify_failed",
        "attestation_identity_mismatch",
        "signature_missing",
        "attestation_missing",
    ],
)
def test_artifact_failure_never_executes(harness, reason):
    agent, host, state, issue = harness
    issue()
    host.error = reason
    agent.poll()
    assert not host.executions
    assert report(host)["reason"] == reason


@pytest.mark.parametrize(
    "reason", ["config_drift", "checkout_drift", "rollback_digest"]
)
def test_host_gates(harness, reason):
    agent, host, state, issue = harness
    issue()
    host.preflight_reason = reason
    agent.poll()
    assert not host.executions and report(host)["reason"] == reason


def test_bad_signature_is_reported(harness):
    agent, host, state, issue = harness
    issue()
    envelope = wire.read_json(host.raw)
    signature = bytearray(wire.unb64(envelope["signature_b64"]))
    signature[0] ^= 1
    envelope["signature_b64"] = wire.b64(bytes(signature))
    host.raw = wire.canonical_json(envelope)
    agent.poll()
    assert not host.executions and report(host)["reason"] == "bad_signature"


def test_expired(harness):
    agent, host, state, issue = harness
    issue(issued_at="2026-01-01T00:00:00Z", expires_at="2026-01-01T04:00:00Z")
    agent.poll()
    assert not host.executions and report(host)["reason"] == "expired"


def test_single_instance_is_distinct_from_deploy_probe(tmp_path, harness):
    agent, host, state, issue = harness
    lock = tmp_path / "single.lock"
    with single_instance(lock):
        with pytest.raises(BlockingIOError), single_instance(lock):
            pass
        assert not HostIO(agent.config).busy()


def test_executor_scrubs_tokens_and_uses_only_digest(tmp_path, harness, monkeypatch):
    from deploy.agent import hostio

    agent, host, state, issue = harness
    monkeypatch.setenv("GITHUB_TOKEN", "must-not-inherit")
    calls = []

    class Child:
        def __init__(self, argv, **kwargs):
            calls.append((argv, kwargs))
            import io

            self.stdout = io.BytesIO(b"DEPLOY RECEIPT: exact\n")

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(hostio.subprocess, "Popen", Child)
    monkeypatch.setattr(hostio.Path, "read_bytes", lambda path: b"engine")
    monkeypatch.setattr(hostio, "digest", lambda body: DIGEST)
    execution = HostIO(agent.config).execute(DIGEST, tmp_path / "run.log", PREVIOUS)
    argv, options = calls[0]
    assert argv == ["/usr/bin/bash", "scripts/deploy_staging.sh", DIGEST]
    assert "GITHUB_TOKEN" not in options["env"]
    assert options["env"]["COMPOSE_PROJECT_NAME"] == "dotmac_sub"
    assert execution.receipt_line == "DEPLOY RECEIPT: exact"


def test_new_nonce_for_matching_but_unaccepted_report_executes(harness):
    agent, host, state, issue = harness
    issue()
    host.ack = False
    agent.poll()
    issue(nonce=wire.b64(b"m" * 32), from_image_digest=DIGEST)
    agent.poll()
    assert len(host.executions) == 2
    assert report(host)["disposition"] == "applied"


def test_changed_predecessor_is_refused(harness):
    agent, host, state, issue = harness
    issue(from_image_digest="sha256:" + "e" * 64)
    agent.poll()
    assert not host.executions
    assert report(host)["reason"] == "from_image_mismatch"


def test_unhealthy_executor_result_fails(harness):
    agent, host, state, issue = harness
    issue()

    def execute(image_digest, logfile, from_image_digest):
        host.current = snapshot(DIGEST, healthy=False)
        return Execution(0, None)

    host.execute = execute
    agent.poll()
    assert report(host)["reason"] == "health_gate_failed"


def test_real_probe_detects_held_lock(harness):
    import fcntl

    agent, host, state, issue = harness
    with agent.config.deploy_lock.open("w") as locked:
        fcntl.flock(locked, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert HostIO(agent.config).busy()
    assert not HostIO(agent.config).busy()


def test_outbox_cannot_be_resealed(harness):
    agent, host, state, issue = harness
    doc = issue()
    state.consume(doc["nonce"], wire.canonical_json(doc))
    state.seal(doc["nonce"], b"first-sealed-bytes")
    with pytest.raises(ValueError, match="sealed_report_mutation"):
        state.seal(doc["nonce"], b"different")


def test_executor_guard_hash_refuses_before_spawn(harness, tmp_path):
    agent, host, state, issue = harness
    scripts = agent.config.deploy_dir / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "deploy.sh").write_text("unreviewed")
    with pytest.raises(ValueError, match="guarded_executor_mismatch"):
        HostIO(agent.config).execute(DIGEST, tmp_path / "log", PREVIOUS)
    assert not (tmp_path / "log").exists()


def test_crypto_possession_is_distinct_from_challenge(harness):
    agent, host, state, issue = harness
    challenge = {
        "schema": wire.CHALLENGE,
        "key_id": agent.signer.key_id,
        "deployment_ref": agent.config.deployment_ref,
        "nonce": wire.b64(b"x" * 32),
        "expires_at": (datetime.now(UTC) + timedelta(minutes=10))
        .isoformat()
        .replace("+00:00", "Z"),
    }
    envelope = wire.answer_challenge(
        challenge, agent.signer, PREVIOUS, now=datetime.now(UTC)
    )
    public = agent.signer.private_key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    assert (
        wire.verify(wire.POSSESSION, envelope, public)["observed_image_digest"]
        == PREVIOUS
    )
    with pytest.raises(wire.ContractError):
        wire.verify(wire.CHALLENGE, envelope, public)
    challenge["deployment_ref"] = "dotmac_sub/staging/other"
    with pytest.raises(wire.ContractError, match="host_mismatch"):
        wire.answer_challenge(challenge, agent.signer, PREVIOUS)


def test_artifact_verification_binds_identity_repo_digest_and_provenance(
    harness, monkeypatch
):
    import json
    import subprocess

    agent, host, state, issue = harness
    io = HostIO(agent.config)
    calls = []
    signature = [
        {
            "critical": {
                "image": {"docker-manifest-digest": DIGEST},
                "identity": {"docker-reference": IMAGE},
            }
        }
    ]
    statement = {
        "predicateType": "https://slsa.dev/provenance/v1",
        "subject": [{"name": IMAGE, "digest": {"sha256": DIGEST[7:]}}],
        "predicate": {
            "buildDefinition": {
                "buildType": "https://actions.github.io/buildtypes/workflow/v1",
                "resolvedDependencies": [
                    {
                        "uri": "git+https://github.com/michaelayoade/dotmac_sub@refs/heads/main",
                        "digest": {"gitCommit": REVISION},
                    }
                ],
                "externalParameters": {
                    "workflow": {
                        "repository": "https://github.com/michaelayoade/dotmac_sub",
                        "ref": "refs/heads/main",
                        "path": ".github/workflows/release-candidate.yml",
                    }
                },
            },
            "runDetails": {
                "builder": {"id": "https://github.com/actions/runner/github-hosted"}
            },
        },
    }

    def run(argv, *, timeout=60):
        calls.append(argv)
        output = (
            signature
            if argv[0] == "cosign"
            else [{"verificationResult": {"statement": statement}}]
        )
        return subprocess.CompletedProcess(argv, 0, json.dumps(output), "")

    monkeypatch.setattr(io, "run", run)
    assert io.artifact(IMAGE, DIGEST, REVISION) is None
    assert (
        "--certificate-identity" in calls[0] and "--certificate-oidc-issuer" in calls[0]
    )
    assert calls[0][-1] == IMAGE + "@" + DIGEST
    assert (
        "--repo" in calls[1]
        and "--cert-identity" in calls[1]
        and "--cert-oidc-issuer" in calls[1]
        and "--deny-self-hosted-runners" in calls[1]
    )
    dependency = statement["predicate"]["buildDefinition"]["resolvedDependencies"][0]
    dependency["digest"]["gitCommit"] = "f" * 40
    assert io.artifact(IMAGE, DIGEST, REVISION) == "attestation_identity_mismatch"
    dependency["digest"]["gitCommit"] = REVISION
    statement["subject"][0]["digest"]["sha256"] = "f" * 64
    assert io.artifact(IMAGE, DIGEST, REVISION) == "attestation_identity_mismatch"
    signature[0]["critical"]["identity"]["docker-reference"] = "ghcr.io/other/repo"
    assert io.artifact(IMAGE, DIGEST, REVISION) == "cosign_verify_failed"


def test_committed_cross_repository_conformance_vectors():
    from pathlib import Path

    vector = wire.read_json(
        (
            Path(__file__).parents[1] / "deploy/agent/vectors/conformance.json"
        ).read_bytes()
    )
    for kind, key_id, seed in (
        (wire.DESIRED, "test-cp", 1),
        (wire.APPLIED, "test-host", 2),
        (wire.POSSESSION, "test-host", 2),
    ):
        item = vector[kind]
        signer = Signer(
            key_id,
            item["payload"]["deployment_ref"],
            Ed25519PrivateKey.from_private_bytes(bytes([seed]) * 32),
        )
        public = signer.private_key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        assert wire.seal(kind, item["payload"], signer) == item["envelope"]
        assert wire.verify(kind, item["envelope"], public) == item["payload"]
    assert wire.plan_digest(vector[wire.DESIRED]["payload"]) == vector["plan_digest"]


def test_file_override_still_uses_same_verifier_and_result(harness):
    agent, host, state, issue = harness
    issue()
    raw = host.raw
    host.raw = None
    agent.poll(raw)
    assert agent.result.disposition == "applied"
    assert agent.result.executor_spawned
    assert agent.result.report_id == report(host)["report_id"]
    agent.poll(raw)
    assert agent.result.reason == "nonce_replay"
    assert not agent.result.executor_spawned


def test_running_daemon_rereads_replaced_override(harness, tmp_path, monkeypatch):
    agent, host, state, issue = harness
    issue()
    path = tmp_path / "desired.json"
    path.write_bytes(host.raw)
    checks, results = [], []
    monkeypatch.setattr(agent_module, "protected", checks.append)
    monkeypatch.setattr(host, "fetch", lambda: pytest.fail("unexpected CP fetch"))

    def next_poll(delay):
        results.append(agent.result.disposition)
        if len(results) == 2:
            raise KeyboardInterrupt
        replacement = path.with_suffix(".next")
        replacement.write_bytes(b"invalid envelope")
        replacement.replace(path)

    monkeypatch.setattr(agent_module.time, "sleep", next_poll)
    with pytest.raises(KeyboardInterrupt):
        agent.run(path)
    assert results == ["applied", "quarantined"]
    assert checks == [path, path]
    assert host.executions == [DIGEST]


@pytest.mark.parametrize(
    "case", ["missing", "owner", "writable", "symlink", "oversize"]
)
def test_override_failure_retries_without_cp(harness, tmp_path, monkeypatch, case):
    agent, host, state, issue = harness
    path = tmp_path / "desired.json"
    if case != "missing":
        path.write_bytes(b"x" * (1024 * 1024 + 1) if case == "oversize" else b"x")
    if case == "writable":
        path.chmod(0o666)
    elif case == "symlink":
        link = tmp_path / "desired-link"
        link.symlink_to(path)
        path = link
    lstat = Path.lstat
    monkeypatch.setattr(
        Path,
        "lstat",
        lambda path: SimpleNamespace(
            st_mode=lstat(path).st_mode, st_uid=1 if case == "owner" else 0
        ),
    )
    monkeypatch.setattr(host, "fetch", lambda: pytest.fail("unexpected CP fetch"))
    delays = []

    def stop(delay):
        delays.append(delay)
        raise KeyboardInterrupt

    monkeypatch.setattr(agent_module.time, "sleep", stop)
    with pytest.raises(KeyboardInterrupt):
        agent.run(path)
    assert delays == [min(agent.config.poll_seconds * 2, 300)]
    assert not host.executions and not host.posts


@pytest.mark.parametrize("once", [False, True])
@pytest.mark.parametrize("enabled", [False, True])
def test_desired_file_cli_requires_switch(
    harness, tmp_path, monkeypatch, once, enabled
):
    agent, host, state, issue = harness
    agent.config = replace(agent.config, allow_test_input=enabled)
    path = tmp_path / "desired.json"
    path.write_bytes(b"test envelope")
    monkeypatch.setattr(
        agent_module.sys,
        "argv",
        [str(path), "--config", str(path), "--desired-file", str(path)]
        + (["--once"] if once else []),
    )
    monkeypatch.setattr(Config, "load", lambda _: agent.config)
    monkeypatch.setattr(agent_module, "digest", lambda _: agent.config.artifact_digest)
    monkeypatch.setattr(Signer, "load", lambda _: agent.signer)
    monkeypatch.setattr(Keyring, "load", lambda *_: agent.keyring)
    monkeypatch.setattr(agent_module, "Agent", lambda *_: agent)
    monkeypatch.setattr(agent_module, "protected", lambda _: None)
    calls = []
    monkeypatch.setattr(agent, "poll", lambda raw: calls.append(raw))
    monkeypatch.setattr(agent, "run", lambda source: calls.append(source))
    if enabled:
        agent_module.main()
        assert calls == [b"test envelope" if once else path]
    else:
        with pytest.raises(ValueError, match="test_input_not_enabled"):
            agent_module.main()
        assert not calls


def test_registry_outage_leaves_nonce_unconsumed(harness):
    agent, host, state, issue = harness
    doc = issue()
    host.error = "registry_unreachable"
    agent.poll()
    assert not state.consumed(doc["nonce"])
    assert not host.executions and not host.posts
    assert agent.result.reason == "registry_unreachable"


def test_nonrunning_or_missing_worker_prevents_healthy_success():
    observed = snapshot(DIGEST)
    observed.health["background"]["containers"].pop("celery-worker")
    assert not observed.healthy()


@pytest.mark.parametrize("missing", [None, "app", "celery-worker"])
def test_staging_health_without_beat(missing):
    observed = snapshot(DIGEST)
    containers = observed.health["background"]["containers"]
    containers.pop("celery-beat")
    if missing:
        containers.pop(missing)
    assert observed.healthy() is (missing is None)
    containers["optional-service"] = "exited"
    assert not observed.healthy()


def test_unknown_signer_is_reported_without_executor(harness):
    agent, host, state, issue = harness
    issue()
    agent.keyring = Keyring(2, {})
    agent.poll()
    assert not host.executions and report(host)["reason"] == "unknown_signer_key"


def test_private_key_permissions_follow_effective_agent_uid(tmp_path, monkeypatch):
    from deploy.agent import hostio

    path = tmp_path / "synthetic-key-permission-fixture"
    path.write_bytes(b"not-a-private-key")
    path.chmod(0o600)
    hostio.protected(path, secret=True)
    path.chmod(0o640)
    with pytest.raises(ValueError, match="unsafe_file_permissions"):
        hostio.protected(path, secret=True)
    path.chmod(0o600)
    monkeypatch.setattr(hostio.os, "geteuid", lambda: path.stat().st_uid + 1)
    with pytest.raises(ValueError, match="unsafe_file_permissions"):
        hostio.protected(path, secret=True)


def test_root_runtime_has_no_fallback(tmp_path, monkeypatch):
    from deploy.agent import hostio

    monkeypatch.setattr(hostio.os, "geteuid", lambda: 0)
    with pytest.raises(ValueError, match="dedicated_agent_user_required"):
        Config.load(tmp_path / "absent-config.toml")


def test_explicit_auth_store_paths_are_the_only_auth_environment(harness, monkeypatch):
    agent, host, state, issue = harness
    monkeypatch.setenv("GH_TOKEN", "inherited-must-disappear")
    monkeypatch.setenv("GH_CONFIG_DIR", "/untrusted")
    monkeypatch.setenv("DOCKER_CONFIG", "/untrusted")
    environment = HostIO(agent.config).environment
    assert environment["GH_CONFIG_DIR"] == str(agent.config.gh_config_dir)
    assert environment["DOCKER_CONFIG"] == str(agent.config.docker_config_dir)
    assert environment["HOME"] == str(agent.config.state_dir)
    assert "GH_TOKEN" not in environment and "GITHUB_TOKEN" not in environment


def test_systemd_uses_dedicated_user_and_private_runtime():
    from pathlib import Path

    unit = (
        Path(__file__).parents[1] / "deploy/agent/systemd/dotmac-deploy-agent.service"
    ).read_text()
    for expected in (
        "User=dotmac-deploy",
        "Group=dotmac-deploy",
        "SupplementaryGroups=docker",
        "RuntimeDirectoryMode=0700",
        "StateDirectoryMode=0700",
    ):
        assert expected in unit
    assert "User=root" not in unit


def test_keyring_load_uses_frozen_contract_and_real_public_key(harness):
    agent, host, state, issue = harness
    public = agent.signer.private_key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    key = {
        "key_id": "registered-public-key",
        "public_key_b64": wire.b64(public),
        "revoked": False,
    }
    path = agent.config.keyring_file
    path.write_bytes(wire.canonical_json({"keyring_generation": 1, "keys": [key]}))
    loaded = Keyring.load(agent.config, state)
    assert loaded.generation == 1 and loaded.keys == {"registered-public-key": public}
    # Revocation needs a new generation and removes authority at startup.
    path.write_bytes(
        wire.canonical_json(
            {"keyring_generation": 2, "keys": [{**key, "revoked": True}]}
        )
    )
    assert Keyring.load(agent.config, state).keys == {}
    path.write_bytes(wire.canonical_json({"keyring_generation": 1, "keys": [key]}))
    with pytest.raises(wire.ContractError):
        Keyring.load(agent.config, state)


@pytest.mark.parametrize("generation", [True, 0, -1, "1"])
def test_keyring_rejects_invalid_generation_at_startup(harness, generation):
    agent, host, state, issue = harness
    agent.config.keyring_file.write_bytes(
        wire.canonical_json({"keyring_generation": generation, "keys": []})
    )
    with pytest.raises(wire.ContractError):
        Keyring.load(agent.config, state)
