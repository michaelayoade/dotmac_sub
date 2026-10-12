"""Compatibility refusals for retired vendor-wide plant inventory endpoints."""

from fastapi import APIRouter, Depends

from app.services.field.vendor_auth import require_field_vendor_token
from app.services.field.work_order_access import FieldAccessError

router = APIRouter(prefix="/vendor/map-assets", tags=["field-vendor-map"])


def _retired_vendor_plant_lookup() -> None:
    raise FieldAccessError(
        code="operations.field_work_order_access.denied",
        message="Open an assigned work order to view its authorized destinations.",
        retryable=False,
    )


@router.get("/nearby")
def get_vendor_nearby_map_assets(
    _vendor: dict = Depends(require_field_vendor_token),
) -> None:
    _retired_vendor_plant_lookup()


@router.get("/search")
def search_vendor_map_assets(
    _vendor: dict = Depends(require_field_vendor_token),
) -> None:
    _retired_vendor_plant_lookup()
