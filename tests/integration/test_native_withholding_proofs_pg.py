"""Native proof acceptance uses real migrated PostgreSQL owners, never gross cash.

Authored for hosted CI; no local test execution is authorized.
"""

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from dotmac_billing.commands import (
    AcceptWithholdingDischarge,
    ReverseWithholdingDischarge,
)
from dotmac_billing.queries import get_withholding_discharge
from dotmac_billing.service import (
    accept_withholding_discharge,
    reverse_withholding_discharge,
)
from dotmac_kernel.cache import TenantScope
from dotmac_payments import GetConfirmedCashV1, get_confirmed_cash_v1
from sqlalchemy import func, select

from app.models.billing import InvoiceStatus, Payment, TopupIntent
from app.models.event_store import EventStore
from app.models.payment_proof import (
    PaymentProof,
    PaymentProofStatus,
    WithholdingTaxRecord,
)
from app.services import direct_transfer_intents, payment_proofs, topup_intents
from app.services.billing import starter_positions
from app.services.billing.starter_withholding_proofs import (
    NativeWithholdingClaim,
    NativeWithholdingReview,
)
from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError
from app.services.events import EventType
from app.services.operator_tenant import OPERATOR_TENANT_ID
from app.services.owner_commands import CommandContext
from tests.integration.test_starter_tax_invoice_pg import (
    _issue,
    _publish_dependency_policy,
    _seller_and_bank,  # noqa: F401
    _starter_series_isolation,  # noqa: F401
)
from tests.test_direct_transfer_intents import _patch_policy

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("_seller_and_bank", "_starter_series_isolation"),
]
_SCOPE = TenantScope(OPERATOR_TENANT_ID)
_ACTOR = str(uuid4())


@pytest.fixture
def engine(cloned_database):
    """Independent committed connections against a real head-migrated clone."""
    from sqlalchemy import create_engine

    target = cloned_database("head")
    engine = create_engine(target)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def db_session(engine):
    from sqlalchemy.orm import Session

    with Session(engine, autoflush=False) as session:
        yield session


def _context(scope):
    return CommandContext.system(
        actor="test:native-proof",
        scope=scope,
        reason="Native withholding proof acceptance",
        idempotency_key=str(uuid4()),
    )


def _prepare(db, subscriber, monkeypatch):
    invoice, link = _issue(db, subscriber)
    invoice_id, subscriber_id = invoice.id, subscriber.id
    db.commit()
    _patch_policy(monkeypatch)
    intent_result = direct_transfer_intents.create_direct_transfer_intent(
        db,
        command=direct_transfer_intents.CreateDirectTransferIntentCommand(
            account_id=subscriber_id,
            invoice_id=invoice_id,
            created_by=str(subscriber_id),
        ),
        context=_context(direct_transfer_intents.CREATE_SCOPE),
    )
    intent = db.get(TopupIntent, intent_result.intent_id)
    intent_id = intent.id
    snapshot = intent.metadata_["withholding_tax"]
    claims = tuple(
        NativeWithholdingClaim(
            component_id=UUID(item["component_id"]),
            claimed_amount=Decimal(item["amount"]),
            accepted_amount=Decimal(item["amount"]),
        )
        for item in snapshot["components"]
    )
    review = NativeWithholdingReview(
        claims=claims, acceptance_evidence_ref="finance:reviewed-certificate:001"
    )
    db_session_adapter.release_read_transaction(db)
    proof = payment_proofs.submit_direct_transfer_proof(
        db,
        command=payment_proofs.DirectTransferProofSubmissionCommand(
            intent_id=intent_id,
            account_id=subscriber_id,
            submitted_by=subscriber_id,
            selected_bank_account=topup_intents.DirectTransferBankAccountEvidence(
                id="bank-primary",
                bank_name="Test Bank",
                account_name="Dotmac",
                account_number="0123456789",
            ),
            paid_at=datetime.now(UTC),
            file_path="native-proof.pdf",
        ),
        context=_context(payment_proofs.SUBMISSION_SCOPE),
    )
    return invoice, link, intent_id, proof.id, review


