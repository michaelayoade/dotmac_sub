"""Offline cryptographic and process-boundary regressions; never executes host tools."""

from __future__ import annotations

import copy
import errno
import json
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock, patch
from uuid import uuid4

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from deploy.agent import contracts as c
from deploy.agent.agent import Signer
from deploy.agent.hostio import IMAGE, LIVE, READY
from deploy.gate0 import evidence as e
from deploy.gate0 import host_evidence as host_module
from deploy.gate0 import proof2_refusals as p2
from deploy.gate0 import proof34_transition as p34
from deploy.gate0.host_evidence import EnrolledHost
from deploy.gate0.proof2_refusals import DaemonProof, corrupt_signature

D = "sha256:" + "a" * 64
R = "b" * 40


def stamp(offset: int = 0) -> str:
    return (
        (datetime.now(UTC) + timedelta(seconds=offset))
        .isoformat()
        .replace("+00:00", "Z")
    )


def desired() -> tuple[bytes, bytes]:
    signer = Signer(
        "cp-1", "dotmac_sub/staging/dotmac-sub-staging", Ed25519PrivateKey.generate()
    )
    files = {"docker-compose.yml": D, "docker-compose.override.yml": D}
    doc = cast(
        c.DesiredState,
        {
            "schema": c.DESIRED,
            "product": "dotmac_sub",
            "environment": "staging",
            "host": "dotmac-sub-staging",
            "deployment_ref": signer.deployment_ref,
            "image_reference": IMAGE,
            "image_digest": D,
            "from_image_digest": "sha256:" + "f" * 64,
            "source_revision": R,
            "source_tree": R,
            "product_manifest_digest": D,
            "build_run_id": 1,
            "evidence_run_id": 2,
            "config_files": files,
            "config_digest": c.digest(files),
            "migration_policy": {"mode": "upgrade_heads"},
            "approvals": [{"environment": "staging", "actor": "fixture", "run_id": 2}],
            "nonce": c.b64(bytes(range(32))),
            "issued_at": stamp(-10),
            "expires_at": stamp(600),
            "signer_key_id": signer.key_id,
        },
    )
    doc["plan_digest"] = c.plan_digest(doc)
    public = signer.private_key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return c.canonical_json(c.seal(c.DESIRED, doc, signer)), public


def observation(image: str, count: int, offset: int) -> e.HostObservation:
    return {
        "observed_at": stamp(offset),
        "executor_count": count,
        "observed": {
            "running_image": IMAGE + "@" + image,
            "pinned_image": IMAGE + "@" + image,
            "running_revision_label": R,
            "pinned_git_sha": R,
            "config_files": {"docker-compose.yml": D, "docker-compose.override.yml": D},
            "migration_heads": ["head"],
        },
        "health": {
            "live": {"url": LIVE, "status": 200, "checked_at": stamp(offset - 1)},
            "ready": {
                "url": READY,
                "status": 200,
                "checked_at": stamp(offset - 1),
                "body_status": "ready",
            },
            "background": {
                "celery_inspect_ok": True,
                "restart_counts_zero": True,
                "containers": {"app": "running", "celery-worker": "running"},
            },
        },
    }


def transition_request() -> e.GateEvidence:
    outcome = e.empty_outcome(3)
    outcome.update(
        outcome="pass",
        failure_code="none",
        cleanup="pass",
        transition={
            "nonce": "n" * 32,
            "desired_sha256": D,
            "source_preparation_sha256": D,
            "report_id": str(uuid4()),
            "report_sha256": D,
            "receipt_sha256": D,
            "ledger_commit": R,
            "before": observation("sha256:" + "f" * 64, 0, -20),
            "after": observation(D, 1, -10),
            "signatures_verified": True,
            "runners_stopped": True,
        },
    )
    return {
        "command_id": str(uuid4()),
        "run_id": str(uuid4()),
        "phase": "initial",
        "prior_run_id": None,
        "source_revision": R,
        "source_tree": R,
        "tool_revision": R,
        "host_key_id": "host-1",
        "action_refs": ["forward"],
        "deployment_ref": "dotmac_sub/staging/dotmac-sub-staging",
        "deadline": stamp(3600),
        "proofs": [outcome],
    }


