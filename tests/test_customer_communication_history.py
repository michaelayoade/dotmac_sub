"""Customer-scoped, read-only email detail contract."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from fastapi.routing import APIRoute

from app.models.communication_log import (
    CommunicationChannel,
    CommunicationDirection,
    CommunicationLog,
    CommunicationStatus,
)
from app.models.subscriber import Subscriber, UserType
from app.services.customer_communication_history import (
    CustomerEmailDetailQuery,
    get_customer_email_detail,
)
from app.web.admin import customers as customer_routes


def test_email_detail_is_scoped_to_customer_and_email_channel(db_session, subscriber):
    subscriber.user_type = UserType.customer
    other = Subscriber(
        first_name="Other",
        last_name="Customer",
        email=f"other-{uuid4().hex}@example.com",
        user_type=UserType.customer,
    )
    db_session.add(other)
    db_session.flush()
    email = CommunicationLog(
        subscriber_id=subscriber.id,
        channel=CommunicationChannel.email,
        direction=CommunicationDirection.outbound,
        status=CommunicationStatus.sent,
        subject="Service update",
        sender="support@example.com",
        recipient="customer@example.com",
        body="Full email body\nwith another line.",
    )
    sms = CommunicationLog(
        subscriber_id=subscriber.id,
        channel=CommunicationChannel.sms,
        direction=CommunicationDirection.outbound,
        status=CommunicationStatus.sent,
        body="SMS body",
    )
    db_session.add_all((email, sms))
    db_session.flush()

    detail = get_customer_email_detail(
        db_session,
        query=CustomerEmailDetailQuery(customer_id=subscriber.id, message_id=email.id),
    )
    assert detail is not None
    assert detail.body == "Full email body\nwith another line."
    assert detail.subject == "Service update"
    assert detail.recipient == "customer@example.com"
    assert (
        get_customer_email_detail(
            db_session,
            query=CustomerEmailDetailQuery(customer_id=other.id, message_id=email.id),
        )
        is None
    )
    assert (
        get_customer_email_detail(
            db_session,
            query=CustomerEmailDetailQuery(
                customer_id=subscriber.id, message_id=sms.id
            ),
        )
        is None
    )
    assert (
        get_customer_email_detail(
            db_session,
            query=CustomerEmailDetailQuery(
                customer_id=subscriber.id, message_id=uuid4()
            ),
        )
        is None
    )


def test_email_detail_route_and_template_keep_the_read_boundary():
    route = next(
        item
        for item in customer_routes.router.routes
        if isinstance(item, APIRoute)
        and item.path
        == "/customers/person/{customer_id}/communications/emails/{message_id}"
    )
    dependency_cells = [
        " ".join(
            str(cell.cell_contents)
            for cell in (getattr(dependency.call, "__closure__", None) or ())
        )
        for dependency in route.dependant.dependencies
    ]
    assert route.methods == {"GET"}
    assert any("customer:read" in cells for cells in dependency_cells)

    card = Path("templates/admin/customers/detail.html").read_text(encoding="utf-8")
    detail = Path(
        "templates/admin/customers/communication_email_detail.html"
    ).read_text(encoding="utf-8")
    assert "{% if log.channel.value == 'email' %}" in card
    assert "/communications/emails/{{ log.id }}" in card
    assert "{{ email.body }}" in detail
    assert "email.body | safe" not in detail
