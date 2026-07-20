from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.services.field.config import field_config

router = APIRouter(prefix="/field", tags=["field-config"])


class FieldConfigRead(BaseModel):
    min_app_version: str
    latest_app_version: str
    feature_flags: dict[str, bool]


@router.get("/config", response_model=FieldConfigRead)
def get_field_config(db: Session = Depends(get_db)):
    return field_config.get(db)
