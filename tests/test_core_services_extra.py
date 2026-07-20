from uuid import uuid4

from starlette.requests import Request
from starlette.responses import Response

from app.schemas.subscriber import SubscriberCreate
from app.schemas.webhook import WebhookEndpointCreate, WebhookSubscriptionCreate
from app.services import audit as audit_service
from app.services import rbac_catalog, subscriber_assignments
from app.services import scheduler as scheduler_service
from app.services import subscriber as subscriber_service
from app.services import webhook as webhook_service
from app.services.owner_commands import CommandContext


def test_subscriber_create_list(db_session, person):
    subscriber = subscriber_service.subscribers.create(
        db_session,
        SubscriberCreate(
            first_name="Extra",
            last_name="Subscriber",
            email=f"extra-{person.id}@example.com",
        ),
    )
    items = subscriber_service.subscribers.list(
        db_session,
        business_account_id=None,
        subscriber_type="person",
        order_by="created_at",
        order_dir="desc",
        limit=10,
        offset=0,
    )
    assert any(item.id == subscriber.id for item in items)


def test_rbac_role_permission_link(db_session, person):
    person_id = person.id
    db_session.commit()
    role_command_id = uuid4()
    role = rbac_catalog.create_role(
        db_session,
        rbac_catalog.CreateRoleCommand(
            context=CommandContext(
                command_id=role_command_id,
                correlation_id=role_command_id,
                actor="user:core-service-test",
                scope=rbac_catalog.ROLE_WRITE_SCOPE,
                reason="Core service role fixture",
            ),
            name="admin",
        ),
    )
    permission_command_id = uuid4()
    permission = rbac_catalog.create_permission(
        db_session,
        rbac_catalog.CreatePermissionCommand(
            context=CommandContext(
                command_id=permission_command_id,
                correlation_id=permission_command_id,
                actor="user:core-service-test",
                scope=rbac_catalog.PERMISSION_WRITE_SCOPE,
                reason="Core service permission fixture",
            ),
            key="tickets:read",
            description="Tickets Read",
        ),
    )
    assignment_command_id = uuid4()
    link = subscriber_assignments.grant_subscriber_role(
        db_session,
        subscriber_assignments.GrantSubscriberRoleCommand(
            context=CommandContext(
                command_id=assignment_command_id,
                correlation_id=assignment_command_id,
                actor="user:core-service-test",
                scope=subscriber_assignments.ASSIGNMENT_SCOPE,
                reason="Core service subscriber role fixture",
            ),
            subscriber_id=person_id,
            role_id=role.id,
        ),
    )
    assert link.subscriber_id == person_id
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
