from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session
from app.schemas.common import ListResponse

from app.db import SessionLocal
from app.schemas.webhook import (
    WebhookDeliveryCreate,
    WebhookDeliveryRead,
    WebhookDeliveryUpdate,
    WebhookEndpointCreate,
    WebhookEndpointRead,
    WebhookEndpointUpdate,
    WebhookSubscriptionCreate,
    WebhookSubscriptionRead,
    WebhookSubscriptionUpdate,
)
from app.services import webhook as webhook_service

router = APIRouter(prefix="/webhooks")


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@router.post(
    "/endpoints",
    response_model=WebhookEndpointRead,
    status_code=status.HTTP_201_CREATED,
    tags=["webhook-endpoints"],
)
def create_webhook_endpoint(payload: WebhookEndpointCreate, db: Session = Depends(get_db)):
    return webhook_service.webhook_endpoints.create(db, payload)


@router.get(
    "/endpoints/{endpoint_id}",
    response_model=WebhookEndpointRead,
    tags=["webhook-endpoints"],
)
def get_webhook_endpoint(endpoint_id: str, db: Session = Depends(get_db)):
    return webhook_service.webhook_endpoints.get(db, endpoint_id)


@router.get(
    "/endpoints",
    response_model=ListResponse[WebhookEndpointRead],
    tags=["webhook-endpoints"],
)
def list_webhook_endpoints(
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return webhook_service.webhook_endpoints.list_response(
        db, is_active, order_by, order_dir, limit, offset
    )


@router.patch(
    "/endpoints/{endpoint_id}",
    response_model=WebhookEndpointRead,
    tags=["webhook-endpoints"],
)
def update_webhook_endpoint(
    endpoint_id: str, payload: WebhookEndpointUpdate, db: Session = Depends(get_db)
):
    return webhook_service.webhook_endpoints.update(db, endpoint_id, payload)


@router.delete(
    "/endpoints/{endpoint_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["webhook-endpoints"],
)
def delete_webhook_endpoint(endpoint_id: str, db: Session = Depends(get_db)):
    webhook_service.webhook_endpoints.delete(db, endpoint_id)


@router.post(
    "/subscriptions",
    response_model=WebhookSubscriptionRead,
    status_code=status.HTTP_201_CREATED,
    tags=["webhook-subscriptions"],
)
def create_webhook_subscription(
    payload: WebhookSubscriptionCreate, db: Session = Depends(get_db)
):
    return webhook_service.webhook_subscriptions.create(db, payload)


@router.get(
    "/subscriptions/{subscription_id}",
    response_model=WebhookSubscriptionRead,
    tags=["webhook-subscriptions"],
)
def get_webhook_subscription(subscription_id: str, db: Session = Depends(get_db)):
    return webhook_service.webhook_subscriptions.get(db, subscription_id)


@router.get(
    "/subscriptions",
    response_model=ListResponse[WebhookSubscriptionRead],
    tags=["webhook-subscriptions"],
)
def list_webhook_subscriptions(
    endpoint_id: str | None = None,
    event_type: str | None = None,
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return webhook_service.webhook_subscriptions.list_response(
        db, endpoint_id, event_type, is_active, order_by, order_dir, limit, offset
    )


@router.patch(
    "/subscriptions/{subscription_id}",
    response_model=WebhookSubscriptionRead,
    tags=["webhook-subscriptions"],
)
def update_webhook_subscription(
    subscription_id: str, payload: WebhookSubscriptionUpdate, db: Session = Depends(get_db)
):
    return webhook_service.webhook_subscriptions.update(db, subscription_id, payload)


@router.delete(
    "/subscriptions/{subscription_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["webhook-subscriptions"],
)
def delete_webhook_subscription(subscription_id: str, db: Session = Depends(get_db)):
    webhook_service.webhook_subscriptions.delete(db, subscription_id)


@router.post(
    "/deliveries",
    response_model=WebhookDeliveryRead,
    status_code=status.HTTP_201_CREATED,
    tags=["webhook-deliveries"],
)
def create_webhook_delivery(
    payload: WebhookDeliveryCreate, db: Session = Depends(get_db)
):
    return webhook_service.webhook_deliveries.create(db, payload)


@router.get(
    "/deliveries/{delivery_id}",
    response_model=WebhookDeliveryRead,
    tags=["webhook-deliveries"],
)
def get_webhook_delivery(delivery_id: str, db: Session = Depends(get_db)):
    return webhook_service.webhook_deliveries.get(db, delivery_id)


@router.get(
    "/deliveries",
    response_model=ListResponse[WebhookDeliveryRead],
    tags=["webhook-deliveries"],
)
def list_webhook_deliveries(
    endpoint_id: str | None = None,
    subscription_id: str | None = None,
    event_type: str | None = None,
    status: str | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return webhook_service.webhook_deliveries.list_response(
        db,
        endpoint_id,
        subscription_id,
        event_type,
        status,
        order_by,
        order_dir,
        limit,
        offset,
    )


@router.patch(
    "/deliveries/{delivery_id}",
    response_model=WebhookDeliveryRead,
    tags=["webhook-deliveries"],
)
def update_webhook_delivery(
    delivery_id: str, payload: WebhookDeliveryUpdate, db: Session = Depends(get_db)
):
    return webhook_service.webhook_deliveries.update(db, delivery_id, payload)