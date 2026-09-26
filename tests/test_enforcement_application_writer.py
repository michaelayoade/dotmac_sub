"""Exercises the real ``_record_enforcement_application`` writer (ADR-0017 §7).

Unlike ``tests/test_enforcement_application_outcomes.py`` (which patches this
writer out to unit-test outcome classification), these tests run the actual
upsert against a real SQLite database, bound in place of
``app.services.enforcement.db_session_adapter.create_session`` so the writer's
own out-of-band-session behaviour is exercised rather than the caller's
session.

Both failure points are swallowed and logged at ERROR: opening the session
(an unreachable database) and executing the upsert. The session open sits
inside the writer's ``try`` so neither can raise into the enforcement caller.
"""

from __future__ import annotations

import logging
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.models.enforcement_application import (
    EnforcementApplication,
    EnforcementEffect,
    EnforcementPath,
)
from app.services.enforcement import EnforcementOutcome, _record_enforcement_application
from app.services.nas.enforcement_failure import classify_enforcement_failure


@pytest.fixture()
def writer_engine():
    """A private, per-test SQLite engine carrying only the one table under
    test — the shared ``tests/conftest.py`` ``engine`` fixture is
    session-scoped and shared across the whole suite, which would leak rows
    between tests here."""
    engine = create_engine(
        "sqlite+pysqlite://", connect_args={"check_same_thread": False}
    )
    EnforcementApplication.__table__.create(bind=engine)
    yield engine
    engine.dispose()


@pytest.fixture()
def writer_sessionmaker(writer_engine):
    return sessionmaker(bind=writer_engine)


@pytest.fixture()
def patch_create_session(writer_sessionmaker, monkeypatch):
    """Route the writer's out-of-band session at the test engine, a NEW
    session per call — matching the real writer's own per-call session."""
    monkeypatch.setattr(
        "app.services.enforcement.db_session_adapter.create_session",
        lambda: writer_sessionmaker(),
    )


def _get_row(
    writer_sessionmaker: sessionmaker[Session],
    subscription_id,
    nas_device_id,
    effect: EnforcementEffect,
) -> EnforcementApplication | None:
    with writer_sessionmaker() as session:
        return session.execute(
            select(EnforcementApplication).where(
                EnforcementApplication.subscription_id == subscription_id,
                EnforcementApplication.nas_device_id == nas_device_id,
                EnforcementApplication.effect == effect.value,
            )
        ).scalar_one_or_none()


def _failed_outcome() -> EnforcementOutcome:
    return EnforcementOutcome.failed_from(
        RuntimeError("no route to host"), path=EnforcementPath.ssh
    )