def complete_request() -> e.GateEvidence:
    request = transition_request()
    first, second, fourth, fifth = (e.empty_outcome(n) for n in (1, 2, 4, 5))
    for proof in (first, second, fourth, fifth):
        proof.update(outcome="pass", failure_code="none", cleanup="pass")
    first["oidc"] = {
        "main_run_id": 1,
        "negative_run_id": 2,
        "main_sha256": D,
        "negative_sha256": D,
        "main_accepted": True,
        "negative_refused": True,
    }
    for case in ("bad_signature", "nonce_replay", "rollback_digest"):
        second["refusals"].append(
            {
                "case": case,
                "submitted_sha256": D,
                "nonce": "n" * 32,
                "report_id": str(uuid4()),
                "report_sha256": D,
                "classification": "accepted",
                "expected_code": case,
                "observed_code": case,
                "journal_start": "start",
                "journal_end": "end",
                "normal_poll_journal_start": "normal-start",
                "normal_poll_journal_end": "normal-end",
                "cleanup_observed_at": stamp(-24),
                "ledger_commit": R,
                "before": observation(D, 0, -30),
                "after": observation(D, 0, -25),
            }
        )
    transition = request["proofs"][0]["transition"]
    fourth["transition"] = copy.deepcopy(transition)
    fourth["egress"] = {
        "hostname": "rotation.invalid",
        "started_at": stamp(-60),
        "old_expiry_observed_at": stamp(-48),
        "finished_at": stamp(-5),
        "transition_report_id": transition["report_id"],
        "transition_report_sha256": transition["report_sha256"],
        "probe_rule_handle": 2,
        "probe_source_port": 40000,
        "probe_rule_removed": True,
        "before": [
            {
                "family": "A",
                "address": "8.8.8.8",
                "ttl": 10,
                "kernel_timeout_ms": 10000,
                "kernel_expires_ms": 9000,
            },
            {
                "family": "AAAA",
                "address": "2001:4860:4860::8888",
                "ttl": 10,
                "kernel_timeout_ms": 10000,
                "kernel_expires_ms": 9000,
            },
        ],
        "after": [
            {
                "family": "A",
                "address": "9.9.9.9",
                "ttl": 10,
                "kernel_timeout_ms": 10000,
                "kernel_expires_ms": 9000,
            },
            {
                "family": "AAAA",
                "address": "2001:4860:4860::8844",
                "ttl": 10,
                "kernel_timeout_ms": 10000,
                "kernel_expires_ms": 9000,
            },
        ],
        "elapsed_seconds": 55,
        "kernel_rules_sha256": D,
        "terminal_drop": True,
        "counter_before": 0,
        "counter_after": 1,
        "disallowed_address": "1.0.0.1",
        "disallowed_port": 443,
        "fresh_connection_failed": True,
        "allowed_deployment_succeeded": True,
    }
    fifth["restore"] = {
        "source_identity": "source",
        "test_host_identity": "dedicated-test",
        "bundle_sha256": D,
        "capture_sha256": D,
        "capture_started_at": stamp(-40),
        "capture_finished_at": stamp(-35),
        "source_inventory_sha256": D,
        "restored_inventory_sha256": D,
        "no_ddl_enforced": True,
        "source_parity": True,
        "scratch_absent": True,
    }
    request["proofs"] = [first, second, request["proofs"][0], fourth, fifth]
    return request


