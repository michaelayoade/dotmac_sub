"""Development-only red/green regression patch; not part of the source PR."""
from pathlib import Path
import sys


def replace(path: str, old: str, new: str) -> None:
    target = Path(path)
    content = target.read_text()
    if content.count(old) != 1:
        raise RuntimeError(f"Expected one exact patch anchor in {path}")
    target.write_text(content.replace(old, new))


if sys.argv[1] == "tests":
    replace(
        "tests/test_auth_session_refresh.py",
        "from datetime import UTC, datetime, timedelta\n",
        "from datetime import UTC, datetime, timedelta\n\nimport pytest\nfrom sqlalchemy import Select\nfrom sqlalchemy.engine import ScalarResult\nfrom sqlalchemy.orm import Session\n\nfrom app.models.subscriber import Subscriber\nfrom app.services import auth_session_refresh\n",
    )
    with Path("tests/test_auth_session_refresh.py").open("a") as handle:
        handle.write('''\n\n@pytest.mark.parametrize(
    "before_lock_seconds,after_lock_seconds,expected",
    [
        (-1, 1, RefreshDisposition.DUPLICATE),
        (1, 6, RefreshDisposition.REUSE_REVOKED),
    ],
)
def test_runtime_refresh_clock_is_sampled_after_row_lock(
    db_session: Session,
    person: Subscriber,
    monkeypatch: pytest.MonkeyPatch,
    before_lock_seconds: int,
    after_lock_seconds: int,
    expected: RefreshDisposition,
) -> None:
    """A delayed lock must neither falsely revoke nor extend the replay window."""
    rotation_time = datetime.now(UTC)
    old_token = "delayed-lock-refresh-token"
    session = _session(db_session, person, old_token, rotation_time)
    first = renew_authentication_session(
        db=db_session, command=_command(old_token, rotation_time)
    )
    before_lock = rotation_time + timedelta(seconds=before_lock_seconds)
    after_lock = rotation_time + timedelta(seconds=after_lock_seconds)
    row_lock_acquired = False
    original_scalars = db_session.scalars

    class LockAwareClock(datetime):
        @classmethod
        def now(cls, tz=None):
            assert tz is UTC
            return after_lock if row_lock_acquired else before_lock

    def locked_scalars(statement: Select[tuple[AuthSession]]) -> ScalarResult[AuthSession]:
        nonlocal row_lock_acquired
        result = original_scalars(statement)
        if statement._for_update_arg is not None:
            row_lock_acquired = True
        return result

    monkeypatch.setattr(auth_session_refresh, "datetime", LockAwareClock)
    monkeypatch.setattr(db_session, "scalars", locked_scalars)
    outcome = renew_authentication_session(
        db=db_session,
        command=RefreshSessionCommand(
            context=_context(old_token),
            refresh_token=old_token,
            client_ip="203.0.113.8",
            user_agent="browser/1",
        ),
    )
    assert row_lock_acquired
    assert outcome.disposition is expected
    assert outcome.decided_at == after_lock
    assert outcome.refresh_token is None
    assert first.refresh_token is not None
    db_session.refresh(session)
    assert session.token_hash == hash_refresh_token(first.refresh_token)
    assert session.status is (
        SessionStatus.active
        if expected is RefreshDisposition.DUPLICATE
        else SessionStatus.revoked
    )
''')
elif sys.argv[1] == "fix":
    replace(
        "app/services/auth_session_refresh.py",
        "    def operation() -> RefreshSessionOutcome:\n        now = _as_utc(command.observed_at) or datetime.now(UTC)\n        supplied_hash",
        "    def operation() -> RefreshSessionOutcome:\n        supplied_hash",
    )
    replace(
        "app/services/auth_session_refresh.py",
        "        principal_type, principal_id = _principal(session)\n        expires_at",
        "        # A concurrent rotation may commit while this request waits for the\n        # row lock. Compare its timestamp against decision time, not wait-entry\n        # time; otherwise a valid duplicate can look older than the rotation.\n        now = _as_utc(command.observed_at) or datetime.now(UTC)\n        principal_type, principal_id = _principal(session)\n        expires_at",
    )
    replace(
        "app/services/sot_registry/domains/application_sessions.py",
        '                        "current or immediately previous refresh-token hash"\n',
        '                        "current or immediately previous refresh-token hash; "\n                        "the default decision clock is sampled after acquiring "\n                        "the lock so waiting neither revokes a fresh duplicate "\n                        "nor extends the five-second replay window"\n',
    )
    replace(
        "docs/designs/AUTH_SESSION_REFRESH_CONCURRENCY.md",
        "  token means. Other renewals for that session wait for the lock.\n",
        "  token means. Other renewals for that session wait for the lock.\n- Runtime decision time is sampled after the row lock is acquired. Sampling\n  before waiting can make a legitimate duplicate appear older than the\n  completed rotation, or allow a replay whose overlap expired while waiting.\n  An explicitly supplied `observed_at` remains authoritative for deterministic\n  callers. Client binding, session expiry and the five-second limit are not\n  relaxed.\n",
    )
    replace(
        "docs/designs/AUTH_SESSION_REFRESH_CONCURRENCY.md",
        "- `tests/test_auth_session_refresh.py` covers the overlap and fail-closed rules.\n",
        "- `tests/test_auth_session_refresh.py` covers the overlap and fail-closed rules,\n  including deterministic lock-order clock inversion and overlap expiry during\n  a lock wait. Both cases must use the post-lock runtime decision time.\n",
    )
else:
    raise SystemExit("Expected tests or fix")
