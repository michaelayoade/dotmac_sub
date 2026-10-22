"""One forward issuance and applied transition, shared by proofs three and four."""

from __future__ import annotations

import errno
import ipaddress
import json
import socket
import time
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Event, Thread
from typing import Literal, TypedDict, cast
from uuid import uuid4

from deploy.agent import contracts as c
from deploy.agent.hostio import IMAGE, now
from deploy.gate0.evidence import (
    CPClient,
    DNSRecord,
    EgressEvidence,
    TransitionEvidence,
    raw_digest,
)
from deploy.gate0.host_evidence import RUNNER, EnrolledHost
from deploy.gate0.proof2_refusals import UNIT, DaemonProof, healthy


class ForwardIssuanceUncertain(ValueError):
    """A command may exist, but no safe fresh authoritative envelope is known."""


class ProbeCleanupUnproved(ValueError):
    """The exact owned probe rule could not be reconciled to observed absence."""


class ForwardRequest(c.Plan):
    deployment_ref: c.BoundedText
    approvals: list[c.Approval]


class SourcePreparation(TypedDict):
    owner: Literal["deploy/gate0/run_proofs.py:observe-prepared-source"]
    action_ref: c.Identifier
    from_revision: c.Revision
    to_revision: c.Revision
    from_image_digest: c.Digest
    to_image_digest: c.Digest
    source_tree: c.Revision
    build_run_id: int
    evidence_run_id: int
    prepared_at: str


def source_ready(
    proof: EnrolledHost,
    desired: ForwardRequest,
    preparation: SourcePreparation,
    action_refs: list[str],
) -> None:
    c._shape(preparation, SourcePreparation)
    c.require(preparation["action_ref"] in action_refs)
    c.require(preparation["from_revision"] != preparation["to_revision"])
    c.require(c.timestamp(preparation["prepared_at"]) <= datetime.now(UTC))
    for left, right in (
        ("to_revision", "source_revision"),
        ("source_tree", "source_tree"),
        ("from_image_digest", "from_image_digest"),
        ("to_image_digest", "image_digest"),
        ("build_run_id", "build_run_id"),
        ("evidence_run_id", "evidence_run_id"),
    ):
        c.require(preparation[left] == desired[right])
    current = proof.host.observe()
    c.require(
        current.healthy()
        and current.running_revision_label == preparation["from_revision"]
        and current.running_image == IMAGE + "@" + preparation["from_image_digest"]
    )
    c.require(proof.run(["git", "rev-parse", "HEAD"]) == preparation["to_revision"])
    c.require(
        proof.run(["git", "rev-parse", "HEAD^{tree}"]) == preparation["source_tree"]
    )
    c.require(not proof.run(["git", "status", "--porcelain", "--untracked-files=no"]))
    proof.run(
        [
            "git",
            "merge-base",
            "--is-ancestor",
            preparation["from_revision"],
            preparation["to_revision"],
        ]
    )


def observe_prepared_source(
    proof: EnrolledHost, desired: ForwardRequest, action_ref: str
) -> SourcePreparation:
    c.identifier(action_ref)
    current = proof.host.observe()
    c.require(current.healthy() and current.running_revision_label is not None)
    receipt: SourcePreparation = {
        "owner": "deploy/gate0/run_proofs.py:observe-prepared-source",
        "action_ref": action_ref,
        "from_revision": current.running_revision_label,
        "to_revision": proof.run(["git", "rev-parse", "HEAD"]),
        "from_image_digest": desired["from_image_digest"],
        "to_image_digest": desired["image_digest"],
        "source_tree": proof.run(["git", "rev-parse", "HEAD^{tree}"]),
        "build_run_id": desired["build_run_id"],
        "evidence_run_id": desired["evidence_run_id"],
        "prepared_at": now(),
    }
    source_ready(proof, desired, receipt, [action_ref])
    return receipt


@dataclass(frozen=True)
class NetworkStart:
    records: list[DNSRecord]
    observed_at: str
    monotonic: float
    stop: Event
    expired: Future[str]
    thread: Thread

    def close(self) -> None:
        self.stop.set()
        self.thread.join(timeout=65)
        c.require(not self.thread.is_alive())


