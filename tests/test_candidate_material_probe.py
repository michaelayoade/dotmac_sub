"""Wire adapter canaries; schema authority remains the migrated PostgreSQL lane."""

from __future__ import annotations

import json
import logging
import re
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock

import pytest
from sqlalchemy.orm import Session

from app.services import deployment_secret_requirements as requirements
from app.services import deployment_secret_schema as schemas
from scripts.setup import verify_candidate_materials as probe
from scripts.setup import verify_openbao_boot_secrets as boot


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (boot.BootSecretStatus.READY, probe.ProbeStatus.READY),
        (boot.BootSecretStatus.ABSENT, probe.ProbeStatus.READY),
        (boot.BootSecretStatus.INVALID, probe.ProbeStatus.REFUSED),
        (boot.BootSecretStatus.UNAVAILABLE, probe.ProbeStatus.UNAVAILABLE),
    ],
)
def test_boot_adapter_preserves_optional_and_failure_policy(
    monkeypatch: pytest.MonkeyPatch,
    status: boot.BootSecretStatus,
    expected: probe.ProbeStatus,
) -> None:
    monkeypatch.setattr(
        boot,
        "check_boot_secrets",
        lambda: boot.BootSecretPreflightResult((boot.BootSecretCheck("key", status),)),
    )
    monkeypatch.setattr(
        probe,
        "_application_requirements",
        lambda **kwargs: pytest.fail("Boot scope must not open an app snapshot"),
    )
    result = probe.evaluate_probe(
        scope=probe.ProbeScope.BOOT, phase=probe.ProbePhase.POST_MIGRATION
    )
    assert result.status is expected
    assert result.as_document() == {
        "schema": "CandidateMaterialProbeResult.v1",
        "status": expected.value,
        "checks": [{"name": "boot.key", "status": expected.value}],
    }


def _schema(*, available: bool = True) -> schemas.DeploymentSecretSchemaResult:
    state = (
        schemas.SchemaCapabilityState.EVALUATED
        if available
        else schemas.SchemaCapabilityState.EVALUATION_UNAVAILABLE
    )
    return schemas.DeploymentSecretSchemaResult(state, state, available)


def test_repeated_requirement_names_reduce_to_strongest_status() -> None:
    checks = tuple(
        requirements.SecretRequirementCheck(
            requirements.RequirementName.INTEGRATION_BINDINGS,
            requirements.SecretRequirement.DEPLOYMENT_REQUIRED,
            status,
            1,
        )
        for status in (
            requirements.MaterialStatus.READY,
            requirements.MaterialStatus.UNAVAILABLE,
            requirements.MaterialStatus.ABSENT,
        )
    )
    result = probe._requirement_checks(
        requirements.DeploymentSecretRequirementsResult(checks, _schema())
    )
    assert result == (
        probe.ProbeCheck("deployment.schema", probe.ProbeStatus.READY),
        probe.ProbeCheck(
            "deployment.integration_required_bindings", probe.ProbeStatus.REFUSED
        ),
    )


def test_operation_only_absence_is_ready_but_failed_schema_is_not() -> None:
    result = probe._requirement_checks(
        requirements.DeploymentSecretRequirementsResult(
            (
                requirements.SecretRequirementCheck(
                    requirements.RequirementName.CONVERSION_INGEST,
                    requirements.SecretRequirement.OPERATION_ONLY,
                    requirements.MaterialStatus.ABSENT,
                    1,
                ),
            ),
            _schema(available=False),
        )
    )
    assert (
        probe.ProbeCheck(
            "deployment.conversion_ingest_api_key", probe.ProbeStatus.READY
        )
        in result
    )
    assert probe.ProbeResult(result).status is probe.ProbeStatus.UNAVAILABLE