class EvidenceTests(unittest.TestCase):
    def test_signature_fixture_preserves_join_and_only_changes_signature(self):
        raw, public = desired()
        damaged = corrupt_signature(raw, public)
        original, changed = c.read_json(raw), c.read_json(damaged)
        self.assertEqual(original["payload_b64"], changed["payload_b64"])
        self.assertEqual(
            {k: v for k, v in original.items() if k != "signature_b64"},
            {k: v for k, v in changed.items() if k != "signature_b64"},
        )
        with self.assertRaisesRegex(c.ContractError, "bad_signature"):
            c.verify(c.DESIRED, cast(c.Envelope, changed), public)
        with self.assertRaises(c.ContractError):
            corrupt_signature(damaged, public)

    def test_closed_schema_and_non_noop_transition(self):
        request = transition_request()
        e.validate(request)
        for change in ("extra", "noop", "spawn", "cleanup", "expired", "prior"):
            bad = copy.deepcopy(request)
            transition = bad["proofs"][0]["transition"]
            if change == "extra":
                bad["credential"] = "forbidden"
            elif change == "noop":
                transition["after"]["observed"]["running_image"] = transition["before"][
                    "observed"
                ]["running_image"]
                transition["after"]["observed"]["pinned_image"] = transition["before"][
                    "observed"
                ]["running_image"]
            elif change == "spawn":
                transition["after"]["executor_count"] = 2
            elif change == "cleanup":
                bad["proofs"][0]["cleanup"] = "fail"
            elif change == "expired":
                bad["deadline"] = stamp(-1)
            else:
                bad["phase"] = "next_morning"
            with self.subTest(change=change), self.assertRaises(c.ContractError):
                e.validate(bad, at=datetime.now(UTC))

    def test_second_transition_must_be_identical(self):
        request = transition_request()
        duplicate = copy.deepcopy(request["proofs"][0])
        duplicate["proof"] = 4
        duplicate["transition"]["report_id"] = str(uuid4())
        request["proofs"].append(duplicate)
        with self.assertRaises(c.ContractError):
            e.validate(request)

    def test_missing_failure_evidence_is_representable(self):
        request = transition_request()
        request["proofs"] = [e.empty_outcome(5)]
        e.validate(request)

    def test_publish_requires_authoritative_exact_readback(self):
        request = complete_request()
        body = c.canonical_json(request)
        receipt = {
            "run_id": request["run_id"],
            "command_sha256": e.raw_digest(body),
            "ledger_commit": R,
            "evidence_sha256": e.raw_digest(body),
            "published": True,
            "verdict": "PASS",
            "evidence": request,
        }
        client = e.CPClient("https://cp.invalid", "Bearer fixture")
        with patch.object(
            client, "request", return_value=c.canonical_json(receipt)
        ) as transport:
            self.assertEqual(client.publish(request), receipt)
            self.assertEqual(transport.call_count, 2)
            self.assertEqual(transport.call_args_list[0], transport.call_args_list[1])
        changed = dict(receipt, ledger_commit="f" * 40)
        with (
            patch.object(
                client,
                "request",
                side_effect=[c.canonical_json(receipt), c.canonical_json(changed)],
            ),
            self.assertRaises(c.ContractError),
        ):
            client.publish(request)

    def test_attempted_restore_unknown_facts_are_fail_only(self):
        request = complete_request()
        outcome = request["proofs"][4]
        outcome.update(outcome="fail", failure_code="cleanup_failed", cleanup="fail")
        outcome["restore"].update(
            restored_inventory_sha256=None, source_parity=None, scratch_absent=None
        )
        e.validate(request)
        outcome.update(outcome="pass", failure_code="none", cleanup="pass")
        with self.assertRaises(c.ContractError):
            e.validate(request)

    def test_origin_and_redirect_refusal(self):
        for origin in (
            "http://cp.invalid",
            "https://x@y.invalid",
            "https://cp.invalid/path",
            "https://cp.invalid?x=1",
        ):
            with self.subTest(origin=origin), self.assertRaises(c.ContractError):
                e.CPClient(origin, "Bearer fixture")
        self.assertIsNone(
            e.NoRedirect().redirect_request(
                None, None, 302, "", {}, "https://other.invalid"
            )
        )
        client = e.CPClient("https://cp.invalid", "Bearer fixture")
        with patch("urllib.request.build_opener") as opener:
            opener.return_value.open.side_effect = OSError("private-body")
            with self.assertRaisesRegex(ValueError, "^gate_evidence_transport_failed$"):
                client.request("gate-evidence", b"{}")

    def test_published_fail_is_read_back_then_raises_typed_failure(self):
        request = transition_request()
        request["proofs"] = [e.empty_outcome(3)]
        body = c.canonical_json(request)
        receipt = {
            "run_id": request["run_id"],
            "command_sha256": e.raw_digest(body),
            "ledger_commit": R,
            "evidence_sha256": e.raw_digest(body),
            "published": True,
            "verdict": "FAIL",
            "evidence": request,
        }
        client = e.CPClient("https://cp.invalid", "Bearer fixture")
        with patch.object(
            client, "request", return_value=c.canonical_json(receipt)
        ) as transport:
            with self.assertRaises(e.PublishedFailure) as failure:
                client.publish(request)
            self.assertEqual(failure.exception.receipt, receipt)
            self.assertEqual(transport.call_count, 2)

    def test_partial_pass_receipt_cannot_become_terminal_pass(self):
        request = transition_request()
        body = c.canonical_json(request)
        receipt = {
            "run_id": request["run_id"],
            "command_sha256": e.raw_digest(body),
            "ledger_commit": R,
            "evidence_sha256": e.raw_digest(body),
            "published": True,
            "verdict": "PASS",
            "evidence": request,
        }
        client = e.CPClient("https://cp.invalid", "Bearer fixture")
        with (
            patch.object(client, "request", return_value=c.canonical_json(receipt)),
            self.assertRaises(c.ContractError),
        ):
            client.publish(request)

    def test_stale_daemon_invocation_cannot_satisfy_refusal(self):
        proof = object.__new__(DaemonProof)
        stale = json.dumps(
            {
                "_SYSTEMD_INVOCATION_ID": "a" * 32,
                "MESSAGE": "refused reason=nonce_replay",
                "__CURSOR": "stale",
            }
        )
        proof.run = Mock(side_effect=[stale, "b" * 32])
        with (
            patch(
                "deploy.gate0.proof2_refusals.time.monotonic", side_effect=[0, 1, 151]
            ),
            patch("deploy.gate0.proof2_refusals.time.sleep"),
        ):
            with self.assertRaisesRegex(ValueError, "daemon_observation_timeout"):
                proof.wait_refusal("before", "nonce_replay")


