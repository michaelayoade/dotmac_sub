"""Finance-reviewed prepaid renewal-term record: four-eyes, evidence-bound.

A never-restored blocked prepaid subscription (no_evidence and the other
evidence-gap decisions) gets its contracted amount only through a request by
one staff member and an approval by a different one. The request changes no
price; the approval re-validates everything under the subscription lock,
writes ``Subscription.unit_price``, and closes the finance work item in the
same transaction. Catalog prices are never consulted for the amount.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from app.models.admin_alert import AdminAlert
from app.models.audit import AuditEvent
from app.models.catalog import (
    BillingCycle,
    BillingMode,
    OfferPrice,
    PriceType,
    Subscription,
    SubscriptionStatus,
)
from app.models.event_store import EventStore
from app.models.system_user import SystemUser
from app.services.owner_commands import CommandContext
from app.services.prepaid_renewal_terms_backfill import (
    _FINDING_PREFIX,
    RENEWAL_TERM_RECORD_PERMISSION,
    RENEWAL_TERMS_RUNBOOK,
    RENEWAL_TERMS_WORK_ITEM_OWNER,
    WORK_ITEM_SUMMARIES,
    ApproveRenewalTermRecordCommand,
    CaptureRenewalTermsBackfillCommand,
    PrepaidRenewalTermsBackfillError,
    RenewalTermRecordStatus,
    RenewalTermsDecision,
    RenewalTermsNextAction,
    RequestRenewalTermRecordCommand,
    approve_reviewed_renewal_term_record,
    capture_prepaid_renewal_terms_backfill,
    list_renewal_term_record_requests,
    preview_prepaid_renewal_terms_backfill,
    request_reviewed_renewal_term_record,
)
from tests.sole_approver_support import (
    DECISION_REF,
    JUSTIFICATION,
    configure_sole_approver_exception,
    future_review_due,
)

_SHA = "a" * 64
_NOON = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)


def _staff(db, name: str = "Finance") -> SystemUser:
    user = SystemUser(
        id=uuid4(),
        first_name=name,
        last_name="Reviewer",
        display_name=f"{name} Reviewer",
        email=f"{name.lower()}-{uuid4().hex}@example.test",
        is_active=True,
    )
    db.add(user)
    db.commit()
    return user


def _ensure_charge_inputs(db, subscription) -> None:
    existing = (
        db.query(OfferPrice)
        .filter(
            OfferPrice.offer_id == subscription.offer_id,
            OfferPrice.price_type == PriceType.recurring,
            OfferPrice.is_active.is_(True),
        )
        .first()
    )
    if existing is None:
        db.add(
            OfferPrice(
                offer_id=subscription.offer_id,
                price_type=PriceType.recurring,
                amount=Decimal("35000.00"),
                currency="NGN",
                billing_cycle=BillingCycle.monthly,
                is_active=True,
            )
        )
        db.flush()


def _block(db, subscription, *, charge_inputs: bool = True) -> None:
    subscription.billing_mode = BillingMode.prepaid
    subscription.status = SubscriptionStatus.active
    subscription.unit_price = None
    if charge_inputs:
        _ensure_charge_inputs(db, subscription)
    else:
        for row in db.query(OfferPrice).filter(
            OfferPrice.offer_id == subscription.offer_id
        ):
            row.is_active = False
        subscription.billing_cycle = None
    db.commit()


def _open_work_items(db) -> None:
    preview = preview_prepaid_renewal_terms_backfill(db, now=_NOON)
    db.commit()
    capture_prepaid_renewal_terms_backfill(
        db,
        CaptureRenewalTermsBackfillCommand(
            preview_fingerprint=preview.fingerprint, as_of=_NOON
        ),
        context=CommandContext.system(
            actor="pytest:renewal-term-record",
            scope="financial.prepaid_renewal_terms_backfill:test",
            reason="open renewal-terms work items",
            idempotency_key=f"capture-{uuid4()}",
        ),
    )


def _work_item(db, subscription) -> AdminAlert:
    return (
        db.query(AdminAlert)
        .filter(AdminAlert.fingerprint == f"{_FINDING_PREFIX}{subscription.id}")
        .one()
    )


def _context(user: SystemUser, key: str, *, scope: str | None = None):
    return CommandContext.system(
        actor=f"user:{user.id}",
        scope=scope or RENEWAL_TERM_RECORD_PERMISSION,
        reason="finance-reviewed renewal-term record test",
        idempotency_key=key,
    )


def _request(
    db,
    subscription,
    requester: SystemUser,
    *,
    amount: str = "17500.00",
    expected: Decimal | None = None,
    key: str = "record-request",
    permission_granted: bool = True,
    sha: str = _SHA,
    scope: str | None = None,
):
    subscription_id = subscription.id
    requester_id = requester.id
    context = _context(requester, key, scope=scope)
    db.commit()
    return request_reviewed_renewal_term_record(
        db,
        RequestRenewalTermRecordCommand(
            subscription_id=subscription_id,
            reviewed_amount=Decimal(amount),
            expected_current_amount=expected,
            reason="Signed order form; customer contracted at 17,500/month",
            evidence_reference="FIN-2026-1008/order-form.pdf",
            evidence_sha256=sha,
            requested_by=requester_id,
            permission_granted=permission_granted,
        ),
        context=context,
    )


def _approve(
    db,
    request_id,
    approver: SystemUser,
    *,
    amount: str = "17500.00",
    key: str = "record-approve",
    permission_granted: bool = True,
    sole_justification: str | None = None,
):
    approver_id = approver.id
    context = _context(approver, key)
    db.commit()
    return approve_reviewed_renewal_term_record(
        db,
        ApproveRenewalTermRecordCommand(
            request_id=request_id,
            approved_amount=Decimal(amount),
            approved_by=approver_id,
            permission_granted=permission_granted,
            sole_approver_justification=sole_justification,
        ),
        context=context,
    )


def _code(captured) -> str:
    return captured.value.code.rsplit(".", maxsplit=1)[-1]


def test_request_then_distinct_approval_records_amount_and_closes_work_item(
    db_session, subscription
):
    _block(db_session, subscription)
    _open_work_items(db_session)
    item = _work_item(db_session, subscription)
    assert item.status.value == "open"
    assert item.details["decision"] == RenewalTermsDecision.no_evidence.value
    assert item.details["owner"] == RENEWAL_TERMS_WORK_ITEM_OWNER
    assert item.details["runbook"] == RENEWAL_TERMS_RUNBOOK
    assert item.details["next_action"] == RenewalTermsNextAction.reviewed_record.value

    requester = _staff(db_session, "Ada")
    approver = _staff(db_session, "Bola")
    requested = _request(db_session, subscription, requester)

    assert requested.status is RenewalTermRecordStatus.requested
    db_session.refresh(subscription)
    assert subscription.unit_price is None  # a request never changes the price
    pending = list_renewal_term_record_requests(
        db_session, subscription_id=subscription.id
    )
    assert [p.request_id for p in pending] == [requested.request_id]
    assert pending[0].requested_by == requester.id
    assert pending[0].decision is RenewalTermsDecision.no_evidence

    recorded = _approve(db_session, requested.request_id, approver)

    assert recorded.status is RenewalTermRecordStatus.recorded
    assert recorded.previous_amount is None
    assert recorded.new_amount == Decimal("17500.00")
    assert recorded.work_item_resolved is True
    db_session.refresh(subscription)
    assert subscription.unit_price == Decimal("17500.00")
    db_session.expire_all()
    assert _work_item(db_session, subscription).status.value == "resolved"
    assert list_renewal_term_record_requests(db_session) == ()

    # Durable provenance carries both staff identities and the evidence.
    event = (
        db_session.query(EventStore)
        .filter(EventStore.event_type == "prepaid_renewal_terms.recorded")
        .one()
    )
    assert event.payload["requested_by_system_user_id"] == str(requester.id)
    assert event.payload["approved_by_system_user_id"] == str(approver.id)
    assert event.payload["evidence_sha256"] == _SHA
    assert event.payload["evidence_reference"] == "FIN-2026-1008/order-form.pdf"
    assert event.payload["previous_amount"] is None
    assert event.payload["new_amount"] == "17500.00"
    assert event.payload["request_id"] == str(requested.request_id)
    request_event = (
        db_session.query(EventStore)
        .filter(EventStore.event_type == "prepaid_renewal_terms.record_requested")
        .one()
    )
    assert request_event.event_id == requested.request_id
    assert request_event.status.value == "completed"  # record-only evidence

    # The recorded subscription leaves the cohort, so the sweep keeps it closed.
    preview = preview_prepaid_renewal_terms_backfill(db_session, now=_NOON)
    assert subscription.id not in {i.subscription_id for i in preview.items}


def test_the_requester_cannot_approve_their_own_request(db_session, subscription):
    _block(db_session, subscription)
    requester = _staff(db_session)
    requested = _request(db_session, subscription, requester)

    with pytest.raises(PrepaidRenewalTermsBackfillError) as captured:
        _approve(db_session, requested.request_id, requester)
    assert _code(captured) == "self_approval_forbidden"
    db_session.rollback()
    db_session.refresh(subscription)
    assert subscription.unit_price is None


@pytest.mark.parametrize("amount", ["0.00", "0", "-5000.00"])
def test_zero_or_negative_amount_is_refused(db_session, subscription, amount):
    _block(db_session, subscription)
    with pytest.raises(PrepaidRenewalTermsBackfillError) as captured:
        _request(db_session, subscription, _staff(db_session), amount=amount)
    assert _code(captured) == "invalid_reviewed_amount"


def test_open_billing_treatment_is_refused(db_session, subscription):
    from app.models.subscription_billing_treatment import (
        BillingTreatmentReason,
        SubscriptionBillingArrangement,
        SubscriptionBillingTreatment,
    )

    _block(db_session, subscription)
    now = datetime.now(UTC)
    db_session.add(
        SubscriptionBillingArrangement(
            subscription_id=subscription.id,
            account_id=subscription.subscriber_id,
            authorized_offer_id=subscription.offer_id,
            treatment=SubscriptionBillingTreatment.complimentary,
            reason_code=BillingTreatmentReason.staff_benefit,
            reason="Approved staff service",
            starts_at=now + timedelta(days=1),
            ends_at=now + timedelta(days=60),
            approval_policy_max_days=366,
            maximum_recurring_amount=Decimal("35000.00"),
            billing_cycle=BillingCycle.monthly,
            currency="NGN",
            approved_by="user:approver",
            approved_at=now,
            command_id=uuid4(),
            correlation_id=uuid4(),
            idempotency_key_sha256="b" * 64,
            command_fingerprint="c" * 64,
        )
    )
    db_session.commit()

    with pytest.raises(PrepaidRenewalTermsBackfillError) as captured:
        _request(db_session, subscription, _staff(db_session))
    assert _code(captured) == "billing_treatment_open"


def test_effective_billing_treatment_is_outside_the_renewal_terms_cohort(
    db_session, subscription
):
    """Mirror the threshold: suppressed customer billing needs no terms."""
    from app.models.subscription_billing_treatment import (
        BillingTreatmentReason,
        SubscriptionBillingArrangement,
        SubscriptionBillingTreatment,
    )

    _block(db_session, subscription)
    preview = preview_prepaid_renewal_terms_backfill(db_session, now=_NOON)
    assert subscription.id in {i.subscription_id for i in preview.items}

    db_session.add(
        SubscriptionBillingArrangement(
            subscription_id=subscription.id,
            account_id=subscription.subscriber_id,
            authorized_offer_id=subscription.offer_id,
            treatment=SubscriptionBillingTreatment.complimentary,
            reason_code=BillingTreatmentReason.internal_service,
            reason="Approved internal service",
            starts_at=_NOON - timedelta(days=1),
            ends_at=_NOON + timedelta(days=60),
            approval_policy_max_days=366,
            maximum_recurring_amount=Decimal("35000.00"),
            billing_cycle=BillingCycle.monthly,
            currency="NGN",
            approved_by="user:approver",
            approved_at=_NOON - timedelta(days=1),
            command_id=uuid4(),
            correlation_id=uuid4(),
            idempotency_key_sha256="d" * 64,
            command_fingerprint="e" * 64,
        )
    )
    db_session.commit()

    preview = preview_prepaid_renewal_terms_backfill(db_session, now=_NOON)
    assert subscription.id not in {i.subscription_id for i in preview.items}


def test_priced_or_postpaid_subscription_is_outside_the_record_cohort(
    db_session, subscription
):
    _block(db_session, subscription)
    subscription.unit_price = Decimal("12000.00")
    db_session.commit()
    with pytest.raises(PrepaidRenewalTermsBackfillError) as captured:
        _request(
            db_session,
            subscription,
            _staff(db_session),
            expected=Decimal("12000.00"),
        )
    assert _code(captured) == "not_in_record_cohort"

    subscription.unit_price = None
    subscription.billing_mode = BillingMode.postpaid
    db_session.commit()
    with pytest.raises(PrepaidRenewalTermsBackfillError) as captured:
        _request(db_session, subscription, _staff(db_session), key="postpaid")
    assert _code(captured) == "not_in_record_cohort"


def test_unknown_subscription_is_refused(db_session, subscription):
    ghost = Subscription(id=uuid4())
    with pytest.raises(PrepaidRenewalTermsBackfillError) as captured:
        _request(db_session, ghost, _staff(db_session))
    assert _code(captured) == "subscription_not_found"


def test_stale_expected_value_is_refused_at_request_and_approval(
    db_session, subscription
):
    _block(db_session, subscription)
    requester = _staff(db_session, "Ada")
    approver = _staff(db_session, "Bola")

    with pytest.raises(PrepaidRenewalTermsBackfillError) as captured:
        _request(db_session, subscription, requester, expected=Decimal("0.00"))
    assert _code(captured) == "stale_current_amount"
    db_session.rollback()

    requested = _request(db_session, subscription, requester, key="fresh")
    # The stored value moves (still unpriced) between request and approval.
    subscription.unit_price = Decimal("0.00")
    db_session.commit()
    with pytest.raises(PrepaidRenewalTermsBackfillError) as captured:
        _approve(db_session, requested.request_id, approver)
    assert _code(captured) == "stale_current_amount"
    db_session.rollback()
    db_session.refresh(subscription)
    assert subscription.unit_price == Decimal("0.00")


def test_request_and_approval_replay_idempotently(db_session, subscription):
    _block(db_session, subscription)
    requester = _staff(db_session, "Ada")
    approver = _staff(db_session, "Bola")

    first = _request(db_session, subscription, requester, key="same-key")
    again = _request(db_session, subscription, requester, key="same-key")
    assert again.replayed is True
    assert again.request_id == first.request_id
    assert (
        db_session.query(EventStore)
        .filter(EventStore.event_type == "prepaid_renewal_terms.record_requested")
        .count()
        == 1
    )

    with pytest.raises(PrepaidRenewalTermsBackfillError) as captured:
        _request(db_session, subscription, requester, key="same-key", amount="9.00")
    assert _code(captured) == "idempotency_conflict"
    db_session.rollback()

    applied = _approve(db_session, first.request_id, approver)
    assert applied.replayed is False
    replay = _approve(db_session, first.request_id, approver, key="approve-again")
    assert replay.replayed is True
    assert replay.new_amount == Decimal("17500.00")
    assert (
        db_session.query(EventStore)
        .filter(EventStore.event_type == "prepaid_renewal_terms.recorded")
        .count()
        == 1
    )

    with pytest.raises(PrepaidRenewalTermsBackfillError) as captured:
        _approve(db_session, first.request_id, _staff(db_session, "Chidi"))
    assert _code(captured) == "request_already_decided"


def test_approver_must_restate_the_requested_amount(db_session, subscription):
    _block(db_session, subscription)
    requested = _request(db_session, subscription, _staff(db_session, "Ada"))
    with pytest.raises(PrepaidRenewalTermsBackfillError) as captured:
        _approve(
            db_session,
            requested.request_id,
            _staff(db_session, "Bola"),
            amount="18000.00",
        )
    assert _code(captured) == "approval_amount_mismatch"


def test_missing_permission_scope_or_inactive_staff_is_refused(
    db_session, subscription
):
    _block(db_session, subscription)
    with pytest.raises(PrepaidRenewalTermsBackfillError) as captured:
        _request(db_session, subscription, _staff(db_session), permission_granted=False)
    assert _code(captured) == "permission_denied"
    db_session.rollback()

    with pytest.raises(PrepaidRenewalTermsBackfillError) as captured:
        _request(
            db_session,
            subscription,
            _staff(db_session),
            key="wrong-scope",
            scope="billing-target-shadow",
        )
    assert _code(captured) == "permission_denied"
    db_session.rollback()

    inactive = _staff(db_session, "Gone")
    inactive.is_active = False
    db_session.commit()
    with pytest.raises(PrepaidRenewalTermsBackfillError) as captured:
        _request(db_session, subscription, inactive, key="inactive")
    assert _code(captured) == "invalid_actor"


def test_evidence_digest_must_be_sha256(db_session, subscription):
    _block(db_session, subscription)
    with pytest.raises(PrepaidRenewalTermsBackfillError) as captured:
        _request(db_session, subscription, _staff(db_session), sha="not-a-digest")
    assert _code(captured) == "invalid_evidence"


def test_missing_charge_inputs_cannot_be_cleared_by_a_price(db_session, subscription):
    _block(db_session, subscription, charge_inputs=False)
    _open_work_items(db_session)
    item = _work_item(db_session, subscription)
    assert item.details["decision"] == "missing_charge_inputs"
    assert item.details["next_action"] == RenewalTermsNextAction.charge_inputs.value
    assert "no_active_recurring_price" in item.details["insufficiency_reasons"]

    with pytest.raises(PrepaidRenewalTermsBackfillError) as captured:
        _request(db_session, subscription, _staff(db_session))
    assert _code(captured) == "charge_inputs_missing"
    db_session.rollback()
    db_session.refresh(subscription)
    assert subscription.unit_price is None
    db_session.expire_all()
    assert _work_item(db_session, subscription).status.value == "open"


def test_charge_inputs_lost_between_request_and_approval_stay_open(
    db_session, subscription
):
    _block(db_session, subscription)
    _open_work_items(db_session)
    requested = _request(db_session, subscription, _staff(db_session, "Ada"))
    for row in db_session.query(OfferPrice).filter(
        OfferPrice.offer_id == subscription.offer_id
    ):
        row.is_active = False
    subscription.billing_cycle = None
    db_session.commit()

    with pytest.raises(PrepaidRenewalTermsBackfillError) as captured:
        _approve(db_session, requested.request_id, _staff(db_session, "Bola"))
    assert _code(captured) == "charge_inputs_missing"
    db_session.rollback()
    db_session.expire_all()
    assert _work_item(db_session, subscription).status.value == "open"


def test_work_item_summaries_fit_admin_alert_schema():
    for summary in WORK_ITEM_SUMMARIES.values():
        assert len(summary) <= 255
        assert "runbook" in summary
    assert set(WORK_ITEM_SUMMARIES) == set(RenewalTermsNextAction)


def test_generic_subscription_edit_cannot_write_a_missing_prepaid_term(
    db_session, subscription
):
    from fastapi import HTTPException

    from app.schemas.catalog import SubscriptionUpdate
    from app.services.catalog.subscriptions import Subscriptions

    _block(db_session, subscription)
    with pytest.raises(HTTPException) as captured:
        Subscriptions.update(
            db_session,
            str(subscription.id),
            SubscriptionUpdate(unit_price=Decimal("17500.00")),
        )
    assert captured.value.status_code == 409
    assert "PREPAID_RENEWAL_TERMS_FINANCE_REVIEW" in str(captured.value.detail)
    db_session.rollback()
    db_session.refresh(subscription)
    assert subscription.unit_price is None


# --- Confirmed non-billable services (canonical chargeability) ----------


def _declare_zero_catalog_price(db, subscription) -> None:
    """The catalog owner's declaration: one active recurring price of ZERO."""
    for row in db.query(OfferPrice).filter(
        OfferPrice.offer_id == subscription.offer_id,
        OfferPrice.price_type == PriceType.recurring,
    ):
        row.is_active = False
    db.add(
        OfferPrice(
            offer_id=subscription.offer_id,
            price_type=PriceType.recurring,
            amount=Decimal("0.00"),
            currency="NGN",
            billing_cycle=BillingCycle.monthly,
            is_active=True,
        )
    )
    db.commit()


