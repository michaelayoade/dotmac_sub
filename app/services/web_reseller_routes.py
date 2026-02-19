"""Service helpers for reseller portal routes."""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from typing import Any
from uuid import UUID

from app.services import customer_portal
from app.services import reseller_portal

templates = Jinja2Templates(directory="templates")


def _require_reseller_context(request: Request, db: Session):
    context = reseller_portal.get_context(
        db, request.cookies.get(reseller_portal.SESSION_COOKIE_NAME)
    )
    if not context:
        return None
    return context


def reseller_home(request: Request, db: Session):
    context = _require_reseller_context(request, db)
    if not context:
        return RedirectResponse(url="/reseller/auth/login", status_code=303)
    return RedirectResponse(url="/reseller/dashboard", status_code=303)


def reseller_dashboard(
    request: Request,
    db: Session,
    page: int,
    per_page: int,
):
    context = _require_reseller_context(request, db)
    if not context:
        return RedirectResponse(url="/reseller/auth/login", status_code=303)

    offset = (page - 1) * per_page
    summary = reseller_portal.get_dashboard_summary(
        db,
        reseller_id=str(context["reseller"].id),
        limit=per_page,
        offset=offset,
    )

    return templates.TemplateResponse(
        "reseller/dashboard/index.html",
        {
            "request": request,
            "active_page": "dashboard",
            "current_user": context["current_user"],
            "reseller": context["reseller"],
            "summary": summary,
            "page": page,
            "per_page": per_page,
        },
    )


def reseller_accounts(
    request: Request,
    db: Session,
    page: int,
    per_page: int,
):
    context = _require_reseller_context(request, db)
    if not context:
        return RedirectResponse(url="/reseller/auth/login", status_code=303)

    offset = (page - 1) * per_page
    accounts = reseller_portal.list_accounts(
        db,
        reseller_id=str(context["reseller"].id),
        limit=per_page,
        offset=offset,
    )
    return templates.TemplateResponse(
        "reseller/accounts/index.html",
        {
            "request": request,
            "active_page": "accounts",
            "current_user": context["current_user"],
            "reseller": context["reseller"],
            "accounts": accounts,
            "page": page,
            "per_page": per_page,
        },
    )


def reseller_account_view(
    request: Request,
    db: Session,
    account_id: str,
):
    context = _require_reseller_context(request, db)
    if not context:
        return RedirectResponse(url="/reseller/auth/login", status_code=303)

    # Note: underlying service function name is legacy misspelling.
    session_token = reseller_portal.create_customer_imsubscriberation_session(
        db=db,
        reseller_id=str(context["reseller"].id),
        account_id=account_id,
        return_to="/reseller/accounts",
    )
    response = RedirectResponse(url="/portal/dashboard", status_code=303)
    response.set_cookie(
        key=customer_portal.SESSION_COOKIE_NAME,
        value=session_token,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=customer_portal.get_session_max_age(db),
    )
    return response


