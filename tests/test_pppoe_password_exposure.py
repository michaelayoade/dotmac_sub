"""PPPoE service password exposure (D10, 2026-10-19).

* The admin subscription edit page never carries an existing password.
* Revealing it is an explicit POST, CSRF-protected, gated by
  ``subscription:credential:reveal`` and audited without the secret.
"""

from __future__ import annotations

import json
import logging
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.db import get_db
from app.models.audit import AuditEvent
from app.models.catalog import AccessCredential, ConnectionType
from app.models.rbac import Permission
from app.services.auth_dependencies import require_user_auth
from app.services.credential_crypto import encrypt_credential
from app.web.admin import catalog as admin_catalog
from app.web.admin import customers as admin_customers

SECRET = "Zq9-Not-A-Real-Secret-7f3"  # noqa: S105 - test fixture value
REVEAL = "subscription:credential:reveal"
CSRF = "csrf-test-token"  # noqa: S105


@pytest.fixture()
def credential(db_session, subscriber, subscription):
    from app.models.subscriber import UserType

    subscriber.user_type = UserType.customer
    db_session.add(Permission(key=REVEAL, is_active=True))
    db_session.add(Permission(key="subscription:update", is_active=True))
    row = AccessCredential(
        subscriber_id=subscriber.id,
        subscription_id=subscription.id,
        username="100099999",
        secret_hash=encrypt_credential(SECRET),
        connection_type=ConnectionType.pppoe,
        is_active=True,
    )
    db_session.add(row)
    db_session.commit()
    return row


def _client(db_session, scopes: list[str], monkeypatch=None) -> TestClient:
    app = FastAPI()
    app.include_router(admin_catalog.router, prefix="/admin")
    app.include_router(admin_customers.router, prefix="/admin")

    def _db():
        yield db_session

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[require_user_auth] = lambda: {
        "principal_type": "system_user",
        "principal_id": str(uuid4()),
        "roles": ["support"],
        "scopes": scopes,
    }
    client = TestClient(app)
    client.cookies.set("csrf_token", CSRF)
    return client


def _headers(csrf: bool = True) -> dict[str, str]:
    return {"X-CSRF-Token": CSRF} if csrf else {}


# --- 1. edit page HTML -----------------------------------------------------


def test_edit_page_html_contains_no_password(
    db_session, subscription, credential, monkeypatch
):
    from fastapi.responses import HTMLResponse

    # Render the real template with the real form context; only the page chrome
    # (sidebar, user menu) is stubbed.
    monkeypatch.setattr(
        admin_catalog,
        "_base_context",
        lambda request, *a, **k: {"request": request, "csrf_token": CSRF},
    )
    real = admin_catalog.templates

    class _Render:
        @staticmethod
        def TemplateResponse(name, context, **kwargs):  # noqa: N802
            template = real.env.get_template(name)
            return HTMLResponse(
                template.render(
                    {**context, "can": lambda *_a, **_k: True},
                ),
                **{k: v for k, v in kwargs.items() if k == "status_code"},
            )

    monkeypatch.setattr(admin_catalog, "templates", _Render)
    client = _client(db_session, ["subscription:update", REVEAL])
    response = client.get(f"/admin/catalog/subscriptions/{subscription.id}/edit")

    assert response.status_code == 200, response.text[:500]
    html = response.text
    assert "100099999" in html  # the login is still shown
    assert SECRET not in html
    assert 'name="service_password"' in html
    assert 'name="service_password" id="service_password" value=""' in html


def test_edit_page_does_not_echo_submitted_password_on_rerender():
    from pathlib import Path

    source = Path("templates/admin/catalog/subscription_form.html").read_text()
    assert 'value="{{ subscription.service_password }}"' not in source
    assert "{{ current_service_password" not in source


# --- 2. reveal permission --------------------------------------------------


def _reveal_url(subscription) -> str:
    return f"/admin/catalog/subscriptions/{subscription.id}/reveal-service-password"


