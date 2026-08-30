from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.models.admin_alert import AdminNotification
from app.models.notification import Notification, NotificationChannel
from app.models.service_team import ServiceTeam, ServiceTeamMember
from app.schemas.project import ProjectCreate
from app.schemas.support import TicketCreate
from app.services.staff_notifications import (
    StaffAssignmentEventType,
    StaffNotificationError,
    StageStaffAssignmentNotifications,
    resolve_assignment_users,
    stage_staff_assignment_notifications,
)
from tests.staff_identity_fixtures import add_bound_staff_user


def test_assignment_audience_combines_users_and_service_teams(db_session) -> None:
    direct, _direct_person = add_bound_staff_user(
        db_session, email=f"direct-{uuid4()}@example.com"
    )
    member, member_person = add_bound_staff_user(
        db_session, email=f"member-{uuid4()}@example.com"
    )
    team = ServiceTeam(name=f"Assignment {uuid4()}", is_active=True)
    db_session.add(team)
    db_session.flush()
    db_session.add(
        ServiceTeamMember(
            team_id=team.id,
            person_id=member_person.id,
            is_active=True,
        )
    )
    db_session.flush()

    users = resolve_assignment_users(
        db_session,
        person_ids={str(direct.id)},
        service_team_ids={str(team.id)},
    )

    assert {user.id for user in users} == {direct.id, member.id}

    source_event_id = uuid4()
    source_entity_id = uuid4()
    target_url = f"/admin/support/tickets/{source_entity_id}"
    command = StageStaffAssignmentNotifications(
        system_user_ids=tuple(user.id for user in users),
        source_event_id=source_event_id,
        source_entity_id=source_entity_id,
        event_type=StaffAssignmentEventType.ticket_assignment,
        subject="Assigned",
        body="Please review",
        target_url=target_url,
    )

    outcome = stage_staff_assignment_notifications(
        db_session,
        command,
    )
    replay = stage_staff_assignment_notifications(db_session, command)
    rows = db_session.query(Notification).all()
    assert {(row.channel, row.recipient) for row in rows} == {
        (NotificationChannel.push, str(direct.id)),
        (NotificationChannel.email, direct.email),
        (NotificationChannel.push, str(member.id)),
        (NotificationChannel.email, member.email),
    }
    assert set(outcome.notified_system_user_ids) == {direct.id, member.id}
    assert replay == outcome
    assert db_session.query(AdminNotification).count() == 2
    assert {row.target_url for row in db_session.query(AdminNotification).all()} == {
        target_url
    }

    with pytest.raises(
        StaffNotificationError,
        match="already staged with different data",
    ):
        stage_staff_assignment_notifications(
            db_session,
            replace(command, target_url=f"/admin/support/tickets/{uuid4()}"),
        )


def test_assignment_notification_rejects_dashboard_fallback(db_session) -> None:
    user, _person = add_bound_staff_user(
        db_session, email=f"assigned-{uuid4()}@example.com"
    )

    with pytest.raises(
        StaffNotificationError,
        match="require an exact admin target",
    ):
        stage_staff_assignment_notifications(
            db_session,
            StageStaffAssignmentNotifications(
                system_user_ids=(user.id,),
                source_event_id=uuid4(),
                source_entity_id=uuid4(),
                event_type=StaffAssignmentEventType.ticket_assignment,
                subject="Assigned",
                body="Please review",
                target_url="/admin",
            ),
        )


@pytest.mark.parametrize(
    ("schema", "field"),
    [
        (ProjectCreate, "assistant_manager_person_id"),
        (TicketCreate, "site_coordinator_person_id"),
    ],
)
def test_new_records_reject_retired_site_coordinator(schema, field) -> None:
    with pytest.raises(ValidationError):
        schema(name="Project", title="Ticket", **{field: uuid4()})