class SourcePreparationTests(unittest.TestCase):
    def test_observer_only_reads_and_binds_real_prepared_head(self):
        raw, _ = desired()
        doc = c.validate_desired(
            c.read_json(c.unb64(cast(str, c.read_json(raw)["payload_b64"])))
        )
        request = cast(
            p34.ForwardRequest,
            dict(
                c.plan_document(doc),
                deployment_ref=doc["deployment_ref"],
                approvals=doc["approvals"],
            ),
        )
        current = SimpleNamespace(
            healthy=lambda: True,
            running_revision_label="c" * 40,
            running_image=IMAGE + "@" + doc["from_image_digest"],
        )
        calls = []

        def command(argv):
            calls.append(argv)
            if argv[1:] in (["rev-parse", "HEAD"], ["rev-parse", "HEAD^{tree}"]):
                return R
            return ""

        proof = SimpleNamespace(
            run=command, host=SimpleNamespace(observe=lambda: current)
        )
        receipt = p34.observe_prepared_source(proof, request, "source-preparation")
        self.assertEqual(receipt["from_revision"], "c" * 40)
        self.assertEqual(receipt["to_revision"], R)
        self.assertTrue(
            all(
                command[1] in ("rev-parse", "status", "merge-base") for command in calls
            )
        )
        with self.assertRaises(c.ContractError):
            p34.source_ready(proof, request, receipt, ["foreign-action"])


