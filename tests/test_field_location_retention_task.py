from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.models.dispatch import TechnicianProfile
from app.models.field_location import FieldTechLocationPing
from app.models.subscriber import UserType
from app.models.system_user import SystemUser
from app.tasks import field_location as field_location_tasks


def test_prune_field_location_pings_task_deletes_only_expired_evidence(
    db_session,
    monkeypatch,
):
    user = SystemUser(
        first_name="Retention",
        last_name="Tech",
        email=f"retention-{uuid4().hex}@example.com",
        user_type=UserType.system_user,
    )
    db_session.add(user)
    db_session.flush()
    profile = TechnicianProfile(
        person_id=user.id,
        system_user_id=user.id,
        crm_person_id="crm-retention-tech",
    )
    db_session.add(profile)
    db_session.flush()
    now = datetime.now(UTC)
    fresh = FieldTechLocationPing(
        technician_id=profile.id,
        person_id=profile.person_id,
        client_observation_id=uuid4(),
        payload_fingerprint="f" * 64,
        latitude=6.5,
        longitude=3.3,
        accuracy_m=8,
        captured_at=now,
        received_at=now,
        source="mobile",
    )
    old = FieldTechLocationPing(
        technician_id=profile.id,
        person_id=profile.person_id,
        client_observation_id=uuid4(),
        payload_fingerprint="a" * 64,
        latitude=6.5,
        longitude=3.3,
        accuracy_m=8,
        captured_at=now - timedelta(hours=100),
        received_at=now - timedelta(hours=100),
        source="mobile",
    )
    db_session.add_all([fresh, old])
    db_session.flush()
    fresh_id = fresh.id
    db_session.commit()
    monkeypatch.setattr(field_location_tasks, "SessionLocal", lambda: db_session)

    result = field_location_tasks.prune_field_location_pings.run(older_than_hours=72)

    assert result == {"deleted": 1, "older_than_hours": 72}
    assert [row.id for row in db_session.query(FieldTechLocationPing).all()] == [
        fresh_id
    ]
