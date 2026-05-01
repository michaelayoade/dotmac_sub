"""Architecture guardrails for ONT ownership boundaries."""

from __future__ import annotations

import ast
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
APP_DIR = PROJECT_ROOT / "app"

STATUS_WRITE_PATTERN = re.compile(
    r"\.("
    r"olt_status|olt_status_seen_at|acs_last_inform_at|effective_status|"
    r"effective_status_source|consecutive_offline_polls"
    r")\s*(?<![=!<>])=(?!=)"
)
DESIRED_CONFIG_WRITE_PATTERN = re.compile(r"\.desired_config\s*(?<![=!<>])=(?!=)")
ACS_FACTORY_PATTERN = re.compile(
    r"\bcreate_acs_(config_writer|state_reader|event_ingestor)\b"
)

APPROVED_STATUS_WRITERS = {
    Path("app/services/network/ont_status.py"),
    Path("app/services/network/olt_autofind.py"),
    Path("app/services/network/ont_authorization.py"),
    Path("app/services/network/ont_decommission.py"),
    Path("app/tasks/olt_polling.py"),
}

APPROVED_DESIRED_CONFIG_WRITERS = {
    Path("app/models/network.py"),
    Path("app/services/network/ont_desired_config.py"),
}

APPROVED_ACS_FACTORY_USERS = {
    Path("app/services/genieacs_client.py"),
    Path("app/services/genieacs_service.py"),
}

APPROVED_PROVISION_WITH_RECONCILIATION_CALLERS = {
    Path("app/services/network/ont_provision_steps.py"),
    Path("app/services/network/ont_provisioning/orchestrator.py"),
}

def _iter_app_python_files() -> list[Path]:
    return sorted(path for path in APP_DIR.rglob("*.py") if path.is_file())


def _violations(pattern: re.Pattern[str], approved: set[Path]) -> list[str]:
    violations: list[str] = []
    for path in _iter_app_python_files():
        rel = path.relative_to(PROJECT_ROOT)
        if rel in approved:
            continue
        text = path.read_text(encoding="utf-8")
        for match in pattern.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            violations.append(f"{rel}:{line}")
    return violations


def _attribute_violations(attr_name: str, approved: set[Path]) -> list[str]:
    violations: list[str] = []
    for path in _iter_app_python_files():
        rel = path.relative_to(PROJECT_ROOT)
        if rel in approved:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(rel))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == attr_name:
                violations.append(f"{rel}:{node.lineno}")
    return violations


def _call_violations(function_name: str, approved: set[Path]) -> list[str]:
    violations: list[str] = []
    for path in _iter_app_python_files():
        rel = path.relative_to(PROJECT_ROOT)
        if rel in approved:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(rel))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name) and func.id == function_name:
                violations.append(f"{rel}:{node.lineno}")
            elif isinstance(func, ast.Attribute) and func.attr == function_name:
                violations.append(f"{rel}:{node.lineno}")
    return violations


def test_ont_status_snapshot_writes_stay_in_status_owner_modules() -> None:
    violations = _violations(STATUS_WRITE_PATTERN, APPROVED_STATUS_WRITERS)

    assert not violations, "\n".join(violations)


def test_ont_desired_config_writes_stay_in_desired_config_owner_modules() -> None:
    violations = _violations(
        DESIRED_CONFIG_WRITE_PATTERN,
        APPROVED_DESIRED_CONFIG_WRITERS,
    )

    assert not violations, "\n".join(violations)


def test_ont_desired_config_reads_stay_in_desired_config_owner_modules() -> None:
    violations = _attribute_violations("desired_config", APPROVED_DESIRED_CONFIG_WRITERS)

    assert not violations, "\n".join(violations)


def test_application_code_uses_acs_service_facade_not_raw_acs_factories() -> None:
    violations = _violations(ACS_FACTORY_PATTERN, APPROVED_ACS_FACTORY_USERS)

    assert not violations, "\n".join(violations)


def test_full_ont_provisioning_uses_orchestrator_entrypoint() -> None:
    violations = _call_violations(
        "provision_with_reconciliation",
        APPROVED_PROVISION_WITH_RECONCILIATION_CALLERS,
    )

    assert not violations, "\n".join(violations)
