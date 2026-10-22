"""End-to-end offline regressions for the independent proof review blockers."""

from __future__ import annotations

import copy
import json
import tempfile
import time
import unittest
from concurrent.futures import Future
from contextlib import nullcontext
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock, patch

from deploy.agent import contracts as c
from deploy.gate0 import host_evidence as host_module
from deploy.gate0 import proof34_transition as p34
from deploy.gate0 import run_proofs as cli
from deploy.gate0.host_evidence import EnrolledHost, NftUnitFixture
from tests.test_gate0_evidence_proofs import (
    D,
    R,
    complete_request,
    desired,
    observation,
    stamp,
)


def source_receipt(request: p34.ForwardRequest) -> p34.SourcePreparation:
    return {
        "owner": "deploy/gate0/run_proofs.py:observe-prepared-source",
        "action_ref": "source",
        "from_revision": "c" * 40,
        "to_revision": request["source_revision"],
        "from_image_digest": request["from_image_digest"],
        "to_image_digest": request["image_digest"],
        "source_tree": request["source_tree"],
        "build_run_id": request["build_run_id"],
        "evidence_run_id": request["evidence_run_id"],
        "prepared_at": stamp(-1),
    }


class KernelHarness:
    def __init__(self):
        self.enrollment = {
            "nft_element_time_unit": "seconds",
            "tools": {"nft": {"sha256": D}},
        }
        self.probe = None
        self.terminal_packets = 0
        self.insert_lost = False
        self.delete_lost = False
        self.foreign_flow = False
        self.old_ipv6_present = False
        self.cleanup_read_fails = False
        self.calls = []
        self.stream = Mock()
        self.stream.__enter__ = Mock(return_value=self.stream)
        self.stream.__exit__ = Mock(return_value=False)
        self.stream.getsockname.return_value = ("0.0.0.0", 40000)
        self.stream.connect.side_effect = self.connect
        self.observe = lambda: observation(D, 1, -1)
        self.result = lambda argv: SimpleNamespace(
            returncode=3 if argv[0] == "systemctl" else 1, stdout="inactive\n"
        )

    def fixture(self, directory: Path) -> None:
        fixture: NftUnitFixture = {
            "schema": "dotmac.nft-element-unit/1",
            "tool_sha256": D,
            "unit": "seconds",
            "known_timeout_ms": 10000,
            "observed_timeout_literal": 10,
            "source_revision": R,
            "source_sha256": D,
            "verification_run_id": "offline-unit-fixture",
        }
        c._shape(fixture, NftUnitFixture)
        path = directory / "fixture.json"
        raw = c.canonical_json(fixture)
        path.write_bytes(raw)
        host = object.__new__(EnrolledHost)
        with patch.object(host_module, "protected_path"):
            host.verify(path, host_module.raw_digest(raw))
        self.enrollment["nft_unit_fixture_sha256"] = host_module.raw_digest(raw)
        self.enrollment["nft_element_time_unit"] = fixture["unit"]

    def probe_rule(self, tag: str):
        return {
            "chain": "out",
            "handle": 2,
            "comment": tag,
            "expr": [
                {
                    "match": {
                        "op": "==",
                        "left": {"payload": {"protocol": "ip", "field": "daddr"}},
                        "right": "1.0.0.1",
                    }
                },
                {
                    "match": {
                        "op": "==",
                        "left": {"payload": {"protocol": "tcp", "field": "sport"}},
                        "right": 40000,
                    }
                },
                {
                    "match": {
                        "op": "==",
                        "left": {"payload": {"protocol": "tcp", "field": "dport"}},
                        "right": 443,
                    }
                },
                {
                    "match": {
                        "op": "in",
                        "left": {"ct": {"key": "state"}},
                        "right": "new",
                    }
                },
                {"counter": {"packets": 0, "bytes": 0}},
            ],
        }

    def run(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if argv[0] == "dig":
            family = argv[3]
            return (
                "rotation.invalid. 10 IN "
                + family
                + (" 9.9.9.9" if family == "A" else " 2001:4860:4860:0:0:0:0:8844")
            )
        if argv[:2] == ["nft", "insert"]:
            self.probe = self.probe_rule(json.loads(argv[-1]))
            if self.foreign_flow:
                self.probe["expr"][2]["match"]["right"] = 444
            if self.insert_lost:
                raise TimeoutError("insert reply lost after kernel accepted")
            return ""
        if argv[:2] == ["nft", "delete"]:
            self.assert_handle(argv[-1])
            self.probe = None
            if self.delete_lost:
                raise TimeoutError("delete reply lost after kernel accepted")
            return ""
        if argv[:2] == ["nft", "-j"]:
            if self.cleanup_read_fails and self.probe is not None:
                raise TimeoutError("cleanup read unavailable")
            body = [{"chain": {"name": "out", "hook": "output", "type": "filter"}}]
            if self.probe is not None:
                body.append({"rule": self.probe})
            body.append(
                {
                    "rule": {
                        "chain": "out",
                        "handle": 1,
                        "expr": [
                            {"counter": {"packets": self.terminal_packets}},
                            {"drop": None},
                        ],
                    }
                }
            )
            body.append(
                {
                    "set": {
                        "name": "egress_v4",
                        "elem": [
                            {"elem": {"val": "9.9.9.9", "timeout": 10, "expires": 9}}
                        ],
                    }
                }
            )
            entries = [
                {
                    "elem": {
                        "val": "2001:4860:4860:0000:0000:0000:0000:8844",
                        "timeout": 10,
                        "expires": 9,
                    }
                }
            ]
            if self.old_ipv6_present:
                entries.append(
                    {
                        "elem": {
                            "val": "2001:4860:4860:0000:0000:0000:0000:8888",
                            "timeout": 10,
                            "expires": 1,
                        }
                    }
                )
            body.append({"set": {"name": "egress_v6", "elem": entries}})
            return json.dumps({"nftables": body})
        return ""

    def assert_handle(self, handle):
        if self.probe is None or handle != str(self.probe["handle"]):
            raise AssertionError("only exact owned rule may be deleted")

    def connect(self, destination):
        if destination != ("1.0.0.1", 443):
            raise AssertionError("wrong flow")
        self.probe["expr"][-1]["counter"]["packets"] += 2
        self.terminal_packets += 2
        raise TimeoutError("terminal DROP")


class ReviewNetworkTests(unittest.TestCase):
    def full_network(self, harness: KernelHarness):
        request = complete_request()
        outcome = request["proofs"][3]
        expired = Future()
        expired.set_result(stamp(-48))
        start = p34.NetworkStart(
            copy.deepcopy(outcome["egress"]["before"]),
            stamp(-60),
            time.monotonic() - 60,
            Event(),
            expired,
            Mock(),
        )
        with tempfile.TemporaryDirectory() as directory:
            harness.fixture(Path(directory))
            with (
                patch.object(p34.socket, "socket", return_value=harness.stream),
                patch.object(p34.time, "sleep"),
            ):
                return p34.network_after(
                    harness,
                    "rotation.invalid",
                    start,
                    outcome["transition"],
                    "1.0.0.1",
                    443,
                )

    def test_full_rotated_network_path_parses_units_ipv6_and_removes_rule(self):
        harness = KernelHarness()
        result = self.full_network(harness)
        self.assertTrue(result["probe_rule_removed"])
        self.assertTrue(result["fresh_connection_failed"])
        self.assertEqual(result["after"][1]["address"], "2001:4860:4860::8844")
        self.assertEqual(result["after"][1]["kernel_timeout_ms"], 10000)
        self.assertEqual(result["counter_after"], 2)
        self.assertIsNone(harness.probe)
        self.assertEqual(
            sum(argv[:2] == ["nft", "insert"] for argv, _ in harness.calls), 1
        )
        self.assertEqual(
            sum(argv[:2] == ["nft", "delete"] for argv, _ in harness.calls), 1
        )
        self.assertTrue(
            any(
                kwargs.get("timeout") == 5
                for argv, kwargs in harness.calls
                if argv[:2] == ["nft", "-j"]
            )
        )

    def test_old_expanded_ipv6_alias_still_present_cannot_pass(self):
        harness = KernelHarness()
        harness.old_ipv6_present = True
        with self.assertRaises(c.ContractError):
            self.full_network(harness)
        self.assertFalse(
            any(argv[:2] == ["nft", "insert"] for argv, _ in harness.calls)
        )

    def test_ambiguous_insert_is_reconciled_and_owned_rule_removed(self):
        harness = KernelHarness()
        harness.insert_lost = True
        with (
            patch.object(p34.socket, "socket", return_value=harness.stream),
            patch.object(p34.time, "sleep"),
        ):
            with self.assertRaises(TimeoutError):
                p34.dropped_fresh_flow(harness, "1.0.0.1", 443)
        self.assertIsNone(harness.probe)
        self.assertTrue(any(argv[:2] == ["nft", "delete"] for argv, _ in harness.calls))

    def test_foreign_flow_with_matching_tag_is_never_deleted(self):
        harness = KernelHarness()
        harness.insert_lost = harness.foreign_flow = True
        with (
            patch.object(p34.socket, "socket", return_value=harness.stream),
            patch.object(p34.time, "sleep"),
        ):
            with self.assertRaises(p34.ProbeCleanupUnproved):
                p34.dropped_fresh_flow(harness, "1.0.0.1", 443)
        self.assertIsNotNone(harness.probe)
        self.assertFalse(
            any(argv[:2] == ["nft", "delete"] for argv, _ in harness.calls)
        )

    def test_unavailable_cleanup_readback_is_bounded_and_never_success(self):
        harness = KernelHarness()
        harness.insert_lost = harness.cleanup_read_fails = True
        with (
            patch.object(p34.socket, "socket", return_value=harness.stream),
            patch.object(p34.time, "sleep"),
        ):
            with self.assertRaises(p34.ProbeCleanupUnproved):
                p34.dropped_fresh_flow(harness, "1.0.0.1", 443)
        cleanup_reads = [
            kwargs
            for argv, kwargs in harness.calls
            if argv[:2] == ["nft", "-j"] and kwargs.get("timeout") == 5
        ]
        self.assertEqual(len(cleanup_reads), 3)
        self.assertIsNotNone(harness.probe)
        self.assertFalse(
            any(argv[:2] == ["nft", "delete"] for argv, _ in harness.calls)
        )

    def test_lost_delete_reply_requires_actual_absence_before_success(self):
        harness = KernelHarness()
        harness.delete_lost = True
        result = self.full_network(harness)
        self.assertTrue(result["probe_rule_removed"])
        self.assertIsNone(harness.probe)


class ReviewIssuanceTests(unittest.TestCase):
    def test_ambiguous_or_invalid_response_never_restarts_daemon(self):
        signed, public = desired()
        doc = c.validate_desired(
            c.read_json(c.unb64(cast(str, c.read_json(signed)["payload_b64"])))
        )
        request = cast(
            p34.ForwardRequest,
            dict(
                c.plan_document(doc),
                deployment_ref=doc["deployment_ref"],
                approvals=doc["approvals"],
            ),
        )
        for case in ("timeout", "malformed", "wrong_plan", "consumed"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                proof = SimpleNamespace(
                    config=SimpleNamespace(
                        deployment_ref=doc["deployment_ref"], state_dir=Path(directory)
                    ),
                    enrollment={"cp_public_b64": c.b64(public)},
                    control=Mock(),
                    audit=lambda: nullcontext(),
                    observe=lambda: observation(doc["from_image_digest"], 0, -20),
                )
                candidate_request = copy.deepcopy(request)
                returned = signed
                if case == "consumed":
                    path = (
                        Path(directory)
                        / "nonces"
                        / (p34.raw_digest(doc["nonce"].encode())[7:] + ".json")
                    )
                    path.parent.mkdir()
                    path.write_bytes(c.canonical_json(doc))
                if case == "wrong_plan":
                    candidate_request["image_digest"] = "sha256:" + "e" * 64
                if case == "malformed":
                    returned = b"{"
                with (
                    patch.object(p34, "source_ready"),
                    patch.object(p34, "runners_stopped"),
                    patch.object(
                        p34,
                        "issue_desired",
                        side_effect=TimeoutError() if case == "timeout" else None,
                        return_value=returned,
                    ),
                ):
                    with self.assertRaises(p34.ForwardIssuanceUncertain):
                        p34.forward(
                            proof,
                            Mock(),
                            candidate_request,
                            "command",
                            source_receipt(candidate_request),
                            ["source"],
                        )
                self.assertEqual(
                    proof.control.call_args_list, [unittest.mock.call("stop")]
                )

    def test_stage_retains_transport_fail_without_fabricated_transition(self):
        signed, _ = desired()
        doc = c.validate_desired(
            c.read_json(c.unb64(cast(str, c.read_json(signed)["payload_b64"])))
        )
        request = cast(
            p34.ForwardRequest,
            dict(
                c.plan_document(doc),
                deployment_ref=doc["deployment_ref"],
                approvals=doc["approvals"],
            ),
        )
        preparation: p34.SourcePreparation = {
            "owner": "deploy/gate0/run_proofs.py:observe-prepared-source",
            "action_ref": "source",
            "from_revision": "c" * 40,
            "to_revision": R,
            "from_image_digest": doc["from_image_digest"],
            "to_image_digest": D,
            "source_tree": R,
            "build_run_id": 1,
            "evidence_run_id": 2,
            "prepared_at": stamp(-1),
        }
        context = {
            key: value
            for key, value in complete_request().items()
            if key not in ("command_id", "proofs")
        }
        context["action_refs"] = ["source"]
        reference: cli.ArtifactRef = {"path": "/nonexistent/public.json", "sha256": D}
        proof = SimpleNamespace(run=Mock(side_effect=[R, ""]))
        plan: cli.RunPlan = {
            "bad_signature": request,
            "rollback": request,
            "forward": request,
            "source_preparation": reference,
            "proof1_plan": reference,
            "proof5": reference,
            "source_action_ref": "source",
            "dns_hostname": "rotation.invalid",
            "disallowed_address": "1.0.0.1",
            "disallowed_port": 443,
            "context": cast(cli.GateContext, context),
        }
        start = Mock()
        with (
            patch.object(cli, "artifact", return_value=c.canonical_json(preparation)),
            patch.object(cli, "network_before", return_value=start),
            patch.object(cli, "forward", side_effect=p34.ForwardIssuanceUncertain()),
        ):
            third, fourth = cli.proof34(proof, Mock(), plan)
        for outcome in (third, fourth):
            self.assertEqual(outcome["outcome"], "fail")
            self.assertEqual(outcome["failure_code"], "transport_failed")
            self.assertEqual(outcome["cleanup"], "fail")
            self.assertIsNone(outcome["transition"])
        start.close.assert_called_once()


class ReviewPublicationTests(unittest.TestCase):
    def test_published_fail_does_not_require_or_start_active_daemon(self):
        request = complete_request()
        request["proofs"] = [cli.empty_outcome(3)]
        receipt = {
            "run_id": request["run_id"],
            "command_sha256": c.digest(request),
            "ledger_commit": R,
            "evidence_sha256": c.digest(request),
            "published": True,
            "verdict": "FAIL",
            "evidence": request,
        }
        plan = {
            "context": {
                key: value
                for key, value in request.items()
                if key not in ("command_id", "proofs")
            }
        }
        config = SimpleNamespace(
            host_key_id=request["host_key_id"], cp_url="https://cp.invalid"
        )
        host = SimpleNamespace(
            config=config,
            enrollment={"proof_source_revision": request["tool_revision"]},
        )
        args = SimpleNamespace(
            command="publish",
            plan=Path("/nonexistent/plan.json"),
            command_id=request["command_id"],
        )
        with (
            patch("argparse.ArgumentParser.parse_args", return_value=args),
            patch.object(cli, "load_plan", return_value=plan),
            patch.object(cli.Config, "load", return_value=config),
            patch.object(cli, "EnrolledHost", return_value=host) as readonly,
            patch.object(cli, "DaemonProof") as daemon,
            patch.dict(cli.os.environ, {"DOTMAC_CP_AUTHORIZATION": "Bearer fixture"}),
            patch.object(cli, "publish", side_effect=cli.PublishedFailure(receipt)),
            patch.object(cli, "store") as store,
            patch("builtins.print") as output,
        ):
            self.assertEqual(cli.main(), 1)
        readonly.assert_called_once_with(config)
        daemon.assert_not_called()
        store.assert_called_once_with(request["run_id"], "receipt", receipt)
        self.assertEqual(json.loads(output.call_args.args[0])["outcome"], "fail")
