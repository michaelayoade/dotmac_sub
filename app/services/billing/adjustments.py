"""Owner for previewed debits against prepaid account funding.

The ledger remains the append-only transaction writer. This owner decides why
an adjustment is allowed, locks and recomputes its preview, records idempotency
and audit evidence, and links the decision to the exact ledger transaction.
Credits are deliberately excluded: customer account credits belong to
``financial.credit_notes``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.billing import (
    AccountAdjustment,
    LedgerCategory,
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
)
from app.models.subscriber import Subscriber
from app.schemas.audit import AuditEventCreate
from app.schemas.billing import (
    AccountAdjustmentConfirm,
    AccountAdjustmentPreviewRequest,
    AccountAdjustmentReversalConfirm,
    AccountAdjustmentReversalPreviewRequest,
    LedgerEntryCreate,
)
from app.services.audit import AuditEvents
from app.services.billing._common import get_account_credit_balance, lock_account
from app.services.billing.ledger import LedgerEntries
from app.services.common import coerce_uuid, round_money
from app.services.customer_financial_position import get_customer_financial_position

_ORIGINS = {"manual", "addon_purchase", "prepaid_plan_change"}


def _fingerprint(kind: str, **values: object) -> str:
    normalized = {
        key: f"{value:.2f}" if isinstance(value, Decimal) else str(value)
        for key, value in values.items()
    }
    payload = json.dumps(
        {"kind": kind, **normalized}, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _text(value: str, field: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise HTTPException(status_code=400, detail=f"{field} is required")
    return normalized


@dataclass(frozen=True)
class AccountAdjustmentPreview:
    account_id: UUID
    category: LedgerCategory
    amount: Decimal
    currency: str
    memo: str
    reason: str
    origin: str
    origin_ref: str | None
    prepaid_funding_before: Decimal
    prepaid_funding_after: Decimal
    postpaid_receivables: Decimal
    collection_blocking_balance: Decimal
    shortfall: Decimal
    allowed: bool
    rejection_reason: str | None
    fingerprint: str
    ledger_entry_type: LedgerEntryType = LedgerEntryType.debit
    ledger_source: LedgerSource = LedgerSource.adjustment
    access_consequence: str = "none_adjustment_only"

    @property
    def ledger_amount(self) -> Decimal:
        return self.amount

    def as_dict(self) -> dict[str, object]:
        return {
            **self.__dict__,
            "ledger_amount": self.ledger_amount,
        }


@dataclass(frozen=True)
class AccountAdjustmentResult:
    adjustment: AccountAdjustment
    ledger_entry: LedgerEntry
    preview: AccountAdjustmentPreview
    replayed: bool = False


@dataclass(frozen=True)
class AccountAdjustmentReversalPreview:
    adjustment_id: UUID
    account_id: UUID
    amount: Decimal
    currency: str
    prepaid_funding_before: Decimal
    prepaid_funding_after: Decimal
    reverses_ledger_entry_id: UUID
    reason: str
    fingerprint: str
    ledger_entry_type: LedgerEntryType = LedgerEntryType.credit
    ledger_source: LedgerSource = LedgerSource.adjustment
    access_consequence: str = "none_adjustment_reversal_only"

    def as_dict(self) -> dict[str, object]:
        return self.__dict__


@dataclass(frozen=True)
class AccountAdjustmentReversalResult:
    adjustment: AccountAdjustment
    ledger_entry: LedgerEntry
    preview: AccountAdjustmentReversalPreview
    replayed: bool = False


def _stored_preview(adjustment: AccountAdjustment) -> AccountAdjustmentPreview:
    return AccountAdjustmentPreview(
        account_id=adjustment.account_id,
        category=adjustment.category,
        amount=round_money(adjustment.amount),
        currency=adjustment.currency,
        memo=adjustment.memo,
        reason=adjustment.reason,
        origin=adjustment.origin,
        origin_ref=adjustment.origin_ref,
        prepaid_funding_before=round_money(adjustment.prepaid_funding_before),
        prepaid_funding_after=round_money(adjustment.prepaid_funding_after),
        postpaid_receivables=round_money(adjustment.postpaid_receivables),
        collection_blocking_balance=round_money(adjustment.collection_blocking_balance),
        shortfall=Decimal("0.00"),
        allowed=True,
        rejection_reason=None,
        fingerprint=adjustment.preview_fingerprint,
        access_consequence=adjustment.access_consequence,
    )


def _validate_origin(origin: str) -> str:
    if origin not in _ORIGINS:
        raise HTTPException(status_code=400, detail="Unsupported adjustment origin")
    return origin


def _existing_for_key(
    db: Session, *, origin: str, idempotency_key: str
) -> AccountAdjustment | None:
    return db.scalar(
        select(AccountAdjustment).where(
            AccountAdjustment.origin == origin,
            AccountAdjustment.idempotency_key == idempotency_key,
        )
    )


def _stage_audit(
    db: Session,
    *,
    adjustment: AccountAdjustment,
    action: str,
    actor_type: AuditActorType,
    actor_id: str | None,
    metadata: dict[str, object],
) -> None:
    AuditEvents.stage(
        db,
        AuditEventCreate(
            actor_type=actor_type,
            actor_id=actor_id,
            action=action,
            entity_type="account_adjustment",
            entity_id=str(adjustment.id),
            metadata_=metadata,
        ),
    )


class AccountAdjustments:
    @staticmethod
    def preview(
        db: Session,
        payload: AccountAdjustmentPreviewRequest,
        *,
        origin: str = "manual",
        origin_ref: str | None = None,
    ) -> AccountAdjustmentPreview:
        origin = _validate_origin(origin)
        account_id = coerce_uuid(payload.account_id)
        if db.get(Subscriber, account_id) is None:
            raise HTTPException(status_code=404, detail="Subscriber account not found")

        amount = round_money(payload.amount)
        if amount <= Decimal("0.00"):
            raise HTTPException(
                status_code=400, detail="Adjustment amount must be positive"
            )
        currency = str(payload.currency or "NGN").strip().upper()
        memo = _text(payload.memo, "memo")
        reason = _text(payload.reason, "reason")
        normalized_ref = (origin_ref or "").strip() or None

        position = get_customer_financial_position(db, account_id)
        funding_before = round_money(
            get_account_credit_balance(db, str(account_id), currency=currency)
        )
        shortfall = round_money(max(Decimal("0.00"), amount - funding_before))
        funding_after = round_money(funding_before - amount)
        allowed = shortfall == Decimal("0.00")
        rejection_reason = None if allowed else "insufficient_prepaid_funding"
        fingerprint = _fingerprint(
            "account_adjustment",
            account_id=account_id,
            category=payload.category.value,
            amount=amount,
            currency=currency,
            memo=memo,
            reason=reason,
            origin=origin,
            origin_ref=normalized_ref or "",
            prepaid_funding_before=funding_before,
            prepaid_funding_after=funding_after,
            postpaid_receivables=round_money(position.open_invoice_balance),
            collection_blocking_balance=round_money(
                position.collection_blocking_balance
            ),
            allowed=allowed,
        )
        return AccountAdjustmentPreview(
            account_id=account_id,
            category=payload.category,
            amount=amount,
            currency=currency,
            memo=memo,
            reason=reason,
            origin=origin,
            origin_ref=normalized_ref,
            prepaid_funding_before=funding_before,
            prepaid_funding_after=funding_after,
            postpaid_receivables=round_money(position.open_invoice_balance),
            collection_blocking_balance=round_money(
                position.collection_blocking_balance
            ),
            shortfall=shortfall,
            allowed=allowed,
            rejection_reason=rejection_reason,
            fingerprint=fingerprint,
        )

    @staticmethod
    def confirm(
        db: Session,
        payload: AccountAdjustmentConfirm,
        *,
        origin: str = "manual",
        origin_ref: str | None = None,
        actor_type: AuditActorType = AuditActorType.system,
        actor_id: str | None = None,
        commit: bool = True,
    ) -> AccountAdjustmentResult:
        origin = _validate_origin(origin)
        existing = _existing_for_key(
            db, origin=origin, idempotency_key=payload.idempotency_key
        )
        if existing is not None:
            if str(existing.account_id) != str(payload.account_id):
                raise HTTPException(
                    status_code=409, detail="Idempotency key belongs to another account"
                )
            if existing.preview_fingerprint != payload.preview_fingerprint:
                raise HTTPException(
                    status_code=409,
                    detail="Idempotency key was used for another adjustment preview",
                )
            return AccountAdjustmentResult(
                adjustment=existing,
                ledger_entry=existing.ledger_entry,
                preview=_stored_preview(existing),
                replayed=True,
            )

        lock_account(db, str(payload.account_id))
        preview = AccountAdjustments.preview(
            db,
            AccountAdjustmentPreviewRequest(
                **payload.model_dump(exclude={"preview_fingerprint", "idempotency_key"})
            ),
            origin=origin,
            origin_ref=origin_ref,
        )
        if preview.fingerprint != payload.preview_fingerprint:
            raise HTTPException(
                status_code=409,
                detail="Financial state changed after preview; preview again",
            )
        if not preview.allowed:
            raise HTTPException(
                status_code=402,
                detail={
                    "code": preview.rejection_reason,
                    "message": "Insufficient prepaid funding for this adjustment",
                    "shortfall": str(preview.shortfall),
                    "currency": preview.currency,
                },
            )
        existing = _existing_for_key(
            db, origin=origin, idempotency_key=payload.idempotency_key
        )
        if existing is not None:
            if existing.preview_fingerprint != payload.preview_fingerprint:
                raise HTTPException(
                    status_code=409,
                    detail="Idempotency key was used for another adjustment preview",
                )
            return AccountAdjustmentResult(
                adjustment=existing,
                ledger_entry=existing.ledger_entry,
                preview=preview,
                replayed=True,
            )

        try:
            entry = LedgerEntries.create(
                db,
                LedgerEntryCreate(
                    account_id=preview.account_id,
                    entry_type=LedgerEntryType.debit,
                    source=LedgerSource.adjustment,
                    category=preview.category,
                    amount=preview.amount,
                    currency=preview.currency,
                    memo=preview.memo,
                ),
                commit=False,
            )
            adjustment = AccountAdjustment(
                account_id=preview.account_id,
                category=preview.category,
                amount=preview.amount,
                currency=preview.currency,
                memo=preview.memo,
                reason=preview.reason,
                origin=preview.origin,
                origin_ref=preview.origin_ref,
                prepaid_funding_before=preview.prepaid_funding_before,
                prepaid_funding_after=preview.prepaid_funding_after,
                postpaid_receivables=preview.postpaid_receivables,
                collection_blocking_balance=preview.collection_blocking_balance,
                access_consequence=preview.access_consequence,
                preview_fingerprint=preview.fingerprint,
                idempotency_key=payload.idempotency_key,
                ledger_entry_id=entry.id,
            )
            db.add(adjustment)
            db.flush()
            _stage_audit(
                db,
                adjustment=adjustment,
                action="confirm",
                actor_type=actor_type,
                actor_id=actor_id,
                metadata={
                    "origin": preview.origin,
                    "origin_ref": preview.origin_ref,
                    "ledger_entry_id": str(entry.id),
                    "ledger_entry_type": LedgerEntryType.debit.value,
                    "ledger_source": LedgerSource.adjustment.value,
                    "amount": str(preview.amount),
                    "currency": preview.currency,
                    "prepaid_funding_before": str(preview.prepaid_funding_before),
                    "prepaid_funding_after": str(preview.prepaid_funding_after),
                    "postpaid_receivables": str(preview.postpaid_receivables),
                    "preview_fingerprint": preview.fingerprint,
                    "access_consequence": preview.access_consequence,
                },
            )
            if commit:
                db.commit()
                db.refresh(adjustment)
                db.refresh(entry)
        except IntegrityError:
            if not commit:
                raise
            db.rollback()
            existing = _existing_for_key(
                db, origin=origin, idempotency_key=payload.idempotency_key
            )
            if (
                existing is not None
                and str(existing.account_id) == str(payload.account_id)
                and existing.preview_fingerprint == payload.preview_fingerprint
            ):
                return AccountAdjustmentResult(
                    adjustment=existing,
                    ledger_entry=existing.ledger_entry,
                    preview=_stored_preview(existing),
                    replayed=True,
                )
            raise
        except Exception:
            if commit:
                db.rollback()
            raise
        return AccountAdjustmentResult(
            adjustment=adjustment,
            ledger_entry=entry,
            preview=preview,
        )

    @staticmethod
    def confirm_system(
        db: Session,
        payload: AccountAdjustmentPreviewRequest,
        *,
        origin: str,
        origin_ref: str | None,
        idempotency_key: str,
        commit: bool = False,
    ) -> AccountAdjustmentResult:
        preview = AccountAdjustments.preview(
            db, payload, origin=origin, origin_ref=origin_ref
        )
        return AccountAdjustments.confirm(
            db,
            AccountAdjustmentConfirm(
                **payload.model_dump(),
                preview_fingerprint=preview.fingerprint,
                idempotency_key=idempotency_key,
            ),
            origin=origin,
            origin_ref=origin_ref,
            commit=commit,
        )

    @staticmethod
    def preview_reversal(
        db: Session,
        adjustment_id: str,
        payload: AccountAdjustmentReversalPreviewRequest,
    ) -> AccountAdjustmentReversalPreview:
        adjustment = db.get(AccountAdjustment, coerce_uuid(adjustment_id))
        if adjustment is None:
            raise HTTPException(status_code=404, detail="Account adjustment not found")
        if adjustment.reversal_ledger_entry_id is not None:
            raise HTTPException(
                status_code=409, detail="Adjustment is already reversed"
            )
        reason = _text(payload.reason, "reason")
        funding_before = round_money(
            get_account_credit_balance(
                db, str(adjustment.account_id), currency=adjustment.currency
            )
        )
        funding_after = round_money(funding_before + adjustment.amount)
        fingerprint = _fingerprint(
            "account_adjustment_reversal",
            adjustment_id=adjustment.id,
            account_id=adjustment.account_id,
            amount=adjustment.amount,
            currency=adjustment.currency,
            prepaid_funding_before=funding_before,
            prepaid_funding_after=funding_after,
            reverses_ledger_entry_id=adjustment.ledger_entry_id,
            reason=reason,
        )
        return AccountAdjustmentReversalPreview(
            adjustment_id=adjustment.id,
            account_id=adjustment.account_id,
            amount=round_money(adjustment.amount),
            currency=adjustment.currency,
            prepaid_funding_before=funding_before,
            prepaid_funding_after=funding_after,
            reverses_ledger_entry_id=adjustment.ledger_entry_id,
            reason=reason,
            fingerprint=fingerprint,
        )

    @staticmethod
    def confirm_reversal(
        db: Session,
        adjustment_id: str,
        payload: AccountAdjustmentReversalConfirm,
        *,
        actor_type: AuditActorType = AuditActorType.system,
        actor_id: str | None = None,
        commit: bool = True,
    ) -> AccountAdjustmentReversalResult:
        unlocked = db.get(AccountAdjustment, coerce_uuid(adjustment_id))
        if unlocked is None:
            raise HTTPException(status_code=404, detail="Account adjustment not found")
        reused = db.scalar(
            select(AccountAdjustment).where(
                AccountAdjustment.origin == unlocked.origin,
                AccountAdjustment.reversal_idempotency_key == payload.idempotency_key,
            )
        )
        if reused is not None:
            if str(reused.id) != str(adjustment_id):
                raise HTTPException(
                    status_code=409,
                    detail="Reversal idempotency key belongs to another adjustment",
                )
            if reused.reversal_preview_fingerprint != payload.preview_fingerprint:
                raise HTTPException(
                    status_code=409,
                    detail="Idempotency key was used for another reversal preview",
                )
            if (
                reused.reversal_prepaid_funding_before is None
                or reused.reversal_prepaid_funding_after is None
                or reused.reversal_ledger_entry is None
            ):
                raise HTTPException(
                    status_code=409,
                    detail="Adjustment reversal evidence is incomplete",
                )
            preview = AccountAdjustmentReversalPreview(
                adjustment_id=reused.id,
                account_id=reused.account_id,
                amount=round_money(reused.amount),
                currency=reused.currency,
                prepaid_funding_before=round_money(
                    reused.reversal_prepaid_funding_before
                ),
                prepaid_funding_after=round_money(
                    reused.reversal_prepaid_funding_after
                ),
                reverses_ledger_entry_id=reused.ledger_entry_id,
                reason=reused.reversal_reason or payload.reason,
                fingerprint=reused.reversal_preview_fingerprint or "",
            )
            return AccountAdjustmentReversalResult(
                adjustment=reused,
                ledger_entry=reused.reversal_ledger_entry,
                preview=preview,
                replayed=True,
            )

        lock_account(db, str(unlocked.account_id))
        adjustment = db.scalar(
            select(AccountAdjustment)
            .where(AccountAdjustment.id == coerce_uuid(adjustment_id))
            .with_for_update()
        )
        if adjustment is None:
            raise HTTPException(status_code=404, detail="Account adjustment not found")
        preview = AccountAdjustments.preview_reversal(
            db,
            adjustment_id,
            AccountAdjustmentReversalPreviewRequest(reason=payload.reason),
        )
        if preview.fingerprint != payload.preview_fingerprint:
            raise HTTPException(
                status_code=409,
                detail="Financial state changed after preview; preview again",
            )
        try:
            reversal = LedgerEntries.reverse(
                db,
                str(adjustment.ledger_entry_id),
                memo=f"{preview.reason} [account adjustment {adjustment.id}]",
                commit=False,
            )
            adjustment.reversal_ledger_entry_id = reversal.id
            adjustment.reversal_preview_fingerprint = preview.fingerprint
            adjustment.reversal_idempotency_key = payload.idempotency_key
            adjustment.reversal_reason = preview.reason
            adjustment.reversal_prepaid_funding_before = preview.prepaid_funding_before
            adjustment.reversal_prepaid_funding_after = preview.prepaid_funding_after
            adjustment.reversed_at = datetime.now(UTC)
            db.flush()
            _stage_audit(
                db,
                adjustment=adjustment,
                action="reverse",
                actor_type=actor_type,
                actor_id=actor_id,
                metadata={
                    "ledger_entry_id": str(reversal.id),
                    "reverses_ledger_entry_id": str(adjustment.ledger_entry_id),
                    "amount": str(preview.amount),
                    "currency": preview.currency,
                    "prepaid_funding_before": str(preview.prepaid_funding_before),
                    "prepaid_funding_after": str(preview.prepaid_funding_after),
                    "preview_fingerprint": preview.fingerprint,
                    "access_consequence": preview.access_consequence,
                },
            )
            if commit:
                db.commit()
                db.refresh(adjustment)
                db.refresh(reversal)
        except Exception:
            if commit:
                db.rollback()
            raise
        return AccountAdjustmentReversalResult(
            adjustment=adjustment,
            ledger_entry=reversal,
            preview=preview,
        )


account_adjustments = AccountAdjustments()

__all__ = [
    "AccountAdjustmentPreview",
    "AccountAdjustmentResult",
    "AccountAdjustmentReversalPreview",
    "AccountAdjustmentReversalResult",
    "AccountAdjustments",
    "account_adjustments",
]
