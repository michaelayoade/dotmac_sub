from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas.geocoding import GeocodePreviewRequest, GeocodePreviewResult
from app.services import geocoding as geocoding_service

router = APIRouter(prefix="/geocode", tags=["geocoding"])


@router.post(
    "/preview",
    response_model=list[GeocodePreviewResult],
    status_code=status.HTTP_200_OK,
)
def preview_geocode(payload: GeocodePreviewRequest, db: Session = Depends(get_db)):
    return geocoding_service.geocode_preview_from_request(db, payload)