@pytest.mark.parametrize("stored_price", [None, Decimal("0.00")])
def test_zero_catalog_price_service_leaves_cohort_and_its_item_resolves(
    db_session, subscription, stored_price
):
    _block(db_session, subscription)
    _open_work_items(db_session)
    assert _work_item(db_session, subscription).status.value == "open"

    _declare_zero_catalog_price(db_session, subscription)
    subscription.unit_price = stored_price
    db_session.commit()

    preview = preview_prepaid_renewal_terms_backfill(db_session, now=_NOON)
    assert subscription.id not in {i.subscription_id for i in preview.items}
    _open_work_items(db_session)
    db_session.expire_all()
    assert _work_item(db_session, subscription).status.value == "resolved"

    # A positive record would contradict the catalog's free declaration.
    with pytest.raises(PrepaidRenewalTermsBackfillError) as captured:
        _request(
            db_session,
            subscription,
            _staff(db_session),
            expected=stored_price,
        )
    assert _code(captured) == "not_in_record_cohort"


def test_zero_catalog_price_service_is_non_billable_for_the_threshold(
    db_session, subscriber, subscription
):
    from app.services.prepaid_threshold import resolve_prepaid_threshold_decision

    _block(db_session, subscription)
    _declare_zero_catalog_price(db_session, subscription)

    decision = resolve_prepaid_threshold_decision(
        db_session, subscriber, now=datetime.now(UTC), currency="NGN"
    )
    assert subscription.id in decision.non_billable_subscription_ids
    assert decision.actionable_uncovered_subscription_ids == ()