class TestEnforcementApplicationWriter:
    def test_first_failure_sets_attempt_count_one_and_first_failed_at(
        self, patch_create_session, writer_sessionmaker
    ):
        sub_id, nas_id = uuid4(), uuid4()

        _record_enforcement_application(
            subscription_id=sub_id,
            nas_device_id=nas_id,
            effect=EnforcementEffect.address_list_block,
            outcome=_failed_outcome(),
        )

        row = _get_row(
            writer_sessionmaker, sub_id, nas_id, EnforcementEffect.address_list_block
        )
        assert row is not None
        assert row.attempt_count == 1
        assert row.first_failed_at is not None
        assert row.failure_class is not None
        assert row.outcome == "failed"

    def test_second_failure_increments_count_and_keeps_first_failed_at(
        self, patch_create_session, writer_sessionmaker
    ):
        sub_id, nas_id = uuid4(), uuid4()
        effect = EnforcementEffect.address_list_block

        _record_enforcement_application(
            subscription_id=sub_id,
            nas_device_id=nas_id,
            effect=effect,
            outcome=_failed_outcome(),
        )
        first_row = _get_row(writer_sessionmaker, sub_id, nas_id, effect)
        first_failed_at = first_row.first_failed_at

        _record_enforcement_application(
            subscription_id=sub_id,
            nas_device_id=nas_id,
            effect=effect,
            outcome=_failed_outcome(),
        )
        second_row = _get_row(writer_sessionmaker, sub_id, nas_id, effect)

        assert second_row.attempt_count == 2
        assert second_row.first_failed_at == first_failed_at

    def test_applied_resets_attempt_count_and_clears_first_failed_at(
        self, patch_create_session, writer_sessionmaker
    ):
        sub_id, nas_id = uuid4(), uuid4()
        effect = EnforcementEffect.address_list_block

        _record_enforcement_application(
            subscription_id=sub_id,
            nas_device_id=nas_id,
            effect=effect,
            outcome=_failed_outcome(),
        )
        _record_enforcement_application(
            subscription_id=sub_id,
            nas_device_id=nas_id,
            effect=effect,
            outcome=EnforcementOutcome.applied(path=EnforcementPath.ssh),
        )

        row = _get_row(writer_sessionmaker, sub_id, nas_id, effect)
        assert row.outcome == "applied"
        assert row.attempt_count == 0
        assert row.first_failed_at is None
        assert row.last_success_at is not None
        assert row.failure_class is None

    def test_not_applicable_after_a_failure_clears_the_failure_streak(
        self, patch_create_session, writer_sessionmaker
    ):
        sub_id, nas_id = uuid4(), uuid4()
        effect = EnforcementEffect.address_list_block

        _record_enforcement_application(
            subscription_id=sub_id,
            nas_device_id=nas_id,
            effect=effect,
            outcome=_failed_outcome(),
        )
        failed_row = _get_row(writer_sessionmaker, sub_id, nas_id, effect)
        failed_attempt_count = failed_row.attempt_count
        failed_first_failed_at = failed_row.first_failed_at

        _record_enforcement_application(
            subscription_id=sub_id,
            nas_device_id=nas_id,
            effect=effect,
            outcome=EnforcementOutcome.not_applicable("no_ssh_credentials"),
        )

        row = _get_row(writer_sessionmaker, sub_id, nas_id, effect)
        # Sensitivity: the prior failure really had a live streak to clear.
        assert failed_attempt_count == 1
        assert failed_first_failed_at is not None
        assert row.outcome == "not_applicable"
        assert row.attempt_count == 0
        assert row.first_failed_at is None
        assert row.failure_class is None

    def test_one_row_per_subscription_nas_effect_and_a_different_effect_gets_its_own_row(
        self, patch_create_session, writer_sessionmaker
    ):
        sub_id, nas_id = uuid4(), uuid4()

        _record_enforcement_application(
            subscription_id=sub_id,
            nas_device_id=nas_id,
            effect=EnforcementEffect.address_list_block,
            outcome=_failed_outcome(),
        )
        _record_enforcement_application(
            subscription_id=sub_id,
            nas_device_id=nas_id,
            effect=EnforcementEffect.address_list_block,
            outcome=_failed_outcome(),
        )
        _record_enforcement_application(
            subscription_id=sub_id,
            nas_device_id=nas_id,
            effect=EnforcementEffect.session_kick,
            outcome=_failed_outcome(),
        )

        with writer_sessionmaker() as session:
            rows = session.execute(select(EnforcementApplication)).scalars().all()

        assert len(rows) == 2
        block_row = _get_row(
            writer_sessionmaker, sub_id, nas_id, EnforcementEffect.address_list_block
        )
        kick_row = _get_row(
            writer_sessionmaker, sub_id, nas_id, EnforcementEffect.session_kick
        )
        assert block_row.attempt_count == 2
        assert kick_row.attempt_count == 1

    def test_create_session_failure_is_swallowed_and_logged(self, monkeypatch, caplog):
        """An unreachable database at session-open time must never raise into
        the enforcement caller (ADR-0017 §7): it is logged and swallowed."""

        def _boom():
            raise RuntimeError("no database available")

        monkeypatch.setattr(
            "app.services.enforcement.db_session_adapter.create_session", _boom
        )

        with caplog.at_level(logging.ERROR, logger="app.services.enforcement"):
            _record_enforcement_application(
                subscription_id=uuid4(),
                nas_device_id=uuid4(),
                effect=EnforcementEffect.address_list_block,
                outcome=_failed_outcome(),
            )

        assert [
            r
            for r in caplog.records
            if r.getMessage() == "enforcement_application_record_failed"
        ], "a session-open failure must still be logged at ERROR"

    def test_execute_failure_is_swallowed_and_logged(
        self, writer_sessionmaker, monkeypatch, caplog
    ):
        """A failure INSIDE the writer's own try block (e.g. the upsert
        itself) is caught, rolled back, and logged — never raised."""
        # A fresh session bound to the same engine, but the table has not
        # been created against it, so the upsert's INSERT fails at execute().
        broken_engine = create_engine(
            "sqlite+pysqlite://", connect_args={"check_same_thread": False}
        )
        broken_sessionmaker = sessionmaker(bind=broken_engine)
        monkeypatch.setattr(
            "app.services.enforcement.db_session_adapter.create_session",
            lambda: broken_sessionmaker(),
        )

        with caplog.at_level(logging.ERROR, logger="app.services.enforcement"):
            _record_enforcement_application(
                subscription_id=uuid4(),
                nas_device_id=uuid4(),
                effect=EnforcementEffect.address_list_block,
                outcome=_failed_outcome(),
            )

        events = [
            record
            for record in caplog.records
            if getattr(record, "event", None) == "enforcement_application_record_failed"
        ]
        assert len(events) == 1
        broken_engine.dispose()


def test_classify_enforcement_failure_is_the_only_source_of_failure_class():
    """Sanity check that ``_failed_outcome`` above exercises a real
    classifier path rather than a hand-picked enum value, so the writer
    tests above are pinning genuine classifier output."""
    failure_class, _detail = classify_enforcement_failure(
        RuntimeError("no route to host")
    )
    assert _failed_outcome().failure_class == failure_class


def test_a_task_time_limit_is_re_raised_not_swallowed(monkeypatch):
    """ADR-0017 section 7: write failures are swallowed, but a Celery soft time
    limit must reach the task so it cannot run past its budget."""
    from billiard.exceptions import SoftTimeLimitExceeded

    # Sensitivity: the pinned billiard makes this an Exception subclass, so a
    # bare `except Exception` WOULD swallow it without the explicit re-raise.
    assert issubclass(SoftTimeLimitExceeded, Exception)

    def _time_limit():
        raise SoftTimeLimitExceeded()

    monkeypatch.setattr(
        "app.services.enforcement.db_session_adapter.create_session", _time_limit
    )
    with pytest.raises(SoftTimeLimitExceeded):
        _record_enforcement_application(
            subscription_id=uuid4(),
            nas_device_id=uuid4(),
            effect=EnforcementEffect.address_list_block,
            outcome=_failed_outcome(),
        )