def test_reveal_refused_without_dedicated_permission(
    db_session, subscription, credential
):
    # customer:read (the former gate) and subscription:update do not suffice.
    client = _client(
        db_session, ["customer:read", "subscription:update", "catalog:write"]
    )
    response = client.post(_reveal_url(subscription), headers=_headers())
    assert response.status_code == 403
    assert SECRET not in response.text


def test_customer_reveal_refused_with_only_customer_read(
    db_session, subscriber, credential
):
    client = _client(db_session, ["customer:read"])
    url = f"/admin/customers/person/{subscriber.id}/pppoe-password"
    response = client.post(url, headers=_headers())
    assert response.status_code == 403
    assert SECRET not in response.text


def test_reveal_is_post_only_and_csrf_protected(db_session, subscription, credential):
    client = _client(db_session, [REVEAL])
    assert client.get(_reveal_url(subscription)).status_code == 405
    no_csrf = client.post(_reveal_url(subscription))
    assert no_csrf.status_code == 403
    assert SECRET not in no_csrf.text
    customer = f"/admin/customers/person/{credential.subscriber_id}/pppoe-password"
    assert client.get(customer).status_code == 405


def test_reveal_returns_password_with_no_store_when_permitted(
    db_session, subscription, credential
):
    client = _client(db_session, [REVEAL])
    response = client.post(_reveal_url(subscription), headers=_headers())
    assert response.status_code == 200
    assert response.json() == {"password": SECRET}
    assert "no-store" in response.headers["cache-control"]
    assert response.headers["pragma"] == "no-cache"

    customer = f"/admin/customers/person/{credential.subscriber_id}/pppoe-password"
    again = client.post(customer, headers=_headers())
    assert again.status_code == 200
    assert "no-store" in again.headers["cache-control"]


# --- 3. audit --------------------------------------------------------------


def test_reveal_writes_audit_event_without_secret(db_session, subscription, credential):
    client = _client(db_session, [REVEAL])
    response = client.post(_reveal_url(subscription), headers=_headers())
    assert response.status_code == 200

    events = (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == "customer.pppoe_password_reveal")
        .all()
    )
    assert len(events) == 1
    event = events[0]
    assert event.is_success is True
    assert event.entity_id == str(credential.subscriber_id)
    assert event.metadata_["subscription_id"] == str(subscription.id)
    blob = json.dumps(
        {
            "metadata": event.metadata_,
            "action": event.action,
            "entity": event.entity_id,
        },
        default=str,
    )
    assert SECRET not in blob
    assert credential.secret_hash not in blob


def test_refused_reveal_writes_no_audit_success(db_session, subscription, credential):
    client = _client(db_session, ["customer:read"])
    client.post(_reveal_url(subscription), headers=_headers())
    assert (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == "customer.pppoe_password_reveal")
        .count()
        == 0
    )


# --- 4. delivery path must not log the secret (guard) ----------------------


def test_send_credentials_does_not_log_secret(
    db_session, subscription, subscriber, credential, monkeypatch, caplog
):
    from app.services import web_catalog_subscriptions as core

    sent: list[str] = []
    monkeypatch.setattr(
        core.email_service, "send_email", lambda **kw: sent.append(kw["body_text"])
    )
    monkeypatch.setattr(
        core.sms_service, "send_sms", lambda db, phone, body, **kw: sent.append(body)
    )
    subscriber.email = "subscriber@example.test"
    subscriber.phone = "+15550100"
    db_session.commit()

    with caplog.at_level(logging.DEBUG):
        result = core.send_subscription_credentials(
            db_session, subscription_id=str(subscription.id)
        )
    assert result["email_sent"] + result["sms_sent"] >= 1
    assert any(SECRET in body for body in sent)  # delivery still carries it
    assert SECRET not in caplog.text


# --- 5. R1: reveal is bound to this subscription's own credential -----------

OTHER_SECRET = "Other-Service-Secret-1b2"  # noqa: S105 - test fixture value


