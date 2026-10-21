from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.models.router_health import NasCoaOutcomeBucket
from app.services.router_health import coa_evidence
from app.services.router_health.contracts import CoaKind


@pytest.fixture()
def maker(monkeypatch):
    engine = create_engine(
        "sqlite+pysqlite://", connect_args={"check_same_thread": False}
    )
    NasCoaOutcomeBucket.__table__.create(bind=engine)
    made = sessionmaker(bind=engine)
    monkeypatch.setattr(
        "app.services.router_health.coa_evidence.db_session_adapter.create_session",
        lambda: made(),
    )
    coa_evidence.reset_breaker()
    yield made
    coa_evidence.reset_breaker()
    engine.dispose()


def rows(maker):
    with maker() as s:
        return list(s.execute(select(NasCoaOutcomeBucket)).scalars())


def test_upsert_increments_within_the_hour(maker) -> None:
    nas = uuid4()
    at = datetime(2026, 10, 12, 10, 30, tzinfo=UTC)
    for _ in range(3):
        coa_evidence.record_coa_outcome(nas, CoaKind.disconnect, "timeout", now=at)
    coa_evidence.record_coa_outcome(nas, CoaKind.update, "sent", now=at)
    by = {(r.kind, r.disposition): r for r in rows(maker)}
    assert by[("disconnect", "timeout")].count == 3
    assert by[("update", "sent")].count == 1
    assert by[("disconnect", "timeout")].bucket_start.minute == 0


def test_next_hour_is_a_new_bucket(maker) -> None:
    nas = uuid4()
    coa_evidence.record_coa_outcome(
        nas, "disconnect", "rejected", now=datetime(2026, 10, 12, 10, 59, tzinfo=UTC)
    )
    coa_evidence.record_coa_outcome(
        nas, "disconnect", "rejected", now=datetime(2026, 10, 12, 11, 1, tzinfo=UTC)
    )
    assert len(rows(maker)) == 2


def test_invalid_kind_or_disposition_is_refused_without_raising(maker, caplog) -> None:
    coa_evidence.record_coa_outcome(uuid4(), "bogus", "timeout")
    coa_evidence.record_coa_outcome(uuid4(), "disconnect", "bogus")
    assert rows(maker) == []


def test_rollback_of_a_caller_session_does_not_remove_evidence(
    maker, db_session
) -> None:
    nas = uuid4()
    db_session.begin_nested()
    coa_evidence.record_coa_outcome(nas, "disconnect", "timeout")
    db_session.rollback()
    assert len(rows(maker)) == 1


def test_write_failure_never_raises_and_opens_the_breaker(monkeypatch, caplog) -> None:
    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(
        "app.services.router_health.coa_evidence.db_session_adapter.create_session",
        boom,
    )
    coa_evidence.reset_breaker()
    with caplog.at_level(logging.ERROR):
        for _ in range(5):
            coa_evidence.record_coa_outcome(uuid4(), "disconnect", "timeout")
    errors = [r for r in caplog.records if r.message == "coa_outcome_record_failed"]
    assert len(errors) == coa_evidence.BREAKER_FAILURES  # then the breaker skips writes
    coa_evidence.reset_breaker()


def test_model_has_no_foreign_keys() -> None:
    assert not NasCoaOutcomeBucket.__table__.foreign_keys


def test_prune_deletes_old_buckets(maker) -> None:
    nas = uuid4()
    coa_evidence.record_coa_outcome(
        nas, "disconnect", "timeout", now=datetime(2026, 1, 1, tzinfo=UTC)
    )
    coa_evidence.record_coa_outcome(nas, "disconnect", "timeout")
    deleted = coa_evidence.prune_coa_outcome_buckets(retention_days=35)
    assert deleted == 1 and len(rows(maker)) == 1
