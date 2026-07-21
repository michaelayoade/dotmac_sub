from __future__ import annotations

import uuid

import pytest
from fastapi.routing import APIRoute

from app.api import crm_referrals as referral_api
from app.models.audit import AuditEvent
from app.models.domain_settings import DomainSetting, SettingDomain, SettingValueType
from app.models.event_store import EventStore
from app.models.party import PartyType
from app.models.referral_native import Referral
from app.models.sales import Lead
from app.models.subscriber import Subscriber, SubscriberStatus
from app.schemas.referral import ReferralSubscriberCreateRequest
from app.schemas.subscriber import SubscriberCreate
from app.services import party as party_service
from app.services import referral_account_conversion
from app.services import subscriber as subscriber_service
from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError
from app.services.owner_commands import CommandContext
from tests.referral_program_testkit import capture, ensure_code


def _email(prefix: str = "ref-convert") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}@example.com"


def _subscriber(db, *, status: SubscriberStatus = SubscriberStatus.active):
    subscriber = Subscriber(
        first_name="Referral",
        last_name="Owner",
        email=_email(),
        status=status,
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


def _captured(db):
    _enable_program(db)
    referrer = _subscriber(db)
    code = ensure_code(db, referrer.id)
    referral = capture(
        db,
        code=code.code,
        name="Reviewed Prospect",
        email=_email("prospect"),
    )
    return referral, referrer


def _payload(*, status: SubscriberStatus = SubscriberStatus.new) -> SubscriberCreate:
    return SubscriberCreate(
        first_name="Reviewed",
        last_name="Prospect",
        email=_email("account"),
        status=status,
    )


def _create(db, referral: Referral, *, payload: SubscriberCreate | None = None):
    referral_id = referral.id
    referred_party_id = referral.referred_party_id
    referred_lead_id = referral.referred_lead_id
    db_session_adapter.release_read_transaction(db)
    return referral_account_conversion.create_account(
        db,
        referral_account_conversion.CreateReferralAccountCommand(
            context=CommandContext.system(
                actor="test_referral_account_conversion",
                scope=referral_account_conversion.REFERRAL_ACCOUNT_CONVERSION_SCOPE,
                reason="Operator reviewed exact Referral, Party, and Lead context",
                idempotency_key=f"test-referral-create:{referral_id}",
            ),
            referral_id=referral_id,
            referred_party_id=referred_party_id,
            referred_lead_id=referred_lead_id,
            subscriber_payload=payload or _payload(),
        ),
    )


def _attach(
    db,
    referral: Referral,
    subscriber_id,
    *,
    referred_party_id=None,
    reason: str = "Operator reviewed protected evidence for this exact Party",
):
    referral_id = referral.id
    resolved_party_id = referred_party_id or referral.referred_party_id
    referred_lead_id = referral.referred_lead_id
    db_session_adapter.release_read_transaction(db)
    return referral_account_conversion.attach_existing_account(
        db,
        referral_account_conversion.AttachExistingReferralAccountCommand(
            context=CommandContext.system(
                actor="test_operator_adjudication",
                scope=referral_account_conversion.REFERRAL_ACCOUNT_CONVERSION_SCOPE,
                reason=reason,
                idempotency_key=(f"test-referral-attach:{referral_id}:{subscriber_id}"),
            ),
            referral_id=referral_id,
            referred_party_id=resolved_party_id,
            referred_lead_id=referred_lead_id,
            subscriber_id=subscriber_id,
        ),
    )


def test_create_account_preserves_exact_context_and_is_idempotent(db_session):
    referral, _ = _captured(db_session)
    before = db_session.query(Subscriber).count()

    created = _create(db_session, referral)

    assert not db_session.in_transaction()
    assert created.outcome == "created"
    assert created.referral_id == referral.id
    assert created.referred_party_id == referral.referred_party_id
    assert created.referred_lead_id == referral.referred_lead_id
    subscriber = db_session.get(Subscriber, created.subscriber_id)
    assert subscriber is not None
    assert subscriber.party_id == referral.referred_party_id
    assert subscriber.status == SubscriberStatus.new
    db_session.refresh(referral)
    assert referral.referred_subscriber_id == subscriber.id
    assert referral.subscriber_link_source == "test_referral_account_conversion"
    lead = db_session.get(Lead, referral.referred_lead_id)
    assert lead is not None
    assert lead.subscriber_id == subscriber.id
    conversion_event = (
        db_session.query(EventStore)
        .filter(EventStore.event_type == "referral_account.converted")
        .one()
    )
    conversion_audit = (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == "referrals.account_converted")
        .one()
    )
    assert conversion_event.payload["schema_version"] == 1
    assert conversion_event.payload["outcome"] == "created"
    assert conversion_event.payload["subscriber_id"] == str(subscriber.id)
    assert set(conversion_event.payload).isdisjoint(
        {"email", "phone", "name", "address", "conversion_token"}
    )
    assert conversion_audit.metadata_["command_id"] == str(created.command_id)

    replay = _create(db_session, referral, payload=_payload())
    assert replay.outcome == "already_attached"
    assert replay.subscriber_id == subscriber.id
    assert db_session.query(Subscriber).count() == before + 1
    assert (
        db_session.query(EventStore)
        .filter(EventStore.event_type == "referral_account.converted")
        .count()
        == 1
    )
    assert (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == "referrals.account_converted")
        .count()
        == 1
    )


