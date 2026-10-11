"""Hosted PostgreSQL proof: admin MFA disable vs recovery-code redemption."""

import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import select, text, update

from app.models.auth import MFAMethod, MFAMethodType, MFARecoveryCode
from app.models.subscriber import Subscriber
from app.services import auth as auth_service
from tests.integration import test_password_auth_credential_race as race

pytestmark = pytest.mark.integration


@pytest.fixture
def factory(engine):
    return race.factory.__wrapped__(engine)


@pytest.fixture
def account(factory):
    yield from race.account.__wrapped__(factory)


def _disable(factory, method_id):
    with factory() as db:
        auth_service.mfa_methods.delete(
            db, str(method_id), actor_id="admin-race", reason="race test reason"
        )


def test_disable_vs_recovery_redemption_has_no_deadlock(engine, factory, account):
    with factory() as setup:
        method = MFAMethod(
            subscriber_id=account.subscriber_id,
            method_type=MFAMethodType.totp,
            secret="unit-fixture-secret",
            is_primary=True,
            enabled=True,
        )
        setup.add(method)
        setup.flush()
        code = MFARecoveryCode(mfa_method_id=method.id, code_hash="race-code-hash")
        setup.add(code)
        setup.commit()
        method_id, code_id = method.id, code.id

    # Redemption order used by complete_mfa: principal KEY SHARE, then the
    # recovery-code row, then the method row.
    with factory() as redeem:
        redeem_pid = redeem.scalar(text("SELECT pg_backend_pid()"))
        redeem.scalar(
            select(Subscriber)
            .where(Subscriber.id == account.subscriber_id)
            .with_for_update(key_share=True)
        )
        redeem.scalar(
            select(MFARecoveryCode.id)
            .where(MFARecoveryCode.id == code_id)
            .with_for_update()
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(_disable, factory, method_id)
            try:
                # The admin must be queued behind the principal lock, not
                # holding the method row.
                with engine.connect() as observer:
                    deadline = time.monotonic() + 5
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
        assert check.get(MFARecoveryCode, code_id).is_active is False
