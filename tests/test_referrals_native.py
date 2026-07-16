"""Native referral service (Phase 3 §2.1): capture → qualify → reward flows,
external_ref idempotency continuity with the CRM's payout path, the
subscriber-activation hook wiring, and §2.5 read-shape compatibility with the
referral mirror."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi import HTTPException

from app.models.billing import CreditNote
from app.models.domain_settings import DomainSetting, SettingDomain, SettingValueType
from app.models.referral_native import Referral, ReferralCode
from app.models.sales import Lead
from app.models.subscriber import PartyStatus, Subscriber, SubscriberStatus
from app.services import crm_api
from app.services.referrals import _CODE_ALPHABET, referrals


def _unique_email() -> str:
    return f"ref-{uuid.uuid4().hex[:10]}@example.com"


def _subscriber(db, *, status=SubscriberStatus.active, email=None) -> Subscriber:
    sub = Subscriber(
        first_name="Refer",
        last_name="Rer",
        email=email or _unique_email(),
        status=status,
        is_active=True,
    )
    db.add(sub)
    db.commit()
    db.refresh(sub)
    return sub


def _program(
    db,
    *,
    enabled: bool = True,
    amount: str = "2500",
    window_days: int | None = None,
    auto_approve: bool | None = None,
):
    rows = {
        "referral_program_enabled": (
            "true" if enabled else "false",
            SettingValueType.boolean,
        ),
        "referral_reward_amount": (amount, SettingValueType.string),
    }
    if window_days is not None:
        rows["referral_qualify_window_days"] = (
            str(window_days),
            SettingValueType.integer,
        )
    if auto_approve is not None:
        rows["referral_auto_approve_reward"] = (
            "true" if auto_approve else "false",
            SettingValueType.boolean,
        )
    for key, (text, value_type) in rows.items():
        db.add(
            DomainSetting(
                domain=SettingDomain.subscriber,
                key=key,
                value_type=value_type,
                value_text=text,
                is_active=True,
            )
        )
    db.commit()


# ── codes ────────────────────────────────────────────────────────────────────


def test_ensure_code_mints_and_reuses(db_session):
    referrer = _subscriber(db_session)
    code = referrals.ensure_code(db_session, str(referrer.id))
    assert len(code.code) == 8
    assert set(code.code) <= set(_CODE_ALPHABET)
    again = referrals.ensure_code(db_session, str(referrer.id))
    assert again.id == code.id  # one active code per referrer


def test_ensure_code_unknown_subscriber_404(db_session):
    with pytest.raises(HTTPException) as exc:
        referrals.ensure_code(db_session, str(uuid.uuid4()))
    assert exc.value.status_code == 404


# ── capture ──────────────────────────────────────────────────────────────────


def test_capture_creates_prospect_lead_and_referral(db_session):
    _program(db_session)
    referrer = _subscriber(db_session)
    code = referrals.ensure_code(db_session, str(referrer.id))

    email = _unique_email()
    referral = referrals.capture(
        db_session,
        code=code.code,
        name="Ada Lovelace",
        email=email,
        phone="0803 000 0001",
        region="Abuja",
        source="public",
    )
    assert referral.status == "pending"
    assert referral.reward_status == "none"
    assert referral.referrer_subscriber_id == referrer.id
    assert referral.referral_code_id == code.id
    assert referral.source == "public"
    assert referral.metadata_["capture"]["email"] == email
    assert referral.metadata_["capture"]["name"] == "Ada Lovelace"

    # Prospect party row: status=new keeps it out of billing/RADIUS sweeps.
    prospect = db_session.get(Subscriber, referral.referred_subscriber_id)
    assert prospect is not None
    assert prospect.status == SubscriberStatus.new
    assert prospect.party_status == PartyStatus.lead.value
    assert prospect.first_name == "Ada"
    assert prospect.email == email

    # Attributed lead.
    lead = db_session.get(Lead, referral.referred_lead_id)
    assert lead is not None
    assert lead.subscriber_id == prospect.id
    assert lead.lead_source == "Referral"
    assert lead.metadata_["referral_code"] == code.code
    assert lead.metadata_["referrer_subscriber_id"] == str(referrer.id)


def test_capture_is_idempotent_per_referred_prospect(db_session):
    _program(db_session)
    referrer = _subscriber(db_session)
    code = referrals.ensure_code(db_session, str(referrer.id))
    email = _unique_email()

    first = referrals.capture(db_session, code=code.code, email=email, name="Once")
    second = referrals.capture(db_session, code=code.code, email=email, name="Twice")
    assert second.id == first.id

    # Phone-only capture dedupes too (prospect row matched by phone).
    phone_first = referrals.capture(db_session, code=code.code, phone="0812 345 6789")
    phone_second = referrals.capture(db_session, code=code.code, phone="+2348123456789")
    assert phone_second.id == phone_first.id


def test_capture_validations(db_session):
    referrer = _subscriber(db_session)

    with pytest.raises(HTTPException) as exc:
        referrals.capture(db_session, code="NOPE1234", email=_unique_email())
    assert exc.value.status_code == 503  # program disabled

    _program(db_session)
    with pytest.raises(HTTPException) as exc:
        referrals.capture(db_session, code="NOPE1234", email=_unique_email())
    assert exc.value.status_code == 404  # invalid code

    code = referrals.ensure_code(db_session, str(referrer.id))
    with pytest.raises(HTTPException) as exc:
        referrals.capture(db_session, code=code.code, name="No Contact")
    assert exc.value.status_code == 422  # email or phone required


def test_capture_self_referral_409(db_session):
    _program(db_session)
    referrer = _subscriber(db_session)
    code = referrals.ensure_code(db_session, str(referrer.id))
    with pytest.raises(HTTPException) as exc:
        referrals.capture(db_session, code=code.code, email=referrer.email)
    assert exc.value.status_code == 409


def test_capture_already_active_customer_409(db_session):
    _program(db_session)
    referrer = _subscriber(db_session)
    existing_customer = _subscriber(db_session, status=SubscriberStatus.active)
    code = referrals.ensure_code(db_session, str(referrer.id))
    with pytest.raises(HTTPException) as exc:
        referrals.capture(db_session, code=code.code, email=existing_customer.email)
    assert exc.value.status_code == 409


# ── qualification ────────────────────────────────────────────────────────────


def _captured_referral(db, referrer=None, **capture_kwargs):
    referrer = referrer or _subscriber(db)
    code = referrals.ensure_code(db, str(referrer.id))
    capture_kwargs.setdefault("email", _unique_email())
    return referrals.capture(db, code=code.code, **capture_kwargs), referrer


def test_qualify_on_activation(db_session):
    _program(db_session, amount="2500")
    referral, _referrer = _captured_referral(db_session)
    prospect = db_session.get(Subscriber, referral.referred_subscriber_id)
    prospect.status = SubscriberStatus.active
    db_session.commit()

    result = referrals.qualify_for_subscriber(db_session, prospect)
    assert result is not None and result.id == referral.id
    assert result.status == "qualified"
    assert result.reward_status == "pending"  # no auto-approve
    assert result.reward_amount == Decimal("2500")
    assert result.qualified_at is not None
    assert result.referred_subscriber_id == prospect.id

    # Idempotent: a second activation event does nothing (no longer pending).
    assert referrals.qualify_for_subscriber(db_session, prospect) is None


def test_qualify_auto_approve(db_session):
    _program(db_session, auto_approve=True)
    referral, _ = _captured_referral(db_session)
    prospect = db_session.get(Subscriber, referral.referred_subscriber_id)
    prospect.status = SubscriberStatus.active
    db_session.commit()
    result = referrals.qualify_for_subscriber(db_session, prospect)
    assert result is not None
    assert result.reward_status == "approved"


def test_qualify_expires_outside_window(db_session):
    _program(db_session, window_days=30)
    referral, _ = _captured_referral(db_session)
    referral.created_at = datetime.now(UTC) - timedelta(days=45)
    prospect = db_session.get(Subscriber, referral.referred_subscriber_id)
    prospect.status = SubscriberStatus.active
    db_session.commit()

    result = referrals.qualify_for_subscriber(db_session, prospect)
    assert result is not None
    assert result.status == "expired"
    assert result.reward_status == "none"


def test_qualify_bridges_identity_when_signup_creates_new_row(db_session):
    """The signup flow may create a fresh subscriber row instead of reusing the
    capture-time prospect — qualification bridges by capture email/phone and
    re-links the referral (the CRM got this via person_identity)."""
    _program(db_session)
    email = _unique_email()
    referral, _ = _captured_referral(db_session, email=email)
    prospect_id = referral.referred_subscriber_id

    signup = _subscriber(db_session, status=SubscriberStatus.active, email=email)
    assert signup.id != prospect_id

    result = referrals.qualify_for_subscriber(db_session, signup)
    assert result is not None and result.id == referral.id
    assert result.status == "qualified"
    assert result.referred_subscriber_id == signup.id  # re-linked


def test_qualify_noops(db_session):
    _program(db_session)
    referral, referrer = _captured_referral(db_session)
    prospect = db_session.get(Subscriber, referral.referred_subscriber_id)

    # Not active yet → no-op.
    assert referrals.qualify_for_subscriber(db_session, prospect) is None
    assert db_session.get(Referral, referral.id).status == "pending"

    # Active subscriber with no referral → no-op.
    unrelated = _subscriber(db_session, status=SubscriberStatus.active)
    assert referrals.qualify_for_subscriber(db_session, unrelated) is None

    # The referrer activating themselves never qualifies their own referral.
    assert referrals.qualify_for_subscriber(db_session, referrer) is None


def test_qualify_noop_when_program_disabled(db_session):
    _program(db_session)
    referral, _ = _captured_referral(db_session)
    prospect = db_session.get(Subscriber, referral.referred_subscriber_id)
    prospect.status = SubscriberStatus.active
    db_session.query(DomainSetting).filter(
        DomainSetting.key == "referral_program_enabled"
    ).update({"value_text": "false"})
    db_session.commit()
    assert referrals.qualify_for_subscriber(db_session, prospect) is None


# ── reward payout (external_ref continuity) ──────────────────────────────────


def _qualified_referral(db, amount="2500"):
    _program(db, amount=amount)
    referral, referrer = _captured_referral(db)
    prospect = db.get(Subscriber, referral.referred_subscriber_id)
    prospect.status = SubscriberStatus.active
    db.commit()
    referral = referrals.qualify_for_subscriber(db, prospect)
    db.commit()
    return referral, referrer


def test_issue_reward_credits_wallet_with_referral_external_ref(db_session):
    referral, referrer = _qualified_referral(db_session)

    result = referrals.issue_reward(db_session, str(referral.id))
    assert result.status == "rewarded"
    assert result.reward_status == "issued"
    assert result.reward_issued_at is not None

    credit = db_session.get(CreditNote, result.metadata_["reward_credit_id"])
    assert credit is not None
    assert credit.total == Decimal("2500")
    assert f"[ref:referral:{referral.id}]" in str(credit.memo)
    assert result.metadata_["reward_subscriber_id"] == str(referrer.id)


def test_issue_reward_is_idempotent(db_session):
    referral, referrer = _qualified_referral(db_session)
    referrals.issue_reward(db_session, str(referral.id))
    again = referrals.issue_reward(db_session, str(referral.id))  # retry
    assert again.status == "rewarded"

    credits = (
        db_session.query(CreditNote)
        .filter(CreditNote.account_id == referrer.id)
        .filter(CreditNote.memo.ilike(f"%[ref:referral:{referral.id}]%"))
        .all()
    )
    assert len(credits) == 1  # never double-credited


def test_issue_reward_external_ref_continuity_with_crm_payout(db_session):
    """A reward the CRM already paid pre-cutover (via POST /crm/credits with
    external_ref='referral:{id}') is returned by the native path, not re-paid —
    the SAME key flows through the SAME dedupe."""
    referral, referrer = _qualified_referral(db_session)
    external_ref = f"referral:{referral.id}"

    # Simulate the CRM's historical payout through the /crm/credits service.
    crm_entry = crm_api.create_account_credit(
        db_session,
        subscriber_id=str(referrer.id),
        amount=Decimal("2500"),
        reason="Referral reward",
        external_ref=external_ref,
    )

    result = referrals.issue_reward(db_session, str(referral.id))
    assert result.status == "rewarded"
    assert result.reward_status == "issued"
    assert result.metadata_["reward_credit_id"] == str(crm_entry.id)

    assert (
        db_session.query(CreditNote)
        .filter(CreditNote.account_id == referrer.id)
        .filter(CreditNote.memo.ilike(f"%[ref:{external_ref}]%"))
        .count()
        == 1
    )


def test_issue_reward_guards(db_session):
    _program(db_session)
    referral, _ = _captured_referral(db_session)

    with pytest.raises(HTTPException) as exc:
        referrals.issue_reward(db_session, str(referral.id))
    assert exc.value.status_code == 409  # still pending

    with pytest.raises(HTTPException) as exc:
        referrals.issue_reward(db_session, str(uuid.uuid4()))
    assert exc.value.status_code == 404

    # Qualified but zero reward: never mark rewarded with no credit behind it.
    prospect = db_session.get(Subscriber, referral.referred_subscriber_id)
    prospect.status = SubscriberStatus.active
    db_session.commit()
    qualified = referrals.qualify_for_subscriber(db_session, prospect)
    qualified.reward_amount = Decimal("0")
    db_session.commit()
    with pytest.raises(HTTPException) as exc:
        referrals.issue_reward(db_session, str(referral.id))
    assert exc.value.status_code == 400


def test_reject_sets_void_and_notes(db_session):
    _program(db_session)
    referral, _ = _captured_referral(db_session)
    result = referrals.reject(db_session, str(referral.id), "Fraudulent capture")
    assert result.status == "rejected"
    assert result.reward_status == "void"
    assert "Rejected: Fraudulent capture" in (result.notes or "")


# ── activation hook wiring ───────────────────────────────────────────────────


def test_referral_handler_registered_in_dispatcher():
    from app.services.events.dispatcher import get_dispatcher, reset_dispatcher

    reset_dispatcher()
    try:
        dispatcher = get_dispatcher()
        names = [h.__class__.__name__ for h in dispatcher._handlers]
        assert "ReferralHandler" in names
    finally:
        reset_dispatcher()


def test_activation_event_qualifies_referral(db_session):
    from app.services.events.handlers.referral import ReferralHandler
    from app.services.events.types import Event, EventType

    _program(db_session)
    referral, _ = _captured_referral(db_session)
    prospect = db_session.get(Subscriber, referral.referred_subscriber_id)
    prospect.status = SubscriberStatus.active
    db_session.commit()

    # subscription.activated carries the subscriber UUID as account_id
    # (activate_subscription's emit).
    event = Event(
        event_type=EventType.subscription_activated,
        payload={"subscription_id": str(uuid.uuid4())},
        account_id=prospect.id,
    )
    ReferralHandler().handle(db_session, event)
    db_session.commit()

    assert db_session.get(Referral, referral.id).status == "qualified"


def test_handler_ignores_unrelated_events_and_missing_subscriber(db_session):
    from app.services.events.handlers.referral import ReferralHandler
    from app.services.events.types import Event, EventType

    _program(db_session)
    referral, _ = _captured_referral(db_session)

    handler = ReferralHandler()
    handler.handle(
        db_session,
        Event(event_type=EventType.invoice_paid, payload={}, account_id=uuid.uuid4()),
    )
    handler.handle(
        db_session,
        Event(
            event_type=EventType.subscription_activated,
            payload={},
            account_id=uuid.uuid4(),  # unknown subscriber
        ),
    )
    handler.handle(
        db_session,
        Event(event_type=EventType.subscription_activated, payload={}),  # no subject
    )
    assert db_session.get(Referral, referral.id).status == "pending"


# ── §2.5 read-shape compatibility ────────────────────────────────────────────


def test_read_for_subscriber_matches_mirror_shape(db_session):
    from app.schemas.portal import MyReferralsResponse

    referral, referrer = _qualified_referral(db_session)
    referrals.issue_reward(db_session, str(referral.id))

    payload = referrals.read_for_subscriber(db_session, str(referrer.id))

    # Exact key sets of referrals_mirror.read_for_subscriber (§2.5 contract).
    assert set(payload) == {"code", "share_url", "program", "totals", "referrals"}
    assert set(payload["program"]) == {"enabled", "reward_amount", "reward_currency"}
    assert set(payload["totals"]) == {
        "total",
        "pending",
        "qualified",
        "rewarded",
        "total_earned",
    }
    assert set(payload["referrals"][0]) == {
        "id",
        "status",
        "referred_name",
        "reward_amount",
        "reward_currency",
        "reward_status",
        "created_at",
        "qualified_at",
    }
    # And the payload validates against the portal response schema.
    parsed = MyReferralsResponse.model_validate(payload)
    assert parsed.code == payload["code"]
    assert payload["share_url"].endswith(f"/r/{payload['code']}")

    item = payload["referrals"][0]
    assert item["id"] == str(referral.id)  # id = referral UUID (§2.5)
    assert item["status"] == "rewarded"
    assert item["reward_status"] == "issued"  # native standardizes on issued
    totals = payload["totals"]
    assert (
        totals["total"],
        totals["pending"],
        totals["qualified"],
        totals["rewarded"],
    ) == (1, 0, 0, 1)
    # Numeric(12,2) round-trips with scale (the mirror serialized the same way).
    assert Decimal(totals["total_earned"]) == Decimal("2500")
    assert payload["program"]["enabled"] is True
    assert Decimal(payload["program"]["reward_amount"]) == Decimal("2500")


def test_refer_a_friend_portal_shape(db_session):
    from app.schemas.portal import ReferAFriendResponse

    _program(db_session)
    referrer = _subscriber(db_session)
    result = referrals.refer_a_friend(
        db_session,
        str(referrer.id),
        name="Friend",
        email=_unique_email(),
        note="from the app",
    )
    assert set(result) == {"id", "status", "message"}
    parsed = ReferAFriendResponse.model_validate(result)
    assert parsed.status == "pending"

    # The referrer now has an active code (minted on first use).
    code = (
        db_session.query(ReferralCode)
        .filter(ReferralCode.subscriber_id == referrer.id)
        .one()
    )
    assert code.is_active
