from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.schemas.common import ListResponse
from app.schemas.field import FieldExpenseRequestCreate, FieldExpenseRequestRead
from app.services.auth_dependencies import require_user_auth
from app.services.field.expense_requests import field_expense_requests

router = APIRouter(prefix="/expense-requests", tags=["field-expense-requests"])


@router.get("", response_model=ListResponse[FieldExpenseRequestRead])
def list_field_expense_requests(
    crm_work_order_id: str | None = None,
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    items = field_expense_requests.list_mine(
        db,
        auth,
        crm_work_order_id=crm_work_order_id,
        status=status_filter,
        limit=limit,
        offset=offset,
    )
    return {"items": items, "count": len(items), "limit": limit, "offset": offset}


@router.post(
    "",
    response_model=FieldExpenseRequestRead,
    status_code=status.HTTP_201_CREATED,
)
def create_field_expense_request(
    payload: FieldExpenseRequestCreate,
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    return field_expense_requests.create(
        db,
        auth,
        crm_work_order_id=payload.crm_work_order_id,
        purpose=payload.purpose,
        expense_date=payload.expense_date,
        currency=payload.currency,
        notes=payload.notes,
        client_ref=payload.client_ref,
        items=[item.model_dump() for item in payload.items],
    )


@router.get("/{expense_request_id}", response_model=FieldExpenseRequestRead)
def get_field_expense_request(
    expense_request_id: str,
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    return field_expense_requests.get(db, auth, expense_request_id)


@router.post("/{expense_request_id}/submit", response_model=FieldExpenseRequestRead)
def submit_field_expense_request(
    expense_request_id: str,
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    return field_expense_requests.submit(db, auth, expense_request_id)


@router.post("/{expense_request_id}/cancel", response_model=FieldExpenseRequestRead)
def cancel_field_expense_request(
    expense_request_id: str,
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    return field_expense_requests.cancel(db, auth, expense_request_id)
