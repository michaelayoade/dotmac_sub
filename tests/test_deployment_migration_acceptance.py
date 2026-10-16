"""Refusal canaries for the hosted real migration acceptance adapter."""

from __future__ import annotations

import ast
from dataclasses import replace
from pathlib import Path

import pytest

from scripts.ci.deployment_migration_acceptance import (
    AcceptanceRefused,
    ImageIdentity,
    MigrationScenarioSpec,
    require_hosted_dispatch,
    validate_identity,
)


def test_hosted_lane_refuses_local_and_non_dispatch_execution() -> None:
    for environment in ({}, {"GITHUB_ACTIONS": "true"}, {"GITHUB_EVENT_NAME": "push"}):
        with pytest.raises(AcceptanceRefused, match="hosted_dispatch_required"):
            require_hosted_dispatch(environment)


def test_identity_rejects_mutable_or_malformed_inputs() -> None:
    valid = ImageIdentity("a" * 40, "b" * 40, "sha256:" + "c" * 64, 10)
    for invalid in (
        replace(valid, revision="main"),
        replace(valid, tree="HEAD"),
        replace(valid, digest="latest"),
        replace(valid, build_run_id=True),
    ):
        with pytest.raises(AcceptanceRefused, match="image_identity_unproved"):
            validate_identity(invalid)


def test_child_spec_refuses_untyped_or_extra_inputs_before_docker(
    tmp_path: Path,
) -> None:
    import json

    valid_shape: dict[str, object] = {
        "deploy_directory": str(tmp_path),
        "binding": {},
        "run_id": 1,
        "authorization_run_id": 1,
        "heads_before": ["690"],
        "candidate_heads": ["691"],
        "budget_seconds": 1,
    }
    for document in (
        {},
        {**valid_shape, "run_id": True},
        {**valid_shape, "heads_before": "690"},
        {**valid_shape, "deploy_directory": "relative"},
        {**valid_shape, "extra": "ignored"},
    ):
        path = tmp_path / "child-spec.json"
        path.write_text(json.dumps(document))
        with pytest.raises(AcceptanceRefused, match="scenario_spec_unproved"):
            MigrationScenarioSpec.read(path)


def test_harness_never_calls_full_deployment_wrapper_or_schema_substitutes() -> None:
    root = Path(__file__).resolve().parents[1]
    for filename in (
        "deployment_migration_acceptance.py",
        "deployment_migration_fault_child.py",
    ):
        source = (root / "scripts" / "ci" / filename).read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr not in {
                    "create_all",
                    "stamp",
                    "run_trusted_deployment",
                }
        assert '"scripts/deploy.sh"' not in source
        assert '"prune"' not in source


def test_fault_hooks_delegate_real_operations_before_acknowledgement_loss() -> None:
    root = Path(__file__).resolve().parents[1]
    source = (root / "scripts/ci/deployment_migration_fault_child.py").read_text()
    tree = ast.parse(source)
    hooks = {
        node.name: node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
    }
    operation = ast.unparse(hooks["run_with_ack_loss"])
    assert operation.index("original_run(") < operation.index(
        "raise probe.ProbeIndeterminate"
    )
    durable = ast.unparse(hooks["write_with_crash"])
    assert durable.index("original_write(") < durable.index("os._exit(")
