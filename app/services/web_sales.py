"""Web helpers for the admin sales routes (leads, pipelines, quotes, orders).

Phase 3 §2.6 admin-web port (PR 11 of the series): the context builders behind
``app/web/admin/sales.py``, adapted from the CRM's
``services/crm/web_{leads,sales,quotes}.py`` and the sales-order slices of
``web/admin/operations.py`` onto sub's context-builder idiom
(``web_support_tickets`` pattern). Everything reads/writes through the merged
native managers in ``app.services.sales`` / ``app.services.sales_orders`` —
no queries in the routes.

CRM → sub adaptations:

* ``person_id`` contacts become ``subscriber_id`` subscribers (§1.3–§1.5);
  contact labels resolve from the batched subscriber map.
* Status columns are plain strings in sub (Phase 1 String-not-enum rule), so
  no ``.value`` handling anywhere.
* CRM agents (Phase 4) are not ported — owner columns render as raw UUID
  short-codes until the agent model lands.
* List totals come from count queries instead of the CRM's
  ``limit=10000`` re-list trick.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import case, func, or_
from sqlalchemy.orm import Session

from app.models.sales import (
    Lead,
    LeadStatus,
    Pipeline,
    PipelineStage,
    Quote,
    QuoteStatus,
    SalesOrder,
    SalesOrderPaymentStatus,
    SalesOrderStatus,
)
from app.models.subscriber import Subscriber
from app.schemas.sales import (
    PipelineCreate,
    PipelineStageCreate,
    PipelineStageUpdate,
    PipelineUpdate,
    QuoteCreate,
    QuoteUpdate,
)
from app.services import sales as sales_service
from app.services import sales_orders as sales_orders_service
from app.services.common import coerce_uuid
from app.services.sales.selfserve import compute_feasibility
from app.services.sales_orders import _resolve_project_for_sales_order

# The CRM's recommended default stage set, seeded when "create default
# stages" is ticked on the new-pipeline form (web_sales.py port).
DEFAULT_PIPELINE_STAGES: list[dict[str, int | str]] = [
    {"name": "Lead Identified", "probability": 10},
    {"name": "Qualification Call Completed", "probability": 20},
    {"name": "Needs Assessment / Demo", "probability": 35},
    {"name": "Proposal Sent", "probability": 50},
    {"name": "Commercial Negotiation", "probability": 70},
    {"name": "Decision Pending", "probability": 85},
    {"name": "Closed Won", "probability": 100},
    {"name": "Closed Lost", "probability": 0},
]

_OPEN_LEAD_STATUSES = {
    LeadStatus.new.value,
    LeadStatus.contacted.value,
    LeadStatus.qualified.value,
    LeadStatus.proposal.value,
    LeadStatus.negotiation.value,
}


def _as_bool(value: str | None) -> bool:
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def lead_status_values() -> list[str]:
    return [status.value for status in LeadStatus]


def quote_status_values() -> list[str]:
    return [status.value for status in QuoteStatus]


def sales_order_status_values() -> list[str]:
    return [status.value for status in SalesOrderStatus]


def sales_order_payment_status_values() -> list[str]:
    return [status.value for status in SalesOrderPaymentStatus]


def _clean_choice(value: str | None, allowed: list[str]) -> str | None:
    """Return the filter value if it is a known vocabulary entry, else None
    (stale/hand-edited query params must not 400 a list page)."""
    candidate = (value or "").strip()
    return candidate if candidate in allowed else None


def subscriber_label(subscriber: Subscriber | None) -> str:
    if subscriber is None:
        return ""
    name = (
        subscriber.display_name
        or f"{subscriber.first_name or ''} {subscriber.last_name or ''}".strip()
    )
    return name or subscriber.email or ""


def _subscriber_map(db: Session, ids: list) -> dict[str, Subscriber]:
    clean = [coerce_uuid(value) for value in ids if value]
    if not clean:
        return {}
    rows = db.query(Subscriber).filter(Subscriber.id.in_(clean)).all()
    return {str(row.id): row for row in rows}


def _total_pages(total: int, per_page: int) -> int:
    return (total + per_page - 1) // per_page if total else 1


def _sales_options(db: Session) -> dict[str, Any]:
    pipelines = sales_service.pipelines.list(
        db,
        is_active=True,
        order_by="name",
        order_dir="asc",
        limit=100,
        offset=0,
    )
    stages = sales_service.pipeline_stages.list(
        db,
        pipeline_id=None,
        is_active=True,
        order_by="order_index",
        order_dir="asc",
        limit=1000,
        offset=0,
    )
    return {
        "pipelines": pipelines,
        "stages": stages,
        "pipeline_map": {str(item.id): item for item in pipelines},
        "stage_map": {str(item.id): item for item in stages},
    }


# ---------------------------------------------------------------------------
# Leads
# ---------------------------------------------------------------------------


def _apply_lead_search(query, search: str):
    pattern = f"%{search.strip()}%"
    if pattern == "%%":
        return query
    full_name = func.trim(
        func.coalesce(Subscriber.first_name, "")
        + " "
        + func.coalesce(Subscriber.last_name, "")
    )
    return query.outerjoin(Subscriber, Subscriber.id == Lead.subscriber_id).filter(
        or_(
            Lead.title.ilike(pattern),
            Subscriber.display_name.ilike(pattern),
            full_name.ilike(pattern),
            Subscriber.first_name.ilike(pattern),
            Subscriber.last_name.ilike(pattern),
            Subscriber.email.ilike(pattern),
            Subscriber.phone.ilike(pattern),
        )
    )


def _count_leads(
    db: Session,
    *,
    status: str | None,
    pipeline_id: str | None,
    stage_id: str | None,
    lead_source: str | None,
    search: str | None,
) -> int:
    query = db.query(func.count(Lead.id)).select_from(Lead)
    query = query.filter(Lead.is_active.is_(True))
    if status:
        query = query.filter(Lead.status == status)
    if pipeline_id:
        query = query.filter(Lead.pipeline_id == coerce_uuid(pipeline_id))
    if stage_id:
        query = query.filter(Lead.stage_id == coerce_uuid(stage_id))
    if lead_source:
        query = query.filter(
            func.lower(Lead.lead_source) == lead_source.strip().lower()
        )
    if search:
        query = _apply_lead_search(query, search)
    return int(query.scalar() or 0)


def _lead_stats(db: Session) -> dict[str, Any]:
    """Status counts + open-pipeline value across all active leads. Won/lost
    leads are excluded from the pipeline value (CRM BUG-030 fix carried)."""
    rows = (
        db.query(
            Lead.status,
            func.count(Lead.id),
            func.coalesce(func.sum(Lead.estimated_value), 0),
        )
        .filter(Lead.is_active.is_(True))
        .group_by(Lead.status)
        .all()
    )
    by_status: dict[str, int] = {}
    total = 0
    total_value = Decimal("0")
    for status_value, count, value_sum in rows:
        key = status_value or LeadStatus.new.value
        by_status[key] = by_status.get(key, 0) + int(count)
        total += int(count)
        if key in _OPEN_LEAD_STATUSES:
            total_value += Decimal(str(value_sum or 0))
    open_count = sum(by_status.get(key, 0) for key in _OPEN_LEAD_STATUSES)
    return {
        "total": total,
        "by_status": by_status,
        "open": open_count,
        "won": by_status.get(LeadStatus.won.value, 0),
        "total_value": total_value,
    }


def build_leads_list_context(
    db: Session,
    *,
    status: str | None,
    pipeline_id: str | None,
    stage_id: str | None,
    lead_source: str | None,
    search: str | None,
    page: int,
    per_page: int,
) -> dict[str, Any]:
    status = _clean_choice(status, lead_status_values())
    lead_source_options = list(sales_service.LEAD_SOURCE_OPTIONS)
    lead_source = _clean_choice(lead_source, lead_source_options)

    offset = (page - 1) * per_page
    leads = sales_service.leads.list(
        db,
        pipeline_id=pipeline_id or None,
        stage_id=stage_id or None,
        owner_agent_id=None,
        status=status,
        is_active=None,
        order_by="created_at",
        order_dir="desc",
        limit=per_page,
        offset=offset,
        lead_source=lead_source,
        search=search or None,
    )
    total = _count_leads(
        db,
        status=status,
        pipeline_id=pipeline_id or None,
        stage_id=stage_id or None,
        lead_source=lead_source,
        search=search or None,
    )

    options = _sales_options(db)
    subscriber_map = _subscriber_map(db, [lead.subscriber_id for lead in leads])

    return {
        "leads": leads,
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": _total_pages(total, per_page),
        "status": status or "",
        "pipeline_id": pipeline_id or "",
        "stage_id": stage_id or "",
        "lead_source": lead_source or "",
        "search": search or "",
        "lead_statuses": lead_status_values(),
        "lead_sources": lead_source_options,
        "pipelines": options["pipelines"],
        "stages": options["stages"],
        "pipeline_map": options["pipeline_map"],
        "stage_map": options["stage_map"],
        "subscriber_map": subscriber_map,
        "lead_stats": _lead_stats(db),
    }


def build_lead_detail_context(db: Session, *, lead_id: str) -> dict[str, Any]:
    lead = sales_service.leads.get(db, lead_id)
    quotes = sales_service.quotes.list(
        db,
        lead_id=str(lead.id),
        status=None,
        is_active=None,
        order_by="created_at",
        order_dir="desc",
        limit=50,
        offset=0,
    )
    return {
        "lead": lead,
        "subscriber": lead.subscriber,
        "subscriber_label": subscriber_label(lead.subscriber),
        "pipeline": lead.pipeline,
        "stage": lead.stage,
        "quotes": quotes,
        "status_val": lead.status or LeadStatus.new.value,
    }


def build_leads_board_context(
    db: Session, *, pipeline_id: str | None
) -> dict[str, Any]:
    pipelines = sales_service.pipelines.list(
        db,
        is_active=True,
        order_by="name",
        order_dir="asc",
        limit=100,
        offset=0,
    )
    selected_pipeline_id = (pipeline_id or "").strip()
    if not selected_pipeline_id and pipelines:
        selected_pipeline_id = str(pipelines[0].id)
    return {
        "pipelines": pipelines,
        "selected_pipeline_id": selected_pipeline_id,
    }


# ---------------------------------------------------------------------------
# Pipelines (settings)
# ---------------------------------------------------------------------------


def build_pipeline_settings_context(
    db: Session, *, bulk_result: str, bulk_count: str
) -> dict[str, Any]:
    pipelines = (
        db.query(Pipeline)
        .order_by(Pipeline.is_active.desc(), Pipeline.created_at.desc())
        .limit(200)
        .all()
    )
    stages = (
        db.query(PipelineStage)
        .order_by(
            PipelineStage.pipeline_id.asc(),
            PipelineStage.order_index.asc(),
            PipelineStage.created_at.asc(),
        )
        .limit(1000)
        .all()
    )
    stage_map: dict[str, list[PipelineStage]] = {}
    for stage in stages:
        stage_map.setdefault(str(stage.pipeline_id), []).append(stage)
    return {
        "pipelines": pipelines,
        "stage_map": stage_map,
        "bulk_result": bulk_result,
        "bulk_count": bulk_count,
        "default_pipeline_stages": DEFAULT_PIPELINE_STAGES,
    }


def build_pipeline_new_context() -> dict[str, Any]:
    return {
        "pipeline": {"name": "", "is_active": True, "create_default_stages": True},
        "form_title": "New Pipeline",
        "submit_label": "Create Pipeline",
        "action_url": "/admin/sales/pipelines",
        "error": None,
    }


def build_pipeline_edit_context(db: Session, *, pipeline_id: str) -> dict[str, Any]:
    pipeline = sales_service.pipelines.get(db, pipeline_id)
    return {
        "pipeline": pipeline,
        "form_title": "Edit Pipeline",
        "submit_label": "Update Pipeline",
        "action_url": f"/admin/sales/pipelines/{pipeline_id}",
        "error": None,
    }


def build_pipeline_form_error_context(
    *,
    mode: str,
    pipeline_id: str | None,
    name: str | None,
    is_active: str | None,
    create_default_stages: str | None,
) -> dict[str, Any]:
    editing = mode == "update"
    return {
        "pipeline": {
            "id": pipeline_id,
            "name": (name or "").strip(),
            "is_active": _as_bool(is_active) if is_active is not None else True,
            "create_default_stages": _as_bool(create_default_stages),
        },
        "form_title": "Edit Pipeline" if editing else "New Pipeline",
        "submit_label": "Update Pipeline" if editing else "Create Pipeline",
        "action_url": (
            f"/admin/sales/pipelines/{pipeline_id}"
            if editing
            else "/admin/sales/pipelines"
        ),
    }


def create_pipeline_from_form(
    db: Session,
    *,
    name: str | None,
    is_active: str | None,
    create_default_stages: str | None,
) -> str:
    pipeline_name = (name or "").strip()
    if not pipeline_name:
        raise ValueError("Pipeline name is required.")
    payload = PipelineCreate(
        name=pipeline_name,
        is_active=_as_bool(is_active) if is_active is not None else True,
    )
    pipeline = sales_service.pipelines.create(db, payload)
    if _as_bool(create_default_stages):
        for index, stage in enumerate(DEFAULT_PIPELINE_STAGES):
            sales_service.pipeline_stages.create(
                db,
                PipelineStageCreate(
                    pipeline_id=pipeline.id,
                    name=str(stage["name"]),
                    order_index=index,
                    default_probability=int(stage["probability"]),
                    is_active=True,
                ),
            )
    return str(pipeline.id)


def update_pipeline_from_form(
    db: Session, *, pipeline_id: str, name: str | None, is_active: str | None
) -> None:
    payload = PipelineUpdate(
        name=(name or "").strip() or None,
        is_active=_as_bool(is_active) if is_active is not None else None,
    )
    sales_service.pipelines.update(db, pipeline_id, payload)


def deactivate_pipeline(db: Session, pipeline_id: str) -> None:
    sales_service.pipelines.delete(db, pipeline_id)


def create_stage_from_form(
    db: Session,
    *,
    pipeline_id: str,
    name: str,
    order_index: int,
    default_probability: int,
) -> None:
    sales_service.pipeline_stages.create(
        db,
        PipelineStageCreate(
            pipeline_id=coerce_uuid(pipeline_id),
            name=name.strip(),
            order_index=order_index,
            default_probability=default_probability,
            is_active=True,
        ),
    )


def update_stage_from_form(
    db: Session,
    *,
    stage_id: str,
    name: str,
    order_index: int,
    default_probability: int,
    is_active: str | None,
) -> None:
    sales_service.pipeline_stages.update(
        db,
        stage_id,
        PipelineStageUpdate(
            name=name.strip(),
            order_index=order_index,
            default_probability=default_probability,
            is_active=_as_bool(is_active) if is_active is not None else False,
        ),
    )


def deactivate_stage(db: Session, *, stage_id: str) -> None:
    sales_service.pipeline_stages.update(
        db, stage_id, PipelineStageUpdate(is_active=False)
    )


def bulk_assign_leads(
    db: Session, *, pipeline_id: str, stage_id: str | None, scope: str
) -> int:
    return sales_service.leads.bulk_assign_pipeline(
        db,
        pipeline_id,
        (stage_id or "").strip() or None,
        scope=scope,
    )


# ---------------------------------------------------------------------------
# Quotes
# ---------------------------------------------------------------------------


def _count_quotes(
    db: Session,
    *,
    status: str | None,
    lead_id: str | None,
    search: str | None,
) -> int:
    query = db.query(func.count(Quote.id)).select_from(Quote)
    query = query.filter(Quote.is_active.is_(True))
    if status:
        query = query.filter(Quote.status == status)
    if lead_id:
        query = query.filter(Quote.lead_id == coerce_uuid(lead_id))
    if search:
        like = f"%{search.strip()}%"
        query = query.outerjoin(
            Subscriber, Quote.subscriber_id == Subscriber.id
        ).filter(
            or_(
                Subscriber.display_name.ilike(like),
                Subscriber.first_name.ilike(like),
                Subscriber.last_name.ilike(like),
                Subscriber.email.ilike(like),
            )
        )
    return int(query.scalar() or 0)


def _coordinate(value: str | None, *, field: str, low: float, high: float) -> float | None:
    """Parse a map-pin coordinate from form input.

    Both coordinates are optional, but a half-pin is meaningless — the caller
    enforces that they arrive together.
    """
    text_value = (value or "").strip()
    if not text_value:
        return None
    try:
        number = float(text_value)
    except ValueError:
        raise ValueError(f"{field} must be a number.") from None
    if not low <= number <= high:
        raise ValueError(f"{field} must be between {low} and {high}.")
    return number


def _install_pin(
    db: Session,
    *,
    latitude: str | None,
    longitude: str | None,
    address: str | None,
    region: str | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Build the ``install{}`` map-pin block, and its feasibility, from a form.

    This is the same contract ``sales.selfserve`` stamps for portal-originated
    quotes (``install{latitude, longitude, address, region}`` on ``metadata_``),
    and downstream estimate/survey/billing read the pin from there. A staff-
    authored quote must be indistinguishable from a portal one, so we reuse the
    shape and the feasibility computation rather than inventing a parallel one.
    """
    lat = _coordinate(latitude, field="Latitude", low=-90.0, high=90.0)
    lng = _coordinate(longitude, field="Longitude", low=-180.0, high=180.0)
    if (lat is None) != (lng is None):
        raise ValueError("Drop a pin on the map: latitude and longitude go together.")

    clean_address = (address or "").strip() or None
    clean_region = (region or "").strip() or None
    if lat is None and lng is None and not clean_address and not clean_region:
        return None, None

    install = {
        "latitude": lat,
        "longitude": lng,
        "address": clean_address,
        "region": clean_region,
    }
    feasibility = (
        compute_feasibility(db, lat, lng) if lat is not None and lng is not None else None
    )
    return install, feasibility


