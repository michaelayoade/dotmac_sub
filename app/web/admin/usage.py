"""Admin usage dashboard web routes."""

from datetime import datetime, timezone
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from typing import Optional

from app.csrf import get_csrf_token
from app.db import SessionLocal
from app.services import usage as usage_service
from app.services import catalog as catalog_service
from app.models.catalog import RadiusProfile, UsageAllowance
from app.models.domain_settings import SettingDomain
from app.models.usage import UsageChargeStatus, UsageRatingRunStatus
from app.services import settings_spec
from app.schemas.usage import UsageChargePostRequest, UsageRatingRunRequest

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/catalog/usage", tags=["web-admin-usage"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _base_context(request: Request, db: Session, active_page: str, usage_tab: str = ""):
    from app.web.admin import get_sidebar_stats, get_current_user
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
    # Get recent records count
    records = usage_service.usage_records.list(
        db=db,
        subscription_id=None,
        quota_bucket_id=None,
        order_by="recorded_at",
        order_dir="desc",
        limit=1000,
        offset=0,
    )

    # Get charges by status
    staged_charges = usage_service.usage_charges.list(
        db=db,
        subscription_id=None,
        subscriber_id=None,
        status="staged",
        order_by="created_at",
        order_dir="desc",
        limit=1000,
        offset=0,
    )
    needs_review_charges = usage_service.usage_charges.list(
        db=db,
        subscription_id=None,
        subscriber_id=None,
        status="needs_review",
        order_by="created_at",
        order_dir="desc",
        limit=1000,
        offset=0,
    )
    posted_charges = usage_service.usage_charges.list(
        db=db,
        subscription_id=None,
        subscriber_id=None,
        status="posted",
        order_by="created_at",
        order_dir="desc",
        limit=1000,
        offset=0,
    )

    # Get recent rating runs
    recent_runs = usage_service.usage_rating_runs.list(
        db=db,
        status=None,
        order_by="run_at",
        order_dir="desc",
        limit=5,
        offset=0,
    )

    # Calculate total billable from staged charges
    total_staged_amount = sum(float(c.amount or 0) for c in staged_charges)

    context = _base_context(request, db, active_page="catalog-usage", usage_tab="dashboard")
    context.update({
        "records_count": len(records),
        "staged_count": len(staged_charges),
        "needs_review_count": len(needs_review_charges),
        "posted_count": len(posted_charges),
        "total_staged_amount": total_staged_amount,
        "recent_runs": recent_runs,
    })
    return templates.TemplateResponse("admin/catalog/usage/index.html", context)


# =============================================================================
# FUP CALCULATOR
# =============================================================================


@router.get("/fup-calculator", response_class=HTMLResponse)
def fup_calculator(request: Request, db: Session = Depends(get_db)):
    """FUP (Fair Usage Policy) calculator for testing usage and throttling scenarios."""
    usage_allowances = (
        db.query(UsageAllowance)
        .filter(UsageAllowance.is_active.is_(True))
        .order_by(UsageAllowance.name)
        .all()
    )

    radius_profiles = (
        db.query(RadiusProfile)
        .filter(RadiusProfile.is_active.is_(True))
        .order_by(RadiusProfile.name)
        .all()
    )

    currency_symbol = settings_spec.resolve_value(
        db, SettingDomain.billing, "currency_symbol"
    ) or "₦"

    context = _base_context(request, db, active_page="catalog-usage", usage_tab="calculator")
    context.update({
        "usage_allowances": usage_allowances,
        "radius_profiles": radius_profiles,
        "currency_symbol": currency_symbol,
    })
    return templates.TemplateResponse("admin/catalog/usage/fup_calculator.html", context)


# =============================================================================
# USAGE RECORDS
# =============================================================================


@router.get("/records", response_class=HTMLResponse)
def usage_records_list(
    request: Request,
    subscription_id: Optional[str] = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=10, le=100),
    db: Session = Depends(get_db),
):
    """List usage records."""
    records = usage_service.usage_records.list(
        db=db,
        subscription_id=subscription_id,
        quota_bucket_id=None,
        order_by="recorded_at",
        order_dir="desc",
        limit=1000,
        offset=0,
    )

    total = len(records)
    total_pages = (total + per_page - 1) // per_page if total else 1
    offset = (page - 1) * per_page
    records = records[offset:offset + per_page]

    # Get subscriptions for filter dropdown
    subscriptions = catalog_service.subscriptions.list(
        db=db,
        subscriber_id=None,
        offer_id=None,
        status=None,
        order_by="created_at",
        order_dir="desc",
        limit=100,
        offset=0,
    )

    context = _base_context(request, db, active_page="catalog-usage", usage_tab="records")
    context.update({
        "records": records,
        "subscriptions": subscriptions,
        "subscription_id": subscription_id,
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
    })
    return templates.TemplateResponse("admin/catalog/usage/records.html", context)