def _verify(db, proof_id, review, **kwargs):
    db_session_adapter.release_read_transaction(db)
    return payment_proofs.verify_proof(
        db,
        str(proof_id),
        context=_context(payment_proofs.REVIEW_SCOPE),
        verified_by=_ACTOR,
        native_withholding_review=review,
        **kwargs,
    )


@pytest.mark.parametrize(
    "methods,expected_wht",
    [
        (("percentage",), "5.00"),
        (("fixed",), "3.00"),
        (("progressive",), "3.00"),
        (("percentage", "fixed", "progressive"), "11.00"),
    ],
)
def test_actual_cash_plus_explicit_components_discharge_without_legacy_cash(
    db_session,
    subscriber,
    monkeypatch,
    starter_native_tax_policy,
    methods,
    expected_wht,
):
    starter_native_tax_policy(
        withholding_rate=Decimal("0.05"), withholding_methods=methods
    )
    invoice, link, intent_id, proof_id, review = _prepare(
        db_session, subscriber, monkeypatch
    )
    fact = starter_positions.starter_invoice_fact(db_session, invoice)
    original = fact.grand_total.amount
    assert fact.expected_withholding.amount == Decimal(expected_wht)
    result = _verify(db_session, proof_id, review)
    assert result.payment_id is None and result.wht_rate is None
    intent = db_session.get(TopupIntent, intent_id)
    resolution = intent.metadata_["payment_proof_resolution"]
    cash = get_confirmed_cash_v1(
        db_session,
        scope=_SCOPE,
        query=GetConfirmedCashV1(UUID(resolution["confirmation_id"])),
    )
    assert cash.confirmed.amount == original - Decimal(expected_wht)
    discharges = tuple(
        get_withholding_discharge(db_session, scope=_SCOPE, discharge_id=UUID(value))
        for value in resolution["discharge_ids"]
    )
    assert (
        cash.confirmed.amount + sum(item.accepted_delta.amount for item in discharges)
        == original
    )
    assert all(
        item.settlement_id == UUID(resolution["settlement_id"]) for item in discharges
    )
    assert invoice.status is InvoiceStatus.paid and invoice.balance_due == 0
    assert (
        intent.completed_payment_id is None
        and intent.actual_amount == cash.confirmed.amount
    )
    assert db_session.scalar(select(func.count()).select_from(Payment)) == 0
    assert (
        db_session.scalar(select(func.count()).select_from(WithholdingTaxRecord)) == 0
    )
    signals = db_session.scalar(
        select(func.count())
        .select_from(EventStore)
        .where(
            EventStore.event_type == EventType.payment_received.value,
            EventStore.invoice_id == invoice.id,
        )
    )
    assert signals == 1
    assert _verify(db_session, proof_id, review) == result
    assert (
        db_session.scalar(
            select(func.count())
            .select_from(EventStore)
            .where(
                EventStore.event_type == EventType.payment_received.value,
                EventStore.invoice_id == invoice.id,
            )
        )
        == 1
    )
    assert starter_positions.starter_invoice_fact(db_session, invoice) == fact
    with pytest.raises(DomainError, match="replay differs"):
        _verify(
            db_session, proof_id, replace(review, acceptance_evidence_ref="changed")
        )


def test_incomplete_claim_refuses_without_cash_or_discharge(
    db_session, subscriber, monkeypatch, starter_native_tax_policy
):
    starter_native_tax_policy(
        withholding_rate=Decimal("0.05"), withholding_methods=("percentage", "fixed")
    )
    invoice, _link, intent_id, proof_id, review = _prepare(
        db_session, subscriber, monkeypatch
    )
    for bad_review in (
        None,
        replace(review, claims=review.claims[:1]),
        replace(
            review,
            claims=(
                replace(review.claims[0], accepted_amount=Decimal("0.01")),
                review.claims[1],
            ),
        ),
    ):
        with pytest.raises(DomainError):
            _verify(db_session, proof_id, bad_review)
        assert (
            db_session.get(PaymentProof, proof_id).status
            is PaymentProofStatus.submitted
        )
        assert db_session.get(TopupIntent, intent_id).actual_amount is None
        assert invoice.balance_due == Decimal("107.50")
    with pytest.raises(DomainError):
        _verify(db_session, proof_id, review, amount="1.00")