def runners_stopped(proof: DaemonProof) -> None:
    result = proof.result(["systemctl", "is-active", RUNNER])
    c.require(result.returncode == 3 and result.stdout.strip() == "inactive")
    c.require(
        not proof.run(
            [
                "systemctl",
                "list-units",
                "--type=service",
                "--state=running",
                "--plain",
                "--no-legend",
                "actions.runner.*",
            ]
        )
    )
    c.require(proof.result(["pgrep", "-f", "[R]unner.Listener"]).returncode == 1)


def dns(proof: DaemonProof, hostname: str) -> list[DNSRecord]:
    from deploy.egress.render import EgressConfiguration

    EgressConfiguration(hostname, hostname)
    records: list[DNSRecord] = []
    for family in ("A", "AAAA"):
        for line in proof.run(
            ["dig", "@127.0.0.1", hostname, family, "+noall", "+answer"]
        ).splitlines():
            name, ttl, kind, observed_family, address = line.split()
            c.require(
                name.rstrip(".") == hostname
                and kind == "IN"
                and observed_family == family
            )
            parsed = ipaddress.ip_address(address)
            c.require(
                parsed.version == (4 if family == "A" else 6) and 1 <= int(ttl) <= 300
            )
            records.append(
                cast(
                    DNSRecord,
                    {
                        "family": family,
                        "address": str(parsed),
                        "ttl": int(ttl),
                        "kernel_timeout_ms": 0,
                        "kernel_expires_ms": 0,
                    },
                )
            )
    c.require(
        {record["family"] for record in records} == {"A", "AAAA"} and len(records) <= 32
    )
    _, _, addresses = kernel(proof)
    for record in records:
        c.require(record["address"] in addresses)
        timeout, expires = addresses[record["address"]]
        c.require(0 < expires <= timeout <= (record["ttl"] + 2) * 1000)
        record["kernel_timeout_ms"], record["kernel_expires_ms"] = timeout, expires
    return records


def kernel(
    proof: DaemonProof, *, verify_terminal: bool = True, timeout: int = 60
) -> tuple[bytes, list[dict[str, object]], dict[str, tuple[int, int]]]:
    raw = proof.run(
        ["nft", "-j", "list", "table", "inet", "dotmac_egress"], timeout=timeout
    ).encode()
    items = json.loads(raw)["nftables"]
    chains = [
        item["chain"]
        for item in items
        if "chain" in item and item["chain"]["name"] == "out"
    ]
    c.require(
        len(chains) == 1
        and chains[0]["hook"] == "output"
        and chains[0]["type"] == "filter"
    )
    rules = [
        item["rule"]
        for item in items
        if "rule" in item and item["rule"]["chain"] == "out"
    ]
    c.require(bool(rules))
    expressions = rules[-1]["expr"]
    if verify_terminal:
        c.require(
            len(expressions) == 2
            and set(expressions[0]) == {"counter"}
            and expressions[1] == {"drop": None}
        )
    c.require(
        type(expressions[0]["counter"]["packets"]) is int
        and expressions[0]["counter"]["packets"] >= 0
    )
    addresses = {}
    for item in items:
        value = item.get("set", {})
        if value.get("name") in ("egress_v4", "egress_v6"):
            for element in value.get("elem", []):
                c.require(isinstance(element, dict) and set(element) == {"elem"})
                detail = element["elem"]
                c.require(
                    type(detail["timeout"]) is int and type(detail["expires"]) is int
                )
                # Units come from protected pinned-tool enrollment and its
                # verified fixture, never from a guessed dump magnitude.
                unit = proof.enrollment["nft_element_time_unit"]
                c.require(unit in ("seconds", "milliseconds"))
                factor = 1000 if unit == "seconds" else 1
                address = ipaddress.ip_address(detail["val"])
                c.require(address.version == (4 if value["name"] == "egress_v4" else 6))
                canonical_address = str(address)
                c.require(canonical_address not in addresses)
                addresses[canonical_address] = (
                    detail["timeout"] * factor,
                    detail["expires"] * factor,
                )
    return raw, rules, addresses


def network_before(proof: DaemonProof, hostname: str) -> NetworkStart:
    observed_at, started = now(), time.monotonic()
    records = dns(proof, hostname)
    stop, expired = Event(), Future()

    def watch() -> None:
        try:
            deadline = started + max(record["ttl"] for record in records) + 2
            old = {record["address"] for record in records}
            while not stop.is_set() and time.monotonic() <= deadline:
                _, _, addresses = kernel(proof)
                if old.isdisjoint(addresses):
                    expired.set_result(now())
                    return
                stop.wait(0.25)
            raise ValueError("kernel_dns_expiry_not_observed_within_ttl")
        except Exception:
            expired.set_exception(
                ValueError("kernel_dns_expiry_not_observed_within_ttl")
            )

    thread = Thread(target=watch, daemon=True)
    thread.start()
    return NetworkStart(records, observed_at, started, stop, expired, thread)


