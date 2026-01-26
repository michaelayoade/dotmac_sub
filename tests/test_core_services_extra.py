from datetime import datetime, timezone

from starlette.requests import Request
from starlette.responses import Response

from app.schemas.rbac import PermissionCreate, PersonRoleCreate, RoleCreate
from app.schemas.subscriber import SubscriberCreate
from app.schemas.webhook import WebhookEndpointCreate, WebhookSubscriptionCreate
from app.services import audit as audit_service
from app.services import rbac as rbac_service
from app.services import scheduler as scheduler_service
from app.services import subscriber as subscriber_service
from app.services import webhook as webhook_service


def test_subscriber_create_list(db_session, person):
    subscriber = subscriber_service.subscribers.create(
        db_session,
        SubscriberCreate(person_id=person.id),
    )
    items = subscriber_service.subscribers.list(
        db_session,
        subscriber_type="person",
        person_id=str(person.id),
        organization_id=None,
        order_by="created_at",
        order_dir="desc",
        limit=10,
        offset=0,
    )
    assert items[0].id == subscriber.id


def test_rbac_role_permission_link(db_session, person):
    role = rbac_service.roles.create(db_session, RoleCreate(name="admin"))
    permission = rbac_service.permissions.create(
        db_session, PermissionCreate(key="tickets:read", name="Tickets Read")
    )
    link = rbac_service.person_roles.create(
        db_session, PersonRoleCreate(person_id=person.id, role_id=role.id)
    )
    assert link.person_id == person.id
    assert permission.key == "tickets:read"


def test_webhook_endpoint_subscription(db_session):
    endpoint = webhook_service.webhook_endpoints.create(
        db_session,
        WebhookEndpointCreate(name="CRM", url="https://example.com/webhook"),
    )
    subscription = webhook_service.webhook_subscriptions.create(
        db_session,
        WebhookSubscriptionCreate(endpoint_id=endpoint.id, event_type="custom"),
    )
    assert subscription.endpoint_id == endpoint.id


def test_audit_log_request(db_session):
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/test",
        "headers": [],
        "client": ("127.0.0.1", 12345),
    }
    request = Request(scope)
    response = Response(status_code=200)
    audit_service.audit_events.log_request(db_session, request, response)
    events = audit_service.audit_events.list(
        db_session,
        actor_id=None,
        actor_type=None,
        action="POST",
        entity_type="/test",
        request_id=None,
        is_success=True,
        status_code=200,
        is_active=None,
        order_by="occurred_at",
        order_dir="desc",
        limit=5,
        offset=0,
    )
    assert len(events) == 1


def test_scheduler_refresh_response():
    result = scheduler_service.refresh_schedule()
    assert "detail" in result
