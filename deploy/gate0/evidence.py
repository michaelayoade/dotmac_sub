"""Closed public Gate0 facts and an authenticated, origin-pinned CP adapter."""

from __future__ import annotations

import hashlib
import ipaddress
import re
import urllib.request
from datetime import datetime, timedelta
from typing import Annotated, Literal, TypedDict, cast
from urllib.parse import urlparse
from uuid import UUID

from deploy.agent import contracts as c
from deploy.agent.hostio import LIVE, READY

UUIDText = Annotated[
    str, r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
]


class HostObservation(TypedDict):
    observed_at: str
    observed: c.Observed
    health: c.Health
    executor_count: int


class RefusalEvidence(TypedDict):
    case: Literal["bad_signature", "nonce_replay", "rollback_digest"]
    submitted_sha256: c.Digest
    nonce: c.BoundedText
    report_id: UUIDText
    report_sha256: c.Digest
    classification: Literal["accepted", "quarantined"]
    expected_code: Literal["bad_signature", "nonce_replay", "rollback_digest"]
    observed_code: Literal["bad_signature", "nonce_replay", "rollback_digest"]
    journal_start: c.BoundedText
    journal_end: c.BoundedText
    normal_poll_journal_start: c.BoundedText
    normal_poll_journal_end: c.BoundedText
    cleanup_observed_at: str
    ledger_commit: c.Revision
    before: HostObservation
    after: HostObservation


class TransitionEvidence(TypedDict):
    nonce: c.BoundedText
    desired_sha256: c.Digest
    source_preparation_sha256: c.Digest
    report_id: UUIDText
    report_sha256: c.Digest
    receipt_sha256: c.Digest
    ledger_commit: c.Revision
    before: HostObservation
    after: HostObservation
    signatures_verified: bool
    runners_stopped: bool


class DNSRecord(TypedDict):
    family: Literal["A", "AAAA"]
    address: c.BoundedText
    ttl: int
    kernel_timeout_ms: int
    kernel_expires_ms: int


class EgressEvidence(TypedDict):
    hostname: c.BoundedText
    started_at: str
    old_expiry_observed_at: str
    finished_at: str
    transition_report_id: UUIDText
    transition_report_sha256: c.Digest
    probe_rule_handle: int
    probe_source_port: int
    probe_rule_removed: bool
    before: list[DNSRecord]
    after: list[DNSRecord]
    elapsed_seconds: int
    kernel_rules_sha256: c.Digest
    terminal_drop: bool
    counter_before: int
    counter_after: int
    disallowed_address: c.BoundedText
    disallowed_port: int
    fresh_connection_failed: bool
    allowed_deployment_succeeded: bool


class RestoreEvidence(TypedDict):
    source_identity: c.BoundedText
    test_host_identity: c.BoundedText
    bundle_sha256: c.Digest
    capture_sha256: c.Digest
    capture_started_at: str
    capture_finished_at: str
    source_inventory_sha256: c.Digest
    restored_inventory_sha256: c.Digest | None
    no_ddl_enforced: bool
    source_parity: bool | None
    scratch_absent: bool | None


class OIDCEvidence(TypedDict):
    main_run_id: int
    negative_run_id: int
    main_sha256: c.Digest
    negative_sha256: c.Digest
    main_accepted: bool
    negative_refused: bool


class ProofOutcome(TypedDict):
    proof: Literal[1, 2, 3, 4, 5]
    outcome: Literal["pass", "fail"]
    failure_code: Literal[
        "none",
        "evidence_missing",
        "observation_mismatch",
        "cleanup_failed",
        "transport_failed",
    ]
    cleanup: Literal["pass", "fail"]
    oidc: OIDCEvidence | None
    refusals: list[RefusalEvidence]
    transition: TransitionEvidence | None
    egress: EgressEvidence | None
    restore: RestoreEvidence | None


