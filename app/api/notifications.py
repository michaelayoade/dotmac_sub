from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.schemas.common import ListResponse
from app.schemas.notification import (
    AlertNotificationLogRead,
    AlertNotificationPolicyCreate,
    AlertNotificationPolicyRead,
    AlertNotificationPolicyUpdate,
    AlertNotificationPolicyStepCreate,
    AlertNotificationPolicyStepRead,
    AlertNotificationPolicyStepUpdate,
    NotificationBulkCreateRequest,
    NotificationBulkCreateResponse,
    NotificationCreate,
    NotificationDeliveryBulkUpdateRequest,
    NotificationDeliveryBulkUpdateResponse,
    NotificationDeliveryCreate,
    NotificationDeliveryRead,
    NotificationDeliveryUpdate,
    NotificationRead,
    NotificationTemplateCreate,
    NotificationTemplateRead,
    NotificationTemplateUpdate,
    NotificationUpdate,
    OnCallRotationCreate,
    OnCallRotationMemberCreate,
    OnCallRotationMemberRead,
    OnCallRotationMemberUpdate,
    OnCallRotationRead,
    OnCallRotationUpdate,
)
from app.services import notification as notification_service

router = APIRouter()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@router.post(
    "/notification-templates",
    response_model=NotificationTemplateRead,
    status_code=status.HTTP_201_CREATED,
    tags=["notification-templates"],
)
def create_template(
    payload: NotificationTemplateCreate, db: Session = Depends(get_db)
):
    return notification_service.templates.create(db, payload)


@router.get(
    "/notification-templates/{template_id}",
    response_model=NotificationTemplateRead,
    tags=["notification-templates"],
)
def get_template(template_id: str, db: Session = Depends(get_db)):
    return notification_service.templates.get(db, template_id)


