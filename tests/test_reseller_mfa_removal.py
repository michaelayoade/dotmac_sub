"""Reseller-management owner command for reseller_user MFA removal."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.models.audit import AuditEvent
from app.models.auth import MFAMethod, MFAMethodType, MFARecoveryCode, SessionStatus
from app.models.auth import Session as AuthSession
from app.models.event_store import EventStore
from app.models.subscriber import Reseller, ResellerUser
from app.services import reseller_onboarding as ro
from tests.mfa_removal_helpers import make_admin, make_context

REASON = "Verified by call-back to number on file, ticket T-2"


def _cmd(actor, reseller_id, principal_id, reason=REASON, **kw):
    return ro.RemoveResellerUserMfaCommand(
        context=make_context(actor, scope=ro.RESELLER_WRITE_SCOPE, **kw),
        reseller_id=reseller_id,
        principal_id=principal_id,
        removal_reason=reason,
    )


def _reseller_user(db, reseller=None):
    if reseller is None:
        reseller = Reseller(name=f"R{uuid4().hex[:6]}")
        db.add(reseller)
        db.flush()
    ru = ResellerUser(
        reseller_id=reseller.id,
        email=f"{uuid4().hex[:8]}@example.test",
        is_active=True,
        full_name="Reseller Login",
    )
    db.add(ru)
    db.flush()
    for primary in (True, False):
        m = MFAMethod(
            reseller_user_id=ru.id,
            method_type=MFAMethodType.totp,
            label="m",
            secret="unit-fixture-secret",
            is_primary=primary,
            enabled=True,
            failed_attempts=2,
        )
        db.add(m)
        db.flush()
        db.add(MFARecoveryCode(mfa_method_id=m.id, code_hash=uuid4().hex))
    db.add(
        AuthSession(
            reseller_user_id=ru.id,
            token_hash=uuid4().hex,
            expires_at=datetime(2099, 1, 1, tzinfo=UTC),
        )
    )
    rid, pid = reseller.id, ru.id
    db.commit()
    db.expunge_all()
    return rid, pid


def _audits(db):
    return db.query(AuditEvent).filter(AuditEvent.action == "auth.mfa_disabled")


def _events(db):
    return db.query(EventStore).filter(
        EventStore.event_type == "credential_administration.completed"
    )


def _live(db, pid):
    db.expire_all()
    return (
        db.query(MFAMethod)
        .filter(MFAMethod.reseller_user_id == pid, MFAMethod.is_active.is_(True))
        .count()
    )


def test_removal_has_all_guarantees(db_session) -> None:
    admin = make_admin(db_session)
    rid, pid = _reseller_user(db_session)
    outcome = ro.remove_reseller_user_mfa(db_session, _cmd(admin, rid, pid))
    assert outcome.changed and outcome.methods_disabled == 2
    assert outcome.sessions_revoked == 1
    db_session.expire_all()
    for row in db_session.query(MFAMethod):
        assert (row.is_active, row.enabled, row.is_primary) == (False,) * 3
        assert row.secret is None and row.failed_attempts == 0
    assert all(not c.is_active for c in db_session.query(MFARecoveryCode))
    assert db_session.query(AuthSession).one().status == SessionStatus.revoked
    audit = _audits(db_session).one()
    assert audit.entity_type == "reseller_user"
    assert audit.actor_id == str(admin)
    assert audit.metadata_["removal_reason"] == REASON
    payload = _events(db_session).one().payload
    assert payload["principal_type"] == "reseller_user"
    assert payload["invalidate_sessions"] is True
    assert payload["notify_mfa_removal"] is True
    assert "unit-fixture-secret" not in repr(payload) + repr(audit.metadata_)


def test_repeat_is_audited_noop(db_session) -> None:
    admin = make_admin(db_session)
    rid, pid = _reseller_user(db_session)
    ro.remove_reseller_user_mfa(db_session, _cmd(admin, rid, pid))
    again = ro.remove_reseller_user_mfa(db_session, _cmd(admin, rid, pid))
    assert again.changed is False
    assert _audits(db_session).count() == 2
    assert _events(db_session).count() == 1


def test_other_resellers_principal_is_not_found_and_unchanged(db_session) -> None:
    admin = make_admin(db_session)
    _rid, pid = _reseller_user(db_session)
    other_rid, _other_pid = _reseller_user(db_session)
    with pytest.raises(ro.ResellerOnboardingError):
        ro.remove_reseller_user_mfa(db_session, _cmd(admin, other_rid, pid))
    assert _live(db_session, pid) == 2
    assert _audits(db_session).count() == 0


def test_wrong_scope_self_removal_and_reason_fail_closed(db_session) -> None:
    admin = make_admin(db_session)
    rid, pid = _reseller_user(db_session)
    bad_scope = ro.RemoveResellerUserMfaCommand(
        context=make_context(admin, scope="rbac:assign"),
        reseller_id=rid,
        principal_id=pid,
        removal_reason=REASON,
    )
    with pytest.raises(ro.ResellerOnboardingError):
        ro.remove_reseller_user_mfa(db_session, bad_scope)
    with pytest.raises(ro.ResellerOnboardingError) as self_exc:
        ro.remove_reseller_user_mfa(db_session, _cmd(pid, rid, pid))
    assert "not permitted" in self_exc.value.message
    for reason in (None, "", "ab", "x" * 501):
        with pytest.raises(ro.ResellerOnboardingError) as exc:
            ro.remove_reseller_user_mfa(db_session, _cmd(admin, rid, pid, reason))
        assert "reason" in exc.value.message
    assert _live(db_session, pid) == 2
    assert _audits(db_session).count() == 0
    assert _events(db_session).count() == 0


def test_legacy_subscriber_linked_principal_is_not_a_reseller_user(
    db_session,
) -> None:
    admin = make_admin(db_session)
    rid, _pid = _reseller_user(db_session)
    with pytest.raises(ro.ResellerOnboardingError):
        ro.remove_reseller_user_mfa(db_session, _cmd(admin, rid, uuid4()))


def test_actor_must_be_admin_user(db_session) -> None:
    from tests.mfa_removal_helpers import make_staff

    rid, pid = _reseller_user(db_session)
    helper = make_staff(db_session, role="helpdesk", permissions=("reseller:write",))
    with pytest.raises(ro.ResellerOnboardingError):
        ro.remove_reseller_user_mfa(db_session, _cmd(helper, rid, pid))
    admin = make_admin(db_session)
    with pytest.raises(ro.ResellerOnboardingError):
        ro.remove_reseller_user_mfa(
            db_session, _cmd(admin, rid, pid, actor_type="api_key")
        )
    assert _live(db_session, pid) == 2


def test_disabled_rows_scrubbed_and_event_has_command_metadata(db_session) -> None:
    admin = make_admin(db_session)
    rid, pid = _reseller_user(db_session)
    old = MFAMethod(
        reseller_user_id=pid,
        method_type=MFAMethodType.totp,
        label="old",
        secret="legacy-seed",
        enabled=False,
        is_active=False,
        is_primary=False,
    )
    db_session.add(old)
    db_session.commit()
    command = _cmd(admin, rid, pid)
    ro.remove_reseller_user_mfa(db_session, command)
    db_session.expire_all()
    assert all(m.secret is None for m in db_session.query(MFAMethod))
    payload = _events(db_session).one().payload
    assert payload["command_id"] == str(command.context.command_id)


def test_lock_timeout_maps_to_retryable_error(db_session, monkeypatch) -> None:
    from sqlalchemy.exc import OperationalError

    class Orig(Exception):
        pgcode = "55P03"

    def boom(*_a, **_k):
        raise OperationalError("SELECT", {}, Orig())

    monkeypatch.setattr(ro, "execute_owner_command", boom)
    rid, pid = _reseller_user(db_session)
    with pytest.raises(ro.ResellerOnboardingError) as exc:
        ro.remove_reseller_user_mfa(db_session, _cmd(make_admin(db_session), rid, pid))
    assert exc.value.retryable is True
