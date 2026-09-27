from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from app.models.audit import AuditEvent
from app.models.billing import (
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
    Payment,
    PaymentSettlement,
    PaymentSettlementOrigin,
    PaymentStatus,
)
from app.models.billing_shadow_verification import BillingCutoverVerificationRun
from app.models.catalog import BillingMode, SubscriptionStatus
from app.models.customer_subledger import (
    CustomerPostingGroup,
    CustomerSubledgerAuthorityCutover,
    CustomerSubledgerOpeningPosition,
    NativePrepaidOpeningRepair,
)
from app.models.event_store import EventStore
from app.models.prepaid_funding import (
    PrepaidFundingBaseline,
    PrepaidFundingReconstructionBatch,
)
from app.models.rbac import Permission, SystemUserPermission
from app.models.splynx_transaction import SplynxBillingTransaction
from app.models.subscriber import SubscriberStatus
from app.models.system_user import SystemUser
from app.services.billing.subledger_opening import (
    NATIVE_REPAIR_SCOPE,
    CustomerSubledgerOpeningError,
    NativePrepaidOpeningApproval,
    PreviewNativePrepaidOpeningRepairQuery,
    RepairNativePrepaidOpeningCommand,
    preview_native_prepaid_opening_repair,
    repair_native_prepaid_opening,
)
from app.services.events.types import EventType
from app.services.owner_commands import CommandContext
from app.services.prepaid_funding_reconstruction import (
    LEGACY_FINANCIAL_HANDOFF_AT,
    verified_prepaid_funding_balance,
)
from tests.prepaid_funding_helpers import ensure_test_prepaid_contract

FUNDING_CUTOVER = datetime(2026, 7, 20, 7, 58, 22, tzinfo=UTC)
SUBLEDGER_CUTOVER = datetime(2026, 8, 2, 20, 15, 25, tzinfo=UTC)


def _verification_run() -> BillingCutoverVerificationRun:
    return BillingCutoverVerificationRun(
        phase="phase_3_subledger_parity",
        cohort_name="pytest-native-opening",
        evidence_schema_version=3,
        policy_version="pytest",
        cutoff_at=SUBLEDGER_CUTOVER,
        observation_started_at=SUBLEDGER_CUTOVER - timedelta(hours=1),
        observation_ended_at=SUBLEDGER_CUTOVER,
        cohort_count=1,
        covered_count=1,
        unresolved_count=0,
        ambiguous_count=0,
        unexpected_unlinked_count=0,
        duplicate_count=0,
        shadow_variance_count=0,
        expected_difference_count=0,
        gap_count=0,
        overlap_count=0,
        source_fingerprint="1" * 64,
        result_fingerprint="2" * 64,
        currency_totals={},
        cohort_classification={},
        event_outcomes={},
        code_version="pytest",
        database_schema_version="624",
        idempotency_key=f"pytest-authority-{uuid4()}",
        command_id=uuid4(),
        correlation_id=uuid4(),
        actor="pytest",
        reason="pytest authority evidence",
    )