class CleanupTests(unittest.TestCase):
    def test_override_restores_normal_poll_even_after_body_error(self):
        self.check_cleanup(False)

    def test_foreign_override_mutation_is_preserved_and_blocks_restart(self):
        self.check_cleanup(True)

    def check_cleanup(self, foreign: bool):
        raw, public = desired()
        doc = c.read_json(c.unb64(cast(str, c.read_json(raw)["payload_b64"])))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.toml"
            config.write_text("allow_test_input = false\n")
            dropin = root / "unit.d/90-gate0.conf"
            input_file, temp_config = root / "desired.json", root / "test.toml"
            state = root / "state"
            (state / "nonces").mkdir(parents=True)
            (
                state / "nonces" / (e.raw_digest(doc["nonce"].encode())[7:] + ".json")
            ).write_bytes(c.canonical_json(doc))
            proof = object.__new__(DaemonProof)
            proof.config = SimpleNamespace(state_dir=state)
            proof.enrollment = {"cp_public_b64": c.b64(public)}
            proof.host = SimpleNamespace(fetch=lambda: raw)
            proof.control = Mock()
            proof.cursor = Mock(return_value="normal-start")
            proof.wait_refusal = Mock(return_value="normal-end")

            def command(argv, **kwargs):
                if argv[1:] == ["rev-parse", "HEAD"]:
                    return R
                if "--abbrev-ref" in argv:
                    return "main"
                if "status" in argv:
                    return ""
                if "is-active" in argv:
                    return "active"
                return "launcher.sh"

            proof.run = command
            with (
                patch.multiple(
                    p2,
                    CONFIG=config,
                    DROPIN=dropin,
                    INPUT=input_file,
                    TEMP_CONFIG=temp_config,
                ),
                patch.object(p2, "protected_path"),
            ):
                with self.assertRaises(c.ContractError if foreign else RuntimeError):
                    with proof.override(raw):
                        if foreign:
                            input_file.write_bytes(b"foreign")
                        else:
                            raise RuntimeError("body failed")
            self.assertFalse(dropin.exists())
            self.assertFalse(temp_config.exists())
            self.assertEqual(config.read_text(), "allow_test_input = false\n")
            if foreign:
                self.assertEqual(input_file.read_bytes(), b"foreign")
                self.assertEqual(proof.control.call_args.args, ("daemon-reload",))
            else:
                self.assertFalse(input_file.exists())
                proof.wait_refusal.assert_called_once_with(
                    "normal-start", "nonce_replay"
                )
                self.assertEqual(proof.normal_poll[:2], ("normal-start", "normal-end"))


class KernelTests(unittest.TestCase):
    def test_terminal_accept_and_log_only_are_refused(self):
        for expr in (
            [{"counter": {"packets": 1}}, {"accept": None}],
            [{"log": {"prefix": "drop"}}],
        ):
            proof = SimpleNamespace(
                run=lambda argv, expr=expr, **kwargs: json.dumps(
                    {
                        "nftables": [
                            {
                                "chain": {
                                    "name": "out",
                                    "hook": "output",
                                    "type": "filter",
                                }
                            },
                            {"rule": {"chain": "out", "handle": 1, "expr": expr}},
                        ]
                    }
                )
            )
            with self.subTest(expr=expr), self.assertRaises(c.ContractError):
                p34.kernel(proof)

    def test_stock_three_times_ttl_is_refused(self):
        proof = SimpleNamespace(
            run=lambda argv: (
                "rotation.invalid. 10 IN "
                + argv[3]
                + (" 8.8.8.8" if argv[3] == "A" else " 2001:4860:4860::8888")
            )
        )
        with patch.object(
            p34,
            "kernel",
            return_value=(
                b"{}",
                [],
                {"8.8.8.8": (30000, 29000), "2001:4860:4860::8888": (30000, 29000)},
            ),
        ):
            with self.assertRaises(c.ContractError):
                p34.dns(proof, "rotation.invalid")

    def test_kernel_seconds_are_normalized_before_ttl_comparison(self):
        body = {
            "nftables": [
                {"chain": {"name": "out", "hook": "output", "type": "filter"}},
                {
                    "rule": {
                        "chain": "out",
                        "handle": 1,
                        "expr": [{"counter": {"packets": 0}}, {"drop": None}],
                    }
                },
                {
                    "set": {
                        "name": "egress_v4",
                        "elem": [
                            {"elem": {"val": "8.8.8.8", "timeout": 30, "expires": 29}}
                        ],
                    }
                },
            ]
        }
        proof = SimpleNamespace(
            run=lambda argv, **kwargs: json.dumps(body),
            enrollment={"nft_element_time_unit": "seconds"},
        )
        self.assertEqual(p34.kernel(proof)[2]["8.8.8.8"], (30000, 29000))

    def test_counter_for_another_flow_is_refused(self):
        rule = {"expr": [{"counter": {"packets": 12}}]}
        with self.assertRaises(c.ContractError):
            p34.probe_packets(rule, "8.8.8.8", 40000, 443)

    def test_probe_cleanup_runs_after_connection_failure(self):
        terminal = {"handle": 1, "expr": [{"counter": {"packets": 1}}, {"drop": None}]}
        proof = SimpleNamespace(run=Mock(return_value=""))
        rules = [terminal]

        def read_kernel(host, **kwargs):
            if proof.run.call_count and not any(
                call.args[0][1] == "delete" for call in proof.run.call_args_list
            ):
                return b"{}", [dict(handle=2, comment="tag", expr=[]), terminal], {}
            return b"{}", rules, {}

        stream = Mock()
        stream.__enter__ = Mock(return_value=stream)
        stream.__exit__ = Mock(return_value=False)
        stream.getsockname.return_value = ("0.0.0.0", 40000)
        stream.connect.side_effect = ConnectionRefusedError(
            errno.ECONNREFUSED, "refused"
        )
        with (
            patch.object(p34, "kernel", side_effect=read_kernel),
            patch.object(p34, "probe_packets", return_value=0),
            patch.object(p34.socket, "socket", return_value=stream),
            patch.object(p34, "uuid4", return_value=SimpleNamespace(hex="")),
        ):
            # tag must match the actual generated finite rule comment.
            original = read_kernel

            def tagged(host, **kwargs):
                raw, current, addresses = original(host, **kwargs)
                return (
                    raw,
                    [
                        dict(rule, comment="dotmac_gate0_")
                        if rule.get("handle") == 2
                        else rule
                        for rule in current
                    ],
                    addresses,
                )

            with (
                patch.object(p34, "kernel", side_effect=tagged),
                self.assertRaises(ConnectionRefusedError),
            ):
                p34.dropped_fresh_flow(proof, "8.8.8.8", 443)
        self.assertTrue(
            any(call.args[0][1] == "delete" for call in proof.run.call_args_list)
        )


