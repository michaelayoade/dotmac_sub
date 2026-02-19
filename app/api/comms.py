from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas.common import ListResponse
from app.schemas.comms import (
    CustomerNotificationCreate,
    CustomerNotificationRead,
    CustomerNotificationUpdate,
    EtaUpdateCreate,
    EtaUpdateRead,
    SurveyCreate,
    SurveyRead,
    SurveyResponseCreate,
    SurveyResponseRead,
    SurveyUpdate,
)
from app.services import comms as comms_service
from app.services.response import list_response

router = APIRouter(prefix="/comms", tags=["comms"])


@router.post(
    "/customer-notifications",
    response_model=CustomerNotificationRead,
    status_code=status.HTTP_201_CREATED,
)
def create_customer_notification(
    payload: CustomerNotificationCreate, db: Session = Depends(get_db)
):
    return comms_service.customer_notifications.create(db, payload)


@router.get(
    "/customer-notifications/{event_id}", response_model=CustomerNotificationRead
)
def get_customer_notification(event_id: str, db: Session = Depends(get_db)):
    return comms_service.customer_notifications.get(db, event_id)


@router.get(
    "/customer-notifications",
    response_model=ListResponse[CustomerNotificationRead],
)
def list_customer_notifications(
    entity_type: str | None = None,
    entity_id: str | None = None,
    status: str | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    items = comms_service.customer_notifications.list(
        db, entity_type, entity_id, status, order_by, order_dir, limit, offset
    )
    return list_response(items, limit, offset)


@router.patch(
    "/customer-notifications/{event_id}",
    response_model=CustomerNotificationRead,
)
def update_customer_notification(
    event_id: str, payload: CustomerNotificationUpdate, db: Session = Depends(get_db)
):
    return comms_service.customer_notifications.update(db, event_id, payload)


@router.post(
    "/eta-updates",
    response_model=EtaUpdateRead,
    status_code=status.HTTP_201_CREATED,
)
def create_eta_update(payload: EtaUpdateCreate, db: Session = Depends(get_db)):
    return comms_service.eta_updates.create(db, payload)


@router.get("/eta-updates/{update_id}", response_model=EtaUpdateRead)
def get_eta_update(update_id: str, db: Session = Depends(get_db)):
    return comms_service.eta_updates.get(db, update_id)


@router.get("/eta-updates", response_model=ListResponse[EtaUpdateRead])
def list_eta_updates(
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    items = comms_service.eta_updates.list(
        db, order_by=order_by, order_dir=order_dir, limit=limit, offset=offset
    )
    return list_response(items, limit, offset)


@router.post("/surveys", response_model=SurveyRead, status_code=status.HTTP_201_CREATED)
def create_survey(payload: SurveyCreate, db: Session = Depends(get_db)):
    return comms_service.surveys.create(db, payload)


@router.get("/surveys/{survey_id}", response_model=SurveyRead)
def get_survey(survey_id: str, db: Session = Depends(get_db)):
    return comms_service.surveys.get(db, survey_id)


@router.get("/surveys", response_model=ListResponse[SurveyRead])
def list_surveys(
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    items = comms_service.surveys.list(db, is_active, order_by, order_dir, limit, offset)
    return list_response(items, limit, offset)


@router.patch("/surveys/{survey_id}", response_model=SurveyRead)
def update_survey(survey_id: str, payload: SurveyUpdate, db: Session = Depends(get_db)):
    return comms_service.surveys.update(db, survey_id, payload)


@router.delete("/surveys/{survey_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_survey(survey_id: str, db: Session = Depends(get_db)):
    comms_service.surveys.delete(db, survey_id)


@router.post(
    "/survey-responses",
    response_model=SurveyResponseRead,
    status_code=status.HTTP_201_CREATED,
)
def create_survey_response(
    payload: SurveyResponseCreate, db: Session = Depends(get_db)
):
    return comms_service.survey_responses.create(db, payload)


@router.get("/survey-responses/{response_id}", response_model=SurveyResponseRead)
def get_survey_response(response_id: str, db: Session = Depends(get_db)):
    return comms_service.survey_responses.get(db, response_id)


@router.get("/survey-responses", response_model=ListResponse[SurveyResponseRead])
def list_survey_responses(
    survey_id: str | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    items = comms_service.survey_responses.list(
        db, survey_id=survey_id, order_by=order_by, order_dir=order_dir, limit=limit, offset=offset
    )
    return list_response(items, limit, offset)
