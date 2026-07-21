"""Keep event-driven access decisions behind their contracted policy owner."""

from __future__ import annotations

import ast
from pathlib import Path

from app.services.sot_relationships import all_services

PROJECT_ROOT = Path(__file__).resolve().parents[2]
OWNER = PROJECT_ROOT / "app" / "services" / "enforcement_event_policy.py"
HANDLER = PROJECT_ROOT / "app" / "services" / "events" / "handlers" / "enforcement.py"
RADIUS_PROJECTION = PROJECT_ROOT / "app" / "services" / "radius_population.py"


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_event_policy_has_a_complete_read_only_manifest() -> None:
    service = next(
        item for item in all_services() if item.name == "access.event_policy"
    )

    assert service.is_contracted
    assert service.contract is not None
    assert service.contract.transaction.mode.value == "read_only"
    assert service.contract.migration.state.value == "complete"
    assert {concern.name for concern in service.contract.concerns} == set(service.owns)


def test_policy_uses_typed_outcomes_and_no_parallel_defaults() -> None:
    source = _source(OWNER)
    tree = ast.parse(source)
    functions = {
        node.name: ast.get_source_segment(source, node) or ""
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    assert "class FupEnforcementAction(StrEnum)" in source
    assert "class FupEventPolicyDecision:" in source
    assert "class AccessEventPolicyError(DomainError)" in source
    assert "settings_spec.resolve_value" in source
    assert "HTTPException" not in source
    assert ".commit(" not in source
    assert ".rollback(" not in source
    assert 'or "throttle"' not in source
    assert "default=" not in source
    assert "resolve_fup_event_policy" in functions


def test_callers_consume_typed_policy_outcomes_only() -> None:
    owner_source = _source(OWNER)
    handler_source = _source(HANDLER)
    projection_source = _source(RADIUS_PROJECTION)

    retired_definitions = (
        "def group_routing_enabled(",
        "def refresh_sessions_on_profile_change_enabled(",
        "def fup_action(",
        "def fup_throttle_radius_profile_id(",
    )
    for definition in retired_definitions:
        assert definition not in owner_source

    retired_calls = (
        "enforcement_event_policy.group_routing_enabled(",
        "enforcement_event_policy.refresh_sessions_on_profile_change_enabled(",
        "enforcement_event_policy.fup_action(",
        "enforcement_event_policy.fup_throttle_radius_profile_id(",
    )
    for call in retired_calls:
        assert call not in handler_source
        assert call not in projection_source

    assert "resolve_fup_event_policy(" in handler_source
    assert "resolve_session_refresh_policy(" in handler_source
    assert "resolve_group_routing_policy(" in handler_source
    assert "resolve_group_routing_policy(" in projection_source