@router.get(
    "/notification-templates",
    response_model=ListResponse[NotificationTemplateRead],
    tags=["notification-templates"],
)
def list_templates(
    channel: str | None = None,
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return notification_service.templates.list_response(
        db, channel, is_active, order_by, order_dir, limit, offset
    )


@router.patch(
    "/notification-templates/{template_id}",
    response_model=NotificationTemplateRead,
    tags=["notification-templates"],
)
def update_template(
    template_id: str, payload: NotificationTemplateUpdate, db: Session = Depends(get_db)
):
    return notification_service.templates.update(db, template_id, payload)


@router.delete(
    "/notification-templates/{template_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["notification-templates"],
)
def delete_template(template_id: str, db: Session = Depends(get_db)):
    notification_service.templates.delete(db, template_id)


@router.post(
    "/notifications",
    response_model=NotificationRead,
    status_code=status.HTTP_201_CREATED,
    tags=["notifications"],
)
def create_notification(payload: NotificationCreate, db: Session = Depends(get_db)):
    return notification_service.notifications.create(db, payload)


@router.post(
    "/notifications/bulk",
    response_model=NotificationBulkCreateResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["notifications"],
)
def create_notifications_bulk(
    payload: NotificationBulkCreateRequest, db: Session = Depends(get_db)
):
    response = notification_service.notifications.bulk_create_response(db, payload)
    return NotificationBulkCreateResponse(**response)


@router.post(
    "/notification-deliveries/bulk",
    response_model=NotificationDeliveryBulkUpdateResponse,
    tags=["notification-deliveries"],
)
def update_notification_deliveries_bulk(
    payload: NotificationDeliveryBulkUpdateRequest, db: Session = Depends(get_db)
):
    response = notification_service.deliveries.bulk_update_response(db, payload)
    return NotificationDeliveryBulkUpdateResponse(**response)


@router.get(
    "/notifications/{notification_id}",
    response_model=NotificationRead,
    tags=["notifications"],
)
def get_notification(notification_id: str, db: Session = Depends(get_db)):
    return notification_service.notifications.get(db, notification_id)


@router.get(
    "/notifications",
    response_model=ListResponse[NotificationRead],
    tags=["notifications"],
)
def list_notifications(
    channel: str | None = None,
    status: str | None = None,
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return notification_service.notifications.list_response(
        db, channel, status, is_active, order_by, order_dir, limit, offset
    )


@router.patch(
    "/notifications/{notification_id}",
    response_model=NotificationRead,
    tags=["notifications"],
)
def update_notification(
    notification_id: str, payload: NotificationUpdate, db: Session = Depends(get_db)
):
    return notification_service.notifications.update(db, notification_id, payload)


@router.delete(
    "/notifications/{notification_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["notifications"],
)
def delete_notification(notification_id: str, db: Session = Depends(get_db)):
    notification_service.notifications.delete(db, notification_id)


@router.post(
    "/notification-deliveries",
    response_model=NotificationDeliveryRead,
    status_code=status.HTTP_201_CREATED,
    tags=["notification-deliveries"],
)
def create_delivery(
    payload: NotificationDeliveryCreate, db: Session = Depends(get_db)
):
    return notification_service.deliveries.create(db, payload)


@router.get(
    "/notification-deliveries/{delivery_id}",
    response_model=NotificationDeliveryRead,
    tags=["notification-deliveries"],
)
def get_delivery(delivery_id: str, db: Session = Depends(get_db)):
    return notification_service.deliveries.get(db, delivery_id)


@router.get(
    "/notification-deliveries",
    response_model=ListResponse[NotificationDeliveryRead],
    tags=["notification-deliveries"],
)
def list_deliveries(
    notification_id: str | None = None,
    status: str | None = None,
    is_active: bool | None = None,
    order_by: str = Query(default="occurred_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return notification_service.deliveries.list_response(
        db, notification_id, status, is_active, order_by, order_dir, limit, offset
    )


@router.patch(
    "/notification-deliveries/{delivery_id}",
    response_model=NotificationDeliveryRead,
    tags=["notification-deliveries"],
)
def update_delivery(
    delivery_id: str, payload: NotificationDeliveryUpdate, db: Session = Depends(get_db)
):
    return notification_service.deliveries.update(db, delivery_id, payload)


@router.delete(
    "/notification-deliveries/{delivery_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["notification-deliveries"],
)
def delete_delivery(delivery_id: str, db: Session = Depends(get_db)):
    notification_service.deliveries.delete(db, delivery_id)


@router.post(
    "/alert-notification-policies",
    response_model=AlertNotificationPolicyRead,
    status_code=status.HTTP_201_CREATED,
    tags=["alert-notification-policies"],
)
def create_alert_notification_policy(
    payload: AlertNotificationPolicyCreate, db: Session = Depends(get_db)
):
    return notification_service.alert_notification_policies.create(db, payload)


@router.get(
    "/alert-notification-policies/{policy_id}",
    response_model=AlertNotificationPolicyRead,
    tags=["alert-notification-policies"],
)
def get_alert_notification_policy(policy_id: str, db: Session = Depends(get_db)):
    return notification_service.alert_notification_policies.get(db, policy_id)


@router.get(
    "/alert-notification-policies",
    response_model=ListResponse[AlertNotificationPolicyRead],
    tags=["alert-notification-policies"],
)
def list_alert_notification_policies(
    channel: str | None = None,
    status: str | None = None,
    severity_min: str | None = None,
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return notification_service.alert_notification_policies.list_response(
        db, channel, status, severity_min, is_active, order_by, order_dir, limit, offset
    )


@router.patch(
    "/alert-notification-policies/{policy_id}",
    response_model=AlertNotificationPolicyRead,
    tags=["alert-notification-policies"],
)
def update_alert_notification_policy(
    policy_id: str, payload: AlertNotificationPolicyUpdate, db: Session = Depends(get_db)
):
    return notification_service.alert_notification_policies.update(db, policy_id, payload)


@router.delete(
    "/alert-notification-policies/{policy_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["alert-notification-policies"],
)
def delete_alert_notification_policy(policy_id: str, db: Session = Depends(get_db)):
    notification_service.alert_notification_policies.delete(db, policy_id)


@router.get(
    "/alert-notification-logs",
    response_model=ListResponse[AlertNotificationLogRead],
    tags=["alert-notification-logs"],
)
def list_alert_notification_logs(
    alert_id: str | None = None,
    policy_id: str | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return notification_service.alert_notification_logs.list_response(
        db, alert_id, policy_id, order_by, order_dir, limit, offset
    )


@router.post(
    "/alert-notification-policy-steps",
    response_model=AlertNotificationPolicyStepRead,
    status_code=status.HTTP_201_CREATED,
    tags=["alert-notification-policy-steps"],
)
def create_alert_notification_policy_step(
    payload: AlertNotificationPolicyStepCreate, db: Session = Depends(get_db)
):
    return notification_service.alert_notification_policy_steps.create(db, payload)


@router.get(
    "/alert-notification-policy-steps/{step_id}",
    response_model=AlertNotificationPolicyStepRead,
    tags=["alert-notification-policy-steps"],
)
def get_alert_notification_policy_step(step_id: str, db: Session = Depends(get_db)):
    return notification_service.alert_notification_policy_steps.get(db, step_id)


@router.get(
    "/alert-notification-policy-steps",
    response_model=ListResponse[AlertNotificationPolicyStepRead],
    tags=["alert-notification-policy-steps"],
)
def list_alert_notification_policy_steps(
    policy_id: str | None = None,
    status: str | None = None,
    is_active: bool | None = None,
    order_by: str = Query(default="step_index"),
    order_dir: str = Query(default="asc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return notification_service.alert_notification_policy_steps.list_response(
        db, policy_id, status, is_active, order_by, order_dir, limit, offset
    )


@router.patch(
    "/alert-notification-policy-steps/{step_id}",
    response_model=AlertNotificationPolicyStepRead,
    tags=["alert-notification-policy-steps"],
)
def update_alert_notification_policy_step(
    step_id: str, payload: AlertNotificationPolicyStepUpdate, db: Session = Depends(get_db)
):
    return notification_service.alert_notification_policy_steps.update(db, step_id, payload)


@router.delete(
    "/alert-notification-policy-steps/{step_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["alert-notification-policy-steps"],
)
def delete_alert_notification_policy_step(step_id: str, db: Session = Depends(get_db)):
    notification_service.alert_notification_policy_steps.delete(db, step_id)


@router.post(
    "/on-call-rotations",
    response_model=OnCallRotationRead,
    status_code=status.HTTP_201_CREATED,
    tags=["on-call-rotations"],
)
def create_on_call_rotation(payload: OnCallRotationCreate, db: Session = Depends(get_db)):
    return notification_service.on_call_rotations.create(db, payload)


@router.get(
    "/on-call-rotations/{rotation_id}",
    response_model=OnCallRotationRead,
    tags=["on-call-rotations"],
)
def get_on_call_rotation(rotation_id: str, db: Session = Depends(get_db)):
    return notification_service.on_call_rotations.get(db, rotation_id)


@router.get(
    "/on-call-rotations",
    response_model=ListResponse[OnCallRotationRead],
    tags=["on-call-rotations"],
)
def list_on_call_rotations(
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return notification_service.on_call_rotations.list_response(
        db, is_active, order_by, order_dir, limit, offset
    )


@router.patch(
    "/on-call-rotations/{rotation_id}",
    response_model=OnCallRotationRead,
    tags=["on-call-rotations"],
)
def update_on_call_rotation(
    rotation_id: str, payload: OnCallRotationUpdate, db: Session = Depends(get_db)
):
    return notification_service.on_call_rotations.update(db, rotation_id, payload)


@router.delete(
    "/on-call-rotations/{rotation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["on-call-rotations"],
)
def delete_on_call_rotation(rotation_id: str, db: Session = Depends(get_db)):
    notification_service.on_call_rotations.delete(db, rotation_id)


@router.post(
    "/on-call-rotation-members",
    response_model=OnCallRotationMemberRead,
    status_code=status.HTTP_201_CREATED,
    tags=["on-call-rotation-members"],
)
def create_on_call_rotation_member(
    payload: OnCallRotationMemberCreate, db: Session = Depends(get_db)
):
    return notification_service.on_call_rotation_members.create(db, payload)


@router.get(
    "/on-call-rotation-members/{member_id}",
    response_model=OnCallRotationMemberRead,
    tags=["on-call-rotation-members"],
)
def get_on_call_rotation_member(member_id: str, db: Session = Depends(get_db)):
    return notification_service.on_call_rotation_members.get(db, member_id)


@router.get(
    "/on-call-rotation-members",
    response_model=ListResponse[OnCallRotationMemberRead],
    tags=["on-call-rotation-members"],
)
def list_on_call_rotation_members(
    rotation_id: str | None = None,
    is_active: bool | None = None,
    order_by: str = Query(default="priority"),
    order_dir: str = Query(default="asc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return notification_service.on_call_rotation_members.list_response(
        db, rotation_id, is_active, order_by, order_dir, limit, offset
    )


@router.patch(
    "/on-call-rotation-members/{member_id}",
    response_model=OnCallRotationMemberRead,
    tags=["on-call-rotation-members"],
)
def update_on_call_rotation_member(
    member_id: str, payload: OnCallRotationMemberUpdate, db: Session = Depends(get_db)
):
    return notification_service.on_call_rotation_members.update(db, member_id, payload)


@router.delete(
    "/on-call-rotation-members/{member_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["on-call-rotation-members"],
)
def delete_on_call_rotation_member(member_id: str, db: Session = Depends(get_db)):
    notification_service.on_call_rotation_members.delete(db, member_id)
