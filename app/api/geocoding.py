from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.schemas.geocoding import GeocodePreviewRequest, GeocodePreviewResult
from app.services import geocoding as geocoding_service

router = APIRouter(prefix="/geocode", tags=["geocoding"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@router.post(
    "/preview",
    response_model=list[GeocodePreviewResult],
    status_code=status.HTTP_200_OK,
)
def preview_geocode(payload: GeocodePreviewRequest, db: Session = Depends(get_db)):
    return geocoding_service.geocode_preview_from_request(db, payload)
