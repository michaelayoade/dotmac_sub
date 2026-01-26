from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.lifecycle import LifecycleEventType, SubscriptionLifecycleEvent
from app.services.common import apply_ordering, apply_pagination, coerce_uuid, validate_enum
from app.models.domain_settings import SettingDomain
from app.schemas.lifecycle import (
    SubscriptionLifecycleEventCreate,
    SubscriptionLifecycleEventUpdate,
)
from app.services.response import ListResponseMixin
from app.services import settings_spec


class SubscriptionLifecycleEvents(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: SubscriptionLifecycleEventCreate):
        data = payload.model_dump()
        fields_set = payload.model_fields_set
        if "event_type" not in fields_set:
            default_type = settings_spec.resolve_value(
                db, SettingDomain.lifecycle, "default_event_type"
            )
            if default_type:
                data["event_type"] = validate_enum(
                    default_type, LifecycleEventType, "event_type"
                )
        event = SubscriptionLifecycleEvent(**data)
        db.add(event)
        db.commit()
        db.refresh(event)
        return event

    @staticmethod
    def get(db: Session, event_id: str):
        event = db.get(SubscriptionLifecycleEvent, event_id)
        if not event:
            raise HTTPException(status_code=404, detail="Lifecycle event not found")
        return event

    @staticmethod
    def list(
        db: Session,
        subscription_id: str | None,
        event_type: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(SubscriptionLifecycleEvent)
        if subscription_id:
            query = query.filter(
                SubscriptionLifecycleEvent.subscription_id == subscription_id
            )
        if event_type:
            query = query.filter(
                SubscriptionLifecycleEvent.event_type
                == validate_enum(event_type, LifecycleEventType, "event_type")
            )
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {
                "created_at": SubscriptionLifecycleEvent.created_at,
                "event_type": SubscriptionLifecycleEvent.event_type,
            },
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, event_id: str, payload: SubscriptionLifecycleEventUpdate):
        event = db.get(SubscriptionLifecycleEvent, event_id)
        if not event:
            raise HTTPException(status_code=404, detail="Lifecycle event not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(event, key, value)
        db.commit()
        db.refresh(event)
        return event

    @staticmethod
    def delete(db: Session, event_id: str):
        event = db.get(SubscriptionLifecycleEvent, event_id)
        if not event:
            raise HTTPException(status_code=404, detail="Lifecycle event not found")
        db.delete(event)
        db.commit()


subscription_lifecycle_events = SubscriptionLifecycleEvents()