def test_late_failure_rolls_back_cash_discharges_signal_and_proof(
    db_session, subscriber, monkeypatch, starter_native_tax_policy
):
    starter_native_tax_policy(withholding_rate=Decimal("0.05"))
    invoice, _link, intent_id, proof_id, review = _prepare(
        db_session, subscriber, monkeypatch
    )
    original_completion = topup_intents.stage_native_withholding_proof_resolution

    def fail(*_args, **_kwargs):
        raise RuntimeError("injected completion refusal")

    monkeypatch.setattr(
        topup_intents, "stage_native_withholding_proof_resolution", fail
    )
    with pytest.raises(RuntimeError, match="injected"):
        _verify(db_session, proof_id, review)
    db_session.expire_all()
    assert db_session.get(PaymentProof, proof_id).status is PaymentProofStatus.submitted
    assert db_session.get(TopupIntent, intent_id).actual_amount is None
    assert invoice.balance_due == Decimal("107.50")
    assert (
        db_session.scalar(
            select(func.count())
            .select_from(EventStore)
            .where(
                EventStore.event_type == EventType.payment_received.value,
                EventStore.invoice_id == invoice.id,
            )
        )
        == 0
    )
    monkeypatch.setattr(
        topup_intents, "stage_native_withholding_proof_resolution", original_completion
    )
    _verify(db_session, proof_id, review)


def test_explicit_reversal_keeps_original_claim(
    db_session, subscriber, monkeypatch, starter_native_tax_policy
):
    starter_native_tax_policy(withholding_rate=Decimal("0.05"))
    invoice, link, intent_id, proof_id, review = _prepare(
        db_session, subscriber, monkeypatch
    )
    _verify(db_session, proof_id, review)
    intent = db_session.get(TopupIntent, intent_id)
    resolution = intent.metadata_["payment_proof_resolution"]
    discharge_id = UUID(resolution["discharge_ids"][0])
    reverse_withholding_discharge(
        db_session,
        scope=_SCOPE,
        command=ReverseWithholdingDischarge(
            scope=_SCOPE,
            discharge_id=discharge_id,
            idempotency_key=str(uuid4()),
            source_ref="finance:correction:001",
            authorization_actor_ref=_ACTOR,
            authorization_evidence_ref="finance:reversal:001",
            occurred_at=datetime.now(UTC),
        ),
    )
    db_session.commit()
    # Exact replay returns historical accepted evidence before mutable balance validation.
    assert _verify(db_session, proof_id, review).status is PaymentProofStatus.verified
    original = get_withholding_discharge(
        db_session, scope=_SCOPE, discharge_id=discharge_id
    )
    assert original.accepted_delta.amount == Decimal("5.00")
    assert original.tax_evidence.component_ref == str(review.claims[0].component_id)
    second_proof_id = uuid4()
    with pytest.raises(DomainError):
        # Product checkout refuses an adjusted document before any new intent.
        direct_transfer_intents._invoice_withholding_tax_snapshot(
            db_session, account_id=subscriber.id, invoice=invoice
        )
    from dotmac_billing.errors import BillingConflict

    with pytest.raises(BillingConflict, match="Tax component already"):
        # Even reversal cannot make the original Tax component reusable by a
        # different proof, source identity or idempotency key.
        accept_withholding_discharge(
            db_session,
            scope=_SCOPE,
            command=AcceptWithholdingDischarge(
                scope=_SCOPE,
                billing_account_id=link.billing_account_id,
                document_id=link.document_id,
                idempotency_key=str(uuid4()),
                source_owner="dotmac-sub.payment-proofs",
                source_ref=f"proof:{second_proof_id}:{original.tax_evidence.component_ref}",
                claim_ref=f"proof:{second_proof_id}:claim:{original.tax_evidence.component_ref}",
                claimed_amount=original.claimed_amount,
                accepted_amount=original.accepted_delta,
                tax_evidence=original.tax_evidence,
                authorization_actor_ref=_ACTOR,
                authorization_evidence_ref="finance:second-claim",
                occurred_at=datetime.now(UTC),
                settlement_id=original.settlement_id,
            ),
        )


