"""Current-run handoff and actual signed Agent fixture consumption, entirely offline."""

from __future__ import annotations

import copy
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from deploy.agent import contracts as c
from deploy.agent.agent import Agent
from deploy.agent.hostio import IMAGE
from deploy.agent.state import State
from deploy.gate0 import run_proofs as cli
from deploy.gate0.evidence import empty_outcome, raw_digest
from deploy.gate0.proof2_refusals import corrupt_signature
from tests.test_deploy_agent import DIGEST, PREVIOUS, harness
from tests.test_gate0_evidence_proofs import complete_request, observation, stamp


class SameRunReplayTests(unittest.TestCase):
    def test_actual_agent_consumes_both_fixtures_and_normal_poll_preserves_report(self):
        for case in ("bad_signature", "rollback_digest"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                agent, host, state, issue = harness.__wrapped__(Path(directory))
                host.current = replace(
                    host.current,
                    running_image=IMAGE + "@" + DIGEST,
                    pinned_image=IMAGE + "@" + DIGEST,
                )
                doc = issue(
                    from_image_digest=DIGEST,
                    image_digest=PREVIOUS if case == "rollback_digest" else DIGEST,
                )
                normal = host.raw
                if case == "bad_signature":
                    public = agent.keyring.keys["desired-1"]
                    submitted = corrupt_signature(normal, public)
                else:
                    host.preflight_reason = "rollback_digest"
                    submitted = normal
                agent.poll(submitted)
                self.assertTrue(state.consumed(doc["nonce"]))
                nonce_path = state.nonce_path(doc["nonce"])
                self.assertEqual(nonce_path.read_bytes(), c.canonical_json(doc))
                outbox = state.directory / "outbox" / nonce_path.name
                original = outbox.read_bytes()
                report = c.validate_applied(
                    c.verify(
                        c.APPLIED,
                        c.read_json(original),
                        agent.signer.private_key.public_key().public_bytes_raw(),
                    )
                )
                self.assertEqual(
                    (report["disposition"], report["reason"]), ("refused", case)
                )
                # The actual NORMAL fetch has the valid original CP signature.
                host.raw = normal
                restarted = Agent(
                    agent.config,
                    host,
                    State(state.directory),
                    agent.signer,
                    agent.keyring,
                )
                restarted.poll()
                self.assertEqual(restarted.result.reason, "nonce_replay")
                self.assertEqual(outbox.read_bytes(), original)
                self.assertEqual(host.executions, [])

    def setup_handoff(self, directory):
        agent, host, state, issue = harness.__wrapped__(directory)
        before = observation(PREVIOUS, 0, -20)
        before["observed"] = host.current.observed()
        execute = host.execute

        def fresh_execute(*args):
            result = execute(*args)
            host.current = replace(
                host.current, health=observation(DIGEST, 1, 0)["health"]
            )
            return result

        host.execute = fresh_execute
        doc = issue()
        normal = host.raw
        agent.poll()
        outbox = state.directory / "outbox" / state.nonce_path(doc["nonce"]).name
        report_raw = outbox.read_bytes()
        report = c.validate_applied(
            c.verify(
                c.APPLIED,
                c.read_json(report_raw),
                agent.signer.private_key.public_key().public_bytes_raw(),
            )
        )
        request = complete_request()
        context = {
            key: value
            for key, value in request.items()
            if key not in ("proofs", "command_id")
        }
        context.update(
            source_revision=doc["source_revision"],
            source_tree=doc["source_tree"],
            deployment_ref=doc["deployment_ref"],
        )
        third, fourth = copy.deepcopy(request["proofs"][2:4])
        transition = third["transition"]
        after = observation(DIGEST, 1, 0)
        after["observed"] = report["observed"]
        transition.update(
            nonce=doc["nonce"],
            desired_sha256=raw_digest(normal),
            report_id=report["report_id"],
            report_sha256=raw_digest(report_raw),
            before=before,
            after=after,
        )
        fourth["transition"] = copy.deepcopy(transition)
        fourth["egress"].update(
            transition_report_id=report["report_id"],
            transition_report_sha256=raw_digest(report_raw),
            finished_at=stamp(1),
            elapsed_seconds=61,
        )
        forward = dict(
            c.plan_document(doc),
            deployment_ref=doc["deployment_ref"],
            approvals=doc["approvals"],
        )
        fixture = dict(forward, from_image_digest=doc["image_digest"])
        plan = {
            "context": context,
            "forward": forward,
            "bad_signature": fixture,
            "rollback": fixture,
        }
        proof = SimpleNamespace(
            agent=agent,
            config=agent.config,
            host=host,
            control=Mock(),
            enrollment={
                "cp_public_b64": c.b64(agent.keyring.keys["desired-1"]),
                "host_public_b64": c.b64(
                    agent.signer.private_key.public_key().public_bytes_raw()
                ),
            },
            published_report=Mock(return_value=transition["ledger_commit"]),
        )
        cli.store(context["run_id"], "proof3", third)
        cli.store(context["run_id"], "proof4", fourth)
        cli.seal_transition(plan, third, fourth)
        return plan, proof, normal, third, fourth

    def test_current_run_handoff_verifies_original_signed_applied_and_ledger(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(cli, "protected_path"),
            patch.object(cli, "RUNS", Path(directory)),
        ):
            plan, proof, normal, _, _ = self.setup_handoff(Path(directory) / "agent")
            self.assertEqual(cli.consumed_forward(proof, plan), normal)
            proof.control.assert_called_once_with("stop")
            self.assertEqual(proof.published_report.call_args.args[2], normal)

    def test_original_applied_replay_never_replaces_report_or_executes_again(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(cli, "protected_path"),
            patch.object(cli, "RUNS", Path(directory)),
        ):
            plan, proof, normal, third, _ = self.setup_handoff(
                Path(directory) / "agent"
            )
            self.assertEqual(cli.consumed_forward(proof, plan), normal)
            doc = c.read_json(c.unb64(c.read_json(normal)["payload_b64"]))
            outbox = (
                proof.config.state_dir
                / "outbox"
                / proof.agent.state.nonce_path(doc["nonce"]).name
            )
            original = outbox.read_bytes()
            proof.agent.poll(normal)
            self.assertEqual(proof.agent.result.reason, "nonce_replay")
            self.assertEqual(
                raw_digest(outbox.read_bytes()), third["transition"]["report_sha256"]
            )
            self.assertEqual(outbox.read_bytes(), original)
            self.assertEqual(proof.host.executions, [DIGEST])

    def test_plan_binds_both_fixture_predecessors_to_forward_target_in_both_phases(
        self,
    ):
        for phase in ("initial", "repeatability"):
            with (
                self.subTest(phase=phase),
                tempfile.TemporaryDirectory() as directory,
                patch.object(cli, "protected_path"),
                patch.object(cli, "RUNS", Path(directory)),
            ):
                plan, proof, _, _, _ = self.setup_handoff(Path(directory) / "agent")
                plan["context"]["phase"] = phase
                plan["context"]["prior_run_id"] = (
                    None
                    if phase == "initial"
                    else "00000000-0000-0000-0000-000000000001"
                )
                reference = {"path": "/public/fixture.json", "sha256": DIGEST}
                plan.update(
                    dns_hostname="rotation.invalid",
                    disallowed_address="1.0.0.1",
                    disallowed_port=443,
                    proof1_plan=reference,
                    proof5=reference,
                    source_preparation=reference,
                    source_action_ref="forward",
                )
                path = Path(directory) / "plan.json"
                with patch.object(cli.Config, "load", return_value=proof.config):
                    path.write_bytes(c.canonical_json(plan))
                    self.assertEqual(cli.load_plan(path), plan)
                    for fixture in ("bad_signature", "rollback"):
                        invalid = copy.deepcopy(plan)
                        invalid[fixture]["from_image_digest"] = PREVIOUS
                        path.write_bytes(c.canonical_json(invalid))
                        with self.assertRaises(ValueError):
                            cli.load_plan(path)
                    invalid = dict(plan, replay=reference)
                    path.write_bytes(c.canonical_json(invalid))
                    with self.assertRaises(ValueError):
                        cli.load_plan(path)

    def test_prior_run_altered_nonpass_or_missing_handoff_never_issues_fixture(self):
        for change in ("prior_run", "context", "altered", "nonpass", "missing"):
            with (
                self.subTest(change=change),
                tempfile.TemporaryDirectory() as directory,
                patch.object(cli, "protected_path"),
                patch.object(cli, "RUNS", Path(directory)),
                patch.object(cli, "issue_desired") as issue,
            ):
                plan, proof, _, _, _ = self.setup_handoff(Path(directory) / "agent")
                root = Path(directory) / plan["context"]["run_id"]
                path = root / "transition-handoff.json"
                if change == "missing":
                    path.unlink()
                elif change in ("prior_run", "context"):
                    value = c.read_json(path.read_bytes())
                    value["run_id" if change == "prior_run" else "context_sha256"] = (
                        "00000000-0000-0000-0000-000000000001"
                        if change == "prior_run"
                        else PREVIOUS
                    )
                    path.write_bytes(c.canonical_json(value))
                else:
                    proof_path = root / "proof3.json"
                    value = c.read_json(proof_path.read_bytes())
                    if change == "nonpass":
                        value.update(
                            outcome="fail", failure_code="observation_mismatch"
                        )
                    else:
                        value["transition"]["nonce"] = "x" * 32
                    proof_path.write_bytes(c.canonical_json(value))
                    if change == "nonpass":
                        value = c.read_json(path.read_bytes())
                        value["proof3_sha256"] = raw_digest(proof_path.read_bytes())
                        path.write_bytes(c.canonical_json(value))
                result = cli.proof2(proof, Mock(), plan)
                self.assertEqual(result["outcome"], "fail")
                self.assertEqual(result["refusals"], [])
                issue.assert_not_called()

    def test_changed_normal_report_journal_or_expiry_remains_stopped(self):
        for change in ("normal", "report", "journal", "ledger", "fixture", "expiry"):
            with (
                self.subTest(change=change),
                tempfile.TemporaryDirectory() as directory,
                patch.object(cli, "protected_path"),
                patch.object(cli, "RUNS", Path(directory)),
                patch.object(cli, "issue_desired") as issue,
            ):
                plan, proof, normal, _, _ = self.setup_handoff(
                    Path(directory) / "agent"
                )
                doc = c.read_json(c.unb64(c.read_json(normal)["payload_b64"]))
                filename = raw_digest(doc["nonce"].encode())[7:] + ".json"
                if change == "normal":
                    proof.host.raw = b"{}"
                elif change in ("report", "journal"):
                    (
                        proof.config.state_dir
                        / ("outbox" if change == "report" else "nonces")
                        / filename
                    ).write_bytes(b"{}")
                elif change == "ledger":
                    proof.published_report.return_value = "f" * 40
                elif change == "fixture":
                    plan["bad_signature"] = dict(
                        plan["bad_signature"], from_image_digest=PREVIOUS
                    )
                with patch.object(cli, "datetime", wraps=datetime) as clock:
                    clock.now.return_value = datetime.now(UTC) + (
                        timedelta(hours=5) if change == "expiry" else timedelta()
                    )
                    result = cli.proof2(proof, Mock(), plan)
                self.assertEqual(result["outcome"], "fail")
                self.assertEqual(
                    proof.control.call_args_list, [unittest.mock.call("stop")]
                )
                issue.assert_not_called()

    def test_run2_uses_same_normal_bytes_then_issues_bound_fixtures(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(cli, "protected_path"),
            patch.object(cli, "RUNS", Path(directory)),
            patch.object(cli, "issue_desired", return_value=b"fixture") as issue,
            patch.object(cli, "refusal", return_value={}) as refusal,
        ):
            plan, proof, normal, _, _ = self.setup_handoff(Path(directory) / "agent")
            self.assertEqual(cli.proof2(proof, Mock(), plan)["outcome"], "pass")
            self.assertEqual(
                refusal.call_args_list[0].args[1:], (normal, "nonce_replay")
            )
            self.assertEqual(issue.call_count, 2)

    def test_failed_replay_stage_can_be_published_without_invented_refusals(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(cli, "protected_path"),
            patch.object(cli, "RUNS", Path(directory)),
            patch.object(cli, "store"),
            patch("builtins.print"),
        ):
            request = complete_request()
            context = {
                key: value
                for key, value in request.items()
                if key not in ("proofs", "command_id")
            }
            root = Path(directory) / context["run_id"]
            root.mkdir()
            (root / "proof2.json").write_bytes(c.canonical_json(empty_outcome(2)))
            client = Mock()
            client.publish.return_value = {"ledger_commit": "a" * 40}
            cli.publish(
                client,
                {
                    "context": context,
                    "proof5": {"path": str(root / "missing"), "sha256": DIGEST},
                },
                request["command_id"],
            )
            submitted = client.publish.call_args.args[0]
            self.assertEqual(submitted["proofs"], [empty_outcome(2)])
