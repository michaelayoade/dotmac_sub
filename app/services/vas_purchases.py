"""VAS purchase engine: catalog, verify, the purchase state machine, requery.

State machine (docs/designs/VTU_BILL_PAYMENTS.md):

    pending -> debited -> submitted -> delivered
                                    -> failed -> refunded (instant wallet credit)
    submitted past the requery cap  -> review (manual queue)

Rules enforced here:
- Never trust the pay response alone — ambiguous outcomes stay `submitted`
  and the requery loop is the source of truth.
- Terminal states are monotonic: a late provider "delivered" can never flip
  a refunded transaction (it flags `review` instead).
- Tokens/PINs are written (encrypted) BEFORE the row is marked delivered.
- Float gate runs before any debit; per-category enablement and per-service
  toggles gate the catalog.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.models.vas import (
    VasEntryCategory,
    VasService,
    VasServiceVariation,
    VasTransaction,
    VasTransactionStatus,
)
from app.services import settings_spec, vtpass
from app.services import vas_wallet as wallet_service
from app.services.credential_crypto import decrypt_credential, encrypt_credential

logger = logging.getLogger(__name__)

# Categories whose ambiguous outcomes must NEVER auto-refund on a timer —
# only a definitive provider "failed" refunds (tokens can arrive hours late).
SLOW_SETTLEMENT_CATEGORIES = {"electricity-bill"}

REQUERY_MAX_ATTEMPTS = 10


def _enabled_categories(db: Session) -> set[str]:
    value = settings_spec.resolve_value(db, SettingDomain.vas, "enabled_categories")
    raw = str(value) if value else "airtime,data"
    return {part.strip() for part in raw.split(",") if part.strip()}


def _txn_limit(db: Session) -> Decimal:
    value = settings_spec.resolve_value(db, SettingDomain.vas, "purchase_txn_limit")
    try:
        return Decimal(str(value)) if value is not None else Decimal("50000")
    except Exception:  # noqa: BLE001
        return Decimal("50000")


def _float_threshold(db: Session) -> Decimal:
    value = settings_spec.resolve_value(db, SettingDomain.vas, "float_min_threshold")
    try:
        return Decimal(str(value)) if value is not None else Decimal("10000")
    except Exception:  # noqa: BLE001
        return Decimal("10000")


# --- Catalog sync -------------------------------------------------------------


def sync_catalog(db: Session) -> dict:
    """Pull VTPass categories/services/variations into local tables.

    Services are created DISABLED — an admin enables services (and the
    category list) deliberately. Variations refresh on every sync.
    """
    stats = {"services": 0, "variations": 0, "errors": 0}
    for category in vtpass.get_service_categories(db):
        identifier = str(category.get("identifier") or "").strip()
        if not identifier:
            continue
        try:
            services = vtpass.get_services(db, identifier)
        except HTTPException:
            stats["errors"] += 1
            continue
        for item in services:
            service_id = str(item.get("serviceID") or "").strip()
            if not service_id:
                continue
            service = db.scalars(
                select(VasService).where(VasService.service_id == service_id)
            ).first()
            if not service:
                service = VasService(service_id=service_id, is_enabled=False)
                db.add(service)
            service.category = identifier
            service.name = str(item.get("name") or service_id)
            service.image_url = item.get("image")
            service.raw = item
            service.synced_at = datetime.now(UTC)
            try:
                if item.get("minimium_amount") or item.get("minimum_amount"):
                    service.min_amount = Decimal(
                        str(item.get("minimium_amount") or item.get("minimum_amount"))
                    )
                if item.get("maximum_amount"):
                    service.max_amount = Decimal(str(item.get("maximum_amount")))
            except Exception:  # noqa: BLE001
                pass
            db.flush()
            stats["services"] += 1
            try:
                content = vtpass.get_variations(db, service_id)
            except HTTPException:
                stats["errors"] += 1
                continue
            for var in content.get("varations") or content.get("variations") or []:
                code = str(var.get("variation_code") or "").strip()
                if not code:
                    continue
                variation = db.scalars(
                    select(VasServiceVariation).where(
                        VasServiceVariation.service_pk == service.id,
                        VasServiceVariation.code == code,
                    )
                ).first()
                if not variation:
                    variation = VasServiceVariation(service_pk=service.id, code=code)
                    db.add(variation)
                variation.name = str(var.get("name") or code)
                try:
                    variation.amount = (
                        Decimal(str(var.get("variation_amount")))
                        if var.get("variation_amount") not in (None, "")
                        else None
                    )
                except Exception:  # noqa: BLE001
                    variation.amount = None
                variation.raw = var
                stats["variations"] += 1
    db.commit()
    return stats


def customer_catalog(db: Session) -> list[dict]:
    """Enabled categories -> enabled services -> enabled variations."""
    wallet_service.require_enabled(db)
    categories = _enabled_categories(db)
    services = (
        db.query(VasService)
        .filter(VasService.is_enabled.is_(True), VasService.category.in_(categories))
        .order_by(VasService.category, VasService.name)
        .all()
    )
    out: dict[str, dict] = {}
    for service in services:
        bucket = out.setdefault(
            service.category, {"category": service.category, "services": []}
        )
        bucket["services"].append(
            {
                "service_id": service.service_id,
                "name": service.name,
                "image_url": service.image_url,
                "identifier_label": service.identifier_label or "Phone number",
                "requires_verify": service.requires_verify,
                "min_amount": service.min_amount,
                "max_amount": service.max_amount,
                "variations": [
                    {
                        "code": variation.code,
                        "name": variation.name,
                        "amount": variation.amount,
                    }
                    for variation in service.variations
                    if variation.is_enabled
                ],
            }
        )
    return list(out.values())


def _get_enabled_service(db: Session, service_id: str) -> VasService:
    service = db.scalars(
        select(VasService).where(VasService.service_id == service_id)
    ).first()
    if (
        not service
        or not service.is_enabled
        or service.category not in _enabled_categories(db)
    ):
        raise HTTPException(status_code=404, detail="Service not available")
    return service


def verify_identifier(
    db: Session, *, service_id: str, identifier: str, variation_type: str | None = None
) -> dict:
    wallet_service.require_enabled(db)
    service = _get_enabled_service(db, service_id)
    content = vtpass.verify_merchant(
        db,
        service_id=service.service_id,
        billers_code=identifier,
        variation_type=variation_type,
    )
    return {
        "customer_name": content.get("Customer_Name") or content.get("customer_name"),
        "address": content.get("Address") or content.get("address"),
        "raw_status": content.get("Status"),
    }


# --- Purchase -----------------------------------------------------------------


def _float_gate(db: Session, amount: Decimal) -> None:
    """Refuse before debit when the provider float can't cover this purchase."""
    try:
        balance = vtpass.get_balance(db)
    except HTTPException:
        # Provider unreachable — fail closed: no debit without fulfillment path.
        raise HTTPException(
            status_code=503, detail="Bill payments are temporarily unavailable"
        ) from None
    if balance - amount < _float_threshold(db):
        logger.error(
            "vas float gate tripped: balance=%s amount=%s threshold=%s",
            balance,
            amount,
            _float_threshold(db),
        )
        raise HTTPException(
            status_code=503, detail="This service is temporarily unavailable"
        )