@pytest.fixture()
def second_service(db_session, subscriber, subscription, credential, catalog_offer):
    """A second service on the same account with its own, different credential."""
    from app.models.catalog import BillingMode, Subscription, SubscriptionStatus

    other = Subscription(
        subscriber_id=subscriber.id,
        offer_id=catalog_offer.id,
        status=SubscriptionStatus.pending,
        billing_mode=BillingMode.postpaid,
    )
    db_session.add(other)
    db_session.flush()
    row = AccessCredential(
        subscriber_id=subscriber.id,
        subscription_id=other.id,
        username="100088888",
        secret_hash=encrypt_credential(OTHER_SECRET),
        connection_type=ConnectionType.pppoe,
        is_active=True,
    )
    db_session.add(row)
    db_session.commit()
    return other


def test_reveal_returns_only_this_services_password(
    db_session, subscription, second_service
):
    client = _client(db_session, [REVEAL])
    mine = client.post(_reveal_url(subscription), headers=_headers())
    theirs = client.post(_reveal_url(second_service), headers=_headers())
    assert mine.json() == {"password": SECRET}
    assert theirs.json() == {"password": OTHER_SECRET}


def test_reveal_does_not_fall_back_to_another_services_credential(
    db_session, subscription, second_service, credential
):
    # This service has no credential of its own; the account's other credential
    # must not be returned in its place.
    credential.subscription_id = second_service.id
    for row in db_session.query(AccessCredential).filter(
        AccessCredential.subscription_id == second_service.id
    ):
        if row.id != credential.id:
            row.is_active = False
    db_session.commit()
    client = _client(db_session, [REVEAL])
    response = client.post(_reveal_url(subscription), headers=_headers())
    assert response.status_code == 404
    assert SECRET not in response.text and OTHER_SECRET not in response.text


def test_reveal_refused_when_two_active_credentials_are_bound(
    db_session, subscriber, subscription, credential
):
    db_session.add(
        AccessCredential(
            subscriber_id=subscriber.id,
            subscription_id=subscription.id,
            username="100077777",
            secret_hash=encrypt_credential(OTHER_SECRET),
            connection_type=ConnectionType.pppoe,
            is_active=True,
        )
    )
    db_session.commit()
    client = _client(db_session, [REVEAL])
    response = client.post(_reveal_url(subscription), headers=_headers())
    assert response.status_code == 409
    assert SECRET not in response.text and OTHER_SECRET not in response.text


# --- 6. H1: secret references are never resolved ----------------------------

REFERENCES = [
    "env://SUB_TEST_APP_SECRET",
    "bao://kv/data/x#k",
    "vault://a/b",
    "openbao://a/b",
]


@pytest.fixture()
def planted_env(monkeypatch):
    monkeypatch.setenv("SUB_TEST_APP_SECRET", "RESOLVED-APP-SECRET-VALUE")


@pytest.mark.parametrize("reference", REFERENCES)
def test_edit_page_reveal_refuses_secret_reference(
    db_session, subscription, credential, planted_env, reference
):
    credential.secret_hash = reference
    db_session.commit()
    client = _client(db_session, [REVEAL])
    response = client.post(_reveal_url(subscription), headers=_headers())
    assert response.status_code == 409
    assert "Credential unusable" in response.json()["detail"]
    assert "RESOLVED-APP-SECRET-VALUE" not in response.text
    event = (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == "customer.pppoe_password_reveal")
        .one()
    )
    assert event.is_success is False
    assert event.metadata_["reason"] == "secret_reference"
    assert "RESOLVED-APP-SECRET-VALUE" not in json.dumps(event.metadata_, default=str)
    assert reference not in json.dumps(event.metadata_, default=str)


def test_customer_reveal_refuses_secret_reference(
    db_session, subscriber, credential, planted_env
):
    credential.secret_hash = "env://SUB_TEST_APP_SECRET"
    db_session.commit()
    client = _client(db_session, [REVEAL])
    url = f"/admin/customers/person/{subscriber.id}/pppoe-password"
    response = client.post(url, headers=_headers())
    assert response.status_code == 409
    assert "RESOLVED-APP-SECRET-VALUE" not in response.text


