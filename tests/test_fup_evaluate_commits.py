"""evaluate_fup_rules must commit per subscription so the periodic sweep never
holds one transaction across the whole active-subscriber list — the long-lived
"idle in transaction" on `subscriptions` that pinned a DB pool slot and blocked
autovacuum in prod.

Driven with mocks (the established pattern for tasks in test_celery_tasks.py):
the task uses the production `SessionLocal` + `commit()`, which the
rollback-isolated `db_session` fixture can't host, and the test engine is
session-scoped so committing real fixture data would leak across tests.
"""

from unittest.mock import MagicMock, patch
from uuid import uuid4


def _fake_sub():
    return MagicMock(id=uuid4(), offer_id=uuid4(), subscriber_id=uuid4())


def _run_with_subs(subs):
    session = MagicMock()
    # session.query(Subscription).join(...).filter(...).filter(...).all()
    session.query.return_value.join.return_value.filter.return_value.filter.return_value.all.return_value = subs
    fup_state_mock = MagicMock()
    fup_state_mock.get.return_value = None  # no period-boundary reset
    bucket = MagicMock(used_gb=0, period_end=None)

    with (
        patch("app.tasks.usage.SessionLocal", return_value=session),
        patch("app.services.fup_state.fup_state", fup_state_mock),
        patch(
            "app.services.usage._resolve_or_create_quota_bucket",
            return_value=bucket,
        ),
        patch("app.services.fup.evaluate_rules", return_value=[]),
    ):
        from app.tasks.usage import evaluate_fup_rules

        result = evaluate_fup_rules()
    return session, result


def test_commits_once_per_subscription():
    subs = [_fake_sub(), _fake_sub(), _fake_sub()]
    session, result = _run_with_subs(subs)

    assert result["processed"] == len(subs)
    # The fix: a commit per subscription. The bug committed exactly once, after
    # the entire loop, so call_count would have been 1 regardless of subscriber
    # count. Allow the trailing post-loop commit too.
    assert session.commit.call_count >= len(subs)
    session.close.assert_called_once()
    session.rollback.assert_not_called()


def test_no_subscriptions_is_clean_noop():
    session, result = _run_with_subs([])
    assert result["processed"] == 0
    session.close.assert_called_once()
    session.rollback.assert_not_called()
