"""Finite human-run staging proof commands; no activation or credential provisioning."""

from __future__ import annotations

import argparse
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, TypedDict, cast
from uuid import UUID

from deploy.agent import contracts as c
from deploy.agent.hostio import Config
from deploy.gate0.evidence import (
    CPClient,
    GateContext,
    GateEvidence,
    ProofOutcome,
    PublishedFailure,
    TransitionEvidence,
    UUIDText,
    empty_outcome,
    raw_digest,
    validate,
)
from deploy.gate0.host_evidence import EnrolledHost, protected_path
from deploy.gate0.proof1_observation import GitHubProofSource, OIDCPlan, observe
from deploy.gate0.proof2_refusals import CONFIG, DaemonProof, refusal
from deploy.gate0.proof34_transition import (
    ForwardIssuanceUncertain,
    ForwardRequest,
    ProbeCleanupUnproved,
    SourcePreparation,
    forward,
    issue_desired,
    network_after,
    network_before,
    observe_prepared_source,
)
from scripts.verify_github_release import _token_from_environment

RUNS = Path("/var/lib/dotmac-gate0/runs")


class TransitionHandoff(TypedDict):
    run_id: UUIDText
    context_sha256: c.Digest
    proof3_sha256: c.Digest
    proof4_sha256: c.Digest


class ArtifactRef(TypedDict):
    path: c.BoundedText
    sha256: c.Digest


class RunPlan(TypedDict):
    context: GateContext
    bad_signature: ForwardRequest
    rollback: ForwardRequest
    forward: ForwardRequest
    dns_hostname: c.BoundedText
    disallowed_address: c.BoundedText
    disallowed_port: int
    proof1_plan: ArtifactRef
    proof5: ArtifactRef
    source_preparation: ArtifactRef
    source_action_ref: c.Identifier


def artifact(reference: ArtifactRef) -> bytes:
    path = Path(reference["path"])
    protected_path(path)
    c.require(path.stat().st_size <= 131072)
    raw = path.read_bytes()
    c.require(raw_digest(raw) == reference["sha256"])
    return raw


def load_plan(path: Path) -> RunPlan:
    protected_path(path)
    c.require(path.stat().st_size <= 65536)
    plan = cast(RunPlan, c.read_json(path.read_bytes()))
    c._shape(plan, RunPlan)
    context = plan["context"]
    c.require(str(UUID(context["run_id"])) == context["run_id"])
    c.require(context["deployment_ref"] == Config.load(CONFIG).deployment_ref)
    for desired in (plan["bad_signature"], plan["rollback"], plan["forward"]):
        c.require(desired["deployment_ref"] == context["deployment_ref"])
    for fixture in (plan["bad_signature"], plan["rollback"]):
        c.require(fixture["from_image_digest"] == plan["forward"]["image_digest"])
    c.require(plan["source_action_ref"] in context["action_refs"])
    c.require(
        plan["forward"]["source_revision"] == context["source_revision"]
        and plan["forward"]["source_tree"] == context["source_tree"]
    )
    return plan


def store(
    run_id: str,
    name: Literal[
        "proof1",
        "proof1-main",
        "proof1-negative",
        "proof2",
        "proof3",
        "proof4",
        "receipt",
        "source-preparation",
        "transition-handoff",
    ],
    value: object,
) -> None:
    c.require(str(UUID(run_id)) == run_id)
    root = RUNS
    protected_path(root, directory=True)
    directory = root / run_id
    directory.mkdir(mode=0o700, exist_ok=True)
    protected_path(directory, directory=True)
    path, raw = directory / (name + ".json"), c.canonical_json(value)
    if path.exists():
        protected_path(path)
        c.require(path.read_bytes() == raw)
        return
    with path.open("xb") as output:
        os.fchmod(output.fileno(), 0o600)
        output.write(raw)
        output.flush()
        os.fsync(output.fileno())


def seal_transition(plan: RunPlan, third: ProofOutcome, fourth: ProofOutcome) -> None:
    if third["outcome"] == fourth["outcome"] == "pass":
        c.require(third["transition"] == fourth["transition"])
        store(
            plan["context"]["run_id"],
            "transition-handoff",
            {
                "run_id": plan["context"]["run_id"],
                "context_sha256": c.digest(plan["context"]),
                "proof3_sha256": c.digest(third),
                "proof4_sha256": c.digest(fourth),
            },
        )


def current_transition(plan: RunPlan) -> TransitionEvidence:
    directory = RUNS / plan["context"]["run_id"]
    records = []
    for name in ("transition-handoff", "proof3", "proof4"):
        path = directory / (name + ".json")
        protected_path(path)
        c.require(path.stat().st_size <= 131072)
        records.append(c.read_json(path.read_bytes()))
    handoff = cast(TransitionHandoff, records[0])
    c._shape(handoff, TransitionHandoff)
    c.require(handoff["run_id"] == plan["context"]["run_id"])
    c.require(handoff["context_sha256"] == c.digest(plan["context"]))
    for number, record in zip((3, 4), records[1:], strict=True):
        c._shape(record, ProofOutcome)
        c.require(handoff["proof" + str(number) + "_sha256"] == c.digest(record))
        c.require(record["proof"] == number and record["outcome"] == "pass")
    validate(
        cast(
            GateEvidence,
            dict(
                plan["context"],
                command_id=plan["context"]["run_id"],
                proofs=records[1:],
            ),
        )
    )
    c.require(records[1]["transition"] == records[2]["transition"])
    transition = cast(TransitionEvidence, records[1]["transition"])
    c.require(transition is not None)
    return transition


