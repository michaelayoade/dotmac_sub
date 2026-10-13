"""Fast unit evidence for customer web/native password-step parity."""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from app.models.auth import AuthProvider, UserCredential
from app.models.catalog import AccessCredential
from app.models.subscriber import Subscriber, SubscriberStatus, UserType
from app.models.system_user import SystemUser
from app.services import customer_authentication as owner
from app.services import password_authentication as pa
from app.services.auth_flow import LoginAudience, hash_password, principal_refusal
from tests.authentication_denial_unit import (
    authentication_denial_unit_session as authentication_denial_unit_session,
)

pytestmark = pytest.mark.usefixtures("authentication_denial_unit_session")


def _query(
    identifier: str, password: str, audience: owner.CustomerLoginAudience
) -> owner.PrepareCustomerLogin:
    return owner.PrepareCustomerLogin(
        identifier=identifier,
        password=password,
        evidence=owner.CustomerLoginEvidence(
            uuid4(), None, pa.DenialSurface.CUSTOMER_API
        ),
        audience=audience,
    )


def _credential(db: Session, subscriber: Subscriber, username: str) -> UserCredential:
    credential = UserCredential(
        subscriber_id=subscriber.id,
        username=username,
        provider=AuthProvider.local,
        password_hash=hash_password("customer-password"),
        is_active=True,
    )
    db.add(credential)
    db.commit()
    return credential


def test_customer_email_collision_never_selects_or_counts_staff_credential(
    db_session: Session, subscriber: Subscriber
) -> None:
    subscriber.email = "shared-login@example.test"
    customer = _credential(db_session, subscriber, "CUSTOMER-100")
    staff = SystemUser(
        first_name="Staff",
        last_name="Collision",
        email=subscriber.email,
        user_type=UserType.system_user,
        is_active=True,
    )
    db_session.add(staff)
    db_session.flush()
    staff_credential = UserCredential(
        system_user_id=staff.id,
        username=staff.email,
        provider=AuthProvider.local,
        password_hash=hash_password("different-staff-password"),
        is_active=True,
    )
    db_session.add(staff_credential)
    db_session.commit()
    for audience in owner.CustomerLoginAudience:
        prepared = owner.prepare_customer_login(
            db_session, _query(subscriber.email, "customer-password", audience)
        )
        assert prepared.subscriber_id == subscriber.id
        assert prepared.verified.credential_id == customer.id
        assert prepared.verified.audience == audience.value
    db_session.refresh(staff_credential)
    assert not staff_credential.failed_login_attempts
    assert staff_credential.last_login_at is None


@pytest.mark.parametrize("identifier_kind", ["username", "email"])
def test_web_and_native_resolve_the_same_customer(
    db_session: Session, subscriber: Subscriber, identifier_kind: str
) -> None:
    credential = _credential(db_session, subscriber, "CUSTOMER-200")
    identifier = (
        credential.username
        if identifier_kind == "username"
        else subscriber.email.upper()
    )
    web = owner.prepare_customer_login(
        db_session,
        _query(identifier, "customer-password", owner.CustomerLoginAudience.BROWSER),
    )
    native = owner.prepare_customer_login(
        db_session,
        _query(
            identifier, "customer-password", owner.CustomerLoginAudience.NATIVE_CUSTOMER
        ),
    )
    assert web.subscriber_id == native.subscriber_id == subscriber.id
    assert web.verified.credential_id == native.verified.credential_id == credential.id


@pytest.mark.parametrize("representation", ["bare", "plain"])
def test_native_preserves_portal_access_fallback_and_password_bytes(
    db_session: Session,
    subscriber: Subscriber,
    monkeypatch: pytest.MonkeyPatch,
    representation: str,
) -> None:
    password = "  reversible password  "
    db_session.add(
        AccessCredential(
            subscriber_id=subscriber.id,
            username="pppoe-parity",
            secret_hash=password if representation == "bare" else "plain:" + password,
            is_active=True,
        )
    )
    db_session.commit()
    monkeypatch.setattr(
        owner.radius_auth,
        "authenticate",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("offline test radius")),
    )
    for audience in owner.CustomerLoginAudience:
        prepared = owner.prepare_customer_login(
            db_session, _query("pppoe-parity", password, audience)
        )
        assert prepared.subscriber_id == subscriber.id
        assert prepared.verified.src == pa.SRC_ACCESS_CREDENTIAL


def test_wrong_customer_email_password_never_tries_radius(
    db_session: Session, subscriber: Subscriber, monkeypatch: pytest.MonkeyPatch
) -> None:
    credential = _credential(db_session, subscriber, "CUSTOMER-300")

    def unexpected_radius(**kwargs: object) -> None:
        pytest.fail(
            "Contact-email password failure must not hop to network credentials"
        )

    monkeypatch.setattr(owner.radius_auth, "authenticate", unexpected_radius)
    with pytest.raises(owner.CustomerLoginError) as refusal:
        owner.prepare_customer_login(
            db_session,
            _query(
                subscriber.email,
                "wrong-password",
                owner.CustomerLoginAudience.NATIVE_CUSTOMER,
            ),
        )
    assert refusal.value.kind is owner.CustomerLoginFailure.INVALID_CREDENTIALS
    db_session.refresh(credential)
    assert credential.failed_login_attempts == 1


def test_native_rejects_reseller_before_session_issuance(
    db_session: Session, subscriber: Subscriber
) -> None:
    subscriber.user_type = UserType.reseller
    _credential(db_session, subscriber, "reseller-is-not-customer")
    with pytest.raises(owner.CustomerLoginError) as refusal:
        owner.prepare_customer_login(
            db_session,
            _query(
                "reseller-is-not-customer",
                "customer-password",
                owner.CustomerLoginAudience.NATIVE_CUSTOMER,
            ),
        )
    assert refusal.value.kind is owner.CustomerLoginFailure.NOT_CUSTOMER


@pytest.mark.parametrize(
    "status", [SubscriberStatus.disabled, SubscriberStatus.canceled]
)
def test_native_preserves_portal_disabled_account_refusal(
    db_session: Session, subscriber: Subscriber, status: SubscriberStatus
) -> None:
    subscriber.status = status
    _credential(db_session, subscriber, "disabled-customer")
    with pytest.raises(owner.CustomerLoginError) as refusal:
        owner.prepare_customer_login(
            db_session,
            _query(
                "disabled-customer",
                "customer-password",
                owner.CustomerLoginAudience.NATIVE_CUSTOMER,
            ),
        )
    assert refusal.value.kind is owner.CustomerLoginFailure.DISABLED


def test_customer_admission_does_not_change_general_or_admin_eligibility(
    subscriber: Subscriber,
) -> None:
    subscriber.user_type = UserType.reseller
    assert (
        principal_refusal("subscriber", subscriber, LoginAudience.customer)
        == "customer_required"
    )
    assert principal_refusal("subscriber", subscriber, LoginAudience.general) is None
    assert (
        principal_refusal("subscriber", subscriber, LoginAudience.admin)
        == "admin_required"
    )


def test_password_and_entered_identifier_are_excluded_from_command_repr() -> None:
    query = _query(
        "secret-email@example.test",
        "private-password",
        owner.CustomerLoginAudience.NATIVE_CUSTOMER,
    )
    assert "secret-email" not in repr(query)
    assert "private-password" not in repr(query)
