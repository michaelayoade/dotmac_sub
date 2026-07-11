from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.schemas.asset_inventory import AssetCatalogResponse
from app.services.asset_inventory import AssetCatalogFilters, asset_inventory
from app.services.auth_dependencies import require_any_permission

router = APIRouter(prefix="/assets", tags=["asset-inventory"])

_asset_read = require_any_permission(
    "inventory:read",
    "network:device:read",
    "router:read",
)


@router.get(
    "",
    response_model=AssetCatalogResponse,
    dependencies=[Depends(_asset_read)],
)
def list_assets(
    source: str | None = Query(
        default=None,
        pattern="^(field_inventory|field_asset|ont|cpe|olt|network_device|router)$",
    ),
    q: str | None = Query(default=None),
    search: str | None = Query(default=None),
    status: str | None = Query(default=None),
    subscriber_id: str | None = Query(default=None),
    assigned_to_technician_id: str | None = Query(default=None),
    assigned_to_system_user_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return asset_inventory.list_catalog(
        db,
        AssetCatalogFilters(
            source=source,
            q=q or search,
            status=status,
            subscriber_id=subscriber_id,
            assigned_to_technician_id=assigned_to_technician_id,
            assigned_to_system_user_id=assigned_to_system_user_id,
            limit=limit,
            offset=offset,
        ),
    )
