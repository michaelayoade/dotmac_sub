"""Hosted-only fault transport around real migration operations.

Never substitute inspect, Alembic, floor readers, authorization, or classification.
The child dies only after a journal fsync, or loses an acknowledgement only after
Docker has returned from the actual operation. Inputs contain public bindings.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path

from scripts import production_deploy_migration as migration
from scripts import production_deploy_probe as probe
from scripts import production_trusted_deploy as trusted
from scripts.ci.deployment_migration_acceptance import (
    MigrationScenarioSpec,
    require_hosted_dispatch,
)


def main() -> int:
    require_hosted_dispatch(dict(os.environ))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path)
    parser.add_argument("--fault", default="none")
    parser.add_argument("--supervise-marker", type=Path)
    args = parser.parse_args()
    if args.supervise_marker is not None:
        descriptor = trusted._acquire_canonical_lock()
        try:
            return trusted._wait_for_child(
                [
                    "python3",
                    "-c",
                    "import os,pathlib,signal,time; "
                    "a=os.fstat(8); b=os.stat('/var/lock/dotmac_sub_deploy.lock'); "
                    "assert (a.st_dev,a.st_ino)==(b.st_dev,b.st_ino); "
                    "pathlib.Path(__import__('sys').argv[1]).write_text('FD8 verified'); "
                    "time.sleep(120)",
                    str(args.supervise_marker),
                ],
                environment=dict(os.environ),
                cwd=Path.cwd(),
            )
        finally:
            os.close(descriptor)
    if args.spec is None:
        parser.error("--spec is required for migration faults")
    spec = MigrationScenarioSpec.read(args.spec)
    fault: str = args.fault
    original_run = probe._run
    original_write = migration._write
    fired = False

    def run_with_ack_loss(
        command: list[str],
        *,
        input_text: str | None = None,
        timeout: int = 30,
        side_effecting: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        nonlocal fired
        result = original_run(
            command,
            input_text=input_text,
            timeout=timeout,
            side_effecting=side_effecting,
        )
        targets = {
            "ack-create": "create",
            "ack-start": "start",
            "ack-remove": "rm",
        }
        # Only the journaled runner is targeted; observation probes stay real.
        record, _digest = migration._load_journal(
            migration._journal_path(spec.deploy_directory)
        )
        runner = record.identity if record is not None else None
        is_runner = runner is not None and (
            runner.container_name in command
            or (runner.container_id is not None and runner.container_id in command)
        )
        if (
            not fired
            and side_effecting
            and is_runner
            and command[:2] == ["docker", targets.get(fault, "")]
            and result.returncode == 0
        ):
            fired = True
            (args.spec.parent / "fault-event.json").write_text(
                json.dumps(
                    {
                        "fault": fault,
                        "operation": command[1],
                        "invocation_id": runner.invocation_id,
                    }
                )
            )
            raise probe.ProbeIndeterminate("acceptance_acknowledgement_dropped")
        return result

    def write_with_crash(path: Path, record: migration.MigrationInvocation) -> None:
        original_write(path, record)
        with (args.spec.parent / "phase-events.jsonl").open("a") as stream:
            stream.write(
                json.dumps(
                    {
                        "phase": record.phase.value,
                        "container_id": record.identity.container_id,
                        "invocation_id": record.identity.invocation_id,
                    }
                )
                + "\n"
            )
            stream.flush()
            os.fsync(stream.fileno())
        if fault == f"crash-{record.phase.value}":
            os._exit(86)

    probe._run = run_with_ack_loss
    migration._write = write_with_crash
    try:
        return migration.run_migration(
            deploy_directory=spec.deploy_directory,
            binding_json=json.dumps(spec.binding.public_json()),
            run_id=spec.run_id,
            authorization_run_id=spec.authorization_run_id,
            target_revision=spec.binding.candidate_revision,
            candidate_digest=spec.binding.candidate_digest,
            heads_before=spec.heads_before,
            candidate_heads=spec.candidate_heads,
            budget_seconds=spec.budget_seconds,
        )
    except (migration.MigrationRefused, probe.ProbeRefused):
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