def probe_packets(
    rule: dict[str, object], address: str, source: int, destination: int
) -> int:
    family = "ip" if ipaddress.ip_address(address).version == 4 else "ip6"
    expected = [
        {
            "match": {
                "op": "==",
                "left": {"payload": {"protocol": family, "field": "daddr"}},
                "right": address,
            }
        },
        {
            "match": {
                "op": "==",
                "left": {"payload": {"protocol": "tcp", "field": "sport"}},
                "right": source,
            }
        },
        {
            "match": {
                "op": "==",
                "left": {"payload": {"protocol": "tcp", "field": "dport"}},
                "right": destination,
            }
        },
        {"match": {"op": "in", "left": {"ct": {"key": "state"}}, "right": "new"}},
    ]
    expressions = cast(list[dict[str, object]], rule["expr"])
    c.require(len(expressions) == 5 and expressions[:3] == expected[:3])
    # nft versions use == or in for a single ct-state member.
    state = expressions[3]
    c.require(
        state
        in (
            expected[3],
            {"match": {"op": "==", "left": {"ct": {"key": "state"}}, "right": "new"}},
        )
    )
    c.require(set(expressions[4]) == {"counter"})
    packets = cast(dict[str, int], expressions[4]["counter"])["packets"]
    c.require(type(packets) is int and packets >= 0)
    return packets


def dropped_fresh_flow(
    proof: DaemonProof, disallowed: str, port: int
) -> tuple[str, int, int, int, int]:
    address = ipaddress.ip_address(disallowed)
    c.require(address.is_global and 1 <= port <= 65535)
    raw, rules, addresses = kernel(proof)
    c.require(str(address) not in addresses)
    terminal_handle = rules[-1]["handle"]
    c.require(type(terminal_handle) is int and terminal_handle > 0)
    tag = "dotmac_gate0_" + uuid4().hex
    c.require(not any(rule.get("comment") == tag for rule in rules))
    with socket.socket(
        socket.AF_INET if address.version == 4 else socket.AF_INET6, socket.SOCK_STREAM
    ) as stream:
        stream.bind(("0.0.0.0" if address.version == 4 else "::", 0))  # noqa: S104 - outbound socket only, never listens
        source = stream.getsockname()[1]
        c.require(32768 <= source <= 60999)
        family = "ip" if address.version == 4 else "ip6"
        handle = None
        try:
            proof.run(
                [
                    "nft",
                    "insert",
                    "rule",
                    "inet",
                    "dotmac_egress",
                    "out",
                    "position",
                    str(terminal_handle),
                    family,
                    "daddr",
                    str(address),
                    "tcp",
                    "sport",
                    str(source),
                    "tcp",
                    "dport",
                    str(port),
                    "ct",
                    "state",
                    "new",
                    "counter",
                    "comment",
                    json.dumps(tag),
                ]
            )
            raw, rules, _ = kernel(proof)
            matches = [rule for rule in rules if rule.get("comment") == tag]
            c.require(
                len(matches) == 1
                and rules[-2] == matches[0]
                and rules[-1]["handle"] == terminal_handle
            )
            handle = cast(int, matches[0]["handle"])
            terminal_initial = cast(list[dict[str, object]], rules[-1]["expr"])[0][
                "counter"
            ]["packets"]
            initial = probe_packets(matches[0], str(address), source, port)
            c.require(initial == 0)
            stream.settimeout(3)
            failure = None
            try:
                stream.connect((str(address), port))
                failure = 0
            except TimeoutError:
                failure = errno.ETIMEDOUT
            c.require(failure == errno.ETIMEDOUT)
            raw, rules, _ = kernel(proof)
            c.require(
                rules[-1]["handle"] == terminal_handle
                and rules[-2].get("comment") == tag
            )
            final = probe_packets(rules[-2], str(address), source, port)
            terminal_final = cast(list[dict[str, object]], rules[-1]["expr"])[0][
                "counter"
            ]["packets"]
            c.require(
                final > initial and terminal_final - terminal_initial >= final - initial
            )
            return raw_digest(raw), handle, source, initial, final
        finally:
            # The kernel may have accepted insertion even if nft lost its reply.
            # Reconcile only this unique tag and exact flow before deleting.
            try:
                for attempt in range(3):
                    try:
                        _, rules, _ = kernel(proof, verify_terminal=False, timeout=5)
                        break
                    except (OSError, ValueError, KeyError, TypeError, TimeoutError):
                        if attempt == 2:
                            raise
                        time.sleep(0.1)
                owned = [rule for rule in rules if rule.get("comment") == tag]
                c.require(len(owned) <= 1)
                if owned:
                    probe_packets(owned[0], str(address), source, port)
                    owned_handle = cast(int, owned[0]["handle"])
                    c.require(
                        type(owned_handle) is int
                        and owned_handle > 0
                        and (handle is None or owned_handle == handle)
                    )
                    try:
                        proof.run(
                            [
                                "nft",
                                "delete",
                                "rule",
                                "inet",
                                "dotmac_egress",
                                "out",
                                "handle",
                                str(owned_handle),
                            ],
                            timeout=5,
                        )
                    except (OSError, ValueError, KeyError, TypeError, TimeoutError):
                        pass  # A lost delete reply is resolved only by absence readback.
                # Repeat absence observation, including the ambiguous insert case.
                for _ in range(3):
                    _, remaining, _ = kernel(proof, timeout=5)
                    c.require(
                        not any(rule.get("comment") == tag for rule in remaining)
                        and remaining[-1]["handle"] == terminal_handle
                    )
                    time.sleep(0.1)
            except (OSError, ValueError, KeyError, TypeError, TimeoutError):
                raise ProbeCleanupUnproved("probe_rule_cleanup_unproved") from None