def _resolve_amount(
    service: VasService, variation: VasServiceVariation | None, amount: Decimal | None
) -> Decimal:
    if variation is not None and variation.amount and variation.amount > 0:
        return Decimal(str(variation.amount))
    if amount is None or amount <= 0:
        raise HTTPException(status_code=400, detail="Amount is required")
    value = Decimal(str(amount)).quantize(Decimal("0.01"))
    if service.min_amount and value < service.min_amount:
        raise HTTPException(
            status_code=400, detail=f"Minimum amount is {service.min_amount:.0f}"
        )
    if service.max_amount and service.max_amount > 0 and value > service.max_amount:
        raise HTTPException(
            status_code=400, detail=f"Maximum amount is {service.max_amount:.0f}"
        )
    return value


def _extract_token(body: dict) -> str | None:
    candidates = [body.get("purchased_code"), body.get("Token"), body.get("token")]
    content = body.get("content")
    if isinstance(content, dict):
        transactions = content.get("transactions")
        if isinstance(transactions, dict):
            candidates.append(transactions.get("purchased_code"))
        candidates.append(content.get("purchased_code"))
    mainToken = body.get("mainToken")
    if mainToken:
        candidates.append(mainToken)
    for value in candidates:
        if value:
            return str(value)
    return None


def _provider_outcome(body: dict) -> tuple[str, str]:
    """Map a pay/requery body to ('delivered'|'processing'|'failed', detail)."""
    code = str(body.get("code") or "")
    content = body.get("content")
    status = ""
    if isinstance(content, dict):
        transactions = content.get("transactions")
        if isinstance(transactions, dict):
            status = str(transactions.get("status") or "")
    if code == vtpass.CODE_DELIVERED and status.lower() == "delivered":
        return "delivered", status
    if code == vtpass.CODE_PROCESSING or status.lower() in {"pending", "initiated"}:
        return "processing", status or code
    if code == vtpass.CODE_DELIVERED and status.lower() in {"", "processed"}:
        # 000 without an explicit delivered status — keep polling.
        return "processing", status or code
    detail = str(body.get("response_description") or status or f"code {code}")
    return "failed", detail