def test_create_account_preserves_requested_billing_block_state(db_session):
    referral, _ = _captured(db_session)

    result = _create(
        db_session,
        referral,
        payload=_payload(status=SubscriberStatus.blocked),
    )

    subscriber = db_session.get(Subscriber, result.subscriber_id)
    assert subscriber is not None
    assert subscriber.status == SubscriberStatus.blocked
    assert subscriber.lifecycle_override_status == SubscriberStatus.blocked


def test_attach_existing_account_adjudicates_only_the_exact_party(db_session):
    referral, _ = _captured(db_session)
    subscriber = subscriber_service.subscribers.create(db_session, _payload())

    result = _attach(db_session, referral, subscriber.id)

    assert result.outcome == "attached"
    db_session.refresh(subscriber)
    db_session.refresh(referral)
    assert subscriber.party_id == referral.referred_party_id
    assert referral.referred_subscriber_id == subscriber.id
    assert subscriber.party_binding_source == "test_operator_adjudication"
    conversion_event = (
        db_session.query(EventStore)
        .filter(EventStore.event_type == "referral_account.converted")
        .one()
    )
    assert conversion_event.payload["outcome"] == "attached"

    replay = _attach(
        db_session,
        referral,
        subscriber.id,
        reason="Exact operator retry",
    )
    assert replay.outcome == "already_attached"
    assert (
        db_session.query(EventStore)
        .filter(EventStore.event_type == "referral_account.converted")
        .count()
        == 1
    )


def test_stale_context_and_different_party_are_refused_without_repoint(db_session):
    referral, _ = _captured(db_session)
    subscriber = subscriber_service.subscribers.create(db_session, _payload())

    with pytest.raises(
        referral_account_conversion.ReferralAccountConversionError,
        match="Party context is stale",
    ):
        _attach(
            db_session,
            referral,
            subscriber.id,
            referred_party_id=uuid.uuid4(),
            reason="Stale hidden form context",
        )

    other_party = party_service.create_party(
        db_session,
        party_type=PartyType.person,
        display_name="Different reviewed person",
    )
    party_service.bind_subscriber_account(
        db_session,
        subscriber_id=subscriber.id,
        party_id=other_party.id,
        source="test_existing_binding",
        reason="Subscriber already belongs to another reviewed Party",
    )
    db_session.commit()

    with pytest.raises(
        referral_account_conversion.ReferralAccountConversionError,
        match="already bound to Party",
    ):
        _attach(
            db_session,
            referral,
            subscriber.id,
            reason="Must not repoint",
        )

    db_session.refresh(subscriber)
    db_session.refresh(referral)
    assert subscriber.party_id == other_party.id
    assert referral.referred_subscriber_id is None


