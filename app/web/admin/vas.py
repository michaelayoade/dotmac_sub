"""Admin VAS operations: service toggles, rate cards, review queue, refunds."""

import logging
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.subscription_engine import SettingValueType
from app.models.vas import (
    VasEntryCategory,
    VasEntryType,
    VasPartyType,
    VasRateCard,
    VasService,
    VasTransaction,
    VasTransactionStatus,
    VasWallet,
    VasWalletEntry,
)
from app.services import vas_purchases, vas_wallet, vtpass
from app.services.auth_dependencies import require_permission
from app.services.domain_settings import vas_settings

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/vas", tags=["web-admin-vas"])

logger = logging.getLogger(__name__)


def _context(request: Request, db: Session, **extra) -> dict:
    from app.web.admin import get_current_user, get_sidebar_stats

    return {
        "request": request,
        "active_page": "vas",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
        **extra,
    }


@router.get(
    "",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:read"))],
)
def vas_admin_page(request: Request, db: Session = Depends(get_db)):
    float_balance = None
    float_error = None
    try:
        float_balance = vtpass.get_balance(db)
    except HTTPException as exc:
        float_error = str(exc.detail)

    services = db.query(VasService).order_by(VasService.category, VasService.name).all()
    review_queue = (
        db.query(VasTransaction)
        .filter(VasTransaction.status == VasTransactionStatus.review)
        .order_by(VasTransaction.created_at)
        .limit(100)
        .all()
    )
    rate_cards = (
        db.query(VasRateCard)
        .order_by(
            VasRateCard.category,
            VasRateCard.party_type,
            VasRateCard.effective_from.desc(),
        )
        .limit(200)
        .all()
    )
    from app.models.domain_settings import SettingDomain
    from app.services import settings_spec

    enabled_categories = str(
        settings_spec.resolve_value(db, SettingDomain.vas, "enabled_categories")
        or "airtime,data"
    )
    return templates.TemplateResponse(
        "admin/system/vas.html",
        _context(
            request,
            db,
            vas_enabled=vas_wallet.is_enabled(db),
            float_balance=float_balance,
            float_error=float_error,
            services=services,
            review_queue=review_queue,
            rate_cards=rate_cards,
            enabled_categories=enabled_categories,
            categories=sorted({service.category for service in services}),
            submitted=request.query_params.get("ok"),
            form_error=request.query_params.get("error"),
        ),
    )


@router.post(
    "/services/{service_pk}/toggle",
    dependencies=[Depends(require_permission("billing:write"))],
)
def vas_toggle_service(
    service_pk: str, db: Session = Depends(get_db)
) -> RedirectResponse:
    service = db.get(VasService, service_pk)
    if not service:
        raise HTTPException(status_code=404, detail="Service not found")
    service.is_enabled = not service.is_enabled
    db.commit()
    return RedirectResponse(url="/admin/vas?ok=1", status_code=303)


@router.post(
    "/categories",
    dependencies=[Depends(require_permission("billing:write"))],
)
def vas_set_categories(
    enabled_categories: str = Form(""), db: Session = Depends(get_db)
) -> RedirectResponse:
    cleaned = ",".join(
        part.strip().lower() for part in enabled_categories.split(",") if part.strip()
    )
    from app.schemas.settings import DomainSettingUpdate

    vas_settings.upsert_by_key(
        db,
        "enabled_categories",
        DomainSettingUpdate(value_type=SettingValueType.string, value_text=cleaned),
    )
    return RedirectResponse(url="/admin/vas?ok=1", status_code=303)


