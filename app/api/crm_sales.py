"""Leads / pipeline / quotes API — CRM port (Phase 3 §2.4).

Ported from ``dotmac_crm/app/api/crm/sales.py`` (same paths, mounted under
``/api/v1``). Permission tightening per §2.4: the CRM gated leads on
``crm:lead:{read,write}`` but left pipelines and quotes auth-only —
during the port quotes gain ``crm:quote:{read,write}`` and pipelines ride
``crm:lead:*`` (they are lead-vertical infrastructure; §2.4 introduces no
dedicated pipeline key). RBAC seeding of the new keys lands with the
referrals-admin PR of the Phase 3 series (§6 PR 12); until then the keys
resolve for admins only.
"""

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_permission
from app.schemas.common import ListResponse
from app.schemas.sales import (
    LeadCreate,
    LeadRead,
    LeadUpdate,
    PipelineCreate,
    PipelineRead,
    PipelineStageCreate,
    PipelineStageRead,
    PipelineStageUpdate,
    PipelineUpdate,
    QuoteCreate,
    QuoteLineItemCreate,
    QuoteLineItemRead,
    QuoteLineItemUpdate,
    QuoteRead,
    QuoteUpdate,
)
from app.services import sales as sales_service

router = APIRouter(prefix="/crm", tags=["crm-sales"])


@router.post(
    "/pipelines",
    response_model=PipelineRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("crm:lead:write"))],
)
def create_pipeline(payload: PipelineCreate, db: Session = Depends(get_db)):
    return sales_service.pipelines.create(db, payload)


@router.get(
    "/pipelines",
    response_model=ListResponse[PipelineRead],
    dependencies=[Depends(require_permission("crm:lead:read"))],
)
def list_pipelines(
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return sales_service.pipelines.list_response(
        db, is_active, order_by, order_dir, limit, offset
    )


@router.get(
    "/pipelines/{pipeline_id}",
    response_model=PipelineRead,
    dependencies=[Depends(require_permission("crm:lead:read"))],
)
def get_pipeline(pipeline_id: str, db: Session = Depends(get_db)):
    return sales_service.pipelines.get(db, pipeline_id)


@router.patch(
    "/pipelines/{pipeline_id}",
    response_model=PipelineRead,
    dependencies=[Depends(require_permission("crm:lead:write"))],
)
def update_pipeline(
    pipeline_id: str, payload: PipelineUpdate, db: Session = Depends(get_db)
):
    return sales_service.pipelines.update(db, pipeline_id, payload)


@router.delete(
    "/pipelines/{pipeline_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_permission("crm:lead:write"))],
)
def delete_pipeline(pipeline_id: str, db: Session = Depends(get_db)):
    sales_service.pipelines.delete(db, pipeline_id)


@router.post(
    "/pipelines/{pipeline_id}/stages",
    response_model=PipelineStageRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("crm:lead:write"))],
)
def create_pipeline_stage(
    pipeline_id: str, payload: PipelineStageCreate, db: Session = Depends(get_db)
):
    data = payload.model_copy(update={"pipeline_id": pipeline_id})
    return sales_service.pipeline_stages.create(db, data)


@router.get(
    "/pipelines/{pipeline_id}/stages",
    response_model=ListResponse[PipelineStageRead],
    dependencies=[Depends(require_permission("crm:lead:read"))],
)
def list_pipeline_stages(
    pipeline_id: str,
    is_active: bool | None = None,
    order_by: str = Query(default="order_index"),
    order_dir: str = Query(default="asc", pattern="^(asc|desc)$"),
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return sales_service.pipeline_stages.list_response(
        db, pipeline_id, is_active, order_by, order_dir, limit, offset
    )


@router.patch(
    "/pipeline-stages/{stage_id}",
    response_model=PipelineStageRead,
    dependencies=[Depends(require_permission("crm:lead:write"))],
)
def update_pipeline_stage(
    stage_id: str, payload: PipelineStageUpdate, db: Session = Depends(get_db)
):
    return sales_service.pipeline_stages.update(db, stage_id, payload)


@router.post(
    "/leads",
    response_model=LeadRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("crm:lead:write"))],
)
def create_lead(payload: LeadCreate, db: Session = Depends(get_db)):
    return sales_service.leads.create(db, payload)