def test_contradictory_positive_subscription_price_is_not_confirmed_free(
    db_session, subscription
):
    from app.services.customer_chargeability import (
        ChargeabilityReason,
        confirmed_free_subscription_ids,
        resolve_subscription_chargeability,
    )

    _block(db_session, subscription)
    _declare_zero_catalog_price(db_session, subscription)
    subscription.unit_price = Decimal("17500.00")
    db_session.commit()

    classified = resolve_subscription_chargeability(db_session, (subscription.id,))
    assert (
        classified[subscription.id].reason
        is ChargeabilityReason.catalog_subscription_price_mismatch
    )
    assert confirmed_free_subscription_ids(db_session, [subscription]) == frozenset()


def test_missing_price_row_is_review_work_not_free_service(db_session, subscription):
    from app.services.customer_chargeability import confirmed_free_subscription_ids

    _block(db_session, subscription, charge_inputs=False)
    assert confirmed_free_subscription_ids(db_session, [subscription]) == frozenset()
    preview = preview_prepaid_renewal_terms_backfill(db_session, now=_NOON)
    item = next(i for i in preview.items if i.subscription_id == subscription.id)
    assert item.decision is RenewalTermsDecision.missing_charge_inputs
    assert "no_active_recurring_price" in item.insufficiency_reasons