class GateContext(TypedDict):
    run_id: UUIDText
    phase: Literal["initial", "next_morning"]
    prior_run_id: UUIDText | None
    source_revision: c.Revision
    source_tree: c.Revision
    deployment_ref: c.BoundedText
    tool_revision: c.Revision
    host_key_id: c.Identifier
    action_refs: list[c.Identifier]
    deadline: str


class GateEvidence(GateContext):
    command_id: UUIDText
    proofs: list[ProofOutcome]


class PublishedReceipt(TypedDict):
    run_id: UUIDText
    command_sha256: c.Digest
    ledger_commit: c.Revision
    evidence_sha256: c.Digest
    published: Literal[True]
    verdict: Literal["PASS", "FAIL"]
    evidence: GateEvidence


def raw_digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def empty_outcome(proof: Literal[1, 2, 3, 4, 5]) -> ProofOutcome:
    return {
        "proof": proof,
        "outcome": "fail",
        "failure_code": "evidence_missing",
        "cleanup": "fail",
        "oidc": None,
        "refusals": [],
        "transition": None,
        "egress": None,
        "restore": None,
    }


def host_healthy(value: HostObservation) -> bool:
    health = value["health"]
    observed = value["observed"]
    containers = health["background"]["containers"]
    return (
        health["live"]["url"] == LIVE
        and health["ready"]["url"] == READY
        and health["live"]["status"] == 200
        and health["ready"]["status"] == 200
        and health["ready"]["body_status"] == "ready"
        and health["background"]["celery_inspect_ok"]
        and health["background"]["restart_counts_zero"]
        and {"app", "celery-worker"} <= set(containers)
        and all(state == "running" for state in containers.values())
        and observed["running_image"] == observed["pinned_image"]
        and observed["running_revision_label"] == observed["pinned_git_sha"]
    )


