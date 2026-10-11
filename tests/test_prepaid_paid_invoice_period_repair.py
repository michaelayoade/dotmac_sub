"""Finance-reviewed repair of a paid prepaid invoice's malformed service period.

A paid invoice with no valid period quarantines its prepaid subscription
(``malformed_paid_invoice_period``). The repair is four-eyes: one staff member
requests a fingerprint-bound proposal, a different one approves it. Approval
rechecks under lock, writes the period through the invoice owner, creates
coverage only through the paid-line entitlement writer, and records audit and
events. The next quarantine sweep then closes the finance work item.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from app.models.admin_alert import AdminAlert
from app.models.audit import AuditEvent
from app.models.billing import (
    Invoice,
    InvoiceLine,
    InvoiceStatus,
    Payment,
    PaymentAllocation,
    PaymentStatus,
    ServiceEntitlement,
    ServiceEntitlementStatus,
)
from app.models.catalog import BillingCycle, BillingMode, SubscriptionStatus
from app.models.event_store import EventStore
from app.models.subscriber import SubscriberStatus
from app.models.system_user import SystemUser
from app.services.collections import scheduled
from app.services.collections.scheduled import repair_prepaid_coverage_evidence
from app.services.owner_commands import CommandContext
from app.services.prepaid_coverage_quarantine_review import (
    PrepaidCoverageQuarantineReviewQuery,
    review_prepaid_coverage_quarantine,
)
from app.services.prepaid_coverage_reconciliation import (
    CoverageReconciliationReason,
    preview_prepaid_coverage_reconciliation,
    resolve_prepaid_coverage_enforcement_blockers,
)
from app.services.prepaid_paid_invoice_period_repair import (
    QUARANTINE_FINDING_PREFIX,
    REPAIR_PERMISSION,
    ApprovePaidInvoicePeriodRepairCommand,
    EntitlementDisposition,
    PaidInvoicePeriodRepairBlocker,
    PaidInvoicePeriodRepairError,
    PaidInvoicePeriodRepairQuery,
    PaidInvoicePeriodRepairStatus,
    PaidInvoicePeriodRepairWarning,
    RequestPaidInvoicePeriodRepairCommand,
    approve_paid_invoice_period_repair,
    list_paid_invoice_period_repair_requests,
    preview_paid_invoice_period_repair,
    request_paid_invoice_period_repair,
)
from tests.sole_approver_support import (
    DECISION_REF,
    JUSTIFICATION,
    configure_sole_approver_exception,
    future_review_due,
)

NOW = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)
PAST_START = datetime(2026, 4, 22, 12, 0, tzinfo=UTC)
PAST_END = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)
PRICE = Decimal("17500.00")
SHA = "b" * 64


def _staff(db, name: str) -> SystemUser:
    user = SystemUser(
        id=uuid4(),
        first_name=name,
        last_name="Finance",
        display_name=f"{name} Finance",
        email=f"{name.lower()}-{uuid4().hex}@example.test",
        is_active=True,
    )
    db.add(user)
    db.commit()
    return user


def _prepare(db, account, subscription) -> None:
    account.billing_mode = BillingMode.prepaid
    account.status = SubscriberStatus.active
    account.is_active = True
    account.billing_enabled = True
    subscription.billing_mode = BillingMode.prepaid
    subscription.status = SubscriptionStatus.active
    subscription.billing_cycle = BillingCycle.monthly
    subscription.unit_price = PRICE
    subscription.next_billing_at = NOW - timedelta(days=5)
    db.commit()


def _paid_invoice(
    db,
    account,
    subscription,
    *,
    amount: Decimal = PRICE,
    start: datetime | None = None,
    end: datetime | None = None,
    allocated: Decimal | None = None,
    kind: str | None = None,
    link: bool = True,
) -> tuple[Invoice, InvoiceLine]:
    invoice = Invoice(
        account_id=account.id,
        invoice_number=f"INV-PERIOD-{uuid4().hex[:8]}",
        status=InvoiceStatus.paid,
        currency="NGN",
        subtotal=amount,
        total=amount,
        balance_due=Decimal("0.00"),
        billing_period_start=start,
        billing_period_end=end,
        issued_at=PAST_START,
        paid_at=PAST_START,
    )
    payment = Payment(
        account_id=account.id,
        amount=allocated if allocated is not None else amount,
        currency="NGN",
        status=PaymentStatus.succeeded,
    )
    db.add_all([invoice, payment])
    db.flush()
    line = InvoiceLine(
        invoice_id=invoice.id,
        subscription_id=subscription.id if link else None,
        description="Unlimited Basic — monthly service",
        quantity=Decimal("1.000"),
        unit_price=amount,
        amount=amount,
        metadata_={"kind": kind} if kind else None,
    )
    db.add(line)
    db.add(
        PaymentAllocation(
            payment_id=payment.id,
            invoice_id=invoice.id,
            amount=allocated if allocated is not None else amount,
        )
    )
    db.commit()
    return invoice, line


def _query(invoice, line, subscription, **overrides) -> PaidInvoicePeriodRepairQuery:
    values = {
        "invoice_id": invoice.id,
        "line_id": line.id,
        "subscription_id": subscription.id,
        "period_start": PAST_START,
        "period_end": PAST_END,
    }
    values.update(overrides)
    return PaidInvoicePeriodRepairQuery(**values)


def _context(user: SystemUser, key: str, *, scope: str = REPAIR_PERMISSION):
    return CommandContext.system(
        actor=f"user:{user.id}",
        scope=scope,
        reason="finance-reviewed paid invoice period repair test",
        idempotency_key=key,
    )


def _request(db, query, requester, *, fingerprint=None, key=None, granted=True):
    if fingerprint is None:
        fingerprint = preview_paid_invoice_period_repair(
            db, query, as_of=NOW
        ).fingerprint
    requester_id = requester.id
    context = _context(requester, key or f"request-{uuid4()}")
    db.commit()  # adapters hand owners a transaction-free session
    return request_paid_invoice_period_repair(
        db,
        RequestPaidInvoicePeriodRepairCommand(
            query=query,
            preview_fingerprint=fingerprint,
            reason="Splynx invoice 1234 and receipt show the April cycle was bought",
            evidence_reference="finance-ticket-FIN-42",
            evidence_sha256=SHA,
            requested_by=requester_id,
            permission_granted=granted,
        ),
        context=context,
    )


def _approve(
    db, request_id, fingerprint, approver, *, key=None, sole_justification=None
):
    approver_id = approver.id
    context = _context(approver, key or f"approve-{uuid4()}")
    db.commit()  # adapters hand owners a transaction-free session
    return approve_paid_invoice_period_repair(
        db,
        ApprovePaidInvoicePeriodRepairCommand(
            request_id=request_id,
            preview_fingerprint=fingerprint,
            approved_by=approver_id,
            permission_granted=True,
            sole_approver_justification=sole_justification,
        ),
        context=context,
    )


def _open_work_item(db) -> None:
    repair_prepaid_coverage_evidence(db, now=NOW)
    db.commit()


def _work_item(db, account) -> AdminAlert:
    return (
        db.query(AdminAlert)
        .filter(AdminAlert.fingerprint == f"{QUARANTINE_FINDING_PREFIX}{account.id}")
        .one()
    )


def test_prefix_matches_the_scheduled_work_item_contract():
    assert QUARANTINE_FINDING_PREFIX == (
        scheduled.PREPAID_COVERAGE_QUARANTINE_FINDING_PREFIX
    )


def test_preview_shows_before_after_entitlement_and_quarantine_effect(
    db_session, subscriber_account, subscription
):
    _prepare(db_session, subscriber_account, subscription)
    invoice, line = _paid_invoice(
        db_session, subscriber_account, subscription, kind="base_subscription"
    )
    _open_work_item(db_session)

    preview = preview_paid_invoice_period_repair(
        db_session, _query(invoice, line, subscription), as_of=NOW
    )

    assert preview.actionable, preview.blockers
    assert preview.warnings == ()
    assert preview.before.billing_period_start is None
    assert preview.before.billing_period_end is None
    assert preview.after.billing_period_start == PAST_START
    assert preview.after.billing_period_end == PAST_END
    planned = preview.planned_entitlement
    assert planned.disposition is EntitlementDisposition.create_from_paid_line
    assert planned.existing_entitlement_id is None
    assert planned.source_invoice_line_id == line.id
    assert (planned.starts_at, planned.ends_at) == (PAST_START, PAST_END)
    assert planned.amount_funded == PRICE
    assert preview.settlement.allocated_payments == PRICE
    effect = preview.quarantine_effect
    assert effect.work_item_open is True
    assert effect.current_blocking_reasons == (
        CoverageReconciliationReason.malformed_paid_invoice_period,
    )
    assert effect.target_reason_before is (
        CoverageReconciliationReason.malformed_paid_invoice_period
    )
    # Historical period, anchor already lapsed: ordinary due, not quarantine.
    assert effect.target_reason_after is (
        CoverageReconciliationReason.due_without_coverage
    )
    assert effect.projected_blocking_reasons == ()
    assert effect.work_item_resolves_on_next_sweep is True
    # The preview is deterministic and writes nothing.
    again = preview_paid_invoice_period_repair(
        db_session, _query(invoice, line, subscription), as_of=NOW
    )
    assert again.fingerprint == preview.fingerprint
    db_session.refresh(invoice)
    assert invoice.billing_period_start is None
    assert db_session.query(ServiceEntitlement).count() == 0


def test_four_eyes_repair_applies_and_next_sweep_clears_the_quarantine(
    db_session, subscriber_account, subscription
):
    _prepare(db_session, subscriber_account, subscription)
    invoice, line = _paid_invoice(
        db_session, subscriber_account, subscription, kind="base_subscription"
    )
    _open_work_item(db_session)
    requester = _staff(db_session, "Requester")
    approver = _staff(db_session, "Approver")
    query = _query(invoice, line, subscription)

    requested = _request(db_session, query, requester)

    assert requested.status is PaidInvoicePeriodRepairStatus.requested
    db_session.refresh(invoice)
    assert invoice.billing_period_start is None  # a request changes nothing
    (pending,) = list_paid_invoice_period_repair_requests(db_session)
    assert pending.request_id == requested.request_id

    with pytest.raises(PaidInvoicePeriodRepairError) as self_approval:
        _approve(
            db_session, requested.request_id, requested.preview_fingerprint, requester
        )
    assert self_approval.value.code.endswith("self_approval_forbidden")
    with pytest.raises(PaidInvoicePeriodRepairError) as wrong_fingerprint:
        _approve(db_session, requested.request_id, "0" * 64, approver)
    assert wrong_fingerprint.value.code.endswith("approval_fingerprint_mismatch")

    applied = _approve(
        db_session, requested.request_id, requested.preview_fingerprint, approver
    )

    assert applied.status is PaidInvoicePeriodRepairStatus.applied
    assert applied.replayed is False
    assert applied.projected_blocking_reasons == ()
    db_session.expire_all()
    invoice = db_session.get(Invoice, invoice.id)
    line = db_session.get(InvoiceLine, line.id)
    assert invoice.billing_period_start.replace(tzinfo=UTC) == PAST_START
    assert invoice.billing_period_end.replace(tzinfo=UTC) == PAST_END
    assert invoice.status is InvoiceStatus.paid
    assert invoice.total == PRICE
    assert line.metadata_["kind"] == "base_subscription"
    assert line.metadata_["billing_period_source"] == "finance_reviewed_period_repair"
    (entitlement,) = db_session.query(ServiceEntitlement).all()
    assert entitlement.id == applied.entitlement_id
    assert entitlement.source_invoice_line_id == line.id
    assert entitlement.amount_funded == PRICE
    assert entitlement.metadata_["reconciled_by"] == (
        "financial.prepaid_paid_invoice_period_repair"
    )
    audit = (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == "repair_paid_prepaid_invoice_period")
        .one()
    )
    assert audit.entity_id == str(invoice.id)
    assert audit.actor_id == str(approver.id)
    assert audit.metadata_["requested_by_system_user_id"] == str(requester.id)
    assert audit.metadata_["economic_delta"] == "0.00"
    events = {
        row.event_type
        for row in db_session.query(EventStore).filter(
            EventStore.invoice_id == invoice.id
        )
    }
    assert {
        "prepaid_paid_invoice_period_repair.requested",
        "prepaid_paid_invoice_period.repaired",
    } <= events
    assert list_paid_invoice_period_repair_requests(db_session) == ()

    # Every path that computes malformed_paid_invoice_period now agrees.
    coverage = preview_prepaid_coverage_reconciliation(
        db_session, as_of=NOW, subscription_ids=(subscription.id,)
    )
    assert coverage.items[0].reason is CoverageReconciliationReason.due_without_coverage
    assert (
        resolve_prepaid_coverage_enforcement_blockers(
            db_session, [subscription], as_of=NOW
        )
        == ()
    )
    review = review_prepaid_coverage_quarantine(
        db_session,
        PrepaidCoverageQuarantineReviewQuery(
            account_ids=(subscriber_account.id,), as_of=NOW
        ),
    )
    assert review.accounts[0].invoice_findings == ()
    _open_work_item(db_session)
    assert _work_item(db_session, subscriber_account).status.value == "resolved"

    # Replays: the same approver gets the stored outcome; nothing is rewritten.
    replay = _approve(
        db_session, requested.request_id, requested.preview_fingerprint, approver
    )
    assert replay.replayed is True
    assert replay.entitlement_id == entitlement.id
    assert db_session.query(ServiceEntitlement).count() == 1
    other = _staff(db_session, "Other")
    with pytest.raises(PaidInvoicePeriodRepairError) as decided:
        _approve(db_session, requested.request_id, requested.preview_fingerprint, other)
    assert decided.value.code.endswith("request_already_decided")


def test_current_period_repair_covers_service(
    db_session, subscriber_account, subscription
):
    _prepare(db_session, subscriber_account, subscription)
    invoice, line = _paid_invoice(
        db_session, subscriber_account, subscription, kind="base_subscription"
    )
    start = datetime(2026, 7, 10, 12, 0, tzinfo=UTC)
    end = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
    query = _query(invoice, line, subscription, period_start=start, period_end=end)

    preview = preview_paid_invoice_period_repair(db_session, query, as_of=NOW)

    assert preview.actionable, preview.blockers
    assert preview.quarantine_effect.target_reason_after is (
        CoverageReconciliationReason.funded_entitlement
    )
    assert preview.quarantine_effect.projected_blocking_reasons == ()


def test_request_requires_permission_scope_reason_and_evidence(
    db_session, subscriber_account, subscription
):
    _prepare(db_session, subscriber_account, subscription)
    invoice, line = _paid_invoice(
        db_session, subscriber_account, subscription, kind="base_subscription"
    )
    requester = _staff(db_session, "Requester")
    query = _query(invoice, line, subscription)

    with pytest.raises(PaidInvoicePeriodRepairError) as denied:
        _request(db_session, query, requester, granted=False)
    assert denied.value.code.endswith("permission_denied")

    fingerprint = preview_paid_invoice_period_repair(
        db_session, query, as_of=NOW
    ).fingerprint
    requester_id = requester.id
    context = _context(requester, "weak-reason")
    db_session.commit()
    with pytest.raises(PaidInvoicePeriodRepairError) as weak:
        request_paid_invoice_period_repair(
            db_session,
            RequestPaidInvoicePeriodRepairCommand(
                query=query,
                preview_fingerprint=fingerprint,
                reason="looks fine",
                evidence_reference="ticket",
                evidence_sha256=SHA,
                requested_by=requester_id,
                permission_granted=True,
            ),
            context=context,
        )
    assert weak.value.code.endswith("invalid_reason")
    with pytest.raises(PaidInvoicePeriodRepairError) as stale:
        _request(db_session, query, requester, fingerprint="f" * 64)
    assert stale.value.code.endswith("stale_preview")


def test_request_replays_and_rejects_a_reused_key(
    db_session, subscriber_account, subscription
):
    _prepare(db_session, subscriber_account, subscription)
    invoice, line = _paid_invoice(
        db_session, subscriber_account, subscription, kind="base_subscription"
    )
    requester = _staff(db_session, "Requester")
    query = _query(invoice, line, subscription)
    first = _request(db_session, query, requester, key="same-key")

    replay = _request(db_session, query, requester, key="same-key")
    assert replay.replayed is True
    assert replay.request_id == first.request_id

    shifted = _query(
        invoice,
        line,
        subscription,
        period_start=PAST_START + timedelta(days=1),
        period_end=PAST_END + timedelta(days=1),
        acknowledged_warnings=(),
    )
    with pytest.raises(PaidInvoicePeriodRepairError) as conflict:
        _request(db_session, shifted, requester, key="same-key", fingerprint="a" * 64)
    assert conflict.value.code.endswith("idempotency_conflict")


def test_evidence_change_after_request_makes_approval_stale(
    db_session, subscriber_account, subscription
):
    _prepare(db_session, subscriber_account, subscription)
    invoice, line = _paid_invoice(
        db_session, subscriber_account, subscription, kind="base_subscription"
    )
    requester = _staff(db_session, "Requester")
    approver = _staff(db_session, "Approver")
    requested = _request(db_session, _query(invoice, line, subscription), requester)

    db_session.add(
        ServiceEntitlement(
            account_id=subscriber_account.id,
            subscription_id=subscription.id,
            starts_at=PAST_START + timedelta(days=3),
            ends_at=PAST_END + timedelta(days=3),
            amount_funded=PRICE,
            currency="NGN",
            status=ServiceEntitlementStatus.active,
        )
    )
    db_session.commit()

    with pytest.raises(PaidInvoicePeriodRepairError) as stale:
        _approve(
            db_session, requested.request_id, requested.preview_fingerprint, approver
        )
    assert stale.value.code.endswith("stale_preview")
    db_session.refresh(invoice)
    assert invoice.billing_period_start is None


@pytest.mark.parametrize(
    ("mutate", "blocker"),
    [
        (
            lambda db, invoice, line: setattr(invoice, "status", InvoiceStatus.issued),
            PaidInvoicePeriodRepairBlocker.invoice_not_paid,
        ),
        (
            lambda db, invoice, line: (
                setattr(invoice, "billing_period_start", PAST_START),
                setattr(invoice, "billing_period_end", PAST_END),
            ),
            PaidInvoicePeriodRepairBlocker.invoice_period_not_malformed,
        ),
        (
            lambda db, invoice, line: setattr(invoice, "currency", "USD"),
            PaidInvoicePeriodRepairBlocker.currency_mismatch,
        ),
    ],
)
def test_invoice_facts_block_the_repair(
    db_session, subscriber_account, subscription, mutate, blocker
):
    _prepare(db_session, subscriber_account, subscription)
    invoice, line = _paid_invoice(
        db_session, subscriber_account, subscription, kind="base_subscription"
    )
    mutate(db_session, invoice, line)
    db_session.commit()

    preview = preview_paid_invoice_period_repair(
        db_session, _query(invoice, line, subscription), as_of=NOW
    )

    assert blocker in preview.blockers
    assert preview.actionable is False


def test_over_allocated_invoice_is_blocked(
    db_session, subscriber_account, subscription
):
    _prepare(db_session, subscriber_account, subscription)
    invoice, line = _paid_invoice(
        db_session,
        subscriber_account,
        subscription,
        kind="base_subscription",
        allocated=Decimal("36312.50"),
    )

    preview = preview_paid_invoice_period_repair(
        db_session, _query(invoice, line, subscription), as_of=NOW
    )

    assert PaidInvoicePeriodRepairBlocker.settlement_does_not_match_total in (
        preview.blockers
    )


def test_account_mismatch_and_other_subscription_link_block(
    db_session, subscriber_account, subscription, subscriber
):
    _prepare(db_session, subscriber_account, subscription)
    invoice, line = _paid_invoice(
        db_session, subscriber_account, subscription, kind="base_subscription"
    )
    other_line = InvoiceLine(
        invoice_id=invoice.id,
        subscription_id=subscription.id,
        description="Second linked charge",
        quantity=Decimal("1.000"),
        unit_price=Decimal("10.00"),
        amount=Decimal("10.00"),
    )
    db_session.add(other_line)
    db_session.commit()

    preview = preview_paid_invoice_period_repair(
        db_session, _query(invoice, line, subscription), as_of=NOW
    )

    assert PaidInvoicePeriodRepairBlocker.invoice_has_other_subscription_lines in (
        preview.blockers
    )


def test_proration_line_needs_every_warning_acknowledged(
    db_session, subscriber_account, subscription
):
    _prepare(db_session, subscriber_account, subscription)
    invoice, line = _paid_invoice(
        db_session, subscriber_account, subscription, amount=Decimal("8477.75")
    )
    start = PAST_START
    end = PAST_START + timedelta(days=14)
    query = _query(invoice, line, subscription, period_start=start, period_end=end)

    preview = preview_paid_invoice_period_repair(db_session, query, as_of=NOW)

    assert set(preview.warnings) == {
        PaidInvoicePeriodRepairWarning.line_not_base_subscription,
        PaidInvoicePeriodRepairWarning.amount_differs_from_subscription_terms,
        PaidInvoicePeriodRepairWarning.period_not_one_billing_cycle,
    }
    assert preview.blockers == (PaidInvoicePeriodRepairBlocker.unacknowledged_warning,)

    acknowledged = _query(
        invoice,
        line,
        subscription,
        period_start=start,
        period_end=end,
        acknowledged_warnings=preview.warnings,
    )
    ok = preview_paid_invoice_period_repair(db_session, acknowledged, as_of=NOW)
    assert ok.actionable
    assert ok.fingerprint != preview.fingerprint

    over = _query(
        invoice,
        line,
        subscription,
        period_start=start,
        period_end=end,
        acknowledged_warnings=(
            *preview.warnings,
            PaidInvoicePeriodRepairWarning.subscription_terms_unpriced,
        ),
    )
    assert PaidInvoicePeriodRepairBlocker.acknowledged_warning_not_present in (
        preview_paid_invoice_period_repair(db_session, over, as_of=NOW).blockers
    )


def test_overlapping_entitlement_blocks_until_explicitly_retained(
    db_session, subscriber_account, subscription
):
    _prepare(db_session, subscriber_account, subscription)
    invoice, line = _paid_invoice(
        db_session, subscriber_account, subscription, kind="base_subscription"
    )
    existing = ServiceEntitlement(
        account_id=subscriber_account.id,
        subscription_id=subscription.id,
        starts_at=PAST_START - timedelta(days=10),
        ends_at=PAST_START + timedelta(days=10),
        amount_funded=PRICE,
        currency="NGN",
        status=ServiceEntitlementStatus.active,
    )
    db_session.add(existing)
    db_session.commit()

    blocked = preview_paid_invoice_period_repair(
        db_session, _query(invoice, line, subscription), as_of=NOW
    )
    assert blocked.blockers == (
        PaidInvoicePeriodRepairBlocker.overlapping_entitlement_unresolved,
    )
    assert [row.entitlement_id for row in blocked.overlapping_entitlements] == [
        existing.id
    ]

    retained = preview_paid_invoice_period_repair(
        db_session,
        _query(
            invoice,
            line,
            subscription,
            acknowledged_overlapping_entitlement_ids=(existing.id,),
        ),
        as_of=NOW,
    )
    assert retained.actionable
    assert retained.overlapping_entitlements[0].acknowledged is True

    stray = preview_paid_invoice_period_repair(
        db_session,
        _query(
            invoice,
            line,
            subscription,
            acknowledged_overlapping_entitlement_ids=(existing.id, uuid4()),
        ),
        as_of=NOW,
    )
    assert PaidInvoicePeriodRepairBlocker.acknowledged_overlap_not_found in (
        stray.blockers
    )


def test_existing_entitlement_that_funds_the_payment_is_adopted_not_duplicated(
    db_session, subscriber_account, subscription
):
    _prepare(db_session, subscriber_account, subscription)
    invoice, line = _paid_invoice(
        db_session, subscriber_account, subscription, kind="base_subscription"
    )
    funded = ServiceEntitlement(
        account_id=subscriber_account.id,
        subscription_id=subscription.id,
        starts_at=PAST_START,
        ends_at=PAST_END,
        amount_funded=Decimal("18812.50"),
        currency="NGN",
        status=ServiceEntitlementStatus.active,
        metadata_={
            "source": "reviewed_succeeded_payment_reconciliation",
            "paid_invoice_id": str(invoice.id),
        },
    )
    db_session.add(funded)
    db_session.commit()
    requester = _staff(db_session, "Requester")
    approver = _staff(db_session, "Approver")

    plain = preview_paid_invoice_period_repair(
        db_session, _query(invoice, line, subscription), as_of=NOW
    )
    assert PaidInvoicePeriodRepairBlocker.overlapping_entitlement_unresolved in (
        plain.blockers
    )

    query = _query(
        invoice,
        line,
        subscription,
        disposition=EntitlementDisposition.existing_entitlement_funds_line,
        adopted_entitlement_id=funded.id,
        acknowledged_warnings=(
            PaidInvoicePeriodRepairWarning.adopted_entitlement_amount_differs,
        ),
    )
    preview = preview_paid_invoice_period_repair(db_session, query, as_of=NOW)
    assert preview.actionable, preview.blockers
    assert preview.planned_entitlement.existing_entitlement_id == funded.id
    db_session.commit()

    requested = _request(db_session, query, requester)
    applied = _approve(
        db_session, requested.request_id, requested.preview_fingerprint, approver
    )

    assert applied.disposition is EntitlementDisposition.existing_entitlement_funds_line
    assert applied.entitlement_id == funded.id
    assert db_session.query(ServiceEntitlement).count() == 1
    db_session.expire_all()
    assert db_session.get(Invoice, invoice.id).billing_period_start is not None


def test_adoption_requires_structured_link_and_exact_period(
    db_session, subscriber_account, subscription
):
    _prepare(db_session, subscriber_account, subscription)
    invoice, line = _paid_invoice(
        db_session, subscriber_account, subscription, kind="base_subscription"
    )
    unlinked = ServiceEntitlement(
        account_id=subscriber_account.id,
        subscription_id=subscription.id,
        starts_at=PAST_START,
        ends_at=PAST_END + timedelta(days=1),
        amount_funded=PRICE,
        currency="NGN",
        status=ServiceEntitlementStatus.active,
        metadata_={"memo": f"paid by {invoice.invoice_number}"},
    )
    db_session.add(unlinked)
    db_session.commit()

    preview = preview_paid_invoice_period_repair(
        db_session,
        _query(
            invoice,
            line,
            subscription,
            disposition=EntitlementDisposition.existing_entitlement_funds_line,
            adopted_entitlement_id=unlinked.id,
        ),
        as_of=NOW,
    )

    # Memo text is never proof, and the period must match exactly.
    assert {
        PaidInvoicePeriodRepairBlocker.adopted_entitlement_not_linked_to_invoice,
        PaidInvoicePeriodRepairBlocker.adopted_entitlement_period_mismatch,
    } <= set(preview.blockers)


def test_unlinked_line_is_linked_to_exactly_the_reviewed_subscription(
    db_session, subscriber_account, subscription
):
    _prepare(db_session, subscriber_account, subscription)
    invoice, line = _paid_invoice(
        db_session,
        subscriber_account,
        subscription,
        kind="base_subscription",
        link=False,
    )
    requester = _staff(db_session, "Requester")
    approver = _staff(db_session, "Approver")

    requested = _request(db_session, _query(invoice, line, subscription), requester)
    _approve(db_session, requested.request_id, requested.preview_fingerprint, approver)

    db_session.expire_all()
    assert db_session.get(InvoiceLine, line.id).subscription_id == subscription.id


def test_invalid_period_input_is_refused(db_session, subscriber_account, subscription):
    _prepare(db_session, subscriber_account, subscription)
    invoice, line = _paid_invoice(db_session, subscriber_account, subscription)

    with pytest.raises(PaidInvoicePeriodRepairError) as inverted:
        preview_paid_invoice_period_repair(
            db_session,
            _query(
                invoice,
                line,
                subscription,
                period_start=PAST_END,
                period_end=PAST_START,
            ),
        )
    assert inverted.value.code.endswith("invalid_period")
    with pytest.raises(PaidInvoicePeriodRepairError) as naive:
        preview_paid_invoice_period_repair(
            db_session,
            _query(
                invoice,
                line,
                subscription,
                period_start=PAST_START.replace(tzinfo=None),
            ),
        )
    assert naive.value.code.endswith("invalid_period")


def test_cli_preview_request_approve_and_refusal(
    db_session, subscriber_account, subscription, monkeypatch, capsys
):
    import json
    from contextlib import contextmanager

    from scripts.billing import repair_prepaid_paid_invoice_period as cli

    _prepare(db_session, subscriber_account, subscription)
    invoice, line = _paid_invoice(
        db_session, subscriber_account, subscription, kind="base_subscription"
    )
    requester = _staff(db_session, "Requester")
    approver = _staff(db_session, "Approver")
    requester_id, approver_id = requester.id, approver.id
    db_session.commit()

    @contextmanager
    def _session():
        yield db_session

    monkeypatch.setattr(cli.db_session_adapter, "read_session", _session)
    monkeypatch.setattr(cli.db_session_adapter, "owner_command_session", _session)
    monkeypatch.setattr(cli, "_permission_granted", lambda db, system_user_id: True)
    proposal = [
        "--invoice-id",
        str(invoice.id),
        "--line-id",
        str(line.id),
        "--subscription-id",
        str(subscription.id),
        "--period-start",
        PAST_START.isoformat(),
        "--period-end",
        PAST_END.isoformat(),
    ]

    assert cli.main(["preview", *proposal]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["financial_state_changed"] is False
    assert preview["actionable"] is True
    assert preview["planned_entitlement"]["disposition"] == "create_from_paid_line"
    db_session.commit()

    request_args = [
        "request",
        *proposal,
        "--fingerprint",
        preview["fingerprint"],
        "--reason",
        "Splynx invoice and receipt prove the April 2026 cycle",
        "--evidence-ref",
        "FIN-42",
        "--evidence-sha256",
        SHA,
        "--actor",
        str(requester_id),
        "--idempotency-key",
        "cli-request",
    ]
    assert cli.main(request_args) == 0
    requested = json.loads(capsys.readouterr().out)
    assert requested["status"] == "requested"

    approve_args = [
        "approve",
        "--request",
        requested["request_id"],
        "--fingerprint",
        preview["fingerprint"],
        "--idempotency-key",
        "cli-approve",
    ]
    db_session.commit()
    assert cli.main([*approve_args, "--approver", str(requester_id)]) == 3
    refused = json.loads(capsys.readouterr().out)
    assert refused["error"].endswith("self_approval_forbidden")
    assert refused["financial_state_changed"] is False

    db_session.commit()
    assert cli.main([*approve_args, "--approver", str(approver_id)]) == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied["status"] == "applied"
    assert applied["entitlement_id"]

    db_session.commit()
    assert cli.main(["preview", *proposal]) == 2
    blocked = json.loads(capsys.readouterr().out)
    assert "invoice_period_not_malformed" in blocked["blockers"]


# --- governed sole-approver exception ---------------------------------------


def _requested_by_one_staff(db_session, subscriber_account, subscription):
    _prepare(db_session, subscriber_account, subscription)
    invoice, line = _paid_invoice(
        db_session, subscriber_account, subscription, kind="base_subscription"
    )
    _open_work_item(db_session)
    requester = _staff(db_session, "Michael")
    other = _staff(db_session, "Other")
    requested = _request(db_session, _query(invoice, line, subscription), requester)
    return invoice, requester, other, requested


@pytest.mark.parametrize(
    "case", ["disabled", "expired", "wrong_principal", "missing_justification"]
)
def test_sole_approver_exception_refusals_leave_self_approval_forbidden(
    db_session, subscriber_account, subscription, case
):
    invoice, requester, other, requested = _requested_by_one_staff(
        db_session, subscriber_account, subscription
    )
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

    with pytest.raises(PaidInvoicePeriodRepairError) as refused:
        _approve(
            db_session,
            requested.request_id,
            requested.preview_fingerprint,
            requester,
            sole_justification=None
            if case == "missing_justification"
            else JUSTIFICATION,
        )

    assert refused.value.code.endswith("self_approval_forbidden")
    db_session.rollback()
    db_session.refresh(invoice)
    assert invoice.billing_period_start is None
    assert (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == "approval.sole_approver_exception_used")
        .count()
        == 0
    )


def test_sole_approver_exception_allows_self_approval_with_evidence(
    db_session, subscriber_account, subscription
):
    invoice, requester, _other, requested = _requested_by_one_staff(
        db_session, subscriber_account, subscription
    )
    configure_sole_approver_exception(
        db_session, principal=requester.id, review_due=future_review_due()
    )

    applied = _approve(
        db_session,
        requested.request_id,
        requested.preview_fingerprint,
        requester,
        sole_justification=JUSTIFICATION,
    )

    assert applied.status is PaidInvoicePeriodRepairStatus.applied
    db_session.expire_all()
    event = (
        db_session.query(EventStore)
        .filter(EventStore.event_type == "prepaid_paid_invoice_period.repaired")
        .one()
    )
    assert event.payload["sole_approver_exception"] is True
    assert event.payload["sole_approver_exception_decision_ref"] == DECISION_REF
    assert event.payload["sole_approver_exception_justification"] == JUSTIFICATION
    repair_audit = (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == "repair_paid_prepaid_invoice_period")
        .one()
    )
    assert repair_audit.metadata_["sole_approver_exception"] is True
    used = (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == "approval.sole_approver_exception_used")
        .one()
    )
    assert used.entity_id == str(invoice.id)
    assert used.actor_id == str(requester.id)
    assert used.metadata_["sole_approver_exception_decision_ref"] == DECISION_REF
