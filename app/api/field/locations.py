from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.schemas.field import FieldRouteResponse
from app.services.auth_dependencies import require_user_auth
from app.services.field.routing import field_routing

router = APIRouter(prefix="/locations", tags=["field-locations"])


@router.get("/route", response_model=FieldRouteResponse)
def my_day_route(
    start_lat: float = Query(ge=-90, le=90),
    start_lng: float = Query(ge=-180, le=180),
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    return {
        "route": field_routing.order_day_route(
            db,
            auth,
            start_latitude=start_lat,
            start_longitude=start_lng,
        )
    }
