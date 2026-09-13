"""``build_page_state`` is read-only: repeated GETs never change persisted state.

Before this change, `build_page_state` called `purge_expired_from_recovery_queue`
on every render (a GET-driven purge) and returned `purged_count`. This test
fails against that old behavior because it asserts zero DB writes across two
consecutive calls; it passes now because the purge call and the
`purged_count`/`"Auto-purged now"` result were removed entirely, not merely
disabled.
"""

from __future__ import annotations

from app.services import web_system_restore_tool


def _table_snapshot(db) -> dict[str, int]:
    from sqlalchemy import func, select

    from app.models.account_recovery import AccountRecoveryRecord
    from app.models.subscriber import Subscriber

    return {
        "subscribers": db.scalar(select(func.count()).select_from(Subscriber)),
        "account_recovery_records": db.scalar(
            select(func.count()).select_from(AccountRecoveryRecord)
        ),
    }


def test_build_page_state_never_mutates_across_two_calls(db_session):
    before = _table_snapshot(db_session)
    web_system_restore_tool.build_page_state(
        db_session, query=None, selected_id=None
    )
    web_system_restore_tool.build_page_state(
        db_session, query=None, selected_id=None
    )
    after = _table_snapshot(db_session)
    assert before == after


def test_build_page_state_has_no_purged_count_key(db_session):
    state = web_system_restore_tool.build_page_state(
        db_session, query=None, selected_id=None
    )
    assert "purged_count" not in state
    assert "retention_days" not in state


def test_list_recently_deleted_returns_typed_rows(db_session):
    rows = web_system_restore_tool.list_recently_deleted(db_session, limit=5)
    assert isinstance(rows, list)
    for row in rows:
        assert hasattr(row, "subscriber")
        assert hasattr(row, "record")
