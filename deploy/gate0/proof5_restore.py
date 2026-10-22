"""Dedicated-host proof5; consumes only root-protected human-transferred captures."""

from __future__ import annotations

import json
import re
import subprocess
import zlib
from datetime import UTC, datetime
from pathlib import Path

from deploy.gate0.evidence import ProofOutcome, RestoreEvidence, empty_outcome
from deploy.gate0.restore_capture import (
    SOURCE,
    CapturePlan,
    FreezeObservation,
    RestoreCapture,
    bundle_time,
    pinned_file,
    protected_directory,
    utc_now,
    verify_tools,
)
from scripts.restore_drill import (
    RestoreVerdict,
    drill_bundle,
    inventory_digest,
    sha256_file,
)
from scripts.restore_guard import (
    HostPurpose,
    RestoreRefusal,
    enrolled,
    protected,
    unique_object,
)
from scripts.restore_inventory import parse_inventory


def proof5(directory: Path, image: str, plan_path: Path) -> ProofOutcome:
    """Enrolled-host guard precedes any Docker subprocess or private file read."""
    permit = enrolled(HostPurpose.RESTORE)
    if not re.fullmatch(
        r"(?:[a-z0-9.-]+(?::[0-9]+)?/)?[a-z0-9_./-]+@sha256:[0-9a-f]{64}", image
    ):
        raise RestoreRefusal("restore_image_unpinned")
    plan = CapturePlan.load(plan_path)
    if plan.capture_sha256 is None or image != plan.image:
        raise RestoreRefusal("restore_plan_phase")
    pinned_file(Path("/etc/dotmac/gate0-host.json"), plan.restore_permit_sha256)
    verify_tools(plan)
    plan.window(utc_now(), utc_now())
    protected_directory(directory)
    record_path = directory / "capture.json"
    inventory_path = directory / "inventory.json"
    protected(record_path)
    protected(inventory_path)
    if (
        record_path.stat().st_size > 65536
        or inventory_path.stat().st_size > 64 * 1024 * 1024
    ):
        raise RestoreRefusal("capture_size")
    if "sha256:" + sha256_file(record_path) != plan.capture_sha256:
        raise RestoreRefusal("capture_hash_unapproved")
    try:
        record = json.loads(record_path.read_text(), object_pairs_hook=unique_object)
        keys = {
            "source_identity",
            "started_at",
            "finished_at",
            "bundle_sha256",
            "inventory_sha256",
            "inventory_file_sha256",
            "owner_sha256",
            "permit_sha256",
            "freeze",
            "context",
            "source_revision",
            "source_tree",
            "tool_revision",
            "library_sha256",
            "capture_tool_sha256",
            "restore_tool_sha256",
        }
        if (
            not isinstance(record, dict)
            or set(record) != keys
            or record["source_identity"] != SOURCE
        ):
            raise ValueError
        if record["context"] != plan.context or any(
            record[key] != plan.context[key]
            for key in ("source_revision", "source_tree", "tool_revision")
        ):
            raise ValueError
        for key in (
            "owner_sha256",
            "library_sha256",
            "capture_tool_sha256",
            "restore_tool_sha256",
        ):
            if "sha256:" + str(record[key]) != getattr(plan, key):
                raise ValueError
        if "sha256:" + str(record["permit_sha256"]) != plan.source_permit_sha256:
            raise ValueError
        for key in {
            "bundle_sha256",
            "inventory_sha256",
            "inventory_file_sha256",
            "owner_sha256",
            "permit_sha256",
            "library_sha256",
            "capture_tool_sha256",
            "restore_tool_sha256",
        }:
            if not isinstance(record[key], str) or not re.fullmatch(
                r"[0-9a-f]{64}", record[key]
            ):
                raise ValueError
        start, finish = (
            datetime.fromisoformat(record[key]) for key in ("started_at", "finished_at")
        )
        if start.tzinfo is None or finish.tzinfo is None or start >= finish:
            raise ValueError
        plan.window(record["started_at"], record["finished_at"])
        freeze = record["freeze"]
        if (
            not isinstance(freeze, dict)
            or set(freeze) != {"pid", "relations"}
            or type(freeze["pid"]) is not int
            or freeze["pid"] <= 0
            or not isinstance(freeze["relations"], list)
            or len(freeze["relations"]) < 20
            or any(type(x) is not int or x <= 0 for x in freeze["relations"])
            or len(set(freeze["relations"])) != len(freeze["relations"])
        ):
            raise ValueError
        manifests = list(directory.glob("*.manifest.json"))
        if len(manifests) != 1:
            raise ValueError
        manifest = manifests[0]
        protected(manifest)
        if (
            sha256_file(manifest) != record["bundle_sha256"]
            or sha256_file(inventory_path) != record["inventory_file_sha256"]
        ):
            raise ValueError
        inventory = parse_inventory(inventory_path.read_text())
        if inventory_digest(inventory) != record["inventory_sha256"]:
            raise ValueError
        from scripts.restore_drill import load_bundle

        bundle, artifacts = load_bundle(manifest)
        if (
            bundle.database != "dotmac_sub"
            or bundle.git_sha != plan.context["source_revision"]
        ):
            raise ValueError
        bundle_time(
            plan, bundle.created_at, record["started_at"], record["finished_at"]
        )
        for path in artifacts.values():
            protected(path)
        capture_record = RestoreCapture(
            **{
                **record,
                "freeze": FreezeObservation(freeze["pid"], tuple(freeze["relations"])),
            }
        )
    except (ValueError, KeyError, TypeError, OSError):
        raise RestoreRefusal("capture_invalid") from None
    outcome = empty_outcome(5)
    evidence: RestoreEvidence = {
        "source_identity": SOURCE,
        "test_host_identity": permit.hostname,
        "bundle_sha256": "sha256:" + capture_record.bundle_sha256,
        "capture_sha256": "sha256:" + sha256_file(record_path),
        "capture_started_at": start.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "capture_finished_at": finish.astimezone(UTC)
        .isoformat()
        .replace("+00:00", "Z"),
        "source_inventory_sha256": "sha256:" + capture_record.inventory_sha256,
        "restored_inventory_sha256": None,
        "no_ddl_enforced": True,
        "source_parity": None,
        "scratch_absent": None,
    }
    outcome["restore"] = evidence
    # Preserve admitted capture facts when actual restore observations are unknown.
    try:
        result = drill_bundle(manifest, image, expected_inventory=inventory)
    except (
        OSError,
        RuntimeError,
        ValueError,
        EOFError,
        zlib.error,
        subprocess.SubprocessError,
    ):
        outcome["failure_code"] = "cleanup_failed"
        outcome["cleanup"] = "fail"
        return outcome
    plan.window(utc_now(), utc_now())
    evidence["restored_inventory_sha256"] = (
        ("sha256:" + result.restored_inventory_sha256)
        if result.restored_inventory_sha256
        else None
    )
    evidence["source_parity"] = result.source_parity
    evidence["scratch_absent"] = result.scratch_absent
    outcome["cleanup"] = "pass" if result.scratch_absent else "fail"
    if (
        result.verdict is RestoreVerdict.PASS
        and result.source_parity
        and result.scratch_absent
        and evidence["restored_inventory_sha256"] is not None
        and result.restored_inventory_sha256 == capture_record.inventory_sha256
    ):
        outcome["outcome"] = "pass"
        outcome["failure_code"] = "none"
    else:
        outcome["failure_code"] = "observation_mismatch"
    return outcome


def main() -> int:
    """Write one public outcome in the private transfer directory; no gate PASS."""
    import argparse
    import os

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--plan", type=Path, required=True)
    args = parser.parse_args()
    try:
        outcome = proof5(args.capture, args.image, args.plan)
        output = args.capture / "proof5-outcome.json"
        descriptor = os.open(
            output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(descriptor, "w") as handle:
            json.dump(outcome, handle, sort_keys=True)
            handle.write("\n")
        print("PROOF5_OUTCOME_WRITTEN")
        return 0 if outcome["outcome"] == "pass" else 1
    except (
        OSError,
        RuntimeError,
        ValueError,
        EOFError,
        zlib.error,
        subprocess.SubprocessError,
    ):
        print("PROOF5_REFUSED")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