def _merge_install_metadata(
    existing: Any,
    *,
    install: dict[str, Any] | None,
    feasibility: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Fold a new pin into a quote's existing metadata without clobbering it.

    ``metadata_`` carries the whole portal contract (source, project_type,
    deposit, pricing_mode...), so an admin edit must merge into it, never
    replace it. Clearing the pin removes only the keys the pin owns.
    """
    meta: dict[str, Any] = dict(existing) if isinstance(existing, dict) else {}
    if install is None:
        meta.pop("install", None)
        meta.pop("feasibility", None)
    else:
        meta["install"] = install
        if feasibility is not None:
            meta["feasibility"] = feasibility
        else:
            meta.pop("feasibility", None)
    return meta or None


def _quote_form_fields(
    *,
    subscriber_id: str | None,
    lead_id: str | None,
    status: str | None,
    currency: str | None,
    tax_rate: str | None,
    expires_at: str | None,
    notes: str | None,
    latitude: str | None,
    longitude: str | None,
    address: str | None,
    region: str | None,
) -> dict[str, Any]:
    return {
        "subscriber_id": (subscriber_id or "").strip(),
        "lead_id": (lead_id or "").strip(),
        "status": (status or QuoteStatus.draft.value).strip(),
        "currency": (currency or "NGN").strip().upper(),
        "tax_rate": (tax_rate or "").strip(),
        "expires_at": (expires_at or "").strip(),
        "notes": (notes or "").strip(),
        "latitude": (latitude or "").strip(),
        "longitude": (longitude or "").strip(),
        "address": (address or "").strip(),
        "region": (region or "").strip(),
    }


def build_quote_new_context() -> dict[str, Any]:
    return {
        "quote_form": _quote_form_fields(
            subscriber_id=None,
            lead_id=None,
            status=QuoteStatus.draft.value,
            currency="NGN",
            tax_rate=None,
            expires_at=None,
            notes=None,
            latitude=None,
            longitude=None,
            address=None,
            region=None,
        ),
        "status_values": quote_status_values(),
        "form_title": "New Quote",
        "submit_label": "Create Quote",
        "action_url": "/admin/sales/quotes",
        "error": None,
    }


def build_quote_edit_context(db: Session, *, quote_id: str) -> dict[str, Any]:
    quote = sales_service.quotes.get(db, quote_id)
    meta = quote.metadata_ if isinstance(quote.metadata_, dict) else {}
    install = meta.get("install") if isinstance(meta.get("install"), dict) else {}
    return {
        "quote": quote,
        "quote_form": _quote_form_fields(
            subscriber_id=str(quote.subscriber_id),
            lead_id=str(quote.lead_id) if quote.lead_id else None,
            status=quote.status,
            currency=quote.currency,
            tax_rate=str(quote.tax_rate) if quote.tax_rate is not None else None,
            expires_at=(
                quote.expires_at.date().isoformat() if quote.expires_at else None
            ),
            notes=quote.notes,
            latitude=(
                str(install.get("latitude"))
                if install.get("latitude") is not None
                else None
            ),
            longitude=(
                str(install.get("longitude"))
                if install.get("longitude") is not None
                else None
            ),
            address=install.get("address"),
            region=install.get("region"),
        ),
        "status_values": quote_status_values(),
        "form_title": "Edit Quote",
        "submit_label": "Update Quote",
        "action_url": f"/admin/sales/quotes/{quote_id}/edit",
        "error": None,
    }


def build_quote_form_error_context(
    *,
    mode: str,
    quote_id: str | None,
    **fields: str | None,
) -> dict[str, Any]:
    editing = mode == "update"
    return {
        "quote_form": _quote_form_fields(**fields),  # type: ignore[arg-type]
        "status_values": quote_status_values(),
        "form_title": "Edit Quote" if editing else "New Quote",
        "submit_label": "Update Quote" if editing else "Create Quote",
        "action_url": (
            f"/admin/sales/quotes/{quote_id}/edit" if editing else "/admin/sales/quotes"
        ),
    }


def _quote_expiry(value: str | None) -> datetime | None:
    text_value = (value or "").strip()
    if not text_value:
        return None
    try:
        return datetime.fromisoformat(text_value).replace(tzinfo=UTC)
    except ValueError:
        raise ValueError("Expiry must be a valid date.") from None


def _quote_tax_rate(value: str | None) -> Decimal | None:
    text_value = (value or "").strip()
    if not text_value:
        return None
    try:
        return Decimal(text_value)
    except (ArithmeticError, ValueError):
        raise ValueError("Tax rate must be a number.") from None


def create_quote_from_form(
    db: Session,
    *,
    subscriber_id: str | None,
    lead_id: str | None,
    status: str | None,
    currency: str | None,
    tax_rate: str | None,
    expires_at: str | None,
    notes: str | None,
    latitude: str | None,
    longitude: str | None,
    address: str | None,
    region: str | None,
) -> str:
    if not (subscriber_id or "").strip():
        raise ValueError("A subscriber is required.")

    install, feasibility = _install_pin(
        db,
        latitude=latitude,
        longitude=longitude,
        address=address,
        region=region,
    )
    metadata = _merge_install_metadata(
        {"source": "admin"}, install=install, feasibility=feasibility
    )

    payload = QuoteCreate(
        subscriber_id=coerce_uuid(subscriber_id),
        lead_id=coerce_uuid(lead_id) if (lead_id or "").strip() else None,
        status=QuoteStatus(_clean_choice(status, quote_status_values()) or "draft"),
        currency=(currency or "NGN").strip().upper(),
        tax_rate=_quote_tax_rate(tax_rate),
        expires_at=_quote_expiry(expires_at),
        notes=(notes or "").strip() or None,
        metadata_=metadata,
    )
    quote = sales_service.quotes.create(db, payload)
    return str(quote.id)


def update_quote_from_form(
    db: Session,
    *,
    quote_id: str,
    subscriber_id: str | None,
    lead_id: str | None,
    status: str | None,
    currency: str | None,
    tax_rate: str | None,
    expires_at: str | None,
    notes: str | None,
    latitude: str | None,
    longitude: str | None,
    address: str | None,
    region: str | None,
) -> None:
    quote = sales_service.quotes.get(db, quote_id)
    install, feasibility = _install_pin(
        db,
        latitude=latitude,
        longitude=longitude,
        address=address,
        region=region,
    )
    metadata = _merge_install_metadata(
        quote.metadata_, install=install, feasibility=feasibility
    )

    payload = QuoteUpdate(
        subscriber_id=(
            coerce_uuid(subscriber_id) if (subscriber_id or "").strip() else None
        ),
        lead_id=coerce_uuid(lead_id) if (lead_id or "").strip() else None,
        status=(
            QuoteStatus(_clean_choice(status, quote_status_values()) or "draft")
            if (status or "").strip()
            else None
        ),
        currency=(currency or "").strip().upper() or None,
        tax_rate=_quote_tax_rate(tax_rate),
        expires_at=_quote_expiry(expires_at),
        notes=(notes or "").strip() or None,
        metadata_=metadata,
    )
    sales_service.quotes.update(db, quote_id, payload)


def set_quote_status(db: Session, quote_id: str, status: str | None) -> None:
    clean = _clean_choice(status, quote_status_values())
    if not clean:
        raise ValueError("Unknown quote status.")
    sales_service.quotes.update(db, quote_id, QuoteUpdate(status=QuoteStatus(clean)))


def deactivate_quote(db: Session, quote_id: str) -> None:
    sales_service.quotes.delete(db, quote_id)


def build_quotes_list_context(
    db: Session,
    *,
    status: str | None,
    lead_id: str | None,
    search: str | None,
    page: int,
    per_page: int,
) -> dict[str, Any]:
    status = _clean_choice(status, quote_status_values())
    offset = (page - 1) * per_page
    quotes = sales_service.quotes.list(
        db,
        lead_id=lead_id or None,
        status=status,
        is_active=None,
        order_by="created_at",
        order_dir="desc",
        limit=per_page,
        offset=offset,
        search=search or None,
    )
    total = _count_quotes(
        db, status=status, lead_id=lead_id or None, search=search or None
    )

    leads = sales_service.leads.list(
        db,
        pipeline_id=None,
        stage_id=None,
        owner_agent_id=None,
        status=None,
        is_active=None,
        order_by="created_at",
        order_dir="desc",
        limit=500,
        offset=0,
    )
    subscriber_map = _subscriber_map(db, [quote.subscriber_id for quote in quotes])

    return {
        "quotes": quotes,
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": _total_pages(total, per_page),
        "status": status or "",
        "lead_id": lead_id or "",
        "search": search or "",
        "quote_statuses": quote_status_values(),
        "leads": leads,
        "lead_map": {str(item.id): item for item in leads},
        "subscriber_map": subscriber_map,
        "stats": sales_service.quotes.count_by_status(db),
        "today": datetime.now(UTC),
    }


def build_quote_detail_context(db: Session, *, quote_id: str) -> dict[str, Any]:
    quote = sales_service.quotes.get(db, quote_id)
    items = sales_service.quote_line_items.list(
        db,
        quote_id=str(quote.id),
        order_by="created_at",
        order_dir="asc",
        limit=500,
        offset=0,
    )
    lead = None
    if quote.lead_id:
        lead = db.get(Lead, quote.lead_id)

    meta = quote.metadata_ if isinstance(quote.metadata_, dict) else {}
    deposit = meta.get("deposit") if isinstance(meta.get("deposit"), dict) else {}
    feasibility = (
        meta.get("feasibility") if isinstance(meta.get("feasibility"), dict) else {}
    )
    install = meta.get("install") if isinstance(meta.get("install"), dict) else {}

    return {
        "quote": quote,
        "items": items,
        "lead": lead,
        "subscriber": quote.subscriber,
        "subscriber_label": subscriber_label(quote.subscriber),
        "sales_order": quote.sales_order,
        "status_val": quote.status or QuoteStatus.draft.value,
        "is_accepted": (quote.status or "") == QuoteStatus.accepted.value,
        "quote_source": meta.get("source"),
        "quote_project_type": meta.get("project_type"),
        "deposit": deposit,
        "deposit_percent": meta.get("deposit_percent"),
        "estimate_provisional": meta.get("estimate_provisional"),
        "pricing_mode": meta.get("pricing_mode"),
        "feasibility": feasibility,
        "install": install,
        "today": datetime.now(UTC),
    }


# ---------------------------------------------------------------------------
# Sales orders
# ---------------------------------------------------------------------------


def _sales_orders_query(
    db: Session,
    *,
    status: str | None,
    payment_status: str | None,
    source_type: str | None,
    search: str | None,
):
    query = db.query(SalesOrder).filter(SalesOrder.is_active.is_(True))
    if status:
        query = query.filter(SalesOrder.status == status)
    if payment_status:
        query = query.filter(SalesOrder.payment_status == payment_status)
    if source_type == "quote":
        query = query.filter(SalesOrder.quote_id.isnot(None))
    elif source_type == "manual":
        query = query.filter(SalesOrder.quote_id.is_(None))
    if search:
        like = f"%{search.strip()}%"
        query = query.outerjoin(
            Subscriber, Subscriber.id == SalesOrder.subscriber_id
        ).filter(
            or_(
                SalesOrder.order_number.ilike(like),
                Subscriber.display_name.ilike(like),
                Subscriber.first_name.ilike(like),
                Subscriber.last_name.ilike(like),
                Subscriber.email.ilike(like),
                Subscriber.phone.ilike(like),
            )
        )
    return query


def build_sales_orders_list_context(
    db: Session,
    *,
    status: str | None,
    payment_status: str | None,
    source_type: str | None,
    search: str | None,
    page: int,
    per_page: int,
) -> dict[str, Any]:
    status = _clean_choice(status, sales_order_status_values())
    payment_status = _clean_choice(payment_status, sales_order_payment_status_values())
    if source_type not in {"quote", "manual"}:
        source_type = None

    filters = {
        "status": status,
        "payment_status": payment_status,
        "source_type": source_type,
        "search": search or None,
    }
    offset = (page - 1) * per_page
    orders = (
        _sales_orders_query(db, **filters)
        .order_by(SalesOrder.created_at.desc())
        .limit(per_page)
        .offset(offset)
        .all()
    )

    totals = (
        _sales_orders_query(db, **filters)
        .with_entities(
            func.count(SalesOrder.id).label("total"),
            func.coalesce(func.sum(SalesOrder.total), 0).label("gross_sales"),
            func.coalesce(func.sum(SalesOrder.amount_paid), 0).label("collected"),
            func.coalesce(func.sum(SalesOrder.balance_due), 0).label("outstanding"),
            func.sum(
                case(
                    (
                        SalesOrder.payment_status == SalesOrderPaymentStatus.paid.value,
                        1,
                    ),
                    else_=0,
                )
            ).label("paid"),
            func.sum(
                case(
                    (
                        SalesOrder.payment_status
                        == SalesOrderPaymentStatus.partial.value,
                        1,
                    ),
                    else_=0,
                )
            ).label("partial"),
            func.sum(
                case(
                    (
                        SalesOrder.payment_status
                        == SalesOrderPaymentStatus.pending.value,
                        1,
                    ),
                    else_=0,
                )
            ).label("pending_payment"),
            func.sum(case((SalesOrder.quote_id.isnot(None), 1), else_=0)).label(
                "quote_backed"
            ),
            func.sum(case((SalesOrder.quote_id.is_(None), 1), else_=0)).label("manual"),
        )
        .one()
    )
    total = int(totals.total or 0)
    paid = int(totals.paid or 0)
    stats = {
        "total": total,
        "gross_sales": Decimal(str(totals.gross_sales or 0)),
        "collected": Decimal(str(totals.collected or 0)),
        "outstanding": Decimal(str(totals.outstanding or 0)),
        "paid": paid,
        "partial": int(totals.partial or 0),
        "pending_payment": int(totals.pending_payment or 0),
        "quote_backed": int(totals.quote_backed or 0),
        "manual": int(totals.manual or 0),
        "paid_rate": round((paid / total) * 100, 1) if total else 0,
    }

    subscriber_map = _subscriber_map(db, [order.subscriber_id for order in orders])

    return {
        "orders": orders,
        "stats": stats,
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": _total_pages(total, per_page),
        "status": status or "",
        "payment_status": payment_status or "",
        "source_type": source_type or "",
        "search": search or "",
        "statuses": sales_order_status_values(),
        "payment_statuses": sales_order_payment_status_values(),
        "subscriber_map": subscriber_map,
    }


def build_sales_order_detail_context(
    db: Session, *, sales_order_id: str
) -> dict[str, Any]:
    order = sales_orders_service.sales_orders.get(db, sales_order_id)
    lines = sales_orders_service.sales_order_lines.list(
        db,
        sales_order_id=str(order.id),
        order_by="created_at",
        order_dir="asc",
        limit=500,
        offset=0,
    )
    project = _resolve_project_for_sales_order(db, order.id)
    return {
        "order": order,
        "lines": lines,
        "subscriber": order.subscriber,
        "subscriber_label": subscriber_label(order.subscriber),
        "quote": order.quote,
        "project": project,
    }
