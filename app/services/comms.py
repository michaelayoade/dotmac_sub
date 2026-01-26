from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.comms import (
    CustomerNotificationEvent,
    CustomerNotificationStatus,
    EtaUpdate,
    Survey,
    SurveyResponse,
)
from app.models.domain_settings import SettingDomain
from app.models.tickets import Ticket
from app.models.workforce import WorkOrder
from app.schemas.comms import (
    CustomerNotificationCreate,
    CustomerNotificationUpdate,
    EtaUpdateCreate,
    SurveyCreate,
    SurveyUpdate,
    SurveyResponseCreate,
)
from app.services.common import validate_enum, apply_pagination, apply_ordering, coerce_uuid
from app.services.response import ListResponseMixin
from app.services import settings_spec


def _ensure_work_order(db: Session, work_order_id: str):
    if not db.get(WorkOrder, coerce_uuid(work_order_id)):
        raise HTTPException(status_code=404, detail="Work order not found")


def _ensure_ticket(db: Session, ticket_id: str):
    if not db.get(Ticket, coerce_uuid(ticket_id)):
        raise HTTPException(status_code=404, detail="Ticket not found")


class CustomerNotifications(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: CustomerNotificationCreate):
        data = payload.model_dump()
        fields_set = payload.model_fields_set
        if "status" not in fields_set:
            default_status = settings_spec.resolve_value(
                db, SettingDomain.comms, "default_notification_status"
            )
            if default_status:
                data["status"] = CustomerNotificationStatus(default_status)
        event = CustomerNotificationEvent(**data)
        db.add(event)
        db.commit()
        db.refresh(event)
        return event

    @staticmethod
    def get(db: Session, event_id: str):
        event = db.get(CustomerNotificationEvent, event_id)
        if not event:
            raise HTTPException(status_code=404, detail="Notification event not found")
        return event

    @staticmethod
    def list(
        db: Session,
        entity_type: str | None,
        entity_id: str | None,
        status: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(CustomerNotificationEvent)
        if entity_type:
            query = query.filter(CustomerNotificationEvent.entity_type == entity_type)
        if entity_id:
            query = query.filter(CustomerNotificationEvent.entity_id == entity_id)
        if status:
            try:
                query = query.filter(
                    CustomerNotificationEvent.status
                    == CustomerNotificationStatus(status)
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="Invalid status") from exc
        query = apply_ordering(
            query, order_by, order_dir, {"created_at": CustomerNotificationEvent.created_at}
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, event_id: str, payload: CustomerNotificationUpdate):
        event = db.get(CustomerNotificationEvent, event_id)
        if not event:
            raise HTTPException(status_code=404, detail="Notification event not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(event, key, value)
        db.commit()
        db.refresh(event)
        return event


class EtaUpdates(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: EtaUpdateCreate):
        _ensure_work_order(db, str(payload.work_order_id))
        update = EtaUpdate(**payload.model_dump())
        db.add(update)
        db.commit()
        db.refresh(update)
        return update

    @staticmethod
    def get(db: Session, update_id: str):
        update = db.get(EtaUpdate, update_id)
        if not update:
            raise HTTPException(status_code=404, detail="ETA update not found")
        return update

    @staticmethod
    def list(
        db: Session,
        work_order_id: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(EtaUpdate)
        if work_order_id:
            query = query.filter(EtaUpdate.work_order_id == work_order_id)
        query = apply_ordering(query, order_by, order_dir, {"created_at": EtaUpdate.created_at})
        return apply_pagination(query, limit, offset).all()


class Surveys(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: SurveyCreate):
        survey = Survey(**payload.model_dump())
        db.add(survey)
        db.commit()
        db.refresh(survey)
        return survey

    @staticmethod
    def get(db: Session, survey_id: str):
        survey = db.get(Survey, survey_id)
        if not survey:
            raise HTTPException(status_code=404, detail="Survey not found")
        return survey

    @staticmethod
    def list(
        db: Session,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(Survey)
        if is_active is None:
            query = query.filter(Survey.is_active.is_(True))
        else:
            query = query.filter(Survey.is_active == is_active)
        query = apply_ordering(query, order_by, order_dir, {"created_at": Survey.created_at})
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, survey_id: str, payload: SurveyUpdate):
        survey = db.get(Survey, survey_id)
        if not survey:
            raise HTTPException(status_code=404, detail="Survey not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(survey, key, value)
        db.commit()
        db.refresh(survey)
        return survey

    @staticmethod
    def delete(db: Session, survey_id: str):
        survey = db.get(Survey, survey_id)
        if not survey:
            raise HTTPException(status_code=404, detail="Survey not found")
        survey.is_active = False
        db.commit()


class SurveyResponses(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: SurveyResponseCreate):
        if not db.get(Survey, payload.survey_id):
            raise HTTPException(status_code=404, detail="Survey not found")
        if payload.work_order_id:
            _ensure_work_order(db, str(payload.work_order_id))
        if payload.ticket_id:
            _ensure_ticket(db, str(payload.ticket_id))
        response = SurveyResponse(**payload.model_dump())
        db.add(response)
        db.commit()
        db.refresh(response)
        return response

    @staticmethod
    def get(db: Session, response_id: str):
        response = db.get(SurveyResponse, response_id)
        if not response:
            raise HTTPException(status_code=404, detail="Survey response not found")
        return response

    @staticmethod
    def list(
        db: Session,
        survey_id: str | None,
        work_order_id: str | None,
        ticket_id: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(SurveyResponse)
        if survey_id:
            query = query.filter(SurveyResponse.survey_id == survey_id)
        if work_order_id:
            query = query.filter(SurveyResponse.work_order_id == work_order_id)
        if ticket_id:
            query = query.filter(SurveyResponse.ticket_id == ticket_id)
        query = apply_ordering(query, order_by, order_dir, {"created_at": SurveyResponse.created_at})
        return apply_pagination(query, limit, offset).all()


customer_notifications = CustomerNotifications()
eta_updates = EtaUpdates()
surveys = Surveys()
survey_responses = SurveyResponses()
