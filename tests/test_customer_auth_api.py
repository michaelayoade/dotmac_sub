"""Customer JSON adapter tests using the real customer preparation contracts."""

from __future__ import annotations

from uuid import uuid4

import pyotp
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jose import jwt
from sqlalchemy.orm import Session

from app.api.customer_auth import router
from app.db import get_db
from app.models.auth import AuthProvider, UserCredential
from app.models.auth import Session as AuthSession
from app.models.subscriber import Subscriber, UserType
from app.models.system_user import SystemUser
from app.services import customer_authentication as owner
from app.services.auth_flow import hash_password
from tests.authentication_denial_unit import (
    authentication_denial_unit_session as authentication_denial_unit_session,
)
from tests.subscriber_mfa_fixtures import seed_enabled_subscriber_mfa

pytestmark = pytest.mark.usefixtures("authentication_denial_unit_session")


@pytest.fixture
def customer_client(db_session: Session, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("JWT_SECRET", "synthetic-customer-api-secret")
    monkeypatch.setenv("TOTP_ENCRYPTION_KEY", Fernet.generate_key().decode())
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db_session
    with TestClient(app) as client:
        yield client


def _credential(db: Session, subscriber: Subscriber) -> UserCredential:
    credential = UserCredential(
        subscriber_id=subscriber.id,
        username="CUSTOMER-API-100",
        provider=AuthProvider.local,
        password_hash=hash_password("customer-password"),
        is_active=True,
    )
    db.add(credential)
    db.commit()
    return credential


def test_native_customer_login_ignores_staff_email_collision(
    customer_client: TestClient, db_session: Session, subscriber: Subscriber
) -> None:
    subscriber.email = "native-collision@example.test"
    _credential(db_session, subscriber)
    staff = SystemUser(
        first_name="Staff",
        last_name="NativeCollision",
        email=subscriber.email,
        user_type=UserType.system_user,
        is_active=True,
    )
    db_session.add(staff)
    db_session.flush()
    db_session.add(
        UserCredential(
            system_user_id=staff.id,
            username=staff.email,
            provider=AuthProvider.local,
            password_hash=hash_password("different-password"),
            is_active=True,
        )
    )
    db_session.commit()
    response = customer_client.post(
        "/api/v1/customer/auth/login",
        json={"username": subscriber.email, "password": "customer-password"},
        headers={"X-Request-ID": str(uuid4())},
    )
    assert response.status_code == 200
    assert response.json()["access_token"]
    assert response.json()["refresh_token"]
    assert "refresh_token" not in response.cookies
    sessions = db_session.query(AuthSession).all()
    assert len(sessions) == 1
    assert sessions[0].subscriber_id == subscriber.id
    assert sessions[0].system_user_id is None


def test_customer_endpoint_rejects_provider_and_never_runs_general_login(
    customer_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unexpected(*args: object, **kwargs: object) -> None:
        pytest.fail("Customer login must not enter the general credential lookup")

    monkeypatch.setattr(owner, "prepare_customer_login", unexpected)
    response = customer_client.post(
        "/api/v1/customer/auth/login",
        json={"username": "someone", "password": "secret", "provider": "local"},
    )
    assert response.status_code == 422


def test_customer_endpoint_rejects_reseller_without_a_session(
    customer_client: TestClient, db_session: Session, subscriber: Subscriber
) -> None:
    subscriber.user_type = UserType.reseller
    _credential(db_session, subscriber)
    response = customer_client.post(
        "/api/v1/customer/auth/login",
        json={"username": "CUSTOMER-API-100", "password": "customer-password"},
    )
    assert response.status_code == 403
    assert db_session.query(AuthSession).count() == 0


def test_native_mfa_challenge_is_bound_to_customer_audience(
    customer_client: TestClient, db_session: Session, subscriber: Subscriber
) -> None:
    _credential(db_session, subscriber)
    seed_enabled_subscriber_mfa(
        db_session, subscriber_id=subscriber.id, label="native-test"
    )
    response = customer_client.post(
        "/api/v1/customer/auth/login",
        json={"username": "CUSTOMER-API-100", "password": "customer-password"},
    )
    assert response.status_code == 200
    result = response.json()
    assert result["mfa_required"] is True
    assert result["access_token"] is None
    assert db_session.query(AuthSession).count() == 0
    claims = jwt.get_unverified_claims(result["mfa_token"])
    assert claims["login_aud"] == "customer"
    assert claims["principal_type"] == "subscriber"


def test_native_customer_mfa_completes_with_portal_totp_whitespace_policy(
    customer_client: TestClient, db_session: Session, subscriber: Subscriber
) -> None:
    _credential(db_session, subscriber)
    factor = seed_enabled_subscriber_mfa(db_session, subscriber_id=subscriber.id)
    login = customer_client.post(
        "/api/v1/customer/auth/login",
        json={"username": "CUSTOMER-API-100", "password": "customer-password"},
    )
    response = customer_client.post(
        "/api/v1/customer/auth/mfa/verify",
        json={
            "mfa_token": login.json()["mfa_token"],
            "code": " " + pyotp.TOTP(factor.secret).now() + " ",
        },
    )
    assert response.status_code == 200
    assert response.json()["access_token"]
    assert response.json()["refresh_token"]
    assert db_session.query(AuthSession).count() == 1


def test_customer_mfa_rejects_general_challenge_before_issuing_session(
    customer_client: TestClient, db_session: Session, subscriber: Subscriber
) -> None:
    from app.services.auth_flow import AuthFlow
    from tests.test_auth_flow import _make_request

    _credential(db_session, subscriber)
    factor = seed_enabled_subscriber_mfa(db_session, subscriber_id=subscriber.id)
    general = AuthFlow.login(
        db=db_session,
        username="CUSTOMER-API-100",
        password="customer-password",
        request=_make_request(),
        provider=None,
    )
    response = customer_client.post(
        "/api/v1/customer/auth/mfa/verify",
        json={
            "mfa_token": general["mfa_token"],
            "code": pyotp.TOTP(factor.secret).now(),
        },
    )
    assert response.status_code == 401
    assert db_session.query(AuthSession).count() == 0


def test_customer_mfa_rechecks_customer_classification(
    customer_client: TestClient, db_session: Session, subscriber: Subscriber
) -> None:
    _credential(db_session, subscriber)
    factor = seed_enabled_subscriber_mfa(db_session, subscriber_id=subscriber.id)
    login = customer_client.post(
        "/api/v1/customer/auth/login",
        json={"username": "CUSTOMER-API-100", "password": "customer-password"},
    )
    subscriber.user_type = UserType.reseller
    db_session.commit()
    response = customer_client.post(
        "/api/v1/customer/auth/mfa/verify",
        json={
            "mfa_token": login.json()["mfa_token"],
            "code": pyotp.TOTP(factor.secret).now(),
        },
    )
    assert response.status_code == 403
    assert db_session.query(AuthSession).count() == 0