def test_rejection_leaves_native_cash_and_receivable_untouched(
    db_session, subscriber, monkeypatch, starter_native_tax_policy
):
    from dotmac_payments import GetPaymentIntentV1, get_payment_intent_v1

    starter_native_tax_policy(withholding_rate=Decimal("0.05"))
    invoice, _link, intent_id, proof_id, _review = _prepare(
        db_session, subscriber, monkeypatch
    )
    intent = db_session.get(TopupIntent, intent_id)
    reference = intent.reference
    db_session_adapter.release_read_transaction(db_session)
    result = payment_proofs.reject_proof(
        db_session,
        str(proof_id),
        context=_context(payment_proofs.REVIEW_SCOPE),
        verified_by=_ACTOR,
        review_notes="No reviewed withholding evidence",
    )
    assert result.status is PaymentProofStatus.rejected
    observation = get_payment_intent_v1(
        db_session, scope=_SCOPE, query=GetPaymentIntentV1(reference=reference)
    )
    assert observation.confirmed is None
    assert starter_positions.starter_account_collectible(
        db_session, billing_account_id=_link.billing_account_id, currency_code="NGN"
    ) == Decimal("107.50")
    assert db_session.get(TopupIntent, intent_id).actual_amount is None
    assert invoice.status is InvoiceStatus.issued


def test_concurrent_acceptance_and_tenant_scope_preserve_one_cash_and_discharge(
    db_session, engine, subscriber, monkeypatch, starter_native_tax_policy
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from dotmac_billing.errors import BillingError
    from sqlalchemy.orm import Session

    starter_native_tax_policy(withholding_rate=Decimal("0.05"))
    invoice, _link, intent_id, proof_id, review = _prepare(
        db_session, subscriber, monkeypatch
    )
    invoice_id = invoice.id
    db_session_adapter.release_read_transaction(db_session)
    barrier = Barrier(2)

    def accept():
        with Session(engine, autoflush=False) as session:
            barrier.wait(timeout=20)
            return _verify(session, proof_id, review)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(accept) for _ in range(2)]
        results = [future.result(timeout=40) for future in futures]
    assert results[0] == results[1]
    db_session.expire_all()
    resolution = db_session.get(TopupIntent, intent_id).metadata_[
        "payment_proof_resolution"
    ]
    assert len(resolution["discharge_ids"]) == 1
    assert (
        db_session.scalar(
            select(func.count())
            .select_from(EventStore)
            .where(
                EventStore.event_type == EventType.payment_received.value,
                EventStore.invoice_id == invoice_id,
            )
        )
        == 1
    )
    with pytest.raises(BillingError):
        get_withholding_discharge(
            db_session,
            scope=TenantScope(uuid4()),
            discharge_id=UUID(resolution["discharge_ids"][0]),
        )
    assert db_session.scalar(select(func.count()).select_from(Payment)) == 0


