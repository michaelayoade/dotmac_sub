from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.schemas.common import ListResponse
from app.schemas.field import FieldInventoryItemRead, FieldInventoryLocationRead
from app.services.auth_dependencies import require_user_auth
from app.services.field.inventory import field_inventory_lookup

router = APIRouter(prefix="/inventory", tags=["field-inventory"])


@router.get("/items", response_model=ListResponse[FieldInventoryItemRead])
def list_field_inventory_items(
    q: str | None = Query(default=None),
    search: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    items = field_inventory_lookup.list_items(
        db,
        search=q or search,
        limit=limit,
        offset=offset,
    )
    return {"items": items, "count": len(items), "limit": limit, "offset": offset}


@router.get("/locations", response_model=ListResponse[FieldInventoryLocationRead])
def list_field_inventory_locations(
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _auth: dict = Depends(require_user_auth),
):
    items = field_inventory_lookup.list_locations()
    return {"items": items, "count": len(items), "limit": limit, "offset": offset}
