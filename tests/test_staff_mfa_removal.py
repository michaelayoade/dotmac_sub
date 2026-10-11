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
from tests.mfa_removal_helpers import make_admin, make_context, make_staff

REASON = "Verified by call-back to number on file, ticket T-1"


def _cmd(actor, target, reason=REASON, **kw):
    return staff_provisioning.RemoveStaffMfaCommand(
        context=make_context(actor, **kw), user_id=target, removal_reason=reason
    )


def _staff(db, **kw):
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


def _audits(db):
    return db.query(AuditEvent).filter(AuditEvent.action == "auth.mfa_disabled")


def _removal_events(db):
    return db.query(EventStore).filter(
        EventStore.event_type == "credential_administration.completed"
    )


def _unchanged(db, uid):
    db.expire_all()
    assert (
        db.query(MFAMethod)
        .filter(MFAMethod.system_user_id == uid, MFAMethod.is_active.is_(True))
        .count()
        == 2
    )
    assert db.query(AuthSession).one().status == SessionStatus.active
    assert _audits(db).count() == 0
    assert _removal_events(db).count() == 0


def test_removal_wipes_deactivates_revokes_and_audits(db_session) -> None:
    admin = make_admin(db_session)
    uid = _staff(db_session)
    outcome = staff_provisioning.remove_staff_mfa(db_session, _cmd(admin, uid))
    assert outcome.changed is True
    db_session.expire_all()
    for row in db_session.query(MFAMethod).filter(MFAMethod.system_user_id == uid):
        assert (row.is_active, row.enabled, row.is_primary) == (False,) * 3
        assert row.secret is None
        assert row.failed_attempts == 0
    assert all(not c.is_active for c in db_session.query(MFARecoveryCode))
    assert db_session.query(AuthSession).one().status == SessionStatus.revoked
    audit = (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == "auth.mfa_disabled")
        .one()
    )
    assert audit.action == "auth.mfa_disabled"
    assert audit.entity_id == str(uid)
    assert audit.actor_id == str(admin)
    assert audit.metadata_["methods_disabled"] == 2
    assert audit.metadata_["removal_reason"] == REASON
    event = _removal_events(db_session).one()
    payload = event.payload
    assert payload["invalidate_sessions"] is True
    assert payload["principal_type"] == "system_user"
    assert payload["notify_mfa_removal"] is True
    assert "unit-fixture-secret" not in repr(payload) + repr(audit.metadata_)
    assert REASON not in repr(payload)


def test_repeat_removal_is_audited_noop_without_event(db_session) -> None:
    admin = make_admin(db_session)
    uid = _staff(db_session)
    staff_provisioning.remove_staff_mfa(db_session, _cmd(admin, uid))
    again = staff_provisioning.remove_staff_mfa(db_session, _cmd(admin, uid))
    assert again.changed is False
    assert _removal_events(db_session).count() == 1
    assert _audits(db_session).count() == 2


def test_wrong_scope_and_unknown_user_fail_closed(db_session) -> None:
    admin = make_admin(db_session)
    uid = _staff(db_session)
    with pytest.raises(staff_provisioning.StaffProvisioningError):
        staff_provisioning.remove_staff_mfa(
            db_session, _cmd(admin, uid, scope="customer:read")
        )
    with pytest.raises(staff_provisioning.StaffProvisioningError):
        staff_provisioning.remove_staff_mfa(db_session, _cmd(admin, uuid4()))
    _unchanged(db_session, uid)


@pytest.mark.parametrize("reason", [None, "", "  ab ", "x" * 501])
def test_reason_is_required_and_bounded(db_session, reason) -> None:
    admin = make_admin(db_session)
    uid = _staff(db_session)
    with pytest.raises(staff_provisioning.StaffProvisioningError) as exc:
        staff_provisioning.remove_staff_mfa(db_session, _cmd(admin, uid, reason))
    assert "reason" in exc.value.message
    _unchanged(db_session, uid)


def test_self_removal_is_refused(db_session) -> None:
    admin = make_admin(db_session)
    # give the admin a live factor so a change would be observable
    db_session.add(
        MFAMethod(
            system_user_id=admin,
            method_type=MFAMethodType.totp,
            label="m",
            secret="s",
            is_primary=True,
            enabled=True,
        )
    )
    db_session.commit()
    with pytest.raises(staff_provisioning.StaffProvisioningError):
        staff_provisioning.remove_staff_mfa(db_session, _cmd(admin, admin))
    assert db_session.query(MFAMethod).one().is_active is True


def test_admin_target_is_refused_even_for_admin_actor(db_session) -> None:
    actor = make_admin(db_session)
    target = make_admin(db_session)
    db_session.add(
        MFAMethod(
            system_user_id=target,
            method_type=MFAMethodType.totp,
            label="m",
            secret="s",
            is_primary=True,
            enabled=True,
        )
    )
    db_session.commit()
    with pytest.raises(staff_provisioning.StaffProvisioningError) as exc:
        staff_provisioning.remove_staff_mfa(db_session, _cmd(actor, target))
    assert "not permitted" in exc.value.message
    assert db_session.query(MFAMethod).one().is_active is True


