"""Owner for previewed, append-only reversal of imported payment batches."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.billing import Invoice, Payment, PaymentReversalOrigin
from app.models.imports import (
    ImportRowStatus,
    ImportRun,
    ImportRunStatus,
    PaymentImportBatchReversal,
    PaymentImportBatchReversalItem,
)
from app.schemas.audit import AuditEventCreate
from app.schemas.billing import (
    PaymentReversalPreviewRequest,
    PaymentReversalRequest,
)
from app.services.audit import AuditEvents
from app.services.billing._common import lock_account
from app.services.billing.payments import (
    PaymentReversalPreview,
    PaymentReversals,
)
from app.services.common import coerce_uuid, round_money
from app.services.customer_financial_position import get_customer_financial_position

_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,119}$")


def _money(value: object) -> str:
    return f"{round_money(Decimal(str(value or '0.00'))):.2f}"


@dataclass(frozen=True)
class BatchReversalCapability:
    allowed: bool
    reason: str | None


@dataclass(frozen=True)
class ImportedPaymentReversalPreview:
    import_run_row_id: UUID
    row_number: int
    payment_id: UUID
    payment_settlement_id: UUID
    account_id: UUID
    currency: str
    reversal_amount: Decimal
    account_credit_consumption: Decimal
    ledger_entry_type: str
    ledger_source: str
    ledger_amount: Decimal
    access_consequence: str
    source_snapshot: dict[str, object]
    nested_preview_fingerprint: str
    invoice_effects: tuple[dict[str, object], ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "import_run_row_id": str(self.import_run_row_id),
            "row_number": self.row_number,
            "payment_id": str(self.payment_id),
            "payment_settlement_id": str(self.payment_settlement_id),
            "account_id": str(self.account_id),
            "currency": self.currency,
            "reversal_amount": _money(self.reversal_amount),
            "account_credit_consumption": _money(self.account_credit_consumption),
            "ledger_entry_type": self.ledger_entry_type,
            "ledger_source": self.ledger_source,
            "ledger_amount": _money(self.ledger_amount),
            "access_consequence": self.access_consequence,
            "source_evidence": self.source_snapshot,
            "nested_preview_fingerprint": self.nested_preview_fingerprint,
            "invoice_effects": list(self.invoice_effects),
        }


@dataclass(frozen=True)
class PaymentImportBatchReversalPreview:
    import_run_id: UUID
    source_run_id: UUID
    reason: str
    items: tuple[ImportedPaymentReversalPreview, ...]
    skipped_reused_count: int
    totals_by_currency: tuple[dict[str, object], ...]
    account_positions: tuple[dict[str, object], ...]
    invoice_effects: tuple[dict[str, object], ...]
    access_consequence: str

    def as_snapshot(self) -> dict[str, object]:
        return {
            "owner": "financial.import_payment_batch_reversals",
            "import_run_id": str(self.import_run_id),
            "source_run_id": str(self.source_run_id),
            "reason": self.reason,
            "reversed_payment_count": len(self.items),
            "skipped_reused_count": self.skipped_reused_count,
            "totals_by_currency": list(self.totals_by_currency),
            "account_positions": list(self.account_positions),
            "invoice_effects": list(self.invoice_effects),
            "access_consequence": self.access_consequence,
            "items": [item.as_dict() for item in self.items],
        }

    @property
    def fingerprint(self) -> str:
        encoded = json.dumps(self.as_snapshot(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PaymentImportBatchReversalResult:
    batch_reversal: PaymentImportBatchReversal
    idempotent_replay: bool = False


def _normalize_reason(reason: str) -> str:
    value = reason.strip()
    if len(value) < 3:
        raise HTTPException(status_code=400, detail="Reversal reason is required")
    if len(value) > 500:
        raise HTTPException(
            status_code=400, detail="Reversal reason must be at most 500 characters"
        )
    return value


def _normalize_key(key: str) -> str:
    value = key.strip()
    if not _KEY_RE.fullmatch(value):
        raise HTTPException(
            status_code=400,
            detail="Batch reversal idempotency key must be 16-120 safe characters",
        )
    return value


def _source_snapshot(payment: Payment) -> dict[str, object]:
    settlement = payment.settlement
    if settlement is None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Payment {payment.id} lacks exact settlement evidence; "
                "reconcile it before batch reversal"
            ),
        )
    if settlement.unallocated_amount > 0 and not settlement.unallocated_ledger_entry_id:
        raise HTTPException(
            status_code=409,
            detail=f"Payment {payment.id} lacks unallocated-credit evidence",
        )
    if settlement.prepaid_amount > 0 and not settlement.prepaid_ledger_entry_id:
        raise HTTPException(
            status_code=409,
            detail=f"Payment {payment.id} lacks prepaid-funding evidence",
        )

    allocations: list[dict[str, object]] = []
    for allocation in sorted(payment.allocations, key=lambda row: str(row.id)):
        if not allocation.is_active:
            continue
        if allocation.ledger_entry_id is None:
            raise HTTPException(
                status_code=409,
                detail=f"Payment {payment.id} has allocation without ledger evidence",
            )
        allocations.append(
            {
                "allocation_id": str(allocation.id),
                "invoice_id": str(allocation.invoice_id),
                "amount": _money(allocation.amount),
                "ledger_entry_id": str(allocation.ledger_entry_id),
                "account_credit_consumption_ledger_entry_id": (
                    str(allocation.consumption_ledger_entry_id)
                    if allocation.consumption_ledger_entry_id
                    else None
                ),
            }
        )
    return {
        "payment_id": str(payment.id),
        "payment_status": payment.status.value,
        "payment_amount": _money(payment.amount),
        "refunded_amount": _money(payment.refunded_amount),
        "currency": payment.currency,
        "creation_preview_fingerprint": payment.creation_preview_fingerprint,
        "settlement_id": str(settlement.id),
        "settlement_preview_fingerprint": settlement.preview_fingerprint,
        "settlement_amount": _money(settlement.amount),
        "unallocated_amount": _money(settlement.unallocated_amount),
        "unallocated_ledger_entry_id": (
            str(settlement.unallocated_ledger_entry_id)
            if settlement.unallocated_ledger_entry_id
            else None
        ),
        "prepaid_amount": _money(settlement.prepaid_amount),
        "prepaid_ledger_entry_id": (
            str(settlement.prepaid_ledger_entry_id)
            if settlement.prepaid_ledger_entry_id
            else None
        ),
        "allocations": allocations,
    }


def _effect_signature(preview: PaymentReversalPreview) -> dict[str, object]:
    return {
        "payment_id": str(preview.payment_id),
        "currency": preview.currency,
        "reversal_amount": _money(preview.reversal_amount),
        "account_credit_consumption": _money(preview.account_credit_consumption),
        "ledger_entry_type": preview.ledger_entry_type.value,
        "ledger_source": preview.ledger_source.value,
        "ledger_amount": _money(preview.ledger_amount),
        "invoice_effects": [
            {
                "invoice_id": str(effect.invoice_id),
                "refund_attributed": _money(effect.refund_attributed),
            }
            for effect in preview.invoice_effects
        ],
    }


def _load_run_rows(
    db: Session, run_id: str | UUID
) -> tuple[ImportRun, list[tuple[Any, Payment]], int]:
    run = db.get(ImportRun, coerce_uuid(run_id))
    if run is None:
        raise HTTPException(status_code=404, detail="Import run not found")
    if run.module != "payments" or run.dry_run:
        raise HTTPException(
            status_code=409,
            detail="Only applied payment import runs have a batch reversal owner",
        )
    if run.status != ImportRunStatus.completed:
        raise HTTPException(
            status_code=409, detail="Only completed payment imports can be reversed"
        )
    if run.source_run_id is None:
        raise HTTPException(
            status_code=409,
            detail="Payment import is missing its validated source run",
        )

    rows: list[tuple[Any, Payment]] = []
    skipped_reused = 0
    seen_created: set[UUID] = set()
    for row in sorted(run.rows, key=lambda item: item.row_number):
        if row.status != ImportRowStatus.ok:
            continue
        if row.record_created is None or row.payment_id is None:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Import row lacks durable payment-creation provenance; "
                    "historical rows are not inferred from JSON, amount, or memo"
                ),
            )
        payment = db.get(Payment, row.payment_id)
        if payment is None:
            raise HTTPException(
                status_code=409,
                detail=f"Imported payment evidence is missing for row {row.row_number}",
            )
        recorded_id = str((row.result or {}).get("id") or "")
        if recorded_id and recorded_id != str(payment.id):
            raise HTTPException(
                status_code=409,
                detail=f"Import row {row.row_number} conflicts with payment evidence",
            )
        if not row.record_created:
            skipped_reused += 1
            continue
        if payment.import_run_id != run.id:
            raise HTTPException(
                status_code=409,
                detail=f"Payment {payment.id} is not owned by this import run",
            )
        if payment.id in seen_created:
            raise HTTPException(
                status_code=409,
                detail=f"Payment {payment.id} is claimed by multiple created rows",
            )
        seen_created.add(payment.id)
        rows.append((row, payment))
    if not rows:
        raise HTTPException(
            status_code=409,
            detail="This import run created no payments that it can reverse",
        )
    return run, rows, skipped_reused


def _build_preview(
    db: Session, run_id: str | UUID, reason: str
) -> PaymentImportBatchReversalPreview:
    normalized_reason = _normalize_reason(reason)
    run, rows, skipped_reused = _load_run_rows(db, run_id)
    if run.payment_batch_reversal is not None:
        raise HTTPException(
            status_code=409, detail="This payment import was already reversed"
        )

    items: list[ImportedPaymentReversalPreview] = []
    totals: dict[str, Decimal] = {}
    account_values: dict[tuple[str, str], dict[str, Any]] = {}
    invoice_values: dict[str, dict[str, Any]] = {}
    for row, payment in rows:
        nested = PaymentReversals.preview(
            db,
            str(payment.id),
            PaymentReversalPreviewRequest(reason=normalized_reason),
        )
        source = _source_snapshot(payment)
        effects: list[dict[str, object]] = []
        for effect in nested.invoice_effects:
            effect_row: dict[str, object] = {
                "invoice_id": str(effect.invoice_id),
                "invoice_number": effect.invoice_number,
                "receivable_before": _money(effect.receivable_before),
                "refund_attributed": _money(effect.refund_attributed),
            }
            effects.append(effect_row)
            key = str(effect.invoice_id)
            aggregate = invoice_values.get(key)
            if aggregate is None:
                invoice = db.get(Invoice, effect.invoice_id)
                if invoice is None:
                    raise HTTPException(
                        status_code=409,
                        detail=f"Invoice {effect.invoice_id} was not found",
                    )
                aggregate = {
                    "invoice_id": key,
                    "invoice_number": effect.invoice_number,
                    "currency": invoice.currency,
                    "receivable_before": round_money(effect.receivable_before),
                    "invoice_total": round_money(invoice.total),
                    "reopened_amount": Decimal("0.00"),
                }
                invoice_values[key] = aggregate
            aggregate["reopened_amount"] = round_money(
                aggregate["reopened_amount"] + effect.refund_attributed
            )

        item = ImportedPaymentReversalPreview(
            import_run_row_id=row.id,
            row_number=row.row_number,
            payment_id=payment.id,
            payment_settlement_id=payment.settlement.id,
            account_id=nested.account_id,
            currency=nested.currency,
            reversal_amount=nested.reversal_amount,
            account_credit_consumption=nested.account_credit_consumption,
            ledger_entry_type=nested.ledger_entry_type.value,
            ledger_source=nested.ledger_source.value,
            ledger_amount=nested.ledger_amount,
            access_consequence=nested.access_consequence,
            source_snapshot=source,
            nested_preview_fingerprint=nested.fingerprint,
            invoice_effects=tuple(effects),
        )
        items.append(item)
        totals[item.currency] = round_money(
            totals.get(item.currency, Decimal("0.00")) + item.reversal_amount
        )
        account_key = (str(item.account_id), item.currency)
        account = account_values.get(account_key)
        if account is None:
            position = get_customer_financial_position(db, item.account_id)
            account = {
                "account_id": str(item.account_id),
                "currency": item.currency,
                "prepaid_funding_before": round_money(nested.prepaid_funding_before),
                "account_credit_before": round_money(nested.account_credit_before),
                "postpaid_receivables_before": round_money(
                    position.open_invoice_balance
                ),
                "collection_blocking_balance": round_money(
                    position.collection_blocking_balance
                ),
                "reversal_total": Decimal("0.00"),
                "account_credit_consumption": Decimal("0.00"),
                "receivables_reopened": Decimal("0.00"),
            }
            account_values[account_key] = account
        account["reversal_total"] = round_money(
            account["reversal_total"] + item.reversal_amount
        )
        account["account_credit_consumption"] = round_money(
            account["account_credit_consumption"] + item.account_credit_consumption
        )
        account["receivables_reopened"] = round_money(
            account["receivables_reopened"]
            + sum(
                (Decimal(str(effect["refund_attributed"])) for effect in effects),
                Decimal("0.00"),
            )
        )

    invoice_effects: list[dict[str, object]] = []
    for value in sorted(invoice_values.values(), key=lambda item: item["invoice_id"]):
        receivable_after = min(
            value["invoice_total"],
            round_money(value["receivable_before"] + value["reopened_amount"]),
        )
        invoice_effects.append(
            {
                "invoice_id": value["invoice_id"],
                "invoice_number": value["invoice_number"],
                "currency": value["currency"],
                "receivable_before": _money(value["receivable_before"]),
                "reopened_amount": _money(value["reopened_amount"]),
                "receivable_after": _money(receivable_after),
            }
        )

    accounts: list[dict[str, object]] = []
    for value in sorted(
        account_values.values(), key=lambda item: (item["account_id"], item["currency"])
    ):
        accounts.append(
            {
                "account_id": value["account_id"],
                "currency": value["currency"],
                "prepaid_funding_before": _money(value["prepaid_funding_before"]),
                "prepaid_funding_after": _money(
                    value["prepaid_funding_before"] - value["reversal_total"]
                ),
                "unallocated_account_credit_before": _money(
                    value["account_credit_before"]
                ),
                "unallocated_account_credit_consumed": _money(
                    value["account_credit_consumption"]
                ),
                "unallocated_account_credit_after": _money(
                    value["account_credit_before"] - value["account_credit_consumption"]
                ),
                "postpaid_receivables_before": _money(
                    value["postpaid_receivables_before"]
                ),
                "postpaid_receivables_reopened": _money(value["receivables_reopened"]),
                "postpaid_receivables_after": _money(
                    value["postpaid_receivables_before"] + value["receivables_reopened"]
                ),
                "collection_blocking_balance_before": _money(
                    value["collection_blocking_balance"]
                ),
                "service_access_state": "not_predicted_reconcile_after_confirmation",
            }
        )

    source_run_id = run.source_run_id
    if source_run_id is None:
        raise HTTPException(
            status_code=409,
            detail="Payment import is missing its validated source run",
        )
    return PaymentImportBatchReversalPreview(
        import_run_id=run.id,
        source_run_id=source_run_id,
        reason=normalized_reason,
        items=tuple(items),
        skipped_reused_count=skipped_reused,
        totals_by_currency=tuple(
            {
                "currency": currency,
                "reversal_amount": _money(total),
                "ledger_entry_type": "debit",
                "ledger_source": "payment",
            }
            for currency, total in sorted(totals.items())
        ),
        account_positions=tuple(accounts),
        invoice_effects=tuple(invoice_effects),
        access_consequence="recheck_each_account_after_payment_reversal",
    )


class PaymentImportBatchReversals:
    """Canonical wrapper around exact per-payment reversal commands."""

    @staticmethod
    def capability(db: Session, run_id: str | UUID) -> BatchReversalCapability:
        run = db.get(ImportRun, coerce_uuid(run_id))
        if run is None:
            return BatchReversalCapability(False, "Import run not found")
        if run.payment_batch_reversal is not None:
            return BatchReversalCapability(False, "Payment import already reversed")
        try:
            _build_preview(db, run.id, "Reverse payments created by this import run")
        except HTTPException as exc:
            return BatchReversalCapability(False, str(exc.detail))
        return BatchReversalCapability(True, None)

    @staticmethod
    def preview(
        db: Session, run_id: str | UUID, *, reason: str
    ) -> PaymentImportBatchReversalPreview:
        return _build_preview(db, run_id, reason)

    @staticmethod
    def _replay(
        db: Session,
        *,
        run_id: UUID,
        key: str,
        fingerprint: str,
    ) -> PaymentImportBatchReversalResult | None:
        by_key = db.scalar(
            select(PaymentImportBatchReversal).where(
                PaymentImportBatchReversal.idempotency_key == key
            )
        )
        by_run = db.scalar(
            select(PaymentImportBatchReversal).where(
                PaymentImportBatchReversal.import_run_id == run_id
            )
        )
        existing = by_key or by_run
        if existing is None:
            return None
        if (
            existing.import_run_id != run_id
            or existing.idempotency_key != key
            or existing.preview_fingerprint != fingerprint
        ):
            raise HTTPException(
                status_code=409,
                detail="Batch reversal was already confirmed differently",
            )
        return PaymentImportBatchReversalResult(
            batch_reversal=existing, idempotent_replay=True
        )

    @staticmethod
    def confirm(
        db: Session,
        run_id: str | UUID,
        *,
        reason: str,
        preview_fingerprint: str,
        idempotency_key: str,
        actor_id: str | None = None,
    ) -> PaymentImportBatchReversalResult:
        run_uuid = coerce_uuid(run_id)
        key = _normalize_key(idempotency_key)
        replay = PaymentImportBatchReversals._replay(
            db,
            run_id=run_uuid,
            key=key,
            fingerprint=preview_fingerprint,
        )
        if replay:
            return replay

        run = db.scalar(
            select(ImportRun).where(ImportRun.id == run_uuid).with_for_update()
        )
        if run is None:
            raise HTTPException(status_code=404, detail="Import run not found")
        _, rows, _ = _load_run_rows(db, run_uuid)
        for account_id in sorted(
            {str(payment.account_id) for _, payment in rows}, key=str
        ):
            lock_account(db, account_id)

        preview = _build_preview(db, run_uuid, reason)
        if preview.fingerprint != preview_fingerprint:
            raise HTTPException(
                status_code=409,
                detail="Financial state changed after preview; preview the batch again",
            )
        replay = PaymentImportBatchReversals._replay(
            db,
            run_id=run_uuid,
            key=key,
            fingerprint=preview_fingerprint,
        )
        if replay:
            return replay

        batch = PaymentImportBatchReversal(
            import_run_id=run_uuid,
            preview_fingerprint=preview.fingerprint,
            idempotency_key=key,
            reason=preview.reason,
            preview_snapshot=preview.as_snapshot(),
            reversed_payment_count=len(preview.items),
            skipped_reused_count=preview.skipped_reused_count,
            confirmed_by=(actor_id or "").strip() or None,
        )
        db.add(batch)
        try:
            db.flush()
            for expected in preview.items:
                nested = PaymentReversals.preview(
                    db,
                    str(expected.payment_id),
                    PaymentReversalPreviewRequest(reason=preview.reason),
                )
                expected_signature = {
                    "payment_id": str(expected.payment_id),
                    "currency": expected.currency,
                    "reversal_amount": _money(expected.reversal_amount),
                    "account_credit_consumption": _money(
                        expected.account_credit_consumption
                    ),
                    "ledger_entry_type": expected.ledger_entry_type,
                    "ledger_source": expected.ledger_source,
                    "ledger_amount": _money(expected.ledger_amount),
                    "invoice_effects": [
                        {
                            "invoice_id": str(effect["invoice_id"]),
                            "refund_attributed": _money(effect["refund_attributed"]),
                        }
                        for effect in expected.invoice_effects
                    ],
                }
                if _effect_signature(nested) != expected_signature:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "A payment consequence changed while confirming the batch; "
                            "no reversal was committed"
                        ),
                    )
                result = PaymentReversals.process_with_evidence(
                    db,
                    str(expected.payment_id),
                    PaymentReversalRequest(
                        reason=preview.reason,
                        preview_fingerprint=nested.fingerprint,
                        idempotency_key=(
                            f"import-batch-{run_uuid}-{expected.payment_id}"
                        ),
                    ),
                    origin=PaymentReversalOrigin.manual,
                    commit=False,
                )
                actual_preview = result.preview or nested
                item = PaymentImportBatchReversalItem(
                    batch_reversal_id=batch.id,
                    import_run_row_id=expected.import_run_row_id,
                    payment_id=expected.payment_id,
                    payment_settlement_id=expected.payment_settlement_id,
                    payment_reversal_id=result.reversal.id,
                    ledger_entry_id=result.ledger_entry.id,
                    credit_consumption_ledger_entry_id=(
                        result.credit_consumption_ledger_entry.id
                        if result.credit_consumption_ledger_entry
                        else None
                    ),
                    source_snapshot=expected.source_snapshot,
                    result_snapshot={
                        **_effect_signature(actual_preview),
                        "payment_reversal_id": str(result.reversal.id),
                        "ledger_entry_id": str(result.ledger_entry.id),
                        "credit_consumption_ledger_entry_id": (
                            str(result.credit_consumption_ledger_entry.id)
                            if result.credit_consumption_ledger_entry
                            else None
                        ),
                        "nested_preview_fingerprint": actual_preview.fingerprint,
                        "access_consequence": actual_preview.access_consequence,
                    },
                )
                db.add(item)
                db.flush()

            for effect in preview.invoice_effects:
                invoice = db.get(Invoice, coerce_uuid(effect["invoice_id"]))
                if invoice is None or _money(invoice.balance_due) != str(
                    effect["receivable_after"]
                ):
                    raise HTTPException(
                        status_code=409,
                        detail="Confirmed invoice result differs from batch preview",
                    )

            AuditEvents.stage(
                db,
                AuditEventCreate(
                    actor_type=(
                        AuditActorType.user if actor_id else AuditActorType.system
                    ),
                    actor_id=(actor_id or "").strip() or None,
                    action="confirm_payment_import_batch_reversal",
                    entity_type="import_run",
                    entity_id=str(run_uuid),
                    metadata_={
                        "batch_reversal_id": str(batch.id),
                        "preview_fingerprint": preview.fingerprint,
                        "idempotency_key": key,
                        "reversed_payment_count": len(preview.items),
                        "skipped_reused_count": preview.skipped_reused_count,
                        "payment_reversal_ids": [
                            str(item.payment_reversal_id) for item in batch.items
                        ],
                        "ledger_entry_ids": [
                            str(item.ledger_entry_id) for item in batch.items
                        ],
                        "access_consequence": preview.access_consequence,
                    },
                ),
            )
            db.commit()
            db.refresh(batch)
        except Exception:
            db.rollback()
            raise
        return PaymentImportBatchReversalResult(batch_reversal=batch)
