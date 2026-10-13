"""Adapt Sub's existing read-only checks to the Foundation probe wire contract.

Foundation binds the executing image, role and phase. This adapter emits no
identity claims, material values, references or arbitrary diagnostics. It does
not install Foundation into the application or change the legacy executor.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from collections.abc import Sequence
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.services.deployment_secret_requirements import (
        DeploymentSecretRequirementsResult,
    )


class ProbeScope(StrEnum):
    BOOT = "boot"
    APPLICATION = "application"


class ProbePhase(StrEnum):
    PRE_MUTATION = "pre_mutation"
    POST_MIGRATION = "post_migration"


class ProbeStatus(StrEnum):
    READY = "ready"
    REFUSED = "refused"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class ProbeCheck:
    name: str
    status: ProbeStatus


@dataclass(frozen=True, slots=True)
class ProbeResult:
    checks: tuple[ProbeCheck, ...]

    @property
    def status(self) -> ProbeStatus:
        if any(check.status is ProbeStatus.REFUSED for check in self.checks):
            return ProbeStatus.REFUSED
        if not self.checks or any(
            check.status is ProbeStatus.UNAVAILABLE for check in self.checks
        ):
            return ProbeStatus.UNAVAILABLE
        return ProbeStatus.READY

    def as_document(self) -> dict[str, object]:
        return {
            "schema": "CandidateMaterialProbeResult.v1",
            "status": self.status.value,
            "checks": [
                {"name": check.name, "status": check.status.value}
                for check in self.checks
            ],
        }


def _boot_checks() -> tuple[ProbeCheck, ...]:
    from scripts.setup.verify_openbao_boot_secrets import (
        BootSecretStatus,
        check_boot_secrets,
    )

    statuses = {
        BootSecretStatus.READY: ProbeStatus.READY,
        # The existing owner produces ABSENT only for optional material.
        BootSecretStatus.ABSENT: ProbeStatus.READY,
        BootSecretStatus.INVALID: ProbeStatus.REFUSED,
        BootSecretStatus.UNAVAILABLE: ProbeStatus.UNAVAILABLE,
    }
    return tuple(
        ProbeCheck(f"boot.{check.name}", statuses[check.status])
        for check in check_boot_secrets().checks
    )


def _application_requirements(
    *,
    phase: ProbePhase,
    bootstrap_authorization: Path | None,
    candidate_revision: str | None,
    target_server: str | None,
) -> DeploymentSecretRequirementsResult:
    from datetime import UTC, datetime

    from app.db import read_only_snapshot_session
    from app.services.deployment_secret_requirements import (
        DeploymentSecretRequirementsQuery,
        RequirementEvaluationPhase,
        evaluate_deployment_secret_requirements,
    )
    from app.services.deployment_secret_schema import (
        DeploymentSecretSchemaQuery,
        evaluate_deployment_secret_schema,
    )
    from scripts.release_artifact_contract import GitCommitSha, ProductionServerName

    query = DeploymentSecretSchemaQuery(
        bootstrap_authorization_path=bootstrap_authorization,
        candidate_revision=GitCommitSha(candidate_revision)
        if candidate_revision is not None
        else None,
        target_server=ProductionServerName(target_server)
        if target_server is not None
        else None,
    )
    with read_only_snapshot_session() as db:
        schema = evaluate_deployment_secret_schema(db, query=query)
        return evaluate_deployment_secret_requirements(
            db,
            DeploymentSecretRequirementsQuery(
                datetime.now(UTC),
                schema,
                RequirementEvaluationPhase.POST_MIGRATION
                if phase is ProbePhase.POST_MIGRATION
                else RequirementEvaluationPhase.PRE_MIGRATION,
            ),
        )


def _requirement_checks(
    result: DeploymentSecretRequirementsResult,
) -> tuple[ProbeCheck, ...]:
    from app.services.deployment_secret_requirements import (
        MaterialStatus,
        SecretRequirement,
    )

    grouped: dict[str, list[ProbeCheck]] = {}
    for check in result.checks:
        status = (
            ProbeStatus.READY
            if check.ok
            else ProbeStatus.UNAVAILABLE
            if check.material is MaterialStatus.UNAVAILABLE
            or check.requirement is SecretRequirement.EVALUATION_UNAVAILABLE
            else ProbeStatus.REFUSED
        )
        name = f"deployment.{check.name.value}"
        grouped.setdefault(name, []).append(ProbeCheck(name, status))
    return (
        ProbeCheck(
            "deployment.schema",
            ProbeStatus.READY if result.schema.ok else ProbeStatus.UNAVAILABLE,
        ),
        *(
            ProbeCheck(name, ProbeResult(tuple(checks)).status)
            for name, checks in sorted(grouped.items())
        ),
    )


def evaluate_probe(
    *,
    scope: ProbeScope,
    phase: ProbePhase,
    bootstrap_authorization: Path | None = None,
    candidate_revision: str | None = None,
    target_server: str | None = None,
) -> ProbeResult:
    """Delegate semantics to the accepted owners; discard transport output."""
    previous_logging_disable = logging.root.manager.disable
    with open(os.devnull, "w", encoding="utf-8") as sink:
        with redirect_stdout(sink), redirect_stderr(sink):
            logging.disable(2**63 - 1)
            try:
                checks = _boot_checks()
                if checks and ProbeResult(checks).status is not ProbeStatus.READY:
                    return ProbeResult(checks)
                if scope is ProbeScope.APPLICATION:
                    checks += _requirement_checks(
                        _application_requirements(
                            phase=phase,
                            bootstrap_authorization=bootstrap_authorization,
                            candidate_revision=candidate_revision,
                            target_server=target_server,
                        )
                    )
                if not checks:
                    return ProbeResult(
                        (ProbeCheck("probe.evaluation", ProbeStatus.UNAVAILABLE),)
                    )
                return ProbeResult(checks)
            except Exception:
                return ProbeResult(
                    (ProbeCheck("probe.evaluation", ProbeStatus.UNAVAILABLE),)
                )
            finally:
                logging.disable(previous_logging_disable)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scope", type=ProbeScope, choices=tuple(ProbeScope), required=True
    )
    parser.add_argument(
        "--phase", type=ProbePhase, choices=tuple(ProbePhase), required=True
    )
    parser.add_argument("--bootstrap-authorization", type=Path)
    parser.add_argument("--candidate-revision")
    parser.add_argument("--target-server")
    args = parser.parse_args(argv)
    result = evaluate_probe(
        scope=args.scope,
        phase=args.phase,
        bootstrap_authorization=args.bootstrap_authorization,
        candidate_revision=args.candidate_revision,
        target_server=args.target_server,
    )
    print(json.dumps(result.as_document(), separators=(",", ":"), sort_keys=True))
    return 0 if result.status is ProbeStatus.READY else 1


if __name__ == "__main__":
    raise SystemExit(main())
