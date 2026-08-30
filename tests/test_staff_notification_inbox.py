import uuid

from app.models.admin_alert import AdminNotification
from app.models.notification import Notification, NotificationChannel
from app.models.service_team import ServiceTeam, ServiceTeamMember
from app.models.system_user import SystemUser
from app.services.staff_notifications import (
    StaffTagNotificationCommand,
    queue_staff_push,
    queue_staff_tag_notifications,
)
from tests.staff_identity_fixtures import add_bound_staff_user


def test_staff_push_creates_linked_inbox_item(db_session):
    user = SystemUser(
        first_name="Inbox",
        last_name="User",
        email=f"inbox-{uuid.uuid4()}@example.com",
        is_active=True,
    )
    db_session.add(user)
    db_session.flush()

    queue_staff_push(
        db_session,
        recipient=str(user.id),
        subject="Ticket assigned",
        body="You were assigned ticket 42.",
        target_url="/admin/support/tickets/42",
    )
    db_session.flush()

    delivery = db_session.query(Notification).one()
    inbox_item = db_session.query(AdminNotification).one()
    assert delivery.channel == NotificationChannel.push
    assert inbox_item.source_notification_id == delivery.id
    assert inbox_item.system_user_id == user.id
    assert inbox_item.target_url == "/admin/support/tickets/42"


def test_non_uuid_push_recipient_is_not_added_to_staff_inbox(db_session):
    queue_staff_push(
        db_session,
        recipient="legacy@example.com",
        subject="Legacy delivery",
        body="Transport only",
    )
    db_session.flush()

    assert db_session.query(Notification).count() == 1
    assert db_session.query(AdminNotification).count() == 0


def test_staff_tag_notifications_create_user_and_team_inbox_items(db_session):
    direct, _direct_person = add_bound_staff_user(
        db_session, email=f"direct-tag-{uuid.uuid4()}@example.com"
    )
    team_member, team_person = add_bound_staff_user(
        db_session, email=f"team-tag-{uuid.uuid4()}@example.com"
    )
    team = ServiceTeam(name=f"Tagged Team {uuid.uuid4()}", is_active=True)
    db_session.add(team)
    db_session.flush()
    db_session.add(ServiceTeamMember(team_id=team.id, person_id=team_person.id))
    db_session.flush()

    outcome = queue_staff_tag_notifications(
        db_session,
        StaffTagNotificationCommand(
            entity_kind="work order",
            entity_id="wo-1",
            entity_reference="WO-1",
            entity_title="Install fibre",
            target_url="/admin/dispatch/work-orders/WO-1",
            current_tags=(
                "urgent",
                f"person:{direct.id}",
                f"team:{team.id}",
            ),
        ),
    )
    db_session.flush()

    assert outcome.notification_count == 2
    inbox_rows = (
        db_session.query(AdminNotification)
        .order_by(AdminNotification.title.asc())
        .all()
    )
    assert {(row.system_user_id, row.title, row.target_url) for row in inbox_rows} == {
        (
            direct.id,
            "You were tagged in this work order",
            "/admin/dispatch/work-orders/WO-1",
        ),
        (
            team_member.id,
            "Your team was tagged in this work order",
            "/admin/dispatch/work-orders/WO-1",
        ),
    }
