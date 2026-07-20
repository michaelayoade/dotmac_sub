"""Tests for admin billing dunning web helpers."""

from uuid import uuid4

from app.models.collections import DunningCaseStatus
from app.schemas.collections import DunningCaseCreate
from app.services import collections as collections_service
from app.services.web_billing_dunning import apply_bulk_action_result


def test_dunning_bulk_action_result_reports_partial_failure(
    db_session, subscriber_account
):
    case = collections_service.dunning_cases.create(
        db_session,
        DunningCaseCreate(
            account_id=subscriber_account.id,
            status=DunningCaseStatus.open,
        ),
    )

    result = apply_bulk_action_result(
        db_session,
        case_ids_csv=f"{case.id},{uuid4()}",
        action="pause",
    )

    db_session.refresh(case)
    assert case.status == DunningCaseStatus.paused
    assert result.selected == 2
    assert result.processed_ids == [str(case.id)]
    assert len(result.failed_ids) == 1
    assert result.message("pause") == "Paused 1 of 2 selected dunning cases; 1 failed"