def reseller_fiber_map(request: Request, db: Session):
    context = _require_reseller_context(request, db)
    if not context:
        return RedirectResponse(url="/reseller/auth/login", status_code=303)

    import json
    from sqlalchemy import func
    from app.models.network import FdhCabinet, FiberSpliceClosure, FiberSegment, Splitter, FiberSplice, FiberSpliceTray
    from app.services import settings_spec
    from app.models.domain_settings import SettingDomain

    features = []

    # FDH Cabinets
    fdh_cabinets = db.query(FdhCabinet).filter(
        FdhCabinet.is_active.is_(True),
        FdhCabinet.latitude.isnot(None),
        FdhCabinet.longitude.isnot(None)
    ).all()
    splitter_counts: dict[UUID, int] = {}
    if fdh_cabinets:
        fdh_ids = [fdh.id for fdh in fdh_cabinets]
        for fdh_id, count in (
            db.query(Splitter.fdh_id, func.count(Splitter.id))
            .filter(Splitter.fdh_id.in_(fdh_ids))
            .group_by(Splitter.fdh_id)
            .all()
        ):
            if fdh_id is not None:
                splitter_counts[fdh_id] = int(count)
    for fdh in fdh_cabinets:
        splitter_count = splitter_counts.get(fdh.id, 0)
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [fdh.longitude, fdh.latitude]},
            "properties": {
                "id": str(fdh.id),
                "type": "fdh_cabinet",
                "name": fdh.name,
                "code": fdh.code,
                "splitter_count": splitter_count,
            },
        })

    # Splice Closures
    closures = db.query(FiberSpliceClosure).filter(
        FiberSpliceClosure.is_active.is_(True),
        FiberSpliceClosure.latitude.isnot(None),
        FiberSpliceClosure.longitude.isnot(None)
    ).all()
    splice_counts: dict[UUID, int] = {}
    tray_counts: dict[UUID, int] = {}
    if closures:
        closure_ids = [closure.id for closure in closures]
        for closure_id, count in (
            db.query(FiberSplice.closure_id, func.count(FiberSplice.id))
            .filter(FiberSplice.closure_id.in_(closure_ids))
            .group_by(FiberSplice.closure_id)
            .all()
        ):
            splice_counts[closure_id] = int(count)
        for closure_id, count in (
            db.query(FiberSpliceTray.closure_id, func.count(FiberSpliceTray.id))
            .filter(FiberSpliceTray.closure_id.in_(closure_ids))
            .group_by(FiberSpliceTray.closure_id)
            .all()
        ):
            tray_counts[closure_id] = int(count)
    for closure in closures:
        splice_count = splice_counts.get(closure.id, 0)
        tray_count = tray_counts.get(closure.id, 0)
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [closure.longitude, closure.latitude]},
            "properties": {
                "id": str(closure.id),
                "type": "splice_closure",
                "name": closure.name,
                "splice_count": splice_count,
                "tray_count": tray_count,
            },
        })

    # Fiber Segments
    segments = db.query(FiberSegment).filter(FiberSegment.is_active.is_(True)).all()
    segment_geoms = db.query(FiberSegment, func.ST_AsGeoJSON(FiberSegment.route_geom)).filter(
        FiberSegment.is_active.is_(True),
        FiberSegment.route_geom.isnot(None),
    ).all()
    for segment, geojson_str in segment_geoms:
        if not geojson_str:
            continue
        geom = json.loads(geojson_str)
        features.append({
            "type": "Feature",
            "geometry": geom,
            "properties": {
                "id": str(segment.id),
                "type": "fiber_segment",
                "name": segment.name,
                "segment_type": segment.segment_type.value if segment.segment_type else None,
                "cable_type": segment.cable_type.value if segment.cable_type else None,
                "fiber_count": segment.fiber_count,
                "length_m": segment.length_m,
            },
        })

    geojson_data = {"type": "FeatureCollection", "features": features}

    # Stats
    stats = {
        "fdh_cabinets": db.query(func.count(FdhCabinet.id)).filter(FdhCabinet.is_active.is_(True)).scalar(),
        "splice_closures": db.query(func.count(FiberSpliceClosure.id)).filter(FiberSpliceClosure.is_active.is_(True)).scalar(),
        "splitters": db.query(func.count(Splitter.id)).filter(Splitter.is_active.is_(True)).scalar(),
        "total_splices": db.query(func.count(FiberSplice.id)).scalar(),
        "segments": len(segments),
    }

    def _as_float(value: object, default: float) -> float:
        if value is None:
            return default
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value.strip())
            except ValueError:
                return default
        return default

    def _as_str(value: object, default: str) -> str:
        if isinstance(value, str) and value.strip():
            return value
        return default

    cost_settings: dict[str, Any] = {
        "drop_cable_per_meter": _as_float(
            settings_spec.resolve_value(db, SettingDomain.network, "fiber_drop_cable_cost_per_meter"),
            2.50,
        ),
        "labor_per_meter": _as_float(
            settings_spec.resolve_value(db, SettingDomain.network, "fiber_labor_cost_per_meter"),
            1.50,
        ),
        "ont_device": _as_float(
            settings_spec.resolve_value(db, SettingDomain.network, "fiber_ont_device_cost"),
            85.00,
        ),
        "installation_base": _as_float(
            settings_spec.resolve_value(db, SettingDomain.network, "fiber_installation_base_fee"),
            50.00,
        ),
        "currency": _as_str(
            settings_spec.resolve_value(db, SettingDomain.billing, "default_currency"),
            "NGN",
        ),
    }

    return templates.TemplateResponse(
        "reseller/network/fiber-map.html",
        {
            "request": request,
            "active_page": "fiber-map",
            "current_user": context["current_user"],
            "reseller": context["reseller"],
            "geojson_data": geojson_data,
            "stats": stats,
            "cost_settings": cost_settings,
            "read_only": True,
        },
    )
