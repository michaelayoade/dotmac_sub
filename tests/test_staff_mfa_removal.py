"""Staff MFA removal is owned by staff provisioning."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.models.audit import AuditEvent
from app.models.auth import MFAMethod, MFAMethodType, MFARecoveryCode, SessionStatus
from app.models.auth import Session as AuthSession
from app.models.event_store import EventStore
from app.models.system_user import SystemUser
from app.services import staff_provisioning
from app.services.owner_commands import CommandContext


def _context(scope: str = staff_provisioning.STAFF_ASSIGN_SCOPE) -> CommandContext:
    command_id = uuid4()
    return CommandContext(
        command_id=command_id,
        correlation_id=command_id,
        actor="user:admin-test",
        scope=scope,
        reason="test",
        idempotency_key=f"k-{command_id}",
    )


def _staff(db):
    user = SystemUser(
        first_name="S", last_name="U", email=f"{uuid4().hex[:8]}@example.test"
    )
    db.add(user)
    db.flush()
    for primary in (True, False):
        m = MFAMethod(
            system_user_id=user.id,
            method_type=MFAMethodType.totp,
            label="m",
            secret="unit-fixture-secret",
            is_primary=primary,
            enabled=True,
            failed_attempts=3,
        )
        db.add(m)
        db.flush()
        db.add(MFARecoveryCode(mfa_method_id=m.id, code_hash=uuid4().hex))
    db.add(
        AuthSession(
            system_user_id=user.id,
            token_hash=uuid4().hex,
            expires_at=datetime(2099, 1, 1, tzinfo=UTC),
        )
    )
    uid = user.id
    db.commit()
    db.expunge_all()
    return uid


def test_removal_wipes_deactivates_revokes_and_audits(db_session) -> None:
    uid = _staff(db_session)
    outcome = staff_provisioning.remove_staff_mfa(
        db_session,
        staff_provisioning.RemoveStaffMfaCommand(context=_context(), user_id=uid),
    )
    assert outcome.changed is True
    db_session.expire_all()
    for row in db_session.query(MFAMethod).filter(MFAMethod.system_user_id == uid):
        assert (row.is_active, row.enabled, row.is_primary) == (False,) * 3
        assert row.secret is None
        assert row.failed_attempts == 0
    assert all(not c.is_active for c in db_session.query(MFARecoveryCode))
    assert db_session.query(AuthSession).one().status == SessionStatus.revoked
    audit = db_session.query(AuditEvent).one()
    assert audit.action == "auth.mfa_disabled"
    assert audit.entity_id == str(uid)
    assert audit.actor_id == "admin-test"
    assert audit.metadata_["methods_disabled"] == 2
    payload = db_session.query(EventStore).one().payload
    assert payload["invalidate_sessions"] is True
    assert payload["principal_type"] == "system_user"
    assert "unit-fixture-secret" not in repr(payload) + repr(audit.metadata_)


def test_repeat_removal_is_audited_noop_without_event(db_session) -> None:
    uid = _staff(db_session)
    command = staff_provisioning.RemoveStaffMfaCommand(context=_context(), user_id=uid)
    staff_provisioning.remove_staff_mfa(db_session, command)
    again = staff_provisioning.remove_staff_mfa(
        db_session,
        staff_provisioning.RemoveStaffMfaCommand(context=_context(), user_id=uid),
    )
    assert again.changed is False
    assert db_session.query(EventStore).count() == 1


def test_wrong_scope_and_unknown_user_fail_closed(db_session) -> None:
    uid = _staff(db_session)
    with pytest.raises(staff_provisioning.StaffProvisioningError):
        staff_provisioning.remove_staff_mfa(
            db_session,
            staff_provisioning.RemoveStaffMfaCommand(
                context=_context("customer:read"), user_id=uid
            ),
        )
    with pytest.raises(staff_provisioning.StaffProvisioningError):
        staff_provisioning.remove_staff_mfa(
            db_session,
            staff_provisioning.RemoveStaffMfaCommand(
                context=_context(), user_id=uuid4()
            ),
        )
    db_session.expire_all()
    assert (
        db_session.query(MFAMethod)
        .filter(MFAMethod.system_user_id == uid, MFAMethod.is_active.is_(True))
        .count()
        == 2
    )
    assert db_session.query(AuditEvent).count() == 0