# --- CLI adapter ---------------------------------------------------------


def _cli():
    from scripts.billing import billing_target_shadow

    return billing_target_shadow


def test_correct_renewal_terms_cli_requires_an_explicit_actor(monkeypatch):
    cli = _cli()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "billing_target_shadow",
            "correct-renewal-terms",
            "--subscription",
            str(uuid4()),
            "--action",
            "apply_reviewed_term",
            "--source",
            "finance_review",
            "--idempotency-key",
            "k",
        ],
    )
    with pytest.raises(SystemExit) as captured:
        cli.main()
    assert captured.value.code == 2


def test_cli_actor_must_be_a_staff_uuid_not_a_label(monkeypatch):
    cli = _cli()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "billing_target_shadow",
            "approve-renewal-term-record",
            "--request",
            str(uuid4()),
            "--amount",
            "17500.00",
            "--approver",
            "operator:billing_target_shadow",
            "--idempotency-key",
            "k",
        ],
    )
    with pytest.raises(SystemExit) as captured:
        cli.main()
    assert captured.value.code == 2


def test_cli_permission_resolver_uses_real_role_grants(db_session):
    from app.models.rbac import Permission, Role, RolePermission, SystemUserRole

    cli = _cli()
    granted = _staff(db_session, "Grant")
    role = Role(name="finance_manager_test", is_active=True)
    permission = Permission(key=RENEWAL_TERM_RECORD_PERMISSION, is_active=True)
    db_session.add_all([role, permission])
    db_session.flush()
    db_session.add(RolePermission(role_id=role.id, permission_id=permission.id))
    db_session.add(SystemUserRole(system_user_id=granted.id, role_id=role.id))
    db_session.flush()
    ungranted = _staff(db_session, "Plain")

    assert cli._renewal_term_record_permission_granted(
        db_session, system_user_id=granted.id
    )
    assert not cli._renewal_term_record_permission_granted(
        db_session, system_user_id=ungranted.id
    )
    assert not cli._renewal_term_record_permission_granted(
        db_session, system_user_id=uuid4()
    )