def validate(value: object, *, at: datetime | None = None) -> GateEvidence:
    c.require(len(c.canonical_json(value)) <= 131072)
    c._shape(value, GateEvidence)
    request = cast(GateEvidence, value)
    for key in ("run_id", "command_id", "prior_run_id"):
        if request[key] is not None:
            c.require(str(UUID(request[key])) == request[key])
    c.require((request["prior_run_id"] is None) == (request["phase"] == "initial"))
    c.require(request["prior_run_id"] != request["run_id"])
    deadline = c.timestamp(request["deadline"])
    if at is not None:
        c.require(at < deadline <= at + timedelta(hours=24))
    c.require(
        1 <= len(request["action_refs"]) <= 16
        and len(set(request["action_refs"])) == len(request["action_refs"])
    )
    c.require(
        re.fullmatch(r"dotmac_sub/staging/[A-Za-z0-9_.-]+", request["deployment_ref"])
        is not None
    )
    proofs = request["proofs"]
    c.require(
        1 <= len(proofs) <= 5 and len({p["proof"] for p in proofs}) == len(proofs)
    )
    transitions = []
    for proof in proofs:
        passed = proof["outcome"] == "pass"
        c.require(
            (proof["failure_code"] == "none") == passed
            and (proof["cleanup"] == "pass" or not passed)
        )
        for name, expected in (
            ("oidc", proof["proof"] == 1),
            ("refusals", proof["proof"] == 2),
            ("transition", proof["proof"] in (3, 4)),
            ("egress", proof["proof"] == 4),
            ("restore", proof["proof"] == 5),
        ):
            c.require(not bool(proof[name]) or expected)
            c.require(not passed or bool(proof[name]) == expected)
        c.require(len(proof["refusals"]) <= 3)
        if not passed:
            has_payload = bool(
                proof["oidc"]
                or proof["refusals"]
                or proof["transition"]
                or proof["egress"]
                or proof["restore"]
            )
            c.require(
                has_payload
                or proof["failure_code"] in ("evidence_missing", "transport_failed")
            )
            continue
        if passed and proof["proof"] == 2:
            c.require(
                {r["case"] for r in proof["refusals"]}
                == {"bad_signature", "nonce_replay", "rollback_digest"}
            )
        for refusal in proof["refusals"]:
            c.require(
                refusal["case"] == refusal["expected_code"] == refusal["observed_code"]
            )
            c.require(refusal["classification"] == "accepted")
            c.require(
                refusal["journal_start"] != refusal["journal_end"]
                and refusal["normal_poll_journal_start"]
                != refusal["normal_poll_journal_end"]
            )
            c.require(refusal["before"]["observed"] == refusal["after"]["observed"])
            c.require(
                refusal["before"]["executor_count"]
                == refusal["after"]["executor_count"]
            )
            c.require(
                c.timestamp(refusal["after"]["observed_at"])
                <= c.timestamp(refusal["cleanup_observed_at"])
                < deadline
            )
        transition = proof["transition"]
        if transition is not None:
            transitions.append(transition)
            c.require(
                c.timestamp(transition["before"]["observed_at"])
                < c.timestamp(transition["after"]["observed_at"])
            )
            c.require(
                transition["signatures_verified"] and transition["runners_stopped"]
            )
            c.require(
                transition["after"]["executor_count"]
                - transition["before"]["executor_count"]
                == 1
            )
            c.require(
                transition["before"]["observed"]["running_image"]
                != transition["after"]["observed"]["running_image"]
            )
        observations = [o for r in proof["refusals"] for o in (r["before"], r["after"])]
        if transition is not None:
            observations += [transition["before"], transition["after"]]
        for observation in observations:
            c.require(host_healthy(observation) and observation["executor_count"] >= 0)
            c.require(set(observation["observed"]["config_files"]) == c.CONFIG_PATHS)
            c.require(
                len(observation["observed"]["migration_heads"]) <= 32
                and len(observation["health"]["background"]["containers"]) <= 32
            )
            observed_at = c.timestamp(observation["observed_at"])
            c.require(deadline - timedelta(hours=24) <= observed_at <= deadline)
            for name in ("live", "ready"):
                c.require(
                    observed_at - timedelta(minutes=5)
                    <= c.timestamp(observation["health"][name]["checked_at"])
                    <= observed_at
                )
        egress = proof["egress"]
        if egress is not None:
            c.require(
                transition is not None
                and egress["transition_report_id"] == transition["report_id"]
                and egress["transition_report_sha256"] == transition["report_sha256"]
            )
            c.require(
                egress["terminal_drop"]
                and egress["fresh_connection_failed"]
                and egress["allowed_deployment_succeeded"]
                and egress["probe_rule_removed"]
            )
            c.require(
                0 <= egress["counter_before"] < egress["counter_after"]
                and egress["probe_rule_handle"] > 0
                and 32768 <= egress["probe_source_port"] <= 60999
            )
            c.require(
                ipaddress.ip_address(egress["disallowed_address"]).is_global
                and 1 <= egress["disallowed_port"] <= 65535
            )
            start, finish = (
                c.timestamp(egress["started_at"]),
                c.timestamp(egress["finished_at"]),
            )
            c.require(
                start
                <= c.timestamp(transition["before"]["observed_at"])
                < c.timestamp(transition["after"]["observed_at"])
                <= finish
                < deadline
            )
            c.require(
                0 <= egress["elapsed_seconds"] <= 3300
                and abs(
                    int((finish - start).total_seconds()) - egress["elapsed_seconds"]
                )
                <= 2
            )
            for records in (egress["before"], egress["after"]):
                c.require(
                    2 <= len(records) <= 32
                    and {r["family"] for r in records} == {"A", "AAAA"}
                )
                for record in records:
                    c.require(
                        1 <= record["ttl"] <= 300
                        and ipaddress.ip_address(record["address"]).version
                        == (4 if record["family"] == "A" else 6)
                    )
                    c.require(
                        0 < record["kernel_expires_ms"] <= record["kernel_timeout_ms"]
                        and record["kernel_timeout_ms"] <= (record["ttl"] + 2) * 1000
                    )
            for family in ("A", "AAAA"):
                c.require(
                    {
                        r["address"] for r in egress["before"] if r["family"] == family
                    }.isdisjoint(
                        {r["address"] for r in egress["after"] if r["family"] == family}
                    )
                )
            ttl = max(r["ttl"] for r in egress["before"])
            c.require(egress["elapsed_seconds"] >= ttl)
            c.require(
                start
                <= c.timestamp(egress["old_expiry_observed_at"])
                <= start + timedelta(seconds=ttl + 3)
            )
        restore = proof["restore"]
        if restore is not None:
            c.require(
                not passed
                or restore["no_ddl_enforced"]
                and restore["source_parity"]
                and restore["scratch_absent"]
            )
            c.require(
                not passed
                or restore["source_inventory_sha256"]
                == restore["restored_inventory_sha256"]
            )
            c.require(
                c.timestamp(restore["capture_started_at"])
                < c.timestamp(restore["capture_finished_at"])
                < deadline
            )
        oidc = proof["oidc"]
        if oidc is not None:
            c.require(
                oidc["main_run_id"] > 0
                and oidc["negative_run_id"] > 0
                and oidc["main_run_id"] != oidc["negative_run_id"]
                and oidc["main_accepted"]
                and oidc["negative_refused"]
            )
    c.require(not transitions or all(t == transitions[0] for t in transitions))
    return request