def purchase(
    db: Session,
    *,
    subscriber_id: str,
    service_id: str,
    identifier: str,
    variation_code: str | None = None,
    amount: Decimal | None = None,
    phone: str | None = None,
) -> VasTransaction:
    wallet_service.require_enabled(db)
    service = _get_enabled_service(db, service_id)
    variation = None
    if variation_code:
        variation = db.scalars(
            select(VasServiceVariation).where(
                VasServiceVariation.service_pk == service.id,
                VasServiceVariation.code == variation_code,
                VasServiceVariation.is_enabled.is_(True),
            )
        ).first()
        if not variation:
            raise HTTPException(status_code=400, detail="Unknown plan selected")

    value = _resolve_amount(service, variation, amount)
    if value > _txn_limit(db):
        raise HTTPException(
            status_code=400, detail="Amount is above the per-purchase limit"
        )
    identifier = identifier.strip()
    if not identifier:
        raise HTTPException(status_code=400, detail="Customer number is required")

    _float_gate(db, value)

    wallet = wallet_service.get_or_create_wallet(db, subscriber_id)
    request_id = vtpass.generate_request_id()
    txn = VasTransaction(
        wallet_id=wallet.id,
        subscriber_id=wallet.subscriber_id,
        service_pk=service.id,
        variation_code=variation.code if variation else None,
        identifier=identifier,
        amount=value,
        request_id=request_id,
        status=VasTransactionStatus.pending,
    )
    db.add(txn)
    db.commit()
    db.refresh(txn)

    # Debit (immediate; refund-on-failure) — insufficient funds aborts cleanly.
    wallet_service.debit_wallet(
        db,
        wallet,
        amount=value,
        category=VasEntryCategory.purchase,
        reference=f"vas-{txn.request_id}",
        memo=f"{service.name}",
    )
    txn.status = VasTransactionStatus.debited
    db.commit()

    try:
        body = vtpass.pay(
            db,
            request_id=request_id,
            service_id=service.service_id,
            billers_code=identifier,
            variation_code=txn.variation_code,
            amount=value,
            phone=phone or identifier,
        )
    except HTTPException:
        # Transport-level ambiguity: the provider may or may not have seen the
        # request — do NOT refund; let the requery loop find the truth.
        txn.status = VasTransactionStatus.submitted
        txn.error = "Provider unreachable at submit; awaiting requery"
        db.commit()
        return txn

    txn.provider_response = body
    outcome, detail = _provider_outcome(body)
    txn.provider_status = detail
    if outcome == "delivered":
        _mark_delivered(db, txn, body)
    elif outcome == "processing":
        txn.status = VasTransactionStatus.submitted
    else:
        _mark_failed_and_refund(db, txn, detail)
    db.commit()
    db.refresh(txn)
    return txn


