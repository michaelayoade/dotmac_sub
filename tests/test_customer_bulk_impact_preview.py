"""Destructive customer bulk actions require an impact preview + scope-verified
confirmation, matching the safer bulk-update contract.

Deactivating and deleting customers used to execute straight from a raw id list.
They now resolve the scope, return a structured impact on preview, and refuse to
run without a confirmation whose expected count + scope token still match — so an
operator always sees "this affects N customers" and can't act on a stale
selection.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.models.subscriber import Subscriber, UserType
from app.services import web_customer_actions as actions


def _make_customer(db_session, *, is_active: bool = True) -> Subscriber:
    from app.services.subscriber import _default_reseller_id

    sub = Subscriber(
        first_name="Bulk",
        last_name="Customer",
        email=f"{uuid4().hex[:10]}@example.test",
        is_active=is_active,
        user_type=UserType.customer,
        reseller_id=_default_reseller_id(db_session),
    )
    db_session.add(sub)
    db_session.commit()
    db_session.refresh(sub)
    return sub


def test_status_preview_returns_impact_and_changes_nothing(db_session):
    active = _make_customer(db_session, is_active=True)
    inactive = _make_customer(db_session, is_active=False)

    result = actions.bulk_update_customer_status_from_payload(
        db_session,
        {
            "customer_ids": [{"id": str(active.id)}, {"id": str(inactive.id)}],
            "status": "inactive",
            "preview_only": True,
        },
    )

    assert result["preview"] is True
    assert result["matched_count"] == 2
    assert result["scope_token"]
    impact = result["impact"]
    assert impact["target_status"] == "inactive"
    assert impact["destructive"] is True
    # Only the active customer actually transitions; the inactive one is a no-op.
    assert impact["will_change"] == 1
    assert impact["already_in_target_state"] == 1
    db_session.refresh(active)
    assert active.is_active is True  # preview never mutates


def test_status_execute_requires_confirmation(db_session):
    active = _make_customer(db_session, is_active=True)
    with pytest.raises(HTTPException) as exc:
        actions.bulk_update_customer_status_from_payload(
            db_session,
            {"customer_ids": [{"id": str(active.id)}], "status": "inactive"},
        )
    assert exc.value.status_code == 400


def test_status_execute_rejects_a_stale_scope(db_session):
    active = _make_customer(db_session, is_active=True)
    with pytest.raises(HTTPException) as exc:
        actions.bulk_update_customer_status_from_payload(
            db_session,
            {
                "selection": {
                    "mode": "selected",
                    "ids": [{"id": str(active.id)}],
                    # count/token that no longer match the resolved scope
                    "expected_count": 99,
                    "expected_scope_token": "deadbeef",
                },
                "status": "inactive",
                "confirmed": True,
            },
        )
    assert exc.value.status_code == 409


def test_status_execute_applies_after_a_valid_preview(db_session):
    active = _make_customer(db_session, is_active=True)
    preview = actions.bulk_update_customer_status_from_payload(
        db_session,
        {
            "customer_ids": [{"id": str(active.id)}],
            "status": "inactive",
            "preview_only": True,
        },
    )
    result = actions.bulk_update_customer_status_from_payload(
        db_session,
        {
            "selection": {
                "mode": "selected",
                "ids": [{"id": str(active.id)}],
                "expected_count": preview["matched_count"],
                "expected_scope_token": preview["scope_token"],
            },
            "status": "inactive",
            "confirmed": True,
        },
    )
    assert result["preview"] is False
    db_session.refresh(active)
    assert active.is_active is False


def test_delete_preview_flags_active_rows_and_requires_confirmation(db_session):
    active = _make_customer(db_session, is_active=True)
    preview = actions.bulk_delete_customers_from_payload(
        db_session, {"customer_ids": [{"id": str(active.id)}], "preview_only": True}
    )
    assert preview["preview"] is True
    assert preview["impact"]["destructive"] is True
    assert preview["impact"]["active"] == 1
    assert preview["scope_token"]

    with pytest.raises(HTTPException) as exc:
        actions.bulk_delete_customers_from_payload(
            db_session, {"customer_ids": [{"id": str(active.id)}]}
        )
    assert exc.value.status_code == 400