@pytest.fixture()
def native_repair_case(db_session, subscriber, subscription):
    db_session.query(PrepaidFundingReconstructionBatch).filter(
        PrepaidFundingReconstructionBatch.source
        == "pytest-empty-native-install-cutover"
    ).delete(synchronize_session=False)
    subscriber.created_at = LEGACY_FINANCIAL_HANDOFF_AT + timedelta(days=2)
    subscriber.splynx_customer_id = None
    subscriber.billing_mode = BillingMode.prepaid
    subscriber.status = SubscriberStatus.active
    subscriber.is_active = True
    subscriber.billing_enabled = True
    subscriber.min_balance = Decimal("0.00")
    subscription.billing_mode = BillingMode.prepaid
    subscription.status = SubscriptionStatus.active
    ensure_test_prepaid_contract(db_session, subscription)

    cutover = PrepaidFundingReconstructionBatch(
        manifest_sha256="a" * 64,
        manifest_payload_sha256="b" * 64,
        attestation_sha256="c" * 64,
        attestation_key_fingerprint_sha256="d" * 64,
        attestation_signed_at=FUNDING_CUTOVER,
        blocker_manifest_sha256="e" * 64,
        candidate_cohort_sha256="f" * 64,
        source="pytest complete cohort",
        evidence_ref="pytest:funding-cutover",
        position_at=FUNDING_CUTOVER,
        currency="NGN",
        account_count=0,
        total_amount=Decimal("0.00"),
        approved_by="pytest finance",
        is_authority_cutover=True,
        approved_at=FUNDING_CUTOVER,
    )
    run = _verification_run()
    approver = SystemUser(
        first_name="Finance",
        last_name="Approver",
        email=f"finance-native-opening-{uuid4().hex}@example.com",
    )
    operator = SystemUser(
        first_name="Repair",
        last_name="Operator",
        email=f"operator-native-opening-{uuid4().hex}@example.com",
    )
    permission = Permission(
        key=NATIVE_REPAIR_SCOPE,
        description="pytest native opening repair",
        is_active=True,
    )
    db_session.add_all([cutover, run, approver, operator, permission])
    db_session.flush()
    db_session.add_all(
        [
            CustomerSubledgerAuthorityCutover(
                verification_run_id=run.id,
                result_fingerprint=run.result_fingerprint,
                review_reference="pytest:subledger-cutover",
                activated_by="pytest",
                command_id=uuid4(),
                correlation_id=uuid4(),
                cutover_at=SUBLEDGER_CUTOVER,
            ),
            SystemUserPermission(
                system_user_id=operator.id,
                permission_id=permission.id,
            ),
        ]
    )
    opening_payment = Payment(
        account_id=subscriber.id,
        amount=Decimal("17625.00"),
        refunded_amount=Decimal("0.00"),
        currency="NGN",
        status=PaymentStatus.succeeded,
        paid_at=FUNDING_CUTOVER - timedelta(days=1),
        created_at=FUNDING_CUTOVER - timedelta(days=1),
    )
    db_session.add(opening_payment)
    db_session.flush()
    opening_settlement = PaymentSettlement(
        payment_id=opening_payment.id,
        amount=Decimal("17625.00"),
        unallocated_amount=Decimal("17625.00"),
        prepaid_amount=Decimal("0.00"),
        currency="NGN",
        origin=PaymentSettlementOrigin.manual,
        idempotency_key=f"pytest-native-opening-{uuid4()}",
        created_at=FUNDING_CUTOVER - timedelta(days=1),
    )
    later_entry = LedgerEntry(
        account_id=subscriber.id,
        entry_type=LedgerEntryType.credit,
        source=LedgerSource.adjustment,
        amount=Decimal("37625.00"),
        currency="NGN",
        affects_customer_position=True,
        effective_date=FUNDING_CUTOVER + timedelta(days=1),
        created_at=FUNDING_CUTOVER + timedelta(days=1),
    )
    db_session.add_all([opening_settlement, later_entry])
    db_session.commit()
    approval = NativePrepaidOpeningApproval(
        finance_approver_system_user_id=approver.id,
        finance_approver_name="Finance Approver",
        approved_at=datetime(2026, 9, 27, 8, 15, tzinfo=UTC),
        ticket_reference="TICKET-TEST",
        evidence_ref="finance-review:native-opening-test",
        evidence_sha256="9" * 64,
    )
    query = PreviewNativePrepaidOpeningRepairQuery(
        account_id=subscriber.id,
        currency="NGN",
        approval=approval,
    )
    return {
        "account": subscriber,
        "subscription": subscription,
        "cutover": cutover,
        "approver": approver,
        "operator": operator,
        "operator_id": operator.id,
        "permission": permission,
        "opening_payment": opening_payment,
        "opening_settlement": opening_settlement,
        "query": query,
    }


def _command(case, preview, *, key="pytest-native-opening-repair"):
    operator_id = case["operator_id"]
    return RepairNativePrepaidOpeningCommand(
        context=CommandContext.system(
            actor=f"system_user:{operator_id}",
            scope=NATIVE_REPAIR_SCOPE,
            reason="Finance-approved omitted native opening repair",
            idempotency_key=key,
        ),
        query=case["query"],
        expected_preview_fingerprint=preview.fingerprint,
        operator_system_user_id=operator_id,
    )