@pytest.mark.parametrize(
    "field",
    [
        "fact_fingerprint",
        "source_invoice_id",
        "billing_account_id",
        "document_id",
        "fact_version",
        "component_result_fingerprint",
        "coordinated_fact_fingerprint",
        "allocation_posting_group_id",
    ],
)
def test_valid_shaped_checkout_drift_conflicts_with_original_native_acceptance(
    db_session, subscriber, monkeypatch, starter_native_tax_policy, field
):
    from copy import deepcopy

    from app.services.billing.starter_withholding_proofs import read_native_snapshot

    starter_native_tax_policy(withholding_rate=Decimal("0.05"))
    invoice, _link, intent_id, proof_id, review = _prepare(
        db_session, subscriber, monkeypatch
    )
    _verify(db_session, proof_id, review)
    assert invoice.status is InvoiceStatus.paid
    intent = db_session.get(TopupIntent, intent_id)
    metadata = deepcopy(intent.metadata_)
    snapshot = metadata["withholding_tax"]
    if field == "allocation_posting_group_id":
        metadata["payment_proof_resolution"][field] = str(uuid4())
    elif field == "component_result_fingerprint":
        snapshot["components"][0]["result_fingerprint"] = "rv1:" + "f" * 64
    elif field == "coordinated_fact_fingerprint":
        snapshot["fact_fingerprint"] = "f" * 64
        metadata["payment_proof_resolution"]["fact_fingerprint"] = "f" * 64
    elif field == "fact_fingerprint":
        snapshot[field] = "f" * 64
    elif field == "fact_version":
        snapshot[field] += 1
    else:
        snapshot[field] = str(uuid4())
    # Deliberately valid typed JSON: the canary tests identity integrity,
    # rather than rejecting malformed metadata at its parser boundary.
    read_native_snapshot(snapshot)
    intent.metadata_ = metadata
    db_session.commit()
    with pytest.raises(DomainError) as conflict:
        _verify(db_session, proof_id, review)
    assert conflict.value.code.endswith("native_replay_conflict")
    assert db_session.get(PaymentProof, proof_id).status is PaymentProofStatus.verified
    assert (
        db_session.scalar(
            select(func.count())
            .select_from(EventStore)
            .where(
                EventStore.event_type == EventType.payment_received.value,
                EventStore.invoice_id == invoice.id,
            )
        )
        == 1
    )


def test_unchanged_replay_after_payment_and_policy_disable_reads_only_original_evidence(
    db_session, subscriber, monkeypatch, starter_native_tax_policy
):
    from app.services.billing import starter_issuance, starter_settlement

    binding = starter_native_tax_policy(withholding_rate=Decimal("0.05"))
    invoice, _link, _intent_id, proof_id, review = _prepare(
        db_session, subscriber, monkeypatch
    )
    accepted = _verify(db_session, proof_id, review)
    assert invoice.status is InvoiceStatus.paid and invoice.balance_due == 0
    binding["expected_withholding"]["jurisdiction_id"] = str(uuid4())
    binding["invoice_tax"]["jurisdiction_id"] = str(uuid4())
    monkeypatch.setattr(starter_issuance, "resolve_value", lambda *_args: None)

    def refuse_policy_read(*_args):
        raise AssertionError(
            "Replay must not read current settlement or Billing policy"
        )

    monkeypatch.setattr(
        starter_positions, "resolve_starter_billing_policies", refuse_policy_read
    )
    monkeypatch.setattr(
        starter_settlement, "resolve_starter_settlement_policies", refuse_policy_read
    )
    assert _verify(db_session, proof_id, review) == accepted


def test_historical_replay_retains_exact_original_allocation_after_reversal(
    db_session, subscriber, monkeypatch, starter_native_tax_policy
):
    from dotmac_billing.commands import ReversePostingGroupCommand
    from dotmac_billing.queries import get_allocation_accounting_fact_v1
    from dotmac_billing.service import reverse_posting_group

    starter_native_tax_policy(withholding_rate=Decimal("0.05"))
    _invoice, _link, intent_id, proof_id, review = _prepare(
        db_session, subscriber, monkeypatch
    )
    accepted = _verify(db_session, proof_id, review)
    receipt = accepted.native_settlement
    assert receipt is not None
    original = get_allocation_accounting_fact_v1(
        db_session, scope=_SCOPE, posting_group_id=receipt.allocation_posting_group_id
    )
    reverse_posting_group(
        db_session,
        scope=_SCOPE,
        command=ReversePostingGroupCommand(
            posting_group_id=receipt.allocation_posting_group_id,
            occurred_at=datetime.now(UTC),
            source_ref="finance:cash-allocation-reversal:001",
        ),
    )
    db_session.commit()
    # This remains historical acceptance evidence even though allocation capacity
    # has changed. A mutable current-allocation reader would refuse this replay.
    assert (
        get_allocation_accounting_fact_v1(
            db_session,
            scope=_SCOPE,
            posting_group_id=receipt.allocation_posting_group_id,
        )
        == original
    )
    assert _verify(db_session, proof_id, review) == accepted
    intent = db_session.get(TopupIntent, intent_id)
    assert intent.metadata_["payment_proof_resolution"][
        "allocation_posting_group_id"
    ] == str(original.posting_group_id)


