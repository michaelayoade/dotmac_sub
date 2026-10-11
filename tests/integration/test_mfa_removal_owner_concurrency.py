"""Hosted PostgreSQL proof: staff/reseller MFA removal queues behind login's
principal lock (principal first, then method rows) without deadlock."""

import time
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from sqlalchemy import select, text, update

from app.models.auth import MFAMethod, MFAMethodType, MFARecoveryCode
from app.models.subscriber import Reseller, ResellerUser
from app.models.system_user import SystemUser
from app.services import reseller_onboarding, staff_provisioning
from tests.integration import test_password_auth_credential_race as race
from tests.mfa_removal_helpers import make_admin, make_context

pytestmark = pytest.mark.integration

REASON = "Verified by call-back, ticket T-race"


@pytest.fixture
def factory(engine):
    return race.factory.__wrapped__(engine)


def _setup(factory, kind):
    with factory() as db:
        admin = make_admin(db)
        if kind == "system_user":
            principal = SystemUser(
                first_name="S", last_name="U", email=f"{uuid4().hex[:8]}@race.test"
            )
            model, column = SystemUser, "system_user_id"
        else:
            reseller = Reseller(name=f"R{uuid4().hex[:6]}")
            db.add(reseller)
            db.flush()
            principal = ResellerUser(
                reseller_id=reseller.id,
                email=f"{uuid4().hex[:8]}@race.test",
                is_active=True,
                full_name="Race",
            )
            model, column = ResellerUser, "reseller_user_id"
        db.add(principal)
        db.flush()
        method = MFAMethod(
            method_type=MFAMethodType.totp,
            secret="unit-fixture-secret",
            is_primary=True,
            enabled=True,
            **{column: principal.id},
        )
        db.add(method)
        db.flush()
        code = MFARecoveryCode(mfa_method_id=method.id, code_hash=uuid4().hex)
        db.add(code)
        reseller_id = getattr(principal, "reseller_id", None)
        db.commit()
        return admin, model, principal.id, reseller_id, method.id, code.id


def _remove(factory, kind, admin, pid, reseller_id):
    with factory() as db:
        if kind == "system_user":
            staff_provisioning.remove_staff_mfa(
                db,
                staff_provisioning.RemoveStaffMfaCommand(
                    context=make_context(admin), user_id=pid, removal_reason=REASON
                ),
            )
        else:
            reseller_onboarding.remove_reseller_user_mfa(
                db,
                reseller_onboarding.RemoveResellerUserMfaCommand(
                    context=make_context(
                        admin, scope=reseller_onboarding.RESELLER_WRITE_SCOPE
                    ),
                    reseller_id=reseller_id,
                    principal_id=pid,
                    removal_reason=REASON,
                ),
            )


@pytest.mark.parametrize("kind", ["system_user", "reseller_user"])
def test_removal_queues_behind_principal_lock_without_deadlock(engine, factory, kind):
    admin, model, pid, reseller_id, method_id, code_id = _setup(factory, kind)
    with factory() as redeem:
        redeem_pid = redeem.scalar(text("SELECT pg_backend_pid()"))
        redeem.scalar(
            select(model).where(model.id == pid).with_for_update(key_share=True)
        )
        redeem.scalar(
            select(MFARecoveryCode.id)
            .where(MFARecoveryCode.id == code_id)
            .with_for_update()
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(_remove, factory, kind, admin, pid, reseller_id)
            try:
                with engine.connect() as observer:
                    deadline = time.monotonic() + 5
                    waiting = 0
                    while time.monotonic() < deadline:
                        waiting = observer.scalar(
                            text(
                                "SELECT count(*) FROM pg_stat_activity WHERE "
                                "wait_event_type='Lock' AND :pid = ANY(pg_blocking_pids(pid))"
                            ),
                            {"pid": redeem_pid},
                        )
                        if waiting:
                            break
                        time.sleep(0.05)
                    assert waiting
                redeem.execute(
                    update(MFARecoveryCode)
                    .where(MFARecoveryCode.id == code_id)
                    .values(is_active=False)
                )
                redeem.execute(
                    update(MFAMethod)
                    .where(MFAMethod.id == method_id)
                    .values(last_used_at=text("now()"))
                )
            finally:
                redeem.commit()
            result.result(timeout=15)

    with factory() as check:
        row = check.get(MFAMethod, method_id)
        assert (row.is_active, row.enabled, row.is_primary, row.secret) == (
            False,
            False,
            False,
            None,
        )
