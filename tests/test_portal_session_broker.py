"""Customer Portal API broker: assert identity, mint a scoped CRM token (RFC #73).

Mirrors the chat broker — the sub never lets the client self-declare identity;
it asserts the authenticated subscriber to the CRM and returns only a
short-lived, scoped portal token plus the absolute CRM Portal API base.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from app.config import settings
from app.models.subscriber import Subscriber
from app.services import portal_session
from app.services.crm_client import CRMClientError


@contextmanager
def _crm_base(base="https://crm.example"):
    saved = settings.crm_base_url
    object.__setattr__(settings, "crm_base_url", base)
    try:
        yield
    finally:
        object.__setattr__(settings, "crm_base_url", saved)


@contextmanager
def _fake_crm(return_value=None, error=None):
    client = MagicMock()
    if error is not None:
        client.create_portal_session.side_effect = error
    else:
        client.create_portal_session.return_value = return_value
    with patch("app.services.portal_session.get_crm_client", return_value=client):
        yield client


def _make_subscriber(db_session):
    sub = Subscriber(
        first_name="Cust",
        last_name="Omer",
        display_name="Cust Omer",
        email="cust@example.com",
    )
    db_session.add(sub)
    db_session.commit()
    return sub


def test_broker_happy_path(db_session):
    sub = _make_subscriber(db_session)
    crm_resp = {
        "portal_token": "pt-abc",
        "expires_at": 1893456000,
        "api_base": "/api/v1/portal",
    }
    with (
        _crm_base(),
        _fake_crm(crm_resp) as client,
        patch(
            "app.services.portal_session.resolve_crm_subscriber_id",
            return_value="crm-sub-9",
        ),
    ):
        result = portal_session.broker_customer_portal_session(db_session, str(sub.id))

    assert result["portal_token"] == "pt-abc"
    assert result["expires_at"] == 1893456000
    assert result["api_base"] == "https://crm.example/api/v1/portal"

    kwargs = client.create_portal_session.call_args.kwargs
    assert kwargs["crm_subscriber_id"] == "crm-sub-9"
    assert kwargs["actor"] == "subscriber"
    assert kwargs["scopes"] == ["referrals:read", "referrals:write"]


def test_broker_not_linked_returns_409(db_session):
    sub = _make_subscriber(db_session)
    with (
        _crm_base(),
        patch(
            "app.services.portal_session.resolve_crm_subscriber_id", return_value=None
        ),
    ):
        with pytest.raises(HTTPException) as exc:
            portal_session.broker_customer_portal_session(db_session, str(sub.id))
    assert exc.value.status_code == 409


def test_broker_unknown_subscriber_returns_404(db_session):
    with pytest.raises(HTTPException) as exc:
        portal_session.broker_customer_portal_session(db_session, str(uuid.uuid4()))
    assert exc.value.status_code == 404


def test_broker_crm_unavailable_returns_502(db_session):
    sub = _make_subscriber(db_session)
    with (
        _crm_base(),
        _fake_crm(error=CRMClientError("boom")),
        patch(
            "app.services.portal_session.resolve_crm_subscriber_id",
            return_value="crm-sub-9",
        ),
    ):
        with pytest.raises(HTTPException) as exc:
            portal_session.broker_customer_portal_session(db_session, str(sub.id))
    assert exc.value.status_code == 502


def test_broker_invalid_crm_response_returns_502(db_session):
    sub = _make_subscriber(db_session)
    with (
        _crm_base(),
        _fake_crm({"portal_token": "", "expires_at": None}),
        patch(
            "app.services.portal_session.resolve_crm_subscriber_id",
            return_value="crm-sub-9",
        ),
    ):
        with pytest.raises(HTTPException) as exc:
            portal_session.broker_customer_portal_session(db_session, str(sub.id))
    assert exc.value.status_code == 502
