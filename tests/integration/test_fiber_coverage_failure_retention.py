"""Real failed PostgreSQL statements must not roll back captured enquiries."""

from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models.integration_platform import IntegrationInbox
from app.models.sales import Lead
from app.models.team_inbox import InboxConversationLeadLink
from app.services.sales.fiber_feasibility import (
    FiberFeasibilityQuery,
    FiberFeasibilityResult,
)
from tests.test_fiber_inquiry_webhook import _binding, _coverage_payload, _post

pytestmark = pytest.mark.integration


def test_failed_postgres_coverage_query_preserves_lead_and_replay(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    binding = _binding(db_session, monkeypatch)

    def fail_query(
        db: Session, *, query: FiberFeasibilityQuery
    ) -> FiberFeasibilityResult:
        assert query.latitude == Decimal("9.0765")
        db.execute(text("SELECT 1 / 0"))
        raise AssertionError("The failed PostgreSQL statement must raise")

    monkeypatch.setattr("app.services.sales.fiber_feasibility.assess", fail_query)
    payload = _coverage_payload()
    first = _post(db_session, binding.id, payload, "pg-coverage-failure")
    assert first.coverage is not None
    assert first.coverage.status == "technical_error"
    assert first.reference is not None
    assert len(db_session.scalars(select(Lead)).all()) == 1
    assert len(db_session.scalars(select(InboxConversationLeadLink)).all()) == 1
    receipt = db_session.scalar(select(IntegrationInbox))
    assert receipt is not None and receipt.state == "processed"
    replay = _post(db_session, binding.id, payload, "pg-coverage-failure")
    assert replay.replayed
    assert replay.reference == first.reference
    assert len(db_session.scalars(select(Lead)).all()) == 1
