"""Admin usage dashboard web routes."""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from app.csrf import get_csrf_token
from app.db import get_db
from app.schemas.usage import UsageChargePostRequest, UsageRatingRunRequest
from app.services import usage as usage_service
from app.services import web_usage as web_usage_service
from app.web.request_parsing import parse_form_data

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/catalog/usage", tags=["web-admin-usage"])


def _base_context(request: Request, db: Session, active_page: str, usage_tab: str = ""):
    from app.web.admin import get_current_user, get_sidebar_stats
    return {
        "request": request,
        "active_page": active_page,
        "active_menu": "catalog",
        "usage_tab": usage_tab,
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
        "csrf_token": get_csrf_token(request),
    }


# =============================================================================
# USAGE DASHBOARD OVERVIEW
# =============================================================================


@router.get("", response_class=HTMLResponse)
def usage_dashboard_index(request: Request, db: Session = Depends(get_db)):
    """Usage dashboard overview with stats."""
    page_data = web_usage_service.get_dashboard_data(db)
    context = _base_context(request, db, active_page="catalog-usage", usage_tab="dashboard")
    context.update(page_data)
    return templates.TemplateResponse("admin/catalog/usage/index.html", context)


# =============================================================================
# FUP CALCULATOR
# =============================================================================


@router.get("/fup-calculator", response_class=HTMLResponse)
def fup_calculator(request: Request, db: Session = Depends(get_db)):
    """FUP (Fair Usage Policy) calculator for testing usage and throttling scenarios."""
    page_data = web_usage_service.get_calculator_data(db)
    context = _base_context(request, db, active_page="catalog-usage", usage_tab="calculator")
    context.update(page_data)
    return templates.TemplateResponse("admin/catalog/usage/fup_calculator.html", context)


# =============================================================================
# USAGE RECORDS
# =============================================================================


@router.get("/records", response_class=HTMLResponse)
def usage_records_list(
    request: Request,
    subscription_id: str | None = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=10, le=100),
    db: Session = Depends(get_db),
):
    """List usage records."""
    page_data = web_usage_service.get_records_page_data(
        db,
        subscription_id=subscription_id,
        page=page,
        per_page=per_page,
    )
    context = _base_context(request, db, active_page="catalog-usage", usage_tab="records")
    context.update(page_data)
    return templates.TemplateResponse("admin/catalog/usage/records.html", context)


# =============================================================================
# USAGE CHARGES
# =============================================================================


@router.get("/charges", response_class=HTMLResponse)
def usage_charges_list(
    request: Request,
    status: str | None = None,
    subscription_id: str | None = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=10, le=100),
    db: Session = Depends(get_db),
):
    """List usage charges with bulk actions."""
    page_data = web_usage_service.get_charges_page_data(
        db,
        status=status,
        subscription_id=subscription_id,
        page=page,
        per_page=per_page,
    )
    context = _base_context(request, db, active_page="catalog-usage", usage_tab="charges")
    context.update(page_data)
    return templates.TemplateResponse("admin/catalog/usage/charges.html", context)


@router.post("/charges/{charge_id}/post", response_class=HTMLResponse)
def usage_charge_post(request: Request, charge_id: str, db: Session = Depends(get_db)):
    """Post a single usage charge."""
    try:
        usage_service.usage_charges.post(db=db, charge_id=charge_id, payload=UsageChargePostRequest())
    except Exception:
        pass
    return RedirectResponse("/admin/catalog/usage/charges", status_code=303)


@router.post("/charges/bulk-post", response_class=HTMLResponse)
def usage_charges_bulk_post(
    request: Request,
    form: FormData = Depends(parse_form_data),
    db: Session = Depends(get_db),
):
    """Bulk post staged charges."""
    # FormData can include UploadFile items; this endpoint expects string IDs.
    charge_ids = [
        item.strip()
        for item in form.getlist("charge_ids")
        if isinstance(item, str) and item.strip()
    ]
    usage_service.usage_charges.bulk_post_by_ids(db=db, charge_ids=charge_ids)
    return RedirectResponse("/admin/catalog/usage/charges", status_code=303)


# =============================================================================
# RATING RUNS
# =============================================================================


@router.get("/rating", response_class=HTMLResponse)
def rating_runs_list(
    request: Request,
    status: str | None = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=10, le=100),
    db: Session = Depends(get_db),
):
    """List rating runs and trigger new run."""
    page_data = web_usage_service.get_rating_runs_page_data(
        db,
        status=status,
        page=page,
        per_page=per_page,
    )
    context = _base_context(request, db, active_page="catalog-usage", usage_tab="rating")
    context.update(page_data)
    return templates.TemplateResponse("admin/catalog/usage/rating.html", context)


@router.post("/rating/run", response_class=HTMLResponse)
def rating_run_trigger(
    request: Request,
    form: FormData = Depends(parse_form_data),
    db: Session = Depends(get_db),
):
    """Trigger a new rating run."""
    period_start_raw = form.get("period_start")
    period_end_raw = form.get("period_end")
    period_start_str = period_start_raw.strip() if isinstance(period_start_raw, str) else ""
    period_end_str = period_end_raw.strip() if isinstance(period_end_raw, str) else ""
    dry_run = form.get("dry_run") == "true"

    period_start = None
    period_end = None

    if period_start_str:
        try:
            period_start = datetime.strptime(period_start_str, "%Y-%m-%d").replace(tzinfo=UTC)
        except ValueError:
            pass

    if period_end_str:
        try:
            period_end = datetime.strptime(period_end_str, "%Y-%m-%d").replace(tzinfo=UTC)
        except ValueError:
            pass

    try:
        payload = UsageRatingRunRequest(
            period_start=period_start,
            period_end=period_end,
            dry_run=dry_run,
        )
        result = usage_service.usage_rating_runs.run(db=db, payload=payload)
        # Store result in session or query params
        return RedirectResponse(
            f"/admin/catalog/usage/rating?last_run_charges={result.charges_created}&last_run_scanned={result.subscriptions_scanned}",
            status_code=303,
        )
    except Exception as exc:
        return RedirectResponse(
            f"/admin/catalog/usage/rating?error={str(exc)[:100]}",
            status_code=303,
        )


@router.get("/rating/{run_id}", response_class=HTMLResponse)
def rating_run_detail(request: Request, run_id: str, db: Session = Depends(get_db)):
    """View rating run details."""
    try:
        run = usage_service.usage_rating_runs.get(db=db, run_id=run_id)
    except Exception:
        return RedirectResponse("/admin/catalog/usage/rating", status_code=303)

    # Get charges created by this run (by period)
    run_charges = usage_service.usage_charges.list(
        db=db,
        subscription_id=None,
        subscriber_id=None,
        status=None,
        order_by="created_at",
        order_dir="desc",
        limit=100,
        offset=0,
        period_start=run.period_start,
        period_end=run.period_end,
    )

    context = _base_context(request, db, active_page="catalog-usage", usage_tab="rating")
    context.update({
        "run": run,
        "run_charges": run_charges,
    })
    return templates.TemplateResponse("admin/catalog/usage/rating_detail.html", context)
