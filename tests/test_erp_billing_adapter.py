"""Behavior coverage for `integration.dotmac_erp_billing_adapter` (Phase 7)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal
from uuid import uuid4

import pytest
from dotmac_billing import AccountingFactV1
from sqlalchemy.orm import Session

from app.models.erp_billing_export import ErpBillingFlow, ErpExportStatus
from app.services.dotmac_erp.billing_adapter import (
    ErpBillingAdapterError,
    StagedBillingFact,
    StageErpExportCommand,
    pending_exports,
    record_delivery_outcome,
    stage_export,
)
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

OCCURRED = datetime(2026, 3, 6, 9, 0, tzinfo=UTC)

# A real contracted owner hosts the participant staging, exactly as the
# committing money owner would.
_HOST_COMMAND = OwnerCommandDefinition(
    owner="billing.contracts",
    concern="versioned billing contract terms",
    name="record_billing_contract_version",
)


def _context(key: str | None = None) -> CommandContext:
    command_id = uuid4()
    return CommandContext(
        command_id=command_id,
        correlation_id=command_id,
        actor="user:pytest",
        scope="erp-export:test",
        reason="pytest erp billing adapter",
        idempotency_key=key or f"pytest:{command_id}",
    )


def _in_owner_command(db, operation):
    return execute_owner_command(
        db, definition=_HOST_COMMAND, context=_context(), operation=operation
    )


def _payload() -> dict:
    return {
        "document_kind": "invoice",
        "account_id": str(uuid4()),
        "currency": "NGN",
        "lines": [{"description": "Internet", "amount": "25000.00"}],
    }


def _stage(db, *, key=None, payload=None):
    context = _context(key)
    command = StageErpExportCommand(
        flow=ErpBillingFlow.invoice,
        source_kind="invoice",
        source_id=uuid4(),
        payload=payload if payload is not None else _payload(),
    )
    return _in_owner_command(db, lambda: stage_export(db, command, context=context))


def test_staging_outside_an_owner_command_fails_closed(db_session):
    with pytest.raises(ErpBillingAdapterError) as excinfo:
        stage_export(
            db_session,
            StageErpExportCommand(
                flow=ErpBillingFlow.invoice,
                source_kind="invoice",
                source_id=uuid4(),
                payload=_payload(),
            ),
            context=_context(),
        )

    assert excinfo.value.code == (
        "integration.dotmac_erp_billing_adapter.export_requires_owner_command"
    )


def test_an_incomplete_payload_is_never_exported(db_session):
    payload = _payload()
    del payload["currency"]

    with pytest.raises(ErpBillingAdapterError) as excinfo:
        _stage(db_session, payload=payload)

    assert excinfo.value.code == (
        "integration.dotmac_erp_billing_adapter.incomplete_export_payload"
    )
    assert excinfo.value.details["missing_fields"] == ["currency"]


def test_one_export_per_idempotency_key(db_session):
    first = _stage(db_session, key="pytest:same-export")
    first_id = first.id
    db_session.commit()
    second = _stage(db_session, key="pytest:same-export")

    assert second.id == first_id
    assert len(pending_exports(db_session)) == 1


def test_the_full_delivery_chain_is_durable(db_session):
    export = _stage(db_session)
    export_id = export.id
    db_session.commit()

    delivered = record_delivery_outcome(
        db_session,
        export_id=export_id,
        outcome=ErpExportStatus.delivered,
        context=_context(),
        occurred_at=OCCURRED,
    )
    acknowledged = record_delivery_outcome(
        db_session,
        export_id=export_id,
        outcome=ErpExportStatus.acknowledged,
        context=_context(),
        occurred_at=OCCURRED,
        erp_reference="ERP-INV-000123",
    )

    assert delivered is ErpExportStatus.delivered
    assert acknowledged is ErpExportStatus.acknowledged

    from app.models.erp_billing_export import ErpBillingExport

    record = db_session.get(ErpBillingExport, export_id)
    assert record.attempts == 1
    assert record.erp_reference == "ERP-INV-000123"
    assert record.acknowledged_at is not None


def test_acknowledgement_requires_erp_reference(db_session):
    export = _stage(db_session)
    export_id = export.id
    db_session.commit()

    with pytest.raises(ErpBillingAdapterError) as excinfo:
        record_delivery_outcome(
            db_session,
            export_id=export_id,
            outcome=ErpExportStatus.acknowledged,
            context=_context(),
            occurred_at=OCCURRED,
        )

    assert excinfo.value.code == (
        "integration.dotmac_erp_billing_adapter.missing_erp_reference"
    )


def test_a_rejection_requires_reviewable_evidence(db_session):
    export = _stage(db_session)
    export_id = export.id
    db_session.commit()

    with pytest.raises(ErpBillingAdapterError) as excinfo:
        record_delivery_outcome(
            db_session,
            export_id=export_id,
            outcome=ErpExportStatus.rejected,
            context=_context(),
            occurred_at=OCCURRED,
        )

    assert excinfo.value.code == (
        "integration.dotmac_erp_billing_adapter.missing_rejection_evidence"
    )


def test_a_terminal_outcome_cannot_be_replaced(db_session):
    export = _stage(db_session)
    export_id = export.id
    db_session.commit()

    record_delivery_outcome(
        db_session,
        export_id=export_id,
        outcome=ErpExportStatus.rejected,
        context=_context(),
        occurred_at=OCCURRED,
        error="ERP mapping missing for tax code VAT-NG; review required",
    )

    with pytest.raises(ErpBillingAdapterError) as excinfo:
        record_delivery_outcome(
            db_session,
            export_id=export_id,
            outcome=ErpExportStatus.acknowledged,
            context=_context(),
            occurred_at=OCCURRED,
            erp_reference="ERP-LATE-1",
        )

    assert excinfo.value.code == (
        "integration.dotmac_erp_billing_adapter.conflicting_export_outcome"
    )


def test_erp_downtime_leaves_exports_pending(db_session):
    """An undelivered export is durable evidence, not a rollback trigger."""

    _stage(db_session)

    pending = pending_exports(db_session)

    assert len(pending) == 1
    assert pending[0].status is ErpExportStatus.pending
    assert pending[0].attempts == 0


def test_generic_staging_cannot_receive_accounting_facts(db_session: Session) -> None:
    """The invoice transport cannot invent lines for a module accounting fact."""
    with pytest.raises(ErpBillingAdapterError) as excinfo:
        _stage(
            db_session,
            payload={
                **_payload(),
                "contract": "billing.accounting.export.v1",
                "fact": {"fact_id": str(uuid4())},
            },
        )
    assert excinfo.value.code.endswith("accounting_fact_requires_dedicated_flow")


def _released_fact() -> AccountingFactV1:
    from dotmac_billing import AccountingEffectV1
    from dotmac_kernel.cache import TenantScope
    from dotmac_kernel.money import Currency, Money

    from app.services.operator_tenant import OPERATOR_TENANT_ID

    return AccountingFactV1(
        scope=TenantScope(OPERATOR_TENANT_ID),
        source_system="billing",
        fact_id=uuid4(),
        fact_version=1,
        billing_account_id=uuid4(),
        posting_group_id=uuid4(),
        source_ref="opaque",
        source_authority="internal",
        effect_kind="invoice_issued",
        occurred_at=OCCURRED,
        committed_at=OCCURRED,
        effects=(AccountingEffectV1("receivable", Money.of("100", Currency("NGN"))),),
    )


def _stage_released_fact(db: Session, fact: AccountingFactV1) -> StagedBillingFact:
    from app.services.dotmac_erp.billing_adapter import (
        StageCommittedBillingFactCommand,
        stage_committed_billing_fact,
    )

    context = _context()
    return _in_owner_command(
        db,
        lambda: stage_committed_billing_fact(
            db,
            StageCommittedBillingFactCommand(fact),
            context=context,
        ),
    )


def test_fact_staging_requires_active_owner_command(db_session: Session) -> None:
    from app.services.dotmac_erp.billing_adapter import (
        StageCommittedBillingFactCommand,
        stage_committed_billing_fact,
    )

    with pytest.raises(ErpBillingAdapterError) as excinfo:
        stage_committed_billing_fact(
            db_session,
            StageCommittedBillingFactCommand(_released_fact()),
            context=_context(),
        )
    assert excinfo.value.code.endswith("export_requires_owner_command")


def test_fact_retry_after_delivery_replays_original_export(db_session: Session) -> None:
    fact = _released_fact()
    first = _stage_released_fact(db_session, fact)
    record_delivery_outcome(
        db_session,
        export_id=first.export_id,
        outcome=ErpExportStatus.delivered,
        context=_context(),
        occurred_at=OCCURRED,
    )
    replay = _stage_released_fact(db_session, fact)
    assert replay.export_id == first.export_id
    assert replay.fact_digest == first.fact_digest
    assert replay.replayed and not first.replayed


@pytest.mark.parametrize("mutation", ("amount", "tax", "reversal"))
def test_fact_identity_reuse_with_changed_evidence_fails_closed(
    db_session: Session,
    mutation: Literal["amount", "tax", "reversal"],
) -> None:
    from dataclasses import replace

    from dotmac_billing import AccountingEffectV1, AppliedTaxSnapshotV1
    from dotmac_kernel.money import Currency, Money

    fact = _released_fact()
    _stage_released_fact(db_session, fact)
    money = Money.of("101", Currency("NGN"))
    changed = {
        "amount": replace(fact, effects=(AccountingEffectV1("receivable", money),)),
        "tax": replace(
            fact,
            tax_snapshots=(
                AppliedTaxSnapshotV1(
                    treatment_code="VAT",
                    jurisdiction_code="NG",
                    policy_id="policy",
                    policy_version="2",
                    rate=Decimal("0.075"),
                    taxable_basis=money,
                    tax_amount=Money.of("7.58", Currency("NGN")),
                ),
            ),
        ),
        "reversal": replace(fact, reverses_fact_id=uuid4()),
    }[mutation]
    with pytest.raises(ErpBillingAdapterError) as excinfo:
        _stage_released_fact(db_session, changed)
    assert excinfo.value.code.endswith("accounting_fact_identity_conflict")
    assert not excinfo.value.retryable
    assert len(pending_exports(db_session)) == 1


def test_fact_scope_and_version_partition_export_identity(db_session: Session) -> None:
    from dataclasses import replace

    from dotmac_kernel.cache import PlatformScope, TenantScope

    fact = _released_fact()
    identities = {
        _stage_released_fact(db_session, value).export_id
        for value in (
            fact,
            replace(fact, scope=PlatformScope()),
            replace(fact, scope=TenantScope(uuid4())),
            replace(fact, fact_version=2),
        )
    }
    assert len(identities) == 4
    assert len(pending_exports(db_session)) == 4