def consumed_forward(proof: DaemonProof, plan: RunPlan) -> bytes:
    transition = current_transition(plan)
    proof.control("stop")
    # Any failure from here leaves normal polling stopped: a fresh command may
    # have appeared, or the expected accepted command may no longer be safe.
    raw = proof.host.fetch()
    c.require(
        isinstance(raw, bytes) and raw_digest(raw) == transition["desired_sha256"]
    )
    doc = c.validate_desired(
        c.verify(
            c.DESIRED,
            cast(c.Envelope, c.read_json(raw)),
            proof.enrollment["cp_public_b64"],
        ),
        now=datetime.now(UTC),
    )
    c.require(doc["nonce"] == transition["nonce"])
    c.require(all(doc[key] == value for key, value in plan["forward"].items()))
    c.require(
        doc["source_revision"] == plan["context"]["source_revision"]
        and doc["source_tree"] == plan["context"]["source_tree"]
    )
    for fixture in (plan["bad_signature"], plan["rollback"]):
        c.require(fixture["from_image_digest"] == doc["image_digest"])
    filename = raw_digest(doc["nonce"].encode())[7:] + ".json"
    nonce, outbox = (
        proof.config.state_dir / name / filename for name in ("nonces", "outbox")
    )
    c.require(not nonce.is_symlink() and not outbox.is_symlink())
    c.require(nonce.stat().st_size <= 131072 and outbox.stat().st_size <= 131072)
    c.require(nonce.read_bytes() == c.canonical_json(doc))
    report_raw = outbox.read_bytes()
    c.require(raw_digest(report_raw) == transition["report_sha256"])
    report = c.validate_applied(
        c.verify(
            c.APPLIED,
            cast(c.Envelope, c.read_json(report_raw)),
            proof.enrollment["host_public_b64"],
        )
    )
    c.require(
        report["disposition"] == "applied"
        and report["report_id"] == transition["report_id"]
    )
    c.require(
        report["host_key_id"] == plan["context"]["host_key_id"]
        and report["deployment_ref"] == doc["deployment_ref"]
    )
    c.require(all(report["desired"][key] == doc[key] for key in report["desired"]))
    c.require(report["observed"] == transition["after"]["observed"])
    c.require(
        c.timestamp(transition["before"]["observed_at"])
        <= c.timestamp(doc["issued_at"])
        <= c.timestamp(report["observed_at"])
        <= c.timestamp(transition["after"]["observed_at"])
        <= datetime.now(UTC)
    )
    c.require(
        proof.published_report(report["report_id"], report_raw, raw)
        == transition["ledger_commit"]
    )
    return raw


def proof2(proof: DaemonProof, client: CPClient, plan: RunPlan) -> ProofOutcome:
    result = empty_outcome(2)
    try:
        replay = consumed_forward(proof, plan)
        result["refusals"].append(refusal(proof, replay, "nonce_replay"))
        for case, desired in (
            ("bad_signature", plan["bad_signature"]),
            ("rollback_digest", plan["rollback"]),
        ):
            proof.control("stop")
            raw = issue_desired(
                proof, client, desired, plan["context"]["run_id"] + "-" + case
            )
            result["refusals"].append(refusal(proof, raw, case))
    except (OSError, ValueError, KeyError, TypeError, TimeoutError):
        return result
    result.update(outcome="pass", failure_code="none", cleanup="pass")
    return result


def proof34(
    proof: DaemonProof, client: CPClient, plan: RunPlan
) -> tuple[ProofOutcome, ProofOutcome]:
    # The existing release/source-preparation owner must prepare this exact
    # checkout before run34. The proof never becomes a Git writer.
    c.require(
        proof.run(["git", "rev-parse", "HEAD"]) == plan["forward"]["source_revision"]
    )
    c.require(not proof.run(["git", "status", "--porcelain", "--untracked-files=no"]))
    preparation = cast(
        SourcePreparation, c.read_json(artifact(plan["source_preparation"]))
    )
    c._shape(preparation, SourcePreparation)
    third, fourth = empty_outcome(3), empty_outcome(4)
    start = None
    try:
        start = network_before(proof, plan["dns_hostname"])
        transition = forward(
            proof,
            client,
            plan["forward"],
            plan["context"]["run_id"] + "-forward",
            preparation,
            plan["context"]["action_refs"],
        )
        third.update(
            outcome="pass", failure_code="none", cleanup="pass", transition=transition
        )
        fourth["transition"] = transition
        fourth["egress"] = network_after(
            proof,
            plan["dns_hostname"],
            start,
            transition,
            plan["disallowed_address"],
            plan["disallowed_port"],
        )
        fourth.update(outcome="pass", failure_code="none", cleanup="pass")
    except ForwardIssuanceUncertain:
        third.update(failure_code="transport_failed", cleanup="fail")
        fourth.update(failure_code="transport_failed", cleanup="fail")
    except ProbeCleanupUnproved:
        fourth.update(failure_code="cleanup_failed", cleanup="fail")
    except (OSError, ValueError, KeyError, TypeError, TimeoutError):
        pass  # Typed partial facts are retained; CP derives terminal FAIL.
    finally:
        if start is not None:
            try:
                start.close()
            except ValueError:
                fourth.update(
                    outcome="fail",
                    failure_code="cleanup_failed"
                    if fourth["transition"] is not None
                    else "transport_failed",
                    cleanup="fail",
                )
    return third, fourth