class HostTransportTests(unittest.TestCase):
    def host(self):
        host = object.__new__(EnrolledHost)
        host.enrollment = {
            "ledger_origin": "https://github.com/example/ledger.git",
            "ledger_config_sha256": D,
            "tools": {"git": {"path": "/usr/bin/git", "sha256": D}},
        }
        host.config = SimpleNamespace(deploy_dir=Path("/nonexistent"))
        host.host = SimpleNamespace(environment={"HOME": "/unsafe"})
        host.verify = Mock()
        return host

    def test_changed_binary_is_refused_before_subprocess(self):
        host = self.host()
        host.verify.side_effect = c.ContractError()
        with (
            patch.object(host_module.subprocess, "run") as process,
            self.assertRaises(c.ContractError),
        ):
            host.result(["git", "status"])
        process.assert_not_called()

    def test_git_transport_drops_untrusted_global_config(self):
        host = self.host()
        with patch.object(
            host_module.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 0, "", ""),
        ) as process:
            host.result(["git", "status"])
        self.assertEqual(process.call_args.kwargs["env"]["HOME"], "/nonexistent")
        self.assertEqual(
            process.call_args.kwargs["env"]["GIT_CONFIG_GLOBAL"], "/dev/null"
        )
        self.assertIn("core.hooksPath=/dev/null", process.call_args.args[0])

    def test_foreign_ledger_remote_is_refused_before_fetch(self):
        host = self.host()
        host.run = Mock(return_value="https://github.com/foreign/ledger.git")
        with (
            patch.object(host_module, "protected_path"),
            self.assertRaises(c.ContractError),
        ):
            host.published({"desired/dotmac_sub/staging/host/nonce.json": b"{}"})
        self.assertEqual(host.run.call_count, 1)

    def test_async_record_is_retried_then_read_at_published_commit(self):
        host = self.host()
        path, raw = "desired/dotmac_sub/staging/host/nonce.json", b"{}"

        def command(argv, **kwargs):
            if "get-url" in argv:
                return host.enrollment["ledger_origin"]
            if "rev-parse" in argv:
                return R
            if "ls-remote" in argv:
                return R + "\trefs/heads/main"
            return ""

        host.run = Mock(side_effect=command)
        host.result = Mock(
            side_effect=[
                subprocess.CompletedProcess([], 128, "", ""),
                subprocess.CompletedProcess([], 0, "{}", ""),
            ]
        )
        with (
            patch.object(host_module, "protected_path"),
            patch.object(host_module.time, "sleep"),
        ):
            self.assertEqual(host.published({path: raw}), R)
        self.assertEqual(host.result.call_count, 2)
        self.assertIn(R + ":" + path, host.result.call_args.args[0])
