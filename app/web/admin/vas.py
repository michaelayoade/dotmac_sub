"""Admin VAS operations: service toggles, rate cards, review queue, refunds."""

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import (
    vas_admin_commands,
    vas_purchases,
    vas_refunds,
    vas_wallet,
    vtpass,
)
from app.services.auth_dependencies import require_permission

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/vas", tags=["web-admin-vas"])


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
    dependencies=[Depends(require_permission("billing:vas:read"))],
)
def vas_admin_page(request: Request, db: Session = Depends(get_db)):
    float_balance = None
    float_error = None
    try:
        float_balance = vtpass.get_balance(db)
    except HTTPException as exc:
        float_error = str(exc.detail)

    services = vas_purchases.admin_services(db)
    review_queue = vas_purchases.admin_review_queue(db)
    rate_cards = vas_purchases.admin_rate_cards(db)
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
            refund_requests=vas_refunds.admin_refund_requests(db),
            rate_cards=rate_cards,
            enabled_categories=enabled_categories,
            categories=sorted({service.category for service in services}),
            currency_symbol=vas_wallet.currency_symbol(db),
            auto_deduct_result=vas_wallet.last_auto_deduct_result(db),
            submitted=request.query_params.get("ok"),
            form_error=request.query_params.get("error"),
            sweep_status=request.query_params.get("sweep_status"),
            sweep_paid=request.query_params.get("sweep_paid"),
            sweep_errors=request.query_params.get("sweep_errors"),
            sweep_total=request.query_params.get("sweep_total"),
        ),
    )


@router.post(
    "/services/{service_pk}/toggle",
    dependencies=[Depends(require_permission("billing:vas:write"))],
)
def vas_toggle_service(
    service_pk: str, db: Session = Depends(get_db)
) -> RedirectResponse:
    try:
        vas_admin_commands.toggle_service(db, service_pk=service_pk)
    except vas_admin_commands.VasAdminResourceNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return RedirectResponse(url="/admin/vas?ok=1", status_code=303)


@router.post(
    "/auto-deduct/run",
    dependencies=[Depends(require_permission("billing:vas:write"))],
)
def vas_run_auto_deduct(db: Session = Depends(get_db)) -> RedirectResponse:
    result = vas_admin_commands.run_auto_deduct(db)
    return RedirectResponse(
        url=(
            "/admin/vas?"
            f"sweep_status={result.get('status', '')}"
            f"&sweep_paid={result.get('paid', 0)}"
            f"&sweep_errors={result.get('errors', 0)}"
            f"&sweep_total={result.get('swept_total', '0.00')}"
        ),
        status_code=303,
    )


@router.post(
    "/categories",
    dependencies=[Depends(require_permission("billing:vas:write"))],
)
def vas_set_categories(
    enabled_categories: str = Form(""), db: Session = Depends(get_db)
) -> RedirectResponse:
    vas_admin_commands.set_categories(db, enabled_categories=enabled_categories)
    return RedirectResponse(url="/admin/vas?ok=1", status_code=303)


@router.post(
    "/rate-cards",
    dependencies=[Depends(require_permission("billing:vas:write"))],
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
        vas_admin_commands.add_rate_card(
            db,
            category=category,
            party_type=party_type,
            rate_pct=rate_pct,
            effective_from=effective_from,
            memo=memo,
        )
    except vas_admin_commands.VasAdminCommandError as exc:
        return RedirectResponse(url=f"/admin/vas?error={exc}", status_code=303)
    return RedirectResponse(url="/admin/vas?ok=1", status_code=303)


@router.post(
    "/review/{txn_id}/refund",
    dependencies=[Depends(require_permission("billing:vas:write"))],
)
def vas_review_refund(txn_id: str, db: Session = Depends(get_db)) -> RedirectResponse:
    """Manually resolve a parked transaction as failed → wallet refund."""
    try:
        vas_admin_commands.resolve_review_refund(db, txn_id=txn_id)
    except vas_admin_commands.VasAdminResourceNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return RedirectResponse(url="/admin/vas?ok=1", status_code=303)


@router.post(
    "/review/{txn_id}/delivered",
    dependencies=[Depends(require_permission("billing:vas:write"))],
)
def vas_review_delivered(
    txn_id: str, token: str = Form(""), db: Session = Depends(get_db)
) -> RedirectResponse:
    """Manually resolve a parked transaction as delivered (token optional)."""
    try:
        vas_admin_commands.resolve_review_delivered(db, txn_id=txn_id, token=token)
    except vas_admin_commands.VasAdminResourceNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return RedirectResponse(url="/admin/vas?ok=1", status_code=303)


@router.post(
    "/refund-to-source",
    dependencies=[Depends(require_permission("billing:vas:write"))],
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
    try:
        vas_admin_commands.refund_to_source(db, entry_id=entry_id)
    except vas_admin_commands.VasAdminCommandError as exc:
        return RedirectResponse(url=f"/admin/vas?error={exc}", status_code=303)
    return RedirectResponse(url="/admin/vas?ok=1", status_code=303)