def publish(client: CPClient, plan: RunPlan, command_id: str) -> None:
    context = plan["context"]
    run_id = context["run_id"]
    proofs = []
    required = {2, 3, 4} if context["phase"] == "repeatability" else {1, 2, 3, 4, 5}
    for number in sorted(required):
        if number == 5:
            reference = plan["proof5"]
            if not Path(reference["path"]).exists():
                continue
            raw = artifact(reference)
        else:
            path = RUNS / run_id / ("proof" + str(number) + ".json")
            if not path.exists():
                continue
            protected_path(path)
            c.require(path.stat().st_size <= 131072)
            raw = path.read_bytes()
        proof = cast(ProofOutcome, c.read_json(raw))
        c._shape(proof, ProofOutcome)
        c.require(proof["proof"] == number)
        proofs.append(proof)
    request = cast(GateEvidence, dict(context, command_id=command_id, proofs=proofs))
    receipt = client.publish(validate(request))
    store(run_id, "receipt", receipt)
    print(
        c.canonical_json(
            {
                "outcome": "pass",
                "run_id": run_id,
                "ledger_commit": receipt["ledger_commit"],
            }
        ).decode()
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=(
            "observe-proof1",
            "observe-prepared-source",
            "run2",
            "run34",
            "publish",
        ),
    )
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--command-id")
    args = parser.parse_args()
    try:
        plan = load_plan(args.plan)
        if args.command != "publish":
            c.require(
                datetime.now(UTC)
                < c.timestamp(plan["context"]["deadline"])
                <= datetime.now(UTC) + timedelta(hours=24)
            )
        config = Config.load(CONFIG)
        proof = (
            EnrolledHost(config)
            if args.command in ("observe-proof1", "observe-prepared-source", "publish")
            else DaemonProof(config)
        )
        c.require(
            plan["context"]["host_key_id"] == proof.config.host_key_id
            and plan["context"]["tool_revision"]
            == proof.enrollment["proof_source_revision"]
        )
        if args.command == "observe-proof1":
            oidc_plan = cast(OIDCPlan, c.read_json(artifact(plan["proof1_plan"])))
            outcome = empty_outcome(1)
            try:
                outcome, main, negative = observe(
                    GitHubProofSource(token=_token_from_environment()),
                    proof,
                    oidc_plan,
                    plan["context"],
                )
                store(plan["context"]["run_id"], "proof1-main", main)
                store(plan["context"]["run_id"], "proof1-negative", negative)
            except (OSError, ValueError, KeyError, TypeError):
                outcome["failure_code"] = "evidence_missing"
            store(plan["context"]["run_id"], "proof1", outcome)
            return 0 if outcome["outcome"] == "pass" else 1
        if args.command == "observe-prepared-source":
            receipt = observe_prepared_source(
                proof, plan["forward"], plan["source_action_ref"]
            )
            store(plan["context"]["run_id"], "source-preparation", receipt)
            print(
                c.canonical_json(
                    {
                        "readiness": "source_observed",
                        "artifact_sha256": c.digest(receipt),
                    }
                ).decode()
            )
            return 0
        client = CPClient(proof.config.cp_url, os.environ["DOTMAC_CP_AUTHORIZATION"])
        if args.command == "run2":
            outcome = proof2(proof, client, plan)
            store(plan["context"]["run_id"], "proof2", outcome)
            return 0 if outcome["outcome"] == "pass" else 1
        elif args.command == "run34":
            third, fourth = proof34(proof, client, plan)
            store(plan["context"]["run_id"], "proof3", third)
            store(plan["context"]["run_id"], "proof4", fourth)
            seal_transition(plan, third, fourth)
            return 0 if third["outcome"] == fourth["outcome"] == "pass" else 1
        else:
            c.require(args.command_id is not None)
            publish(client, plan, args.command_id)
        return 0
    except PublishedFailure as error:
        store(error.receipt["run_id"], "receipt", error.receipt)
        print(
            c.canonical_json(
                {
                    "outcome": "fail",
                    "run_id": error.receipt["run_id"],
                    "ledger_commit": error.receipt["ledger_commit"],
                }
            ).decode()
        )
        return 1
    except Exception:
        # The adapter emits only a bounded public error, never exception text.
        print('{"outcome":"fail","failure_code":"evidence_missing_or_cleanup_failed"}')
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