@router.post(
    "/rate-cards",
    dependencies=[Depends(require_permission("billing:write"))],
)
def vas_add_rate_card(
    category: str = Form(...),
    party_type: str = Form(...),
    rate_pct: str = Form(...),
    effective_from: str = Form(""),
    memo: str = Form(""),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    try:
        rate = Decimal(rate_pct.strip())
        party = VasPartyType(party_type.strip())
    except (InvalidOperation, ValueError):
        return RedirectResponse(
            url="/admin/vas?error=Invalid rate card values", status_code=303
        )
    effective = datetime.now(UTC)
    if effective_from.strip():
        try:
            effective = datetime.fromisoformat(effective_from.strip()).replace(
                tzinfo=UTC
            )
        except ValueError:
            return RedirectResponse(
                url="/admin/vas?error=Invalid effective date", status_code=303
            )
    db.add(
        VasRateCard(
            category=category.strip().lower(),
            party_type=party,
            rate_pct=rate,
            effective_from=effective,
            memo=memo.strip() or None,
        )
    )
    db.commit()
    return RedirectResponse(url="/admin/vas?ok=1", status_code=303)


@router.post(
    "/review/{txn_id}/refund",
    dependencies=[Depends(require_permission("billing:write"))],
)
def vas_review_refund(txn_id: str, db: Session = Depends(get_db)) -> RedirectResponse:
    """Manually resolve a parked transaction as failed → wallet refund."""
    txn = db.get(VasTransaction, txn_id)
    if not txn or txn.status != VasTransactionStatus.review:
        raise HTTPException(status_code=404, detail="Reviewable transaction not found")
    vas_purchases._mark_failed_and_refund(db, txn, "Manually resolved: refunded")
    db.commit()
    return RedirectResponse(url="/admin/vas?ok=1", status_code=303)


@router.post(
    "/review/{txn_id}/delivered",
    dependencies=[Depends(require_permission("billing:write"))],
)
def vas_review_delivered(
    txn_id: str, token: str = Form(""), db: Session = Depends(get_db)
) -> RedirectResponse:
    """Manually resolve a parked transaction as delivered (token optional)."""
    txn = db.get(VasTransaction, txn_id)
    if not txn or txn.status != VasTransactionStatus.review:
        raise HTTPException(status_code=404, detail="Reviewable transaction not found")
    body: dict = {"manually_resolved": True}
    if token.strip():
        body["purchased_code"] = token.strip()
    vas_purchases._mark_delivered(db, txn, body)
    txn.provider_status = "Manually resolved: delivered"
    db.commit()
    return RedirectResponse(url="/admin/vas?ok=1", status_code=303)


@router.post(
    "/refund-to-source",
    dependencies=[Depends(require_permission("billing:write"))],
)
def vas_refund_to_source(
    entry_id: str = Form(...),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Refund a wallet top-up back to its funding card (refund-to-source).

    The only money-out path: gateway refund against the ORIGINAL top-up
    transaction — never an arbitrary destination. Requires the wallet to
    still hold at least the top-up amount (spent money can't leave twice).
    """
    entry = db.get(VasWalletEntry, entry_id)
    if (
        not entry
        or entry.category != VasEntryCategory.topup
        or entry.entry_type != VasEntryType.credit
        or not entry.reference
    ):
        return RedirectResponse(
            url="/admin/vas?error=Entry is not a refundable top-up", status_code=303
        )
    wallet = db.get(VasWallet, entry.wallet_id)
    if wallet is None:
        return RedirectResponse(
            url="/admin/vas?error=Wallet not found", status_code=303
        )
    balance = vas_wallet.wallet_balance(db, wallet.id)
    amount = Decimal(str(entry.amount))
    if balance < amount:
        return RedirectResponse(
            url="/admin/vas?error=Wallet balance is below the top-up amount",
            status_code=303,
        )
    already = (
        db.query(VasWalletEntry)
        .filter(VasWalletEntry.reference == f"rts-{entry.id}")
        .first()
    )
    if already:
        return RedirectResponse(
            url="/admin/vas?error=This top-up was already refunded", status_code=303
        )
    from app.services import paystack

    try:
        paystack.refund_transaction(db, entry.reference, amount)
    except Exception as exc:  # noqa: BLE001
        logger.warning("refund-to-source failed for entry %s: %s", entry.id, exc)
        return RedirectResponse(
            url="/admin/vas?error=Gateway refund failed — see logs", status_code=303
        )
    vas_wallet.debit_wallet(
        db,
        wallet,
        amount=amount,
        category=VasEntryCategory.adjustment,
        reference=f"rts-{entry.id}",
        memo=f"Refund to source ({entry.reference})",
    )
    logger.info(
        "vas refund-to-source: entry=%s ref=%s amount=%s",
        entry.id,
        entry.reference,
        amount,
    )
    return RedirectResponse(url="/admin/vas?ok=1", status_code=303)