# =============================================================================
# USAGE CHARGES
# =============================================================================


@router.get("/charges", response_class=HTMLResponse)
def usage_charges_list(
    request: Request,
    status: Optional[str] = None,
    subscription_id: Optional[str] = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=10, le=100),
    db: Session = Depends(get_db),
):
    """List usage charges with bulk actions."""
    charges = usage_service.usage_charges.list(
        db=db,
        subscription_id=subscription_id,
        subscriber_id=None,
        status=status,
        order_by="created_at",
        order_dir="desc",
        limit=1000,
        offset=0,
    )

    total = len(charges)
    total_pages = (total + per_page - 1) // per_page if total else 1
    offset = (page - 1) * per_page
    charges = charges[offset:offset + per_page]

    context = _base_context(request, db, active_page="catalog-usage", usage_tab="charges")
    context.update({
        "charges": charges,
        "status": status,
        "subscription_id": subscription_id,
        "charge_statuses": [item.value for item in UsageChargeStatus],
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
    })
    return templates.TemplateResponse("admin/catalog/usage/charges.html", context)


@router.post("/charges/{charge_id}/post", response_class=HTMLResponse)
async def usage_charge_post(request: Request, charge_id: str, db: Session = Depends(get_db)):
    """Post a single usage charge."""
    try:
        usage_service.usage_charges.post(db=db, charge_id=charge_id, payload=UsageChargePostRequest())
    except Exception:
        pass
    return RedirectResponse("/admin/catalog/usage/charges", status_code=303)


@router.post("/charges/bulk-post", response_class=HTMLResponse)
async def usage_charges_bulk_post(request: Request, db: Session = Depends(get_db)):
    """Bulk post staged charges."""
    form = await request.form()
    charge_ids = form.getlist("charge_ids")

    for charge_id in charge_ids:
        try:
            usage_service.usage_charges.post(
                db=db,
                charge_id=charge_id,
                payload=UsageChargePostRequest(),
                commit=False,
            )
        except Exception:
            pass

    if charge_ids:
        db.commit()

    return RedirectResponse("/admin/catalog/usage/charges", status_code=303)


# =============================================================================
# RATING RUNS
# =============================================================================


@router.get("/rating", response_class=HTMLResponse)
def rating_runs_list(
    request: Request,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=10, le=100),
    db: Session = Depends(get_db),
):
    """List rating runs and trigger new run."""
    runs = usage_service.usage_rating_runs.list(
        db=db,
        status=status,
        order_by="run_at",
        order_dir="desc",
        limit=1000,
        offset=0,
    )

    total = len(runs)
    total_pages = (total + per_page - 1) // per_page if total else 1
    offset = (page - 1) * per_page
    runs = runs[offset:offset + per_page]

    # Calculate default period (current month)
    now = datetime.now(timezone.utc)
    default_period_start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
    if now.month == 12:
        default_period_end = datetime(now.year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        default_period_end = datetime(now.year, now.month + 1, 1, tzinfo=timezone.utc)

    context = _base_context(request, db, active_page="catalog-usage", usage_tab="rating")
    context.update({
        "runs": runs,
        "status": status,
        "run_statuses": [item.value for item in UsageRatingRunStatus],
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
        "default_period_start": default_period_start.strftime("%Y-%m-%d"),
        "default_period_end": default_period_end.strftime("%Y-%m-%d"),
    })
    return templates.TemplateResponse("admin/catalog/usage/rating.html", context)


@router.post("/rating/run", response_class=HTMLResponse)
async def rating_run_trigger(request: Request, db: Session = Depends(get_db)):
    """Trigger a new rating run."""
    form = await request.form()
    period_start_str = (form.get("period_start") or "").strip()
    period_end_str = (form.get("period_end") or "").strip()
    dry_run = form.get("dry_run") == "true"

    period_start = None
    period_end = None

    if period_start_str:
        try:
            period_start = datetime.strptime(period_start_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            pass

    if period_end_str:
        try:
            period_end = datetime.strptime(period_end_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
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
    charges = usage_service.usage_charges.list(
        db=db,
        subscription_id=None,
        subscriber_id=None,
        status=None,
        order_by="created_at",
        order_dir="desc",
        limit=100,
        offset=0,
    )
    # Filter to this run's period
    run_charges = [
        c for c in charges
        if c.period_start == run.period_start and c.period_end == run.period_end
    ]

    context = _base_context(request, db, active_page="catalog-usage", usage_tab="rating")
    context.update({
        "run": run,
        "run_charges": run_charges,
    })
    return templates.TemplateResponse("admin/catalog/usage/rating_detail.html", context)