def test_actor_must_hold_admin_role_even_with_rbac_assign(db_session) -> None:
    helper = make_staff(db_session, role="helpdesk", permissions=("rbac:assign",))
    lower = make_staff(db_session, role="viewer", permissions=("reports:read",))
    uid = _staff(db_session)
    for target in (lower, uid):
        with pytest.raises(staff_provisioning.StaffProvisioningError) as exc:
            staff_provisioning.remove_staff_mfa(db_session, _cmd(helper, target))
        assert "not permitted" in exc.value.message
    _unchanged(db_session, uid)


def test_already_disabled_rows_are_scrubbed_too(db_session) -> None:
    admin = make_admin(db_session)
    uid = make_staff(db_session, role="viewer")
    old = MFAMethod(
        system_user_id=uid,
        method_type=MFAMethodType.totp,
        label="old",
        secret="legacy-seed",
        is_primary=False,
        enabled=False,
        is_active=False,
    )
    db_session.add(old)
    db_session.flush()
    db_session.add(MFARecoveryCode(mfa_method_id=old.id, code_hash=uuid4().hex))
    db_session.commit()
    outcome = staff_provisioning.remove_staff_mfa(db_session, _cmd(admin, uid))
    assert outcome.changed is False  # nothing was live
    db_session.expire_all()
    assert db_session.query(MFAMethod).one().secret is None
    assert db_session.query(MFARecoveryCode).one().is_active is False


def test_event_carries_command_metadata(db_session) -> None:
    admin = make_admin(db_session)
    uid = _staff(db_session)
    command = _cmd(admin, uid)
    staff_provisioning.remove_staff_mfa(db_session, command)
    payload = _removal_events(db_session).one().payload
    assert payload["command_id"] == str(command.context.command_id)
    assert payload["correlation_id"] == str(command.context.correlation_id)


def test_lock_timeout_maps_to_retryable_domain_error(db_session, monkeypatch) -> None:
    from sqlalchemy.exc import OperationalError

    class Orig(Exception):
        pgcode = "55P03"

    def boom(*_a, **_k):
        raise OperationalError("SELECT", {}, Orig())

    monkeypatch.setattr(staff_provisioning, "execute_owner_command", boom)
    with pytest.raises(staff_provisioning.StaffProvisioningError) as exc:
        staff_provisioning.remove_staff_mfa(
            db_session, _cmd(make_admin(db_session), uuid4())
        )
    assert exc.value.retryable is True
    assert "lock_timeout" in exc.value.code


def test_api_key_actor_is_refused(db_session) -> None:
    uid = _staff(db_session)
    with pytest.raises(staff_provisioning.StaffProvisioningError):
        staff_provisioning.remove_staff_mfa(
            db_session, _cmd("erp", uid, actor_type="api_key")
        )
    _unchanged(db_session, uid)


def test_web_disable_path_revokes_sessions_and_wipes_seed(db_session) -> None:
    from app.services.web_system_user_mutations import disable_user_mfa

    admin = make_admin(db_session)
    uid = _staff(db_session)
    disable_user_mfa(db_session, command=_cmd(admin, uid))
    db_session.expire_all()
    assert db_session.query(AuthSession).one().status == SessionStatus.revoked
    assert all(m.secret is None for m in db_session.query(MFAMethod))
    assert all(not c.is_active for c in db_session.query(MFARecoveryCode))


def test_forced_enrolment_follows_removal_when_setting_is_on(
    db_session, monkeypatch
) -> None:
    from app.models.domain_settings import (
        DomainSetting,
        SettingDomain,
        SettingValueType,
    )
    from app.services.auth_flow import AuthFlow
    from tests.test_auth_flow import _make_request, _system_user_with_credential

    monkeypatch.delenv("ADMIN_MFA_REQUIRED", raising=False)
    db_session.add(
        DomainSetting(
            domain=SettingDomain.auth,
            key="admin_mfa_required",
            value_type=SettingValueType.boolean,
            value_text="true",
        )
    )
    admin = make_admin(db_session)
    user, _credential = _system_user_with_credential(db_session, "forced@example.com")
    uid, email = user.id, user.email
    db_session.add(
        MFAMethod(
            system_user_id=uid,
            method_type=MFAMethodType.totp,
            label="m",
            secret="s",
            is_primary=True,
            enabled=True,
        )
    )
    db_session.commit()
    first = AuthFlow.login(db_session, email, "secret", _make_request(), None)
    assert first.get("mfa_required") is True
    db_session.rollback()
    staff_provisioning.remove_staff_mfa(db_session, _cmd(admin, uid))
    step = AuthFlow.login(db_session, email, "secret", _make_request(), None)
    assert step["mfa_enrollment_required"] is True
