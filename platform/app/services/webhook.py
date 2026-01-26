from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.connector import ConnectorConfig
from app.models.webhook import (
    WebhookDelivery,
    WebhookDeliveryStatus,
    WebhookEndpoint,
    WebhookEventType,
    WebhookSubscription,
)
from app.schemas.webhook import (
    WebhookDeliveryCreate,
    WebhookDeliveryUpdate,
    WebhookEndpointCreate,
    WebhookEndpointUpdate,
    WebhookSubscriptionCreate,
    WebhookSubscriptionUpdate,
)


def _apply_ordering(query, order_by, order_dir, allowed_columns):
    if order_by not in allowed_columns:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid order_by. Allowed: {', '.join(sorted(allowed_columns))}",
        )
    column = allowed_columns[order_by]
    if order_dir == "desc":
        return query.order_by(column.desc())
    return query.order_by(column.asc())


def _apply_pagination(query, limit, offset):
    return query.limit(limit).offset(offset)


def _validate_enum(value, enum_cls, label):
    if value is None:
        return None
    try:
        return enum_cls(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid {label}") from exc


class WebhookEndpoints:
    @staticmethod
    def create(db: Session, payload: WebhookEndpointCreate):
        if payload.connector_config_id:
            config = db.get(ConnectorConfig, payload.connector_config_id)
            if not config:
                raise HTTPException(status_code=404, detail="Connector config not found")
        endpoint = WebhookEndpoint(**payload.model_dump())
        db.add(endpoint)
        db.commit()
        db.refresh(endpoint)
        return endpoint

    @staticmethod
    def get(db: Session, endpoint_id: str):
        endpoint = db.get(WebhookEndpoint, endpoint_id)
        if not endpoint:
            raise HTTPException(status_code=404, detail="Webhook endpoint not found")
        return endpoint

    @staticmethod
    def list(
        db: Session,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(WebhookEndpoint)
        if is_active is None:
            query = query.filter(WebhookEndpoint.is_active.is_(True))
        else:
            query = query.filter(WebhookEndpoint.is_active == is_active)
        query = _apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": WebhookEndpoint.created_at, "name": WebhookEndpoint.name},
        )
        return _apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, endpoint_id: str, payload: WebhookEndpointUpdate):
        endpoint = db.get(WebhookEndpoint, endpoint_id)
        if not endpoint:
            raise HTTPException(status_code=404, detail="Webhook endpoint not found")
        data = payload.model_dump(exclude_unset=True)
        if "connector_config_id" in data and data["connector_config_id"]:
            config = db.get(ConnectorConfig, data["connector_config_id"])
            if not config:
                raise HTTPException(status_code=404, detail="Connector config not found")
        for key, value in data.items():
            setattr(endpoint, key, value)
        db.commit()
        db.refresh(endpoint)
        return endpoint

    @staticmethod
    def delete(db: Session, endpoint_id: str):
        endpoint = db.get(WebhookEndpoint, endpoint_id)
        if not endpoint:
            raise HTTPException(status_code=404, detail="Webhook endpoint not found")
        endpoint.is_active = False
        db.commit()


class WebhookSubscriptions:
    @staticmethod
    def create(db: Session, payload: WebhookSubscriptionCreate):
        endpoint = db.get(WebhookEndpoint, payload.endpoint_id)
        if not endpoint:
            raise HTTPException(status_code=404, detail="Webhook endpoint not found")
        subscription = WebhookSubscription(**payload.model_dump())
        db.add(subscription)
        db.commit()
        db.refresh(subscription)
        return subscription

    @staticmethod
    def get(db: Session, subscription_id: str):
        subscription = db.get(WebhookSubscription, subscription_id)
        if not subscription:
            raise HTTPException(status_code=404, detail="Webhook subscription not found")
        return subscription

    @staticmethod
    def list(
        db: Session,
        endpoint_id: str | None,
        event_type: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(WebhookSubscription)
        if endpoint_id:
            query = query.filter(WebhookSubscription.endpoint_id == endpoint_id)
        if event_type:
            query = query.filter(
                WebhookSubscription.event_type
                == _validate_enum(event_type, WebhookEventType, "event_type")
            )
        if is_active is None:
            query = query.filter(WebhookSubscription.is_active.is_(True))
        else:
            query = query.filter(WebhookSubscription.is_active == is_active)
        query = _apply_ordering(
            query,
            order_by,
            order_dir,
            {
                "created_at": WebhookSubscription.created_at,
                "event_type": WebhookSubscription.event_type,
            },
        )
        return _apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, subscription_id: str, payload: WebhookSubscriptionUpdate):
        subscription = db.get(WebhookSubscription, subscription_id)
        if not subscription:
            raise HTTPException(status_code=404, detail="Webhook subscription not found")
        data = payload.model_dump(exclude_unset=True)
        if "endpoint_id" in data:
            endpoint = db.get(WebhookEndpoint, data["endpoint_id"])
            if not endpoint:
                raise HTTPException(status_code=404, detail="Webhook endpoint not found")
        for key, value in data.items():
            setattr(subscription, key, value)
        db.commit()
        db.refresh(subscription)
        return subscription

    @staticmethod
    def delete(db: Session, subscription_id: str):
        subscription = db.get(WebhookSubscription, subscription_id)
        if not subscription:
            raise HTTPException(status_code=404, detail="Webhook subscription not found")
        subscription.is_active = False
        db.commit()


class WebhookDeliveries:
    @staticmethod
    def create(db: Session, payload: WebhookDeliveryCreate):
        subscription = db.get(WebhookSubscription, payload.subscription_id)
        if not subscription:
            raise HTTPException(status_code=404, detail="Webhook subscription not found")
        delivery = WebhookDelivery(
            subscription_id=subscription.id,
            endpoint_id=subscription.endpoint_id,
            event_type=payload.event_type,
            status=WebhookDeliveryStatus.pending,
            payload=payload.payload,
        )
        db.add(delivery)
        db.commit()
        db.refresh(delivery)
        return delivery

    @staticmethod
    def get(db: Session, delivery_id: str):
        delivery = db.get(WebhookDelivery, delivery_id)
        if not delivery:
            raise HTTPException(status_code=404, detail="Webhook delivery not found")
        return delivery

    @staticmethod
    def list(
        db: Session,
        endpoint_id: str | None,
        subscription_id: str | None,
        event_type: str | None,
        status: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(WebhookDelivery)
        if endpoint_id:
            query = query.filter(WebhookDelivery.endpoint_id == endpoint_id)
        if subscription_id:
            query = query.filter(WebhookDelivery.subscription_id == subscription_id)
        if event_type:
            query = query.filter(
                WebhookDelivery.event_type
                == _validate_enum(event_type, WebhookEventType, "event_type")
            )
        if status:
            query = query.filter(
                WebhookDelivery.status
                == _validate_enum(status, WebhookDeliveryStatus, "status")
            )
        query = _apply_ordering(
            query,
            order_by,
            order_dir,
            {
                "created_at": WebhookDelivery.created_at,
                "status": WebhookDelivery.status,
            },
        )
        return _apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, delivery_id: str, payload: WebhookDeliveryUpdate):
        delivery = db.get(WebhookDelivery, delivery_id)
        if not delivery:
            raise HTTPException(status_code=404, detail="Webhook delivery not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(delivery, key, value)
        db.commit()
        db.refresh(delivery)
        return delivery


webhook_endpoints = WebhookEndpoints()
webhook_subscriptions = WebhookSubscriptions()
webhook_deliveries = WebhookDeliveries()
