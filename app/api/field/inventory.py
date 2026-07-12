from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.schemas.common import ListResponse
from app.schemas.field import FieldInventoryItemRead, FieldInventoryLocationRead
from app.services.auth_dependencies import require_user_auth
from app.services.dotmac_erp.client import build_erp_client
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
    db: Session = Depends(get_db),
):
    try:
        with build_erp_client(db) as client:
            warehouses = client.list_inventory_warehouses()
    except ValueError:
        warehouses = field_inventory_lookup.list_locations()
    items = [
        {
            "id": row.get("warehouse_id") or row.get("id"),
            "name": row.get("name") or row.get("warehouse_name") or row.get("code"),
            "code": row.get("code") or row.get("warehouse_code"),
            "is_active": row.get("is_active", True),
        }
        for row in warehouses
    ][offset : offset + limit]
    return {"items": items, "count": len(items), "limit": limit, "offset": offset}


@router.get("/stock")
def list_erp_inventory_stock(
    search: str | None = None,
    category_code: str | None = None,
    warehouse_id: str | None = None,
    include_zero_stock: bool = False,
    only_below_reorder: bool = False,
    only_with_available_serials: bool = False,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    _auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    """Live stock read from ERP; Sub does not maintain a second stock ledger."""
    with build_erp_client(db) as client:
        return client.list_inventory(
            search=search,
            category_code=category_code,
            warehouse_id=warehouse_id,
            include_zero_stock=include_zero_stock,
            only_below_reorder=only_below_reorder,
            only_with_available_serials=only_with_available_serials,
            limit=limit,
            offset=offset,
        )


@router.get("/stock/{item_id}")
def get_erp_inventory_stock_item(
    item_id: str,
    _auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    from fastapi import HTTPException

    with build_erp_client(db) as client:
        item = client.get_inventory_item(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Inventory item not found")
    return item


@router.get("/categories")
def list_erp_inventory_categories(
    _auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    with build_erp_client(db) as client:
        items = client.list_inventory_categories()
    return {"items": items, "count": len(items)}


@router.get("/serials/available")
def list_erp_available_serials(
    item_code: str = Query(min_length=1, max_length=50),
    warehouse_code: str = Query(min_length=1, max_length=100),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    _auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    with build_erp_client(db) as client:
        return client.list_available_serials(
            item_code=item_code,
            warehouse_code=warehouse_code,
            limit=limit,
            offset=offset,
        )