@router.get(
    "/leads",
    response_model=ListResponse[LeadRead],
    dependencies=[Depends(require_permission("crm:lead:read"))],
)
def list_leads(
    pipeline_id: str | None = None,
    stage_id: str | None = None,
    owner_agent_id: str | None = None,
    status: str | None = None,
    lead_source: str | None = None,
    is_active: bool | None = None,
    order_by: str = Query(default="updated_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return sales_service.leads.list_response(
        db,
        pipeline_id=pipeline_id,
        stage_id=stage_id,
        owner_agent_id=owner_agent_id,
        status=status,
        lead_source=lead_source,
        is_active=is_active,
        order_by=order_by,
        order_dir=order_dir,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/leads/{lead_id}",
    response_model=LeadRead,
    dependencies=[Depends(require_permission("crm:lead:read"))],
)
def get_lead(lead_id: str, db: Session = Depends(get_db)):
    return sales_service.leads.get(db, lead_id)


@router.patch(
    "/leads/{lead_id}",
    response_model=LeadRead,
    dependencies=[Depends(require_permission("crm:lead:write"))],
)
def update_lead(lead_id: str, payload: LeadUpdate, db: Session = Depends(get_db)):
    return sales_service.leads.update(db, lead_id, payload)


@router.delete(
    "/leads/{lead_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_permission("crm:lead:write"))],
)
def delete_lead(lead_id: str, db: Session = Depends(get_db)):
    sales_service.leads.delete(db, lead_id)


@router.post(
    "/quotes",
    response_model=QuoteRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("crm:quote:write"))],
)
def create_quote(payload: QuoteCreate, db: Session = Depends(get_db)):
    return sales_service.quotes.create(db, payload)


@router.get(
    "/quotes",
    response_model=ListResponse[QuoteRead],
    dependencies=[Depends(require_permission("crm:quote:read"))],
)
def list_quotes(
    lead_id: str | None = None,
    status: str | None = None,
    is_active: bool | None = None,
    order_by: str = Query(default="updated_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return sales_service.quotes.list_response(
        db, lead_id, status, is_active, order_by, order_dir, limit, offset
    )


@router.get(
    "/quotes/{quote_id}",
    response_model=QuoteRead,
    dependencies=[Depends(require_permission("crm:quote:read"))],
)
def get_quote(quote_id: str, db: Session = Depends(get_db)):
    return sales_service.quotes.get(db, quote_id)


@router.patch(
    "/quotes/{quote_id}",
    response_model=QuoteRead,
    dependencies=[Depends(require_permission("crm:quote:write"))],
)
def update_quote(quote_id: str, payload: QuoteUpdate, db: Session = Depends(get_db)):
    return sales_service.quotes.update(db, quote_id, payload)


@router.delete(
    "/quotes/{quote_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_permission("crm:quote:write"))],
)
def delete_quote(quote_id: str, db: Session = Depends(get_db)):
    sales_service.quotes.delete(db, quote_id)


@router.post(
    "/quotes/{quote_id}/line-items",
    response_model=QuoteLineItemRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("crm:quote:write"))],
)
def create_quote_line_item(
    quote_id: str, payload: QuoteLineItemCreate, db: Session = Depends(get_db)
):
    data = payload.model_copy(update={"quote_id": quote_id})
    return sales_service.quote_line_items.create(db, data)


@router.get(
    "/quotes/{quote_id}/line-items",
    response_model=ListResponse[QuoteLineItemRead],
    dependencies=[Depends(require_permission("crm:quote:read"))],
)
def list_quote_line_items(
    quote_id: str,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="asc", pattern="^(asc|desc)$"),
    limit: int = Query(default=200, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return sales_service.quote_line_items.list_response(
        db, quote_id, order_by, order_dir, limit, offset
    )


@router.patch(
    "/quote-line-items/{item_id}",
    response_model=QuoteLineItemRead,
    dependencies=[Depends(require_permission("crm:quote:write"))],
)
def update_quote_line_item(
    item_id: str, payload: QuoteLineItemUpdate, db: Session = Depends(get_db)
):
    return sales_service.quote_line_items.update(db, item_id, payload)