def network_after(
    proof: DaemonProof,
    hostname: str,
    start: NetworkStart,
    transition: TransitionEvidence,
    disallowed: str,
    port: int,
) -> EgressEvidence:
    c.require(
        c.timestamp(start.observed_at)
        <= c.timestamp(transition["before"]["observed_at"])
        < c.timestamp(transition["after"]["observed_at"])
    )
    expiry_observed = start.expired.result(
        timeout=max(0, 305 - (time.monotonic() - start.monotonic))
    )
    ttl = max(record["ttl"] for record in start.records)
    deadline = start.monotonic + 3300
    while time.monotonic() < deadline:
        if time.monotonic() - start.monotonic >= ttl:
            after = dns(proof, hostname)
            if all(
                {r["address"] for r in after if r["family"] == family}.isdisjoint(
                    {r["address"] for r in start.records if r["family"] == family}
                )
                for family in ("A", "AAAA")
            ):
                break
        time.sleep(1)
    else:
        raise ValueError("dns_rotation_not_observed")
    _, _, addresses = kernel(proof)
    c.require(
        {r["address"] for r in after} <= addresses.keys()
        and {r["address"] for r in start.records}.isdisjoint(addresses)
    )
    rules_hash, handle, source, initial, final = dropped_fresh_flow(
        proof, disallowed, port
    )
    runners_stopped(proof)
    c.require(healthy(proof.observe()))
    return {
        "hostname": hostname,
        "started_at": start.observed_at,
        "old_expiry_observed_at": expiry_observed,
        "finished_at": now(),
        "transition_report_id": transition["report_id"],
        "transition_report_sha256": transition["report_sha256"],
        "probe_rule_handle": handle,
        "probe_source_port": source,
        "probe_rule_removed": True,
        "before": start.records,
        "after": after,
        "elapsed_seconds": int(time.monotonic() - start.monotonic),
        "kernel_rules_sha256": rules_hash,
        "terminal_drop": True,
        "counter_before": initial,
        "counter_after": final,
        "disallowed_address": disallowed,
        "disallowed_port": port,
        "fresh_connection_failed": True,
        "allowed_deployment_succeeded": True,
    }


