from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.schemas.field import FieldScheduleEntry
from app.services.auth_dependencies import require_user_auth
from app.services.field.schedule import field_schedule

router = APIRouter(tags=["field-schedule"])


@router.get("/schedule", response_model=list[FieldScheduleEntry])
def get_field_schedule(
    date_from: datetime | None = Query(default=None, alias="from"),
    date_to: datetime | None = Query(default=None, alias="to"),
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    return field_schedule.timeline(db, auth, date_from=date_from, date_to=date_to)
