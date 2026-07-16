"""Tests for the notification list-projection contracts + granular RBAC.

See app/services/web_notifications.py (ui.notification_{templates,queue,history}
_list_projection) and migration 320 (notification:read/write split off the
coarse system:read/write).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from app.services.web_notifications import (
    NOTIFICATION_HISTORY_LIST_DEFINITION,
    NOTIFICATION_QUEUE_LIST_DEFINITION,
    NOTIFICATION_TEMPLATES_LIST_DEFINITION,
    build_history_list_query,
    build_queue_list_query,
    build_templates_list_query,
)


def _seed_rbac_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "seed" / "seed_rbac.py"
    spec = importlib.util.spec_from_file_location("seed_rbac_notif", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- Contracts ---


def test_templates_definition_capabilities():
    d = NOTIFICATION_TEMPLATES_LIST_DEFINITION
    assert d.filterable_keys == ("channel", "status")
    assert d.sortable_keys == ("name",)
    assert d.default_sort == "name"
    assert d.default_sort_dir == "asc"


def test_queue_definition_capabilities():
    d = NOTIFICATION_QUEUE_LIST_DEFINITION
    assert d.filterable_keys == ("status", "channel")
    assert d.sortable_keys == ("created_at",)
    assert d.default_sort_dir == "desc"


def test_history_definition_capabilities():
    d = NOTIFICATION_HISTORY_LIST_DEFINITION
    assert d.filterable_keys == ("status",)
    assert d.sortable_keys == ("occurred_at",)
    assert d.default_sort_dir == "desc"


def test_build_queries_normalize_and_reject():
    q = build_templates_list_query(channel="email", status=" ", search="welcome")
    assert q.filter_value("channel") == "email"
    assert q.filter_value("status") is None  # blank dropped
    assert q.search == "welcome"
    assert q.sort_by == "name"

    assert build_queue_list_query(status="queued").filter_value("status") == "queued"
    assert build_history_list_query().sort_by == "occurred_at"

    with pytest.raises(ValueError):
        build_templates_list_query(sort_by="channel")  # not sortable
    with pytest.raises(ValueError):
        build_queue_list_query(per_page=30)  # not an allowed size


# --- Granular RBAC (migration 320) ---


def test_notification_permissions_are_seeded_and_ui_assignable():
    seed = _seed_rbac_module()
    seeded = {key for key, _ in seed.DEFAULT_PERMISSIONS}
    for key in ("notification:read", "notification:write"):
        assert key in seeded, f"{key} not seeded"
        # role-builder-assignable: not admin-only
        assert key not in seed.ADMIN_ONLY_PERMISSION_KEYS
        # admin retains them via its wildcard grant
        assert key in seed.ROLE_PERMISSIONS["admin"]