def test_exact_native_omission_preview_reconstructs_17625(
    db_session, native_repair_case
):
    preview = preview_native_prepaid_opening_repair(
        db_session, native_repair_case["query"]
    )

    assert preview.source_classification == "native_after_handoff"
    assert preview.original_cutover_batch_id == native_repair_case["cutover"].id
    assert preview.original_cutover_at == FUNDING_CUTOVER
    assert preview.calculated_amount == Decimal("17625.00")
    assert preview.splynx_transaction_count == 0
    assert len(preview.native_evidence_fingerprint) == 64
    assert len(preview.cutover_evidence_fingerprint) == 64
    assert len(preview.fingerprint) == 64


def test_apply_and_exact_idempotent_replay(db_session, native_repair_case):
    preview = preview_native_prepaid_opening_repair(
        db_session, native_repair_case["query"]
    )
    db_session.rollback()
    command = _command(native_repair_case, preview)

    result = repair_native_prepaid_opening(db_session, command)
    replay = repair_native_prepaid_opening(db_session, command)

    assert result.replayed is False
    assert replay.replayed is True
    assert replay.repair_id == result.repair_id
    assert db_session.query(NativePrepaidOpeningRepair).count() == 1
    assert db_session.query(CustomerSubledgerOpeningPosition).count() == 1
    assert db_session.query(CustomerPostingGroup).count() == 1
    assert verified_prepaid_funding_balance(
        db_session, native_repair_case["account"].id
    ) == Decimal("55250.00")
    assert (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == "repair_native_prepaid_opening")
        .count()
        == 1
    )
    assert (
        db_session.query(EventStore)
        .filter(
            EventStore.event_type == EventType.native_prepaid_opening_repaired.value
        )
        .count()
        == 1
    )


def test_stale_fingerprint_and_changed_native_evidence_fail_closed(
    db_session, native_repair_case
):
    preview = preview_native_prepaid_opening_repair(
        db_session, native_repair_case["query"]
    )
    native_repair_case["opening_settlement"].amount = Decimal("17624.00")
    db_session.commit()

    with pytest.raises(CustomerSubledgerOpeningError) as exc:
        repair_native_prepaid_opening(db_session, _command(native_repair_case, preview))

    assert exc.value.code.endswith("stale_reviewed_preview")
    assert db_session.query(NativePrepaidOpeningRepair).count() == 0


def test_unreviewed_preview_fingerprint_is_rejected(db_session, native_repair_case):
    preview = preview_native_prepaid_opening_repair(
        db_session, native_repair_case["query"]
    )
    db_session.rollback()
    command = _command(native_repair_case, preview)
    command = RepairNativePrepaidOpeningCommand(
        context=command.context,
        query=command.query,
        expected_preview_fingerprint="0" * 64,
        operator_system_user_id=command.operator_system_user_id,
    )

    with pytest.raises(CustomerSubledgerOpeningError) as exc:
        repair_native_prepaid_opening(db_session, command)

    assert exc.value.code.endswith("stale_reviewed_preview")
    assert db_session.query(NativePrepaidOpeningRepair).count() == 0


@pytest.mark.parametrize("conflict", ["baseline", "opening"])
def test_existing_authority_evidence_is_rejected(
    db_session, native_repair_case, conflict
):
    account = native_repair_case["account"]
    if conflict == "baseline":
        db_session.add(
            PrepaidFundingBaseline(
                batch_id=native_repair_case["cutover"].id,
                account_id=account.id,
                currency="NGN",
                amount=Decimal("17625.00"),
                position_at=FUNDING_CUTOVER,
                is_active=True,
            )
        )
        suffix = "funding_baseline_already_exists"
    else:
        run = db_session.query(BillingCutoverVerificationRun).one()
        db_session.add(
            CustomerSubledgerOpeningPosition(
                verification_run_id=run.id,
                account_id=account.id,
                currency="NGN",
                legacy_position=Decimal("17625.00"),
                shadow_position_before=Decimal("0.00"),
                opening_delta=Decimal("17625.00"),
                evidence_fingerprint="8" * 64,
                review_reference="pytest",
                captured_by="pytest",
                command_id=uuid4(),
                correlation_id=uuid4(),
                occurred_at=FUNDING_CUTOVER,
            )
        )
        suffix = "opening_position_already_captured"
    db_session.commit()

    with pytest.raises(CustomerSubledgerOpeningError) as exc:
        preview_native_prepaid_opening_repair(db_session, native_repair_case["query"])

    assert exc.value.code.endswith(suffix)


