from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "app"


def test_terminal_service_order_states_have_one_writer() -> None:
    writers: list[str] = []
    for path in (APP / "services").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            if not any(
                isinstance(target, ast.Attribute)
                and target.attr == "status"
                for target in node.targets
            ):
                continue
            value = node.value
            if (
                isinstance(value, ast.Attribute)
                and isinstance(value.value, ast.Name)
                and value.value.id == "ServiceOrderStatus"
                and value.attr in {"active", "failed"}
            ):
                writers.append(path.relative_to(ROOT).as_posix())

    assert sorted(set(writers)) == ["app/services/service_order_lifecycle.py"]


def test_event_adapter_delegates_and_has_no_subscription_wide_completion() -> None:
    source = (APP / "services/events/handlers/provisioning.py").read_text(
        encoding="utf-8"
    )

    assert "evaluate_readiness(" in source
    assert "confirm_activation(" in source
    assert "_advance_order_on_run" not in source
    assert "_complete_service_orders" not in source


def test_raw_service_order_manager_cannot_activate_subscription() -> None:
    source = (APP / "services/provisioning_managers.py").read_text(encoding="utf-8")

    assert "activate_subscription" not in source
    assert "Active and failed states require a provisioning-readiness" in source
    assert "service_order_lifecycle.transition_service_order(" in source


def test_provisioning_lifecycle_owner_is_transport_free() -> None:
    source = (APP / "services/provisioning_lifecycle.py").read_text(encoding="utf-8")

    for prohibited in ("fastapi", "APIRouter", "celery", "DeviceProvisioner"):
        assert prohibited not in source
