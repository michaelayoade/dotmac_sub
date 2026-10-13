"""Check declared secret obligations in the candidate app's read-only snapshot."""

from __future__ import annotations

import argparse
import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from app.db import read_only_snapshot_session
from app.services.deployment_secret_requirements import (
    DeploymentSecretRequirementsQuery,
    RequirementEvaluationPhase,
    evaluate_deployment_secret_requirements,
    unavailable_requirements,
)
from app.services.deployment_secret_schema import (
    DeploymentSecretSchemaQuery,
    evaluate_deployment_secret_schema,
)
from scripts.release_artifact_contract import GitCommitSha, ProductionServerName


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--component", choices=("app",), default="app")
    parser.add_argument("--bootstrap-authorization", type=Path)
    parser.add_argument("--candidate-revision")
    parser.add_argument("--target-server")
    parser.add_argument("--require-installed-schema", action="store_true")
    args = parser.parse_args(argv)
    # The existing secret transport logs URLs and exception text. Keep this
    # operator adapter's output to the bounded typed receipt, even on failure.
    previous_logging_disable = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        try:
            schema_query = DeploymentSecretSchemaQuery(
                bootstrap_authorization_path=args.bootstrap_authorization,
                candidate_revision=GitCommitSha(args.candidate_revision)
                if args.candidate_revision is not None
                else None,
                target_server=ProductionServerName(args.target_server)
                if args.target_server is not None
                else None,
            )
            with read_only_snapshot_session() as db:
                schema = evaluate_deployment_secret_schema(db, query=schema_query)
                result = evaluate_deployment_secret_requirements(
                    db,
                    DeploymentSecretRequirementsQuery(
                        datetime.now(UTC),
                        schema,
                        RequirementEvaluationPhase.POST_MIGRATION
                        if args.require_installed_schema
                        else RequirementEvaluationPhase.PRE_MIGRATION,
                    ),
                )
        except Exception:
            result = unavailable_requirements()
    finally:
        logging.disable(previous_logging_disable)
    print(
        f"Declared secret requirements component={args.component} "
        f"coverage={result.coverage.value} "
        f"enabled_installations={result.enabled_installation_count}"
    )
    print(
        f"Schema capabilities integrations={result.schema.integrations.value} "
        f"machine_credentials={result.schema.machine_credentials.value} "
        f"machine_attribution={'available' if result.schema.machine_attribution_available else 'not_installed'} "
        f"authoritative={str(result.schema.authoritative).lower()}"
    )
    for check in result.checks:
        print(
            f"{check.name.value}: requirement={check.requirement.value} "
            f"material={check.material.value} count={check.count}"
        )
    print(
        "Declared secret requirements satisfied."
        if result.ok
        else "Declared secret requirements failed."
    )
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