def test_same_cash_invoice_reallocation_cannot_replace_original_proof_allocation(
    db_session, subscriber, monkeypatch, starter_native_tax_policy
):
    from copy import deepcopy

    from dotmac_billing.commands import AllocationCommand, ReversePostingGroupCommand
    from dotmac_billing.queries import get_allocation_accounting_fact_v1
    from dotmac_billing.service import allocate_settlement, reverse_posting_group

    starter_native_tax_policy(withholding_rate=Decimal("0.05"))
    _invoice, link, intent_id, proof_id, review = _prepare(
        db_session, subscriber, monkeypatch
    )
    accepted = _verify(db_session, proof_id, review)
    receipt = accepted.native_settlement
    assert receipt is not None
    original = get_allocation_accounting_fact_v1(
        db_session, scope=_SCOPE, posting_group_id=receipt.allocation_posting_group_id
    )
    assert original.source_ref == f"proof:{proof_id}:cash-allocation"
    reverse_posting_group(
        db_session,
        scope=_SCOPE,
        command=ReversePostingGroupCommand(
            posting_group_id=original.posting_group_id,
            occurred_at=datetime.now(UTC),
            source_ref="finance:allocation-replacement-reversal",
        ),
    )
    # Billing's permanent tenant/operation/source-ref idempotency ledger returns
    # the original group; reversal does not make the same proof ref reusable.
    replayed_allocation = allocate_settlement(
        db_session,
        scope=_SCOPE,
        command=AllocationCommand(
            settlement_id=receipt.settlement_id,
            document_id=link.document_id,
            amount=original.allocations[0].amount_delta,
            occurred_at=original.occurred_at,
            source_ref=original.source_ref,
        ),
    )
    assert replayed_allocation.id == original.posting_group_id
    replacement = allocate_settlement(
        db_session,
        scope=_SCOPE,
        command=AllocationCommand(
            settlement_id=receipt.settlement_id,
            document_id=link.document_id,
            amount=original.allocations[0].amount_delta,
            occurred_at=original.occurred_at,
            source_ref="finance:valid-reallocation-different-source",
        ),
    )
    db_session.commit()
    replacement_fact = get_allocation_accounting_fact_v1(
        db_session, scope=_SCOPE, posting_group_id=replacement.id
    )
    assert replacement_fact.posting_group_id != original.posting_group_id
    assert replacement_fact.billing_account_id == original.billing_account_id
    assert replacement_fact.allocations == original.allocations
    assert replacement_fact.effects == original.effects
    assert replacement_fact.source_ref != original.source_ref
    # Both real historical groups have identical cash/invoice/account/amount
    # bindings, but only the original carries this proof's immutable source ref.
    assert _verify(db_session, proof_id, review) == accepted
    intent = db_session.get(TopupIntent, intent_id)
    metadata = deepcopy(intent.metadata_)
    metadata["payment_proof_resolution"]["allocation_posting_group_id"] = str(
        replacement_fact.posting_group_id
    )
    intent.metadata_ = metadata
    db_session.commit()
    with pytest.raises(DomainError) as conflict:
        _verify(db_session, proof_id, review)
    assert conflict.value.code.endswith("native_replay_conflict")