# --- governed sole-approver exception ---------------------------------------


def _refused_self_approval(db_session, subscription, *, configure, justification):
    _block(db_session, subscription)
    requester = _staff(db_session, "Michael")
    other = _staff(db_session, "Other")
    requested = _request(db_session, subscription, requester)
    configure(requester, other)
    with pytest.raises(PrepaidRenewalTermsBackfillError) as captured:
        _approve(
            db_session,
            requested.request_id,
            requester,
            sole_justification=justification,
        )
    assert _code(captured) == "self_approval_forbidden"
    db_session.rollback()
    db_session.refresh(subscription)
    assert subscription.unit_price is None
    assert (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == "approval.sole_approver_exception_used")
        .count()
        == 0
    )


@pytest.mark.parametrize(
    "case", ["disabled", "expired", "wrong_principal", "missing_justification"]
)
def test_sole_approver_exception_refusals_leave_self_approval_forbidden(
    db_session, subscription, case
):
    def configure(requester, other):
        configure_sole_approver_exception(
            db_session,
            enabled=case != "disabled",
            principal=other.id if case == "wrong_principal" else requester.id,
            review_due=(
                future_review_due() - timedelta(days=60)
                if case == "expired"
                else future_review_due()
            ),
        )

    _refused_self_approval(
        db_session,
        subscription,
        configure=configure,
        justification=None if case == "missing_justification" else JUSTIFICATION,
    )