def forward(
    proof: DaemonProof,
    client: CPClient,
    desired: ForwardRequest,
    command_id: str,
    preparation: SourcePreparation,
    action_refs: list[str],
) -> TransitionEvidence:
    c._shape(desired, ForwardRequest)
    c.identifier(command_id)
    c.require(desired["deployment_ref"] == proof.config.deployment_ref)
    source_ready(proof, desired, preparation, action_refs)
    runners_stopped(proof)
    proof.control("stop")
    with proof.audit():
        before = proof.observe()
        c.require(
            healthy(before)
            and before["observed"]["running_image"]
            == IMAGE + "@" + desired["from_image_digest"]
        )
        c.require(desired["image_digest"] != desired["from_image_digest"])
        try:
            raw = issue_desired(proof, client, desired, command_id)
            doc = c.validate_desired(
                c.verify(
                    c.DESIRED,
                    cast(c.Envelope, c.read_json(raw)),
                    proof.enrollment["cp_public_b64"],
                ),
                now=datetime.now(UTC),
            )
            nonce_file = (
                proof.config.state_dir
                / "nonces"
                / (raw_digest(doc["nonce"].encode())[7:] + ".json")
            )
            c.require(
                not nonce_file.exists()
                and all(doc[key] == value for key, value in desired.items())
            )
        except (OSError, ValueError, KeyError, TypeError, TimeoutError):
            # CP may have published a fresh command. Leave the daemon stopped
            # until a human resolves the authoritative command and nonce.
            raise ForwardIssuanceUncertain("desired_issuance_unconfirmed") from None
        proof.control("start")
        deadline = time.monotonic() + min(
            2700,
            (c.timestamp(doc["expires_at"]) - datetime.now(UTC)).total_seconds(),
        )
        report_path = proof.config.state_dir / "outbox" / nonce_file.name
        while not report_path.exists() and time.monotonic() < deadline:
            time.sleep(1)
        report_raw = report_path.read_bytes()
        report = c.validate_applied(
            c.verify(
                c.APPLIED,
                cast(c.Envelope, c.read_json(report_raw)),
                proof.enrollment["host_public_b64"],
            )
        )
        c.require(
            report["disposition"] == "applied"
            and report["desired"] == {key: doc[key] for key in report["desired"]}
        )
        after = proof.observe()
        c.require(
            healthy(after)
            and after["observed"] == report["observed"]
            and after["executor_count"] - before["executor_count"] == 1
        )
        c.require(
            after["observed"]["running_image"] == IMAGE + "@" + doc["image_digest"]
        )
        c.require(
            after["observed"]["running_revision_label"] == doc["source_revision"]
            and after["observed"]["config_files"] == doc["config_files"]
        )
        c.require(
            proof.host.artifact(IMAGE, doc["image_digest"], doc["source_revision"])
            is None
        )
        commit = proof.published_report(report["report_id"], report_raw, raw)
        receipt = {
            "deployment_ref": proof.config.deployment_ref,
            "report_id": report["report_id"],
            "path": "applied/"
            + proof.config.deployment_ref
            + "/"
            + report["report_id"]
            + ".json",
            "fingerprint": c.digest(c.read_json(report_raw)),
        }
        runners_stopped(proof)
        c.require(proof.run(["systemctl", "is-active", UNIT]) == "active")
        return {
            "nonce": doc["nonce"],
            "desired_sha256": raw_digest(raw),
            "source_preparation_sha256": c.digest(preparation),
            "report_id": report["report_id"],
            "report_sha256": raw_digest(report_raw),
            "receipt_sha256": c.digest(receipt),
            "ledger_commit": commit,
            "before": before,
            "after": after,
            "signatures_verified": True,
            "runners_stopped": True,
        }


def issue_desired(
    proof: DaemonProof, client: CPClient, desired: ForwardRequest, command_id: str
) -> bytes:
    c._shape(desired, ForwardRequest)
    c.identifier(command_id)
    c.require(desired["deployment_ref"] == proof.config.deployment_ref)
    preview_desired = {
        key: value for key, value in desired.items() if key != "from_image_digest"
    }
    preview = c.read_json(
        client.request(
            "desired-state",
            c.canonical_json({"mode": "preview", "desired": preview_desired}),
        )
    )
    c.require(preview["plan_digest"] == c.plan_digest(desired))
    raw = client.request(
        "desired-state",
        c.canonical_json(
            {
                "command_id": command_id,
                "desired": desired,
                "approved_plan_digest": preview["plan_digest"],
            }
        ),
    )
    doc = c.validate_desired(
        c.verify(
            c.DESIRED,
            cast(c.Envelope, c.read_json(raw)),
            proof.enrollment["cp_public_b64"],
        ),
        now=datetime.now(UTC),
    )
    c.require(all(doc[key] == value for key, value in desired.items()))
    return raw