@pytest.mark.parametrize(
    ("phase", "expected"),
    [
        (
            probe.ProbePhase.PRE_MUTATION,
            requirements.RequirementEvaluationPhase.PRE_MIGRATION,
        ),
        (
            probe.ProbePhase.POST_MIGRATION,
            requirements.RequirementEvaluationPhase.POST_MIGRATION,
        ),
    ],
)
def test_app_delegates_phase_and_bootstrap_in_one_read_only_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    phase: probe.ProbePhase,
    expected: requirements.RequirementEvaluationPhase,
) -> None:
    import app.db as database

    session = Mock(spec=Session)
    schema = _schema()
    schema_owner = Mock(return_value=schema)
    result = requirements.DeploymentSecretRequirementsResult((), schema)
    requirement_owner = Mock(return_value=result)
    snapshot_events: list[str] = []

    @contextmanager
    def snapshot() -> Iterator[Session]:
        snapshot_events.append("entered")
        yield session
        snapshot_events.append("closed")

    monkeypatch.setattr(database, "read_only_snapshot_session", snapshot)
    monkeypatch.setattr(schemas, "evaluate_deployment_secret_schema", schema_owner)
    monkeypatch.setattr(
        requirements, "evaluate_deployment_secret_requirements", requirement_owner
    )
    authorization = Path("/protected/bootstrap.json")
    assert (
        probe._application_requirements(
            phase=phase,
            bootstrap_authorization=authorization,
            candidate_revision="a" * 40,
            target_server="dotmac-sub-prod",
        )
        is result
    )
    assert snapshot_events == ["entered", "closed"]
    assert schema_owner.call_args.args == (session,)
    query = schema_owner.call_args.kwargs["query"]
    assert query.bootstrap_authorization_path == authorization
    assert query.candidate_revision.value == "a" * 40
    assert query.target_server.value == "dotmac-sub-prod"
    args = requirement_owner.call_args.args
    assert args[0] is session
    assert args[1].schema is schema
    assert args[1].phase is expected
    session.commit.assert_not_called()
    session.flush.assert_not_called()


def test_cli_discards_values_from_logs_streams_and_exceptions(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    canary = "sensitive-material-canary-must-not-leave-probe"
    original = logging.root.manager.disable

    def broken() -> tuple[probe.ProbeCheck, ...]:
        print(canary)
        print(canary, file=sys.stderr)
        logging.log(10000, canary)
        raise RuntimeError(canary)

    monkeypatch.setattr(probe, "_boot_checks", broken)
    assert probe.main(["--scope", "application", "--phase", "pre_mutation"]) == 1
    captured = capsys.readouterr()
    assert canary not in captured.out + captured.err
    assert captured.err == ""
    assert json.loads(captured.out) == {
        "schema": "CandidateMaterialProbeResult.v1",
        "status": "unavailable",
        "checks": [{"name": "probe.evaluation", "status": "unavailable"}],
    }
    assert logging.root.manager.disable == original


def test_cli_success_emits_one_document_and_restores_logging(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    original = logging.root.manager.disable
    monkeypatch.setattr(
        probe,
        "_boot_checks",
        lambda: (probe.ProbeCheck("boot.key", probe.ProbeStatus.READY),),
    )
    assert probe.main(["--scope", "boot", "--phase", "post_migration"]) == 0
    captured = capsys.readouterr()
    assert len(captured.out.splitlines()) == 1
    assert json.loads(captured.out)["status"] == "ready"
    assert captured.err == ""
    assert logging.root.manager.disable == original


def test_empty_inventory_never_emits_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(probe, "_boot_checks", lambda: ())
    result = probe.evaluate_probe(
        scope=probe.ProbeScope.BOOT, phase=probe.ProbePhase.PRE_MUTATION
    )
    assert result.checks
    assert result.status is probe.ProbeStatus.UNAVAILABLE


def test_declared_owner_inventory_fits_closed_wire_without_dynamic_identifiers() -> (
    None
):
    names = (
        *(f"boot.{name}" for name in boot.REQUIRED_REFS),
        *(f"boot.{name}" for name in boot.OPTIONAL_REFS),
        "deployment.schema",
        *(f"deployment.{name.value}" for name in requirements.RequirementName),
    )
    assert 1 <= len(names) <= 64
    assert len(set(names)) == len(names)
    assert all(re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,95}", name) for name in names)
    result = probe.ProbeResult(
        tuple(probe.ProbeCheck(name, probe.ProbeStatus.UNAVAILABLE) for name in names)
    )
    assert len(json.dumps(result.as_document()).encode("utf-8")) <= 16384


def test_failed_boot_admission_does_not_open_an_application_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        probe,
        "_boot_checks",
        lambda: (probe.ProbeCheck("boot.key", probe.ProbeStatus.REFUSED),),
    )
    monkeypatch.setattr(
        probe,
        "_application_requirements",
        lambda **kwargs: pytest.fail("Rejected boot must stop before database reads"),
    )
    result = probe.evaluate_probe(
        scope=probe.ProbeScope.APPLICATION, phase=probe.ProbePhase.PRE_MUTATION
    )
    assert result.status is probe.ProbeStatus.REFUSED