@pytest.mark.parametrize("selection", ("all_prior", "tax_code_ids"))
def test_compounded_v3_proof_retains_tax_v2_lineage_and_rejects_graph_digest_drift(
    db_session, subscriber, monkeypatch, selection
):
    from copy import deepcopy
    from dataclasses import asdict

    from dotmac_billing import InvoiceDocumentFactV3, WithholdingTaxEvidenceV2
    from dotmac_kernel.idempotency import fingerprint_of

    from app.services.billing import starter_issuance
    from app.services.billing.starter_withholding_proofs import read_native_snapshot

    binding = _publish_dependency_policy(db_session, monkeypatch, selection=selection)
    invoice, link, intent_id, proof_id, review = _prepare(
        db_session, subscriber, monkeypatch
    )
    fact = starter_positions.starter_invoice_fact(db_session, invoice)
    assert isinstance(fact, InvoiceDocumentFactV3)
    vat, withholding = fact.tax_evidence
    assert withholding.determination.source_amount.amount == Decimal("100.00")
    assert withholding.determination.components[0].base_amount.amount == Decimal(
        "107.50"
    )
    assert withholding.determination.tax_amount.amount == Decimal("5.38")
    assert withholding.prior_determinations == (vat.determination,)
    dependent_component = withholding.determination.components[0]
    assert dependent_component.selected_dependency_policy is not None
    assert dependent_component.selected_dependency_policy.selection == selection
    assert (
        dependent_component.dependency_components[0].determination_set_id
        == vat.determination.determination_set_id
    )
    assert (
        withholding.determination.commercial_source
        == vat.determination.commercial_source
    )
    assert fact.commercial_sources == (withholding.determination.commercial_source,)
    accepted = _verify(db_session, proof_id, review)
    receipt = accepted.native_settlement
    assert receipt is not None
    cash = get_confirmed_cash_v1(
        db_session, scope=_SCOPE, query=GetConfirmedCashV1(receipt.confirmation_id)
    )
    assert cash.confirmed.amount == Decimal("102.12")
    assert len(receipt.discharge_ids) == 1
    discharge = get_withholding_discharge(
        db_session, scope=_SCOPE, discharge_id=receipt.discharge_ids[0]
    )
    assert isinstance(discharge.tax_evidence, WithholdingTaxEvidenceV2)
    assert discharge.tax_evidence.determination == withholding.determination
    assert (
        discharge.tax_evidence.prior_determinations == withholding.prior_determinations
    )
    assert discharge.tax_evidence.document_id == link.document_id
    assert discharge.tax_evidence.billing_account_id == link.billing_account_id
    assert discharge.accepted_delta.amount == Decimal("5.38")
    assert (
        cash.confirmed.amount + discharge.accepted_delta.amount
        == fact.grand_total.amount
    )
    assert invoice.balance_due == 0 and invoice.status is InvoiceStatus.paid
    assert db_session.scalar(select(func.count()).select_from(Payment)) == 0
    assert (
        db_session.scalar(select(func.count()).select_from(WithholdingTaxRecord)) == 0
    )
    # Re-routing or disabling today's Tax policy cannot rerate the frozen graph.
    binding["expected_withholding"]["prior_purposes"] = []
    monkeypatch.setattr(starter_issuance, "resolve_value", lambda *_args: None)
    assert _verify(db_session, proof_id, review) == accepted
    assert starter_positions.starter_invoice_fact(db_session, invoice) == fact
    # Only lineage changes in this digest preimage: commercial amounts, selected
    # component identities, and claimed/accepted values remain the same.
    graph_drift = asdict(fact)
    graph_drift["tax_evidence"][1]["prior_determinations"][0]["commercial_source"][
        "source_version"
    ] = "tampered-lineage-version"
    drifted_fingerprint = fingerprint_of(graph_drift)
    assert drifted_fingerprint != fingerprint_of(asdict(fact))
    intent = db_session.get(TopupIntent, intent_id)
    metadata = deepcopy(intent.metadata_)
    metadata["withholding_tax"]["fact_fingerprint"] = drifted_fingerprint
    metadata["payment_proof_resolution"]["fact_fingerprint"] = drifted_fingerprint
    read_native_snapshot(metadata["withholding_tax"])
    intent.metadata_ = metadata
    db_session.commit()
    with pytest.raises(DomainError) as conflict:
        _verify(db_session, proof_id, review)
    assert conflict.value.code.endswith("native_replay_conflict")
    assert (
        get_withholding_discharge(
            db_session, scope=_SCOPE, discharge_id=receipt.discharge_ids[0]
        ).tax_evidence
        == discharge.tax_evidence
    )
