"""Committed command boundary for admin VAS mutations.

VAS purchase and wallet services retain their focused state-machine and ledger
rules. This module owns admin command validation, orchestration, and transaction
outcomes so the web route remains an HTTP adapter rather than a parallel writer.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import TypeVar

from sqlalchemy.orm import Session

from app.models.subscription_engine import SettingValueType
from app.models.vas import (
    VasPartyType,
    VasRefundStatus,
    VasTransactionStatus,
)
from app.schemas.settings import DomainSettingUpdate
from app.services import vas_purchases, vas_refunds, vas_wallet
from app.services.domain_settings import vas_settings

T = TypeVar("T")


class VasAdminCommandError(ValueError):
    """An admin command rejection that is safe to render in the UI."""


class VasAdminResourceNotFound(VasAdminCommandError):
    """The command target does not exist in the required mutable state."""


@dataclass(frozen=True)
class RefundToSourceOutcome:
    request_id: str
    entry_id: str
    provider: str
    reference: str
    amount: Decimal
    status: VasRefundStatus
    already_requested: bool


def _commit(db: Session, action: Callable[[], T]) -> T:
    try:
        result = action()
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise


def toggle_service(db: Session, *, service_pk: str) -> bool:
    """Toggle one catalog service and commit the resulting state."""

    def action() -> bool:
        service = vas_purchases.get_service(db, service_pk)
        if service is None:
            raise VasAdminResourceNotFound("Service not found")
        service.is_enabled = not service.is_enabled
        return service.is_enabled

    return _commit(db, action)


def run_auto_deduct(db: Session) -> dict:
    """Run the wallet owner's idempotent auto-deduction sweep."""

    return vas_wallet.run_auto_deduct_sweep(db)


def set_categories(db: Session, *, enabled_categories: str) -> None:
    cleaned = ",".join(
        part.strip().lower() for part in enabled_categories.split(",") if part.strip()
    )
    vas_settings.upsert_by_key(
        db,
        "enabled_categories",
        DomainSettingUpdate(
            value_type=SettingValueType.string,
            value_text=cleaned,
        ),
    )


def add_rate_card(
    db: Session,
    *,
    category: str,
    party_type: str,
    rate_pct: str,
    effective_from: str = "",
    memo: str = "",
) -> None:
    try:
        rate = Decimal(rate_pct.strip())
        party = VasPartyType(party_type.strip())
    except (InvalidOperation, ValueError) as exc:
        raise VasAdminCommandError("Invalid rate card values") from exc

    effective = datetime.now(UTC)
    if effective_from.strip():
        try:
            effective = datetime.fromisoformat(effective_from.strip()).replace(
                tzinfo=UTC
            )
        except ValueError as exc:
            raise VasAdminCommandError("Invalid effective date") from exc

    vas_purchases.add_rate_card(
        db,
        category=category.strip().lower(),
        party=party,
        rate_pct=rate,
        effective_from=effective,
        memo=memo.strip() or None,
    )


def resolve_review_refund(db: Session, *, txn_id: str) -> None:
    """Resolve a parked purchase as failed and restore its wallet funds."""

    def action() -> None:
        txn = vas_purchases.get_transaction_by_id(db, txn_id)
        if txn is None or txn.status != VasTransactionStatus.review:
            raise VasAdminResourceNotFound("Reviewable transaction not found")
        vas_purchases._mark_failed_and_refund(
            db,
            txn,
            "Manually resolved: refunded",
        )

    _commit(db, action)


def resolve_review_delivered(
    db: Session,
    *,
    txn_id: str,
    token: str = "",
) -> None:
    """Resolve a parked purchase as delivered, with an optional token."""

    def action() -> None:
        txn = vas_purchases.get_transaction_by_id(db, txn_id)
        if txn is None or txn.status != VasTransactionStatus.review:
            raise VasAdminResourceNotFound("Reviewable transaction not found")
        body: dict[str, object] = {"manually_resolved": True}
        if token.strip():
            body["purchased_code"] = token.strip()
        vas_purchases._mark_delivered(db, txn, body)
        if txn.status == VasTransactionStatus.delivered:
            txn.provider_status = "Manually resolved: delivered"

    _commit(db, action)


def refund_to_source(db: Session, *, entry_id: str) -> RefundToSourceOutcome:
    """Delegate refund eligibility and lifecycle to the refund owner."""
    try:
        outcome = vas_refunds.request_refund(db, entry_id=entry_id)
    except vas_refunds.VasRefundError as exc:
        raise VasAdminCommandError(str(exc)) from exc
    if outcome.status == VasRefundStatus.failed:
        raise VasAdminCommandError(
            "Gateway refund failed; the wallet reservation was restored"
        )
    return RefundToSourceOutcome(
        request_id=outcome.request_id,
        entry_id=outcome.entry_id,
        provider=outcome.provider,
        reference=outcome.reference,
        amount=outcome.amount,
        status=outcome.status,
        already_requested=outcome.already_requested,
    )