class PublishedFailure(ValueError):
    def __init__(self, receipt: PublishedReceipt):
        self.receipt = receipt
        super().__init__("gate_evidence_published_fail")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class CPClient:
    def __init__(self, origin: str, authorization: str):
        c.require(origin.isascii() and not any(ord(char) <= 32 for char in origin))
        url = urlparse(origin)
        try:
            port = url.port
        except ValueError:
            raise c.ContractError() from None
        c.require(port is None or 1 <= port <= 65535)
        c.require(
            url.scheme == "https"
            and bool(url.hostname)
            and not url.username
            and not url.password
            and url.path in ("", "/")
            and not url.query
            and not url.fragment
        )
        c.require(
            re.fullmatch(r"Bearer [A-Za-z0-9._~-]{1,8192}", authorization) is not None
        )
        self.origin, self.authorization = origin.rstrip("/"), authorization

    def request(self, path: str, body: bytes | None = None) -> bytes:
        c.require(path in ("gate-evidence", "desired-state"))
        request = urllib.request.Request(  # noqa: S310 - constructor pins HTTPS origin
            self.origin + "/platform/deploy/" + path,
            data=body,
            headers={
                "Authorization": self.authorization,
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.build_opener(NoRedirect).open(
                request, timeout=30
            ) as response:
                raw = response.read(262145)
                c.require(len(raw) <= 262144)
                return raw
        except (OSError, ValueError):
            raise ValueError("gate_evidence_transport_failed") from None

    def publish(self, value: GateEvidence) -> PublishedReceipt:
        body = c.canonical_json(validate(value))
        receipt = c.read_json(self.request("gate-evidence", body))
        c._shape(receipt, PublishedReceipt)
        c.require(
            receipt["run_id"] == value["run_id"]
            and receipt["command_sha256"] == raw_digest(body)
        )
        readback = c.read_json(self.request("gate-evidence", body))
        c.require(
            readback == receipt
            and c.canonical_json(receipt["evidence"]) == body
            and raw_digest(body) == receipt["evidence_sha256"]
        )
        result = cast(PublishedReceipt, receipt)
        if result["verdict"] == "FAIL":
            raise PublishedFailure(result)
        required = {1, 2, 3, 4, 5} if value["phase"] == "initial" else {2, 3, 4}
        c.require(
            {proof["proof"] for proof in value["proofs"]} == required
            and all(proof["outcome"] == "pass" for proof in value["proofs"])
        )
        return result
