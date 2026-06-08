"""Unit tests for the self-scoped customer endpoints in app/api/me.py.

They verify the two things the endpoint layer is responsible for: rejecting
non-subscriber principals (403) and forcing the caller's own subscriber_id as
the scope passed to the underlying list services.
"""

import uuid

import pytest
from fastapi import HTTPException

from app.api import me as me_api


def _subscriber_principal():
    sid = str(uuid.uuid4())
    return {"principal_type": "subscriber", "subscriber_id": sid}


def _system_user_principal():
    return {"principal_type": "system_user", "subscriber_id": str(uuid.uuid4())}


def test_subscriber_id_helper_rejects_non_subscriber():
    with pytest.raises(HTTPException) as exc:
        me_api._subscriber_id(_system_user_principal())
    assert exc.value.status_code == 403


def test_my_invoices_scopes_to_caller(monkeypatch):
    principal = _subscriber_principal()
    captured = {}

    def fake_list_response(
        db, account_id, status, is_active, order_by, order_dir, limit, offset
    ):
        captured["account_id"] = account_id
        return {"items": [], "count": 0, "limit": limit, "offset": offset}

    monkeypatch.setattr(
        me_api.billing_service.invoices, "list_response", fake_list_response
    )

    me_api.my_invoices(
        status=None,
        order_by="created_at",
        order_dir="desc",
        limit=50,
        offset=0,
        db=None,
        principal=principal,
    )
    # The caller's own id must be forced as the account scope.
    assert captured["account_id"] == principal["subscriber_id"]


def test_my_invoices_403_for_non_subscriber():
    with pytest.raises(HTTPException) as exc:
        me_api.my_invoices(
            status=None,
            order_by="created_at",
            order_dir="desc",
            limit=50,
            offset=0,
            db=None,
            principal=_system_user_principal(),
        )
    assert exc.value.status_code == 403


def test_my_subscriptions_scopes_to_caller(monkeypatch):
    principal = _subscriber_principal()
    captured = {}

    def fake_list_response(
        db, subscriber_id, offer_id, status, order_by, order_dir, limit, offset
    ):
        captured["subscriber_id"] = subscriber_id
        return {"items": [], "count": 0, "limit": limit, "offset": offset}

    monkeypatch.setattr(
        me_api.catalog_service.subscriptions, "list_response", fake_list_response
    )

    me_api.my_subscriptions(
        status=None,
        order_by="created_at",
        order_dir="desc",
        limit=50,
        offset=0,
        db=None,
        principal=principal,
    )
    assert captured["subscriber_id"] == principal["subscriber_id"]


def test_my_accounting_sessions_403_for_non_subscriber():
    with pytest.raises(HTTPException) as exc:
        me_api.my_accounting_sessions(
            limit=50, offset=0, db=None, principal=_system_user_principal()
        )
    assert exc.value.status_code == 403


def test_my_notifications_scopes_to_caller(monkeypatch):
    principal = _subscriber_principal()
    captured = {}

    def fake(db, subscriber_id, limit, offset):
        captured["subscriber_id"] = subscriber_id
        return {"items": [], "count": 0, "limit": limit, "offset": offset}

    monkeypatch.setattr(
        me_api.notification_service.notifications,
        "list_response_for_subscriber",
        fake,
    )
    me_api.my_notifications(limit=50, offset=0, db=None, principal=principal)
    assert captured["subscriber_id"] == principal["subscriber_id"]


def test_my_notifications_403_for_non_subscriber():
    with pytest.raises(HTTPException) as exc:
        me_api.my_notifications(
            limit=50, offset=0, db=None, principal=_system_user_principal()
        )
    assert exc.value.status_code == 403


def test_topup_initiate_403_for_non_subscriber():
    from app.schemas.billing import TopupInitiateRequest

    with pytest.raises(HTTPException) as exc:
        me_api.my_topup_initiate(
            TopupInitiateRequest(amount=5000),
            db=None,
            principal=_system_user_principal(),
        )
    assert exc.value.status_code == 403


def test_plan_change_submit_403_for_non_subscriber():
    from app.schemas.catalog import PlanChangeSubmitRequest

    with pytest.raises(HTTPException) as exc:
        me_api.my_plan_change_submit(
            subscription_id="s1",
            payload=PlanChangeSubmitRequest(
                offer_id=uuid.uuid4(), effective_date="2030-01-01"
            ),
            db=None,
            principal=_system_user_principal(),
        )
    assert exc.value.status_code == 403


def test_topup_initiate_translates_value_error(monkeypatch):
    from app.schemas.billing import TopupInitiateRequest

    monkeypatch.setattr(
        me_api,
        "_customer",
        lambda db, principal: {"account_id": "x", "subscriber_id": "x"},
    )

    def _boom(db, customer, amount):
        raise ValueError("Top-up amount must be at least ₦1,000.00")

    monkeypatch.setattr(me_api.customer_payments, "create_topup_intent", _boom)
    with pytest.raises(HTTPException) as exc:
        me_api.my_topup_initiate(
            TopupInitiateRequest(amount=1),
            db=None,
            principal=_subscriber_principal(),
        )
    assert exc.value.status_code == 400


def test_my_invoice_detail_404_when_not_owned(monkeypatch):
    principal = _subscriber_principal()

    class _Invoice:
        account_id = uuid.uuid4()  # a different owner

    monkeypatch.setattr(
        me_api.billing_service.invoices, "get", lambda db, invoice_id: _Invoice()
    )
    with pytest.raises(HTTPException) as exc:
        me_api.my_invoice(invoice_id=str(uuid.uuid4()), db=None, principal=principal)
    assert exc.value.status_code == 404