def test_self_referral_failure_rolls_back_temporary_party_binding(db_session):
    referral, referrer = _captured(db_session)
    assert referrer.party_id is None

    with pytest.raises(
        referral_account_conversion.ReferralAccountConversionError,
        match="self-refer",
    ):
        _attach(
            db_session,
            referral,
            referrer.id,
            reason="Invalid self-referral attempt",
        )

    db_session.refresh(referrer)
    db_session.refresh(referral)
    assert referrer.party_id is None
    assert referral.referred_subscriber_id is None


def test_late_event_failure_rolls_back_account_and_all_conversion_links(
    db_session, monkeypatch
):
    referral, _ = _captured(db_session)
    referral_id = referral.id
    before = db_session.query(Subscriber).count()

    def fail_event(*_args, **_kwargs):
        raise RuntimeError("conversion event unavailable")

    monkeypatch.setattr(referral_account_conversion, "emit_event", fail_event)

    with pytest.raises(RuntimeError, match="conversion event unavailable"):
        _create(db_session, referral)

    assert not db_session.in_transaction()
    canonical_referral = db_session.get(Referral, referral_id)
    assert canonical_referral is not None
    assert canonical_referral.referred_subscriber_id is None
    assert db_session.query(Subscriber).count() == before
    assert (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == "referrals.account_converted")
        .count()
        == 0
    )


def test_owner_rejects_an_active_caller_transaction(db_session):
    referral, _ = _captured(db_session)
    command = referral_account_conversion.CreateReferralAccountCommand(
        context=CommandContext.system(
            actor="test_referral_account_conversion",
            scope=referral_account_conversion.REFERRAL_ACCOUNT_CONVERSION_SCOPE,
            reason="Caller must not own the conversion transaction",
        ),
        referral_id=referral.id,
        referred_party_id=referral.referred_party_id,
        referred_lead_id=referral.referred_lead_id,
        subscriber_payload=_payload(),
    )
    assert db_session.in_transaction()

    with pytest.raises(DomainError) as exc:
        referral_account_conversion.create_account(db_session, command)

    assert exc.value.code == "referrals.account_conversion.active_caller_transaction"
    assert not db_session.in_transaction()


def _route(path: str) -> APIRoute:
    for route in referral_api.router.routes:
        if isinstance(route, APIRoute) and route.path == path:
            return route
    raise AssertionError(f"Route not found: {path}")


def _route_permission(path: str, permission: str) -> bool:
    for dependency in _route(path).dependant.dependencies:
        closure = getattr(dependency.call, "__closure__", None) or ()
        if any(permission in str(cell.cell_contents) for cell in closure):
            return True
    return False


def test_staff_conversion_routes_require_referral_and_customer_permissions():
    attach = "/crm/referrals/{referral_id}/attach-subscriber"
    create = "/crm/referrals/{referral_id}/create-subscriber"
    assert _route_permission(attach, "crm:lead:write")
    assert _route_permission(attach, "customer:update")
    assert _route_permission(create, "crm:lead:write")
    assert _route_permission(create, "customer:create")


def test_staff_create_adapter_carries_exact_context_into_account_creation(db_session):
    referral, _ = _captured(db_session)
    actor_id = str(uuid.uuid4())
    request = ReferralSubscriberCreateRequest(
        referred_party_id=referral.referred_party_id,
        referred_lead_id=referral.referred_lead_id,
        subscriber=_payload(),
        reason="Staff reviewed exact signup context",
    )

    result = referral_api.create_referral_subscriber(
        referral_id=str(referral.id),
        payload=request,
        db=db_session,
        auth={"principal_type": "system_user", "principal_id": actor_id},
    )

    assert result.outcome == "created"
    db_session.refresh(referral)
    assert referral.referred_subscriber_id == result.subscriber_id
    assert (
        referral.subscriber_link_source
        == f"staff_referral_create:system_user:{actor_id}"
    )
