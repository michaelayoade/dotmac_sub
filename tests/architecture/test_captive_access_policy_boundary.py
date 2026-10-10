"""Keep the composable captive policy single-owned and the raw flag retired."""

from __future__ import annotations

import ast
import re
from pathlib import Path

from app.services.sot_relationships import all_services

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "app"
SCRIPTS = ROOT / "scripts"
POLICY = APP / "services" / "captive_access_policy.py"
GATE = APP / "services" / "captive_router_gate.py"
CHANGE = APP / "services" / "captive_access_policy_change.py"
WALLED = APP / "services" / "walled_garden_policy.py"
CLI = SCRIPTS / "network" / "captive_access_policy.py"
OWNER_FILES = (POLICY, GATE, CHANGE, WALLED)


def _service(name: str):
    return next(item for item in all_services() if item.name == name)


def _python_files(*roots: Path):
    for root in roots:
        for path in root.rglob("*.py"):
            if "__pycache__" not in path.parts:
                yield path


def _calls(path: Path, name: str) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id == name:
                return True
            if isinstance(func, ast.Attribute) and func.attr == name:
                return True
    return False


def test_contract_modes_and_error_codes_match_the_code() -> None:
    from app.services.captive_access_policy import CaptiveAccessPolicyErrorCode
    from app.services.captive_access_policy_change import (
        CaptivePolicyChangeErrorCode,
    )

    policy = _service("access.captive_access_policy").contract
    gate = _service("access.captive_router_gate").contract
    change = _service("access.captive_access_policy_change").contract
    assert policy and gate and change
    assert policy.transaction.mode.value == "participant"
    assert gate.transaction.mode.value == "read_only"
    assert change.transaction.mode.value == "coordinator_managed"
    assert set(policy.errors.domain_codes) == set(CaptiveAccessPolicyErrorCode.ALL)
    assert set(CaptivePolicyChangeErrorCode.ALL) <= set(change.errors.domain_codes)
    walled = _service("access.walled_garden_policy")
    assert {"access.captive_access_policy", "access.captive_router_gate"} <= set(
        walled.depends_on
    )


def test_only_the_policy_owner_constructs_policy_records() -> None:
    constructors = re.compile(
        r"\b(CaptiveAccessRule|CaptiveCustomerSet|CaptiveCustomerSetMember)\("
    )
    offenders = [
        str(path.relative_to(ROOT))
        for path in _python_files(APP, SCRIPTS)
        if path not in {POLICY, APP / "models" / "captive_access_policy.py"}
        and constructors.search(path.read_text(encoding="utf-8"))
    ]
    assert not offenders, offenders


def test_only_the_coordinator_records_change_evidence() -> None:
    offenders = [
        str(path.relative_to(ROOT))
        for path in _python_files(APP, SCRIPTS)
        if path not in {CHANGE, APP / "models" / "captive_access_policy.py"}
        and "CaptiveAccessPolicyChange(" in path.read_text(encoding="utf-8")
    ]
    assert not offenders, offenders


def test_participant_writers_are_called_only_by_the_coordinator() -> None:
    for name in (
        "stage_captive_policy_change",
        "reevaluate_enforcement_lock_access_modes",
    ):
        callers = [
            str(path.relative_to(ROOT))
            for path in _python_files(APP, SCRIPTS)
            if _calls(path, name)
        ]
        allowed = ["app/services/captive_access_policy_change.py"]
        if name == "stage_captive_policy_change":
            # The participant re-enters itself only for validation helpers.
            callers = [
                item for item in callers if item != str(POLICY.relative_to(ROOT))
            ]
        assert callers == allowed, (name, callers)


def test_only_the_lifecycle_owner_mutates_lock_access_mode() -> None:
    pattern = re.compile(r"\.access_mode\s*=(?!=)")
    offenders = [
        str(path.relative_to(ROOT))
        for path in _python_files(APP)
        if path != APP / "services" / "account_lifecycle.py"
        and pattern.search(path.read_text(encoding="utf-8"))
    ]
    assert not offenders, offenders


def test_no_decision_path_reads_the_retired_account_flag() -> None:
    """``Subscriber.captive_redirect_enabled`` is readable but not a decision input.

    Only migration export/snapshot readers may touch the attribute.
    """

    allowed = {
        APP / "migration_source" / "snapshot.py",
        APP / "services" / "migration_source_export.py",
    }
    offenders: list[str] = []
    for path in _python_files(APP):
        if path in allowed:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and node.attr == "captive_redirect_enabled"
            ):
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "getattr"
                and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant)
                and node.args[1].value == "captive_redirect_enabled"
            ):
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert not offenders, offenders


def test_admin_customer_form_no_longer_writes_the_flag() -> None:
    template = (ROOT / "templates" / "admin" / "customers" / "form.html").read_text(
        encoding="utf-8"
    )
    assert 'name="captive_redirect_enabled"' not in template
    for relative in (
        "app/services/web_customer_actions.py",
        "app/services/web_subscriber_actions.py",
        "app/web/admin/customers.py",
    ):
        assert "captive_redirect_enabled" not in (ROOT / relative).read_text(
            encoding="utf-8"
        ), relative


def test_owners_have_no_transport_or_transaction_completion() -> None:
    for path in OWNER_FILES:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith(("fastapi", "celery")), path.name
            if isinstance(node, ast.Attribute) and node.attr in {
                "commit",
                "rollback",
                "begin_nested",
            }:
                raise AssertionError(f"{path.name}: .{node.attr}")
        assert "dict[str, Any]" not in source, path.name
        assert "from typing import Any" not in source, path.name


def test_cli_is_an_adapter() -> None:
    source = CLI.read_text(encoding="utf-8")
    tree = ast.parse(source)
    forbidden = {"commit", "rollback", "add", "delete", "flush", "execute"}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and node.attr in forbidden
            and isinstance(node.value, ast.Name)
            and node.value.id == "db"
        ):
            raise AssertionError(f"CLI calls db.{node.attr}")
    assert "owner_command_session()" in source
    assert "read_session()" in source
    assert "execute_owner_command" not in source


def test_gate_never_contacts_routers() -> None:
    source = GATE.read_text(encoding="utf-8")
    for name in (
        "RouterConnectionService",
        "fetch_config_export",
        "capture_from_router",
        "create_push",
    ):
        assert name not in source
