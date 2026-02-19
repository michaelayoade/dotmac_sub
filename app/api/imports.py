from fastapi import APIRouter, Depends, File, UploadFile, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import imports as import_service

router = APIRouter(prefix="/imports", tags=["imports"])


@router.post(
    "/subscriber-custom-fields",
    status_code=status.HTTP_201_CREATED,
)
def import_subscriber_custom_fields(
    file: UploadFile = File(...), db: Session = Depends(get_db)
):
    return import_service.import_subscriber_custom_fields_upload(db, file)