def _mark_delivered(db: Session, txn: VasTransaction, body: dict) -> None:
    token = _extract_token(body)
    if token:
        # Write-ahead: token persists before the delivered flag.
        txn.token_encrypted = encrypt_credential(token)
        db.commit()
    txn.status = VasTransactionStatus.delivered
    txn.delivered_at = datetime.now(UTC)


def _mark_failed_and_refund(db: Session, txn: VasTransaction, detail: str) -> None:
    txn.status = VasTransactionStatus.failed
    txn.error = detail
    db.commit()
    wallet_service.credit_wallet(
        db,
        txn.wallet,
        amount=Decimal(str(txn.amount)),
        category=VasEntryCategory.purchase_refund,
        reference=f"vas-refund-{txn.request_id}",
        memo="Refund: purchase could not be delivered",
    )
    txn.status = VasTransactionStatus.refunded
    txn.refunded_at = datetime.now(UTC)


def transaction_token(txn: VasTransaction) -> str | None:
    if not txn.token_encrypted:
        return None
    try:
        return decrypt_credential(txn.token_encrypted)
    except Exception:  # noqa: BLE001
        logger.error("vas token decrypt failed for txn %s", txn.id)
        return None


def list_transactions(
    db: Session, subscriber_id: str, *, limit: int = 50
) -> list[VasTransaction]:
    wallet_service.require_enabled(db)
    wallet = wallet_service.get_or_create_wallet(db, subscriber_id)
    return (
        db.query(VasTransaction)
        .filter(VasTransaction.wallet_id == wallet.id)
        .order_by(VasTransaction.created_at.desc())
        .limit(limit)
        .all()
    )


def get_transaction(db: Session, subscriber_id: str, txn_id: str) -> VasTransaction:
    wallet_service.require_enabled(db)
    wallet = wallet_service.get_or_create_wallet(db, subscriber_id)
    txn = db.get(VasTransaction, txn_id)
    if not txn or txn.wallet_id != wallet.id:
        raise HTTPException(status_code=404, detail="Transaction not found")
    return txn


# --- Requery loop (Celery) ------------------------------------------------------


def run_requery_sweep(db: Session) -> dict:
    """Resolve `submitted` transactions via the requery endpoint.

    Slow-settlement categories never auto-refund on attempt exhaustion —
    they park in `review` (manual queue) instead, like everything else.
    A definitive provider 'failed' refunds immediately regardless.
    """
    if not wallet_service.is_enabled(db):
        return {"status": "disabled"}
    delivered = refunded = review = checked = 0
    pending = (
        db.query(VasTransaction)
        .filter(VasTransaction.status == VasTransactionStatus.submitted)
        .order_by(VasTransaction.created_at)
        .limit(200)
        .all()
    )
    for txn in pending:
        checked += 1
        try:
            body = vtpass.requery(db, txn.request_id)
        except HTTPException:
            continue  # provider unreachable; try next sweep
        txn.requery_attempts = (txn.requery_attempts or 0) + 1
        txn.provider_response = body
        outcome, detail = _provider_outcome(body)
        txn.provider_status = detail
        if outcome == "delivered":
            _mark_delivered(db, txn, body)
            delivered += 1
        elif outcome == "failed":
            _mark_failed_and_refund(db, txn, detail)
            refunded += 1
        elif txn.requery_attempts >= REQUERY_MAX_ATTEMPTS:
            txn.status = VasTransactionStatus.review
            txn.error = "Requery attempts exhausted — needs manual review"
            review += 1
        db.commit()
    return {
        "status": "ok",
        "delivered": delivered,
        "refunded": refunded,
        "review": review,
        "checked": checked,
    }
