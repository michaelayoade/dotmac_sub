"""UI action gating: templates hide actions the principal cannot perform.

``require_permission`` caches the principal's effective permission keys on the
request; ``can`` / ``action_permitted`` are pure set checks over that cache (no
DB) so a template renders a control only when the principal may use it. The
route still authorizes — this only controls visibility.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.services.auth_dependencies import (
    action_permitted,
    can,
    effective_permission_keys,
)


def _request(permission_keys) -> SimpleNamespace:
    return SimpleNamespace(
        state=SimpleNamespace(auth={"permission_keys": permission_keys})
    )


def _action(allowed: bool, permission: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(allowed=allowed, permission=permission)


def test_admin_effective_keys_collapse_to_wildcard_without_a_query():
    # db is never touched for admin, so None is safe here.
    keys = effective_permission_keys({"roles": ["admin"], "principal_id": "x"}, None)
    assert keys == frozenset({"*"})


def test_wildcard_holder_can_do_anything():
    assert can(_request(frozenset({"*"})), "reports:billing:export") is True


def test_can_requires_the_specific_expanded_key():
    held = frozenset({"reports:billing:read"})
    assert can(_request(held), "reports:billing:read") is True
    # Holding read does NOT grant export — the split is enforced.
    assert can(_request(held), "reports:billing:export") is False


def test_legacy_reports_scope_is_read_only_in_ui_projection():
    held = frozenset({"reports:billing"})
    assert can(_request(held), "reports:billing:read") is True
    assert can(_request(held), "reports:billing:export") is False


def test_can_denies_when_no_permission_cache_is_present():
    # An ungated page never populated the cache; deny (route stays the authority).
    assert can(SimpleNamespace(state=SimpleNamespace(auth={})), "x:y:z") is False
    assert can(SimpleNamespace(state=SimpleNamespace()), "x:y:z") is False


def test_action_permitted_combines_eligibility_and_permission():
    request = _request(frozenset({"reports:billing:export"}))
    # Eligible AND permitted -> visible.
    assert action_permitted(request, _action(True, "reports:billing:export")) is True
    # Not eligible (owner said no) -> hidden regardless of permission.
    assert action_permitted(request, _action(False, "reports:billing:export")) is False
    # Eligible but lacking the permission -> hidden.
    assert action_permitted(request, _action(True, "reports:billing:read")) is False
    # Eligible and no permission required -> visible.
    assert action_permitted(request, _action(True, None)) is True