@pytest.mark.parametrize("splynx_kind", ["identity", "transaction"])
def test_splynx_evidence_is_rejected(db_session, native_repair_case, splynx_kind):
    account = native_repair_case["account"]
    if splynx_kind == "identity":
        account.splynx_customer_id = 4242
        suffix = "splynx_identity_present"
    else:
        db_session.add(
            SplynxBillingTransaction(
                splynx_transaction_id=4242,
                splynx_customer_id=4242,
                subscriber_id=account.id,
                entry_type="credit",
                amount=Decimal("1.00"),
            )
        )
        suffix = "splynx_transactions_present"
    db_session.commit()

    with pytest.raises(CustomerSubledgerOpeningError) as exc:
        preview_native_prepaid_opening_repair(db_session, native_repair_case["query"])

    assert exc.value.code.endswith(suffix)


@pytest.mark.parametrize(
    ("created_at", "suffix"),
    [
        (
            LEGACY_FINANCIAL_HANDOFF_AT - timedelta(seconds=1),
            "account_not_native_after_handoff",
        ),
        (FUNDING_CUTOVER + timedelta(seconds=1), "account_not_in_original_cutover"),
    ],
)
def test_creation_boundary_is_enforced(
    db_session, native_repair_case, created_at, suffix
):
    native_repair_case["account"].created_at = created_at
    db_session.commit()

    with pytest.raises(CustomerSubledgerOpeningError) as exc:
        preview_native_prepaid_opening_repair(db_session, native_repair_case["query"])

    assert exc.value.code.endswith(suffix)


def test_current_funding_cohort_membership_is_required(db_session, native_repair_case):
    native_repair_case["account"].billing_mode = BillingMode.postpaid
    native_repair_case["subscription"].billing_mode = BillingMode.postpaid
    db_session.commit()

    with pytest.raises(CustomerSubledgerOpeningError) as exc:
        preview_native_prepaid_opening_repair(db_session, native_repair_case["query"])

    assert exc.value.code.endswith("account_not_in_funding_cohort")


def test_finance_approval_and_operator_permission_are_required(
    db_session, native_repair_case
):
    approval = native_repair_case["query"].approval
    invalid = PreviewNativePrepaidOpeningRepairQuery(
        account_id=native_repair_case["account"].id,
        approval=NativePrepaidOpeningApproval(
            finance_approver_system_user_id=approval.finance_approver_system_user_id,
            finance_approver_name=approval.finance_approver_name,
            approved_at=approval.approved_at.replace(tzinfo=None),
            ticket_reference=approval.ticket_reference,
            evidence_ref=approval.evidence_ref,
            evidence_sha256=approval.evidence_sha256.upper(),
        ),
    )
    with pytest.raises(CustomerSubledgerOpeningError) as approval_exc:
        preview_native_prepaid_opening_repair(db_session, invalid)
    assert approval_exc.value.code.endswith("invalid_finance_approval")

    preview = preview_native_prepaid_opening_repair(
        db_session, native_repair_case["query"]
    )
    db_session.query(SystemUserPermission).delete()
    db_session.commit()
    with pytest.raises(CustomerSubledgerOpeningError) as permission_exc:
        repair_native_prepaid_opening(db_session, _command(native_repair_case, preview))
    assert permission_exc.value.code.endswith("permission_denied")


def test_event_failure_rolls_back_repair_audit_and_opening(
    db_session, native_repair_case, monkeypatch
):
    preview = preview_native_prepaid_opening_repair(
        db_session, native_repair_case["query"]
    )
    db_session.rollback()

    def fail_event(*_args, **_kwargs):
        raise RuntimeError("event unavailable")

    monkeypatch.setattr("app.services.billing.subledger_opening.emit_event", fail_event)
    with pytest.raises(RuntimeError, match="event unavailable"):
        repair_native_prepaid_opening(db_session, _command(native_repair_case, preview))

    assert db_session.query(NativePrepaidOpeningRepair).count() == 0
    assert db_session.query(CustomerSubledgerOpeningPosition).count() == 0
    assert db_session.query(CustomerPostingGroup).count() == 0
    assert (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == "repair_native_prepaid_opening")
        .count()
        == 0
    )
