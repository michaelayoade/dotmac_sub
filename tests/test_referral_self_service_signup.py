from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute
from jose import jwt
from pydantic import ValidationError

from app.api import crm_referrals as referral_api
from app.models.domain_settings import DomainSetting, SettingDomain, SettingValueType
from app.models.party import (
    Party,
    PartyContactPoint,
    PartyContactPointType,
    PartyIdentityStatus,
)
from app.models.referral_native import Referral
from app.models.subscriber import Subscriber, SubscriberStatus
from app.schemas.referral import (
    ReferralCaptureRequest,
    ReferralSelfServiceAccountCreate,
    ReferralSelfServiceSignupRequest,
)
from app.services import referral_account_conversion
from app.services.referrals import referrals


def _email(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}@example.com"


def _subscriber(db) -> Subscriber:
    subscriber = Subscriber(
        first_name="Refer",
        last_name="Owner",
        email=_email("referrer"),
        status=SubscriberStatus.active,
        is_active=True,
    )
    db.add(subscriber)
    db.commit()
    db.refresh(subscriber)
    return subscriber


def _enable_program(db) -> None:
    db.add(
        DomainSetting(
            domain=SettingDomain.subscriber,
            key="referral_program_enabled",
            value_type=SettingValueType.boolean,
            value_text="true",
            is_active=True,
        )
    )
    db.commit()


def _capture(db):
    _enable_program(db)
    referrer = _subscriber(db)
    code = referrals.ensure_code(db, str(referrer.id))
    result = referral_api.capture_referral(
        ReferralCaptureRequest(
            code=code.code,
            name="Captured Prospect",
            email=_email("capture"),
            phone="0803 000 0042",
        ),
        db,
    )
    referral = db.get(Referral, result.id)
    assert referral is not None
    return referral, result


def _account() -> ReferralSelfServiceAccountCreate:
    return ReferralSelfServiceAccountCreate(
        first_name="Signup",
        last_name="Customer",
        email=_email("signup"),
        phone="0804 000 0043",
        city="Abuja",
    )


def test_capture_returns_expiring_pii_free_signed_context(db_session):
    referral, result = _capture(db_session)

    claims = jwt.get_unverified_claims(result.conversion_token)
    assert claims["typ"] == "referral_signup_context"
    assert claims["sub"] == str(referral.id)
    assert claims["referral_id"] == str(referral.id)
    assert claims["referred_party_id"] == str(referral.referred_party_id)
    assert claims["referred_lead_id"] == str(referral.referred_lead_id)
    assert set(claims) == {
        "typ",
        "iss",
        "ver",
        "sub",
        "referral_id",
        "referred_party_id",
        "referred_lead_id",
        "iat",
        "exp",
    }
    assert result.conversion_expires_at > datetime.now(UTC)


def test_public_signup_uses_token_not_contact_matching_and_is_idempotent(
    db_session, monkeypatch
):
    referral, capture = _capture(db_session)
    capture_party = db_session.get(Party, referral.referred_party_id)
    assert capture_party is not None
    account = _account()
    before = db_session.query(Subscriber).count()

    created = referral_api.signup_referral_account(
        ReferralSelfServiceSignupRequest(
            conversion_token=capture.conversion_token,
            account=account,
        ),
        db_session,
    )

    assert created.outcome == "created"
    assert created.enrollment_status == "queued"
    subscriber = db_session.get(Subscriber, created.subscriber_id)
    assert subscriber is not None
    assert subscriber.email == str(account.email)
    captured_email = (
        db_session.query(PartyContactPoint)
        .filter(PartyContactPoint.party_id == referral.referred_party_id)
        .filter(PartyContactPoint.channel_type == PartyContactPointType.email.value)
        .one()
    )
    assert subscriber.email != captured_email.normalized_value
    assert subscriber.party_id == referral.referred_party_id
    assert subscriber.status == SubscriberStatus.new
    assert subscriber.lifecycle_override_status is None
    assert subscriber.party_binding_source == "public_referral_signup"
    db_session.refresh(capture_party)
    assert capture_party.status == PartyIdentityStatus.quarantined.value

    replay = referral_api.signup_referral_account(
        ReferralSelfServiceSignupRequest(
            conversion_token=capture.conversion_token,
            account=_account(),
        ),
        db_session,
    )
    assert replay.outcome == "already_attached"
    assert replay.subscriber_id == subscriber.id
    assert db_session.query(Subscriber).count() == before + 1


def test_public_signup_rejects_tampered_and_expired_context_without_account(db_session):
    referral, capture = _capture(db_session)
    before = db_session.query(Subscriber).count()
    head, body, signature = capture.conversion_token.split(".")
    replacement = "A" if signature[0] != "A" else "B"
    tampered = ".".join((head, body, replacement + signature[1:]))

    with pytest.raises(HTTPException) as exc:
        referral_api.signup_referral_account(
            ReferralSelfServiceSignupRequest(
                conversion_token=tampered,
                account=_account(),
            ),
            db_session,
        )
    assert exc.value.status_code == 401

    expired = referral_account_conversion.issue_public_signup_context(
        db_session,
        referral,
        now=datetime.now(UTC) - timedelta(days=2),
    )
    with pytest.raises(HTTPException) as exc:
        referral_api.signup_referral_account(
            ReferralSelfServiceSignupRequest(
                conversion_token=expired.token,
                account=_account(),
            ),
            db_session,
        )
    assert exc.value.status_code == 401
    assert db_session.query(Subscriber).count() == before


def test_public_signup_schema_forbids_privileged_account_controls():
    base = {
        "first_name": "Narrow",
        "last_name": "Payload",
        "email": _email("narrow"),
    }
    for field, value in (
        ("status", "active"),
        ("reseller_id", str(uuid.uuid4())),
        ("billing_enabled", False),
        ("email_verified", True),
        ("marketing_opt_in", True),
        ("subscriber_number", "FORGED"),
    ):
        with pytest.raises(ValidationError):
            ReferralSelfServiceAccountCreate.model_validate({**base, field: value})


def test_signup_route_is_public_and_registered_on_the_public_router():
    route = next(
        route
        for route in referral_api.public_router.routes
        if isinstance(route, APIRoute) and route.path == "/referrals/signup"
    )
    dependency_calls = {
        getattr(dependency.call, "__name__", "")
        for dependency in route.dependant.dependencies
    }
    assert "require_permission" not in dependency_calls
