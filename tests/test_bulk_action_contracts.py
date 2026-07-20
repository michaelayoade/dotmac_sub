from __future__ import annotations

import pytest

from app.services import web_customer_bulk_actions
from app.services.bulk_actions import (
    BulkActionDefinition,
    BulkResourceDefinition,
    membership_scope_token,
    parse_bulk_selection,
)


def _definition() -> BulkResourceDefinition:
    return BulkResourceDefinition(
        key="examples",
        filtered_selection_supported=True,
        actions=(
            BulkActionDefinition(
                key="update",
                label="Update",
                description="Update selected records.",
                permission="example:write",
                tone="warning",
            ),
            BulkActionDefinition(
                key="notify",
                label="Notify",
                description="Queue notifications.",
                permission="example:notify",
                execution_mode="queued",
                result_reference="notification_ids",
            ),
        ),
    )


def test_bulk_contract_omits_unauthorized_actions_and_permission_vocabulary():
    contract = _definition().project(authorized_permissions={"example:write"})

    assert contract.selection_enabled is True
    assert contract.select_all_scope == "page"
    assert contract.filtered_selection_supported is True
    assert [action.key for action in contract.actions] == ["update"]
    assert contract.actions[0].requires_preview is True
    assert contract.actions[0].requires_confirmation is True
    assert "permission" not in contract.as_dict()["actions"][0]


def test_bulk_contract_disables_selection_when_no_action_is_authorized():
    contract = _definition().project(authorized_permissions=set())

    assert contract.selection_enabled is False
    assert contract.actions == ()


def test_membership_scope_token_is_order_independent_and_scope_specific():
    token = membership_scope_token("selected", ["second", "first", "second"])

    assert token == membership_scope_token("selected", ["first", "second"])
    assert token != membership_scope_token("filtered", ["first", "second"])


def test_bulk_selection_never_treats_an_empty_selection_as_filtered_scope():
    with pytest.raises(ValueError, match="Select at least one record"):
        parse_bulk_selection(
            {"customer_ids": [], "filters": {"status": "active"}},
            allowed_filter_keys=("status",),
            filtered_selection_supported=True,
            legacy_id_key="customer_ids",
        )


def test_bulk_selection_normalizes_explicit_ids_and_legacy_selected_ids():
    selected = parse_bulk_selection(
        {
            "selection": {
                "mode": "selected",
                "ids": ["first", {"id": "second"}, "first", ""],
                "expected_count": "2",
                "expected_scope_token": "preview-token",
            }
        },
        allowed_filter_keys=("status",),
        filtered_selection_supported=True,
    )
    legacy = parse_bulk_selection(
        {"customer_ids": [{"id": "first", "type": "person"}]},
        allowed_filter_keys=("status",),
        filtered_selection_supported=True,
        legacy_id_key="customer_ids",
    )

    assert selected.mode == "selected"
    assert selected.ids == ("first", "second")
    assert selected.expected_count == 2
    assert selected.expected_scope_token == "preview-token"
    assert legacy.ids == ("first",)


def test_filtered_selection_requires_explicit_mode_and_declared_filters():
    selection = parse_bulk_selection(
        {
            "selection": {
                "mode": "filtered",
                "filters": {"search": " Acme ", "status": " active "},
            }
        },
        allowed_filter_keys=("search", "status"),
        filtered_selection_supported=True,
    )

    assert selection.mode == "filtered"
    assert selection.filters == (("search", "Acme"), ("status", "active"))

    with pytest.raises(ValueError, match="Unsupported selection filters"):
        parse_bulk_selection(
            {
                "selection": {
                    "mode": "filtered",
                    "filters": {"undeclared": "value"},
                }
            },
            allowed_filter_keys=("status",),
            filtered_selection_supported=True,
        )


def test_customer_bulk_projection_exposes_actions_only_after_permission_check(
    monkeypatch,
):
    monkeypatch.setattr(
        web_customer_bulk_actions,
        "has_permission",
        lambda auth, _db, permission: permission in auth.get("permissions", []),
    )

    allowed = web_customer_bulk_actions.build_customer_bulk_action_contract(
        object(),
        auth={"permissions": ["customer:write"]},
    )
    denied = web_customer_bulk_actions.build_customer_bulk_action_contract(
        object(),
        auth={"permissions": []},
    )

    assert allowed["selection_enabled"] is True
    assert [action["key"] for action in allowed["actions"]] == [
        "update",
        "send_message",
    ]
    assert denied["selection_enabled"] is False
    assert denied["actions"] == []