def test_send_credentials_refuses_secret_reference_and_audits(
    db_session, subscription, subscriber, credential, planted_env, monkeypatch
):
    from app.services import web_catalog_subscriptions as core

    credential.secret_hash = "env://SUB_TEST_APP_SECRET"
    subscriber.email = "subscriber@example.test"
    db_session.commit()
    sent: list[str] = []
    monkeypatch.setattr(core.email_service, "send_email", lambda **kw: sent.append("x"))
    monkeypatch.setattr(core.sms_service, "send_sms", lambda *a, **k: sent.append("x"))
    with pytest.raises(ValueError, match="Credential unusable"):
        core.send_subscription_credentials(
            db_session, subscription_id=str(subscription.id)
        )
    assert sent == []
    event = (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == "subscription.credentials_send_refused")
        .one()
    )
    assert event.is_success is False
    assert "RESOLVED-APP-SECRET-VALUE" not in json.dumps(event.metadata_, default=str)


def test_ont_reveal_refuses_secret_reference_and_audits(planted_env, monkeypatch):
    from types import SimpleNamespace

    from app.services.pppoe_password_reveal import CredentialUnusable
    from app.services.web_network_ont_actions import credentials as ont_credentials

    ont = SimpleNamespace(pppoe_password="env://SUB_TEST_APP_SECRET")
    db = SimpleNamespace(get=lambda *_a, **_k: ont)
    audits: list[dict] = []
    monkeypatch.setattr(
        ont_credentials, "_log_action_audit", lambda *a, **k: audits.append(k)
    )
    with pytest.raises(CredentialUnusable):
        ont_credentials.reveal_stored_pppoe_password(db, "ont-1", request=object())
    assert audits and audits[0]["action"] == "reveal_pppoe_password_refused"
    assert "RESOLVED-APP-SECRET-VALUE" not in repr(audits)
    with pytest.raises(CredentialUnusable):
        ont_credentials.resolve_stored_pppoe_password(db, "ont-1")


# --- 7. N1: the edit page shows this service's own login ---------------------


def _edit_html(db_session, monkeypatch, subscription) -> str:
    from fastapi.responses import HTMLResponse

    monkeypatch.setattr(
        admin_catalog,
        "_base_context",
        lambda request, *a, **k: {"request": request, "csrf_token": CSRF},
    )
    from fastapi.templating import Jinja2Templates

    real = Jinja2Templates(directory="templates")

    class _Render:
        @staticmethod
        def TemplateResponse(name, context, **kwargs):  # noqa: N802
            body = real.env.get_template(name).render(
                {**context, "can": lambda *_a, **_k: True}
            )
            return HTMLResponse(body)

    monkeypatch.setattr(admin_catalog, "templates", _Render)
    client = _client(db_session, ["subscription:update", REVEAL])
    return client.get(f"/admin/catalog/subscriptions/{subscription.id}/edit").text


def test_edit_page_shows_each_services_own_login(
    db_session, monkeypatch, subscription, second_service
):
    first = _edit_html(db_session, monkeypatch, subscription)
    second = _edit_html(db_session, monkeypatch, second_service)
    assert "100099999" in first and "100088888" not in first
    assert "100088888" in second and "100099999" not in second


def test_edit_page_shows_no_login_when_binding_is_ambiguous(
    db_session, monkeypatch, subscriber, subscription, credential
):
    db_session.add(
        AccessCredential(
            subscriber_id=subscriber.id,
            subscription_id=subscription.id,
            username="100077777",
            secret_hash=encrypt_credential(OTHER_SECRET),
            connection_type=ConnectionType.pppoe,
            is_active=True,
        )
    )
    db_session.commit()
    html = _edit_html(db_session, monkeypatch, subscription)
    assert "100099999" not in html and "100077777" not in html
    assert "Credential unusable" in html


def test_edit_page_marks_unbound_password_unavailable(
    db_session, monkeypatch, subscription, credential
):
    credential.subscription_id = None
    db_session.commit()

    html = _edit_html(db_session, monkeypatch, subscription)
    assert 'id="service-password-unavailable"' in html
    assert 'disabled aria-describedby="service-password-unavailable-reason"' in html
    assert "No password is linked to this service" in html
    assert SECRET not in html

    client = _client(db_session, ["subscription:update", REVEAL])
    response = client.post(_reveal_url(subscription), headers=_headers())
    assert response.status_code == 404
    assert SECRET not in response.text