def test_sole_approver_exception_allows_self_approval_with_evidence(
    db_session, subscription
):
    _block(db_session, subscription)
    requester = _staff(db_session, "Michael")
    requested = _request(db_session, subscription, requester)
    configure_sole_approver_exception(
        db_session, principal=requester.id, review_due=future_review_due()
    )

    recorded = _approve(
        db_session,
        requested.request_id,
        requester,
        sole_justification=JUSTIFICATION,
    )

    assert recorded.status is RenewalTermRecordStatus.recorded
    db_session.refresh(subscription)
    assert subscription.unit_price == Decimal("17500.00")
    event = (
        db_session.query(EventStore)
        .filter(EventStore.event_type == "prepaid_renewal_terms.recorded")
        .one()
    )
    assert event.payload["sole_approver_exception"] is True
    assert event.payload["sole_approver_exception_decision_ref"] == DECISION_REF
    assert event.payload["sole_approver_exception_justification"] == JUSTIFICATION
    audit = (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == "approval.sole_approver_exception_used")
        .one()
    )
    assert audit.entity_id == str(subscription.id)
    assert audit.metadata_["sole_approver_exception_decision_ref"] == DECISION_REF


def test_distinct_approval_records_no_exception(db_session, subscription):
    _block(db_session, subscription)
    requested = _request(db_session, subscription, _staff(db_session, "Ada"))
    _approve(db_session, requested.request_id, _staff(db_session, "Bola"))
    event = (
        db_session.query(EventStore)
        .filter(EventStore.event_type == "prepaid_renewal_terms.recorded")
        .one()
    )
    assert event.payload["sole_approver_exception"] is False
    assert "sole_approver_exception_decision_ref" not in event.payload
