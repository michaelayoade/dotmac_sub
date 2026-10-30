"""Fast unit-lane identity clearance checks; not migration acceptance."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from app.models.party import Party, PartyContactPoint
from app.services import team_inbox_prospect_resolution as resolution
from tests.test_lead_intake import _conversation


def _clearance() -> dict[str, str]:
    return {
        "status": "cleared",
        "cleared_by_principal_id": str(uuid4()),
        "cleared_at": datetime.now(UTC).isoformat(),
    }


@pytest.mark.parametrize(
    ("status", "review", "expected"),
    [
        ("unmatched", None, True),
        ("unmatched", "open", False),
        ("ambiguous", "cleared", False),
        ("reviewed_no_match", None, False),
        ("reviewed_no_match", "open", False),
        ("reviewed_no_match", "cleared", True),
    ],
)
def test_resolution_owner_interprets_review_state(
    db_session: Session, status: str, review: str | None, expected: bool
) -> None:
    conversation, _message = _conversation(db_session)
    metadata: dict[str, object] = {"contact_resolution": {"status": status}}
    if review is not None:
        metadata["identity_correction"] = {**_clearance(), "status": review}
    conversation.metadata_ = metadata
    db_session.flush()
    result = resolution.lead_intake_resolution_eligibility(
        db_session, query=resolution.LeadIntakeResolutionQuery(conversation.id)
    )
    assert result.eligible is expected


@pytest.mark.parametrize("invalid", ["principal", "naive_time", "future_time"])
def test_reviewed_no_match_requires_valid_clearance_provenance(
    db_session: Session, invalid: str
) -> None:
    conversation, _message = _conversation(db_session)
    clearance = _clearance()
    if invalid == "principal":
        clearance.pop("cleared_by_principal_id")
    elif invalid == "naive_time":
        clearance["cleared_at"] = "2026-10-29T12:00:00"
    else:
        clearance["cleared_at"] = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    conversation.metadata_ = {
        "contact_resolution": {"status": "reviewed_no_match"},
        "identity_correction": clearance,
    }
    db_session.flush()
    assert not resolution.lead_intake_resolution_eligibility(
        db_session, query=resolution.LeadIntakeResolutionQuery(conversation.id)
    ).eligible


def test_clearance_does_not_override_current_canonical_identity(
    db_session: Session,
) -> None:
    conversation, _message = _conversation(db_session)
    conversation.metadata_ = {
        "contact_resolution": {"status": "reviewed_no_match"},
        "identity_correction": _clearance(),
    }
    party = Party(party_type="person", display_name="Fixture Party")
    db_session.add(party)
    db_session.flush()
    db_session.add(
        PartyContactPoint(
            party_id=party.id,
            channel_type="whatsapp",
            normalized_value=conversation.contact_address,
            display_value=conversation.contact_address,
            is_active=True,
        )
    )
    db_session.flush()
    assert not resolution.lead_intake_resolution_eligibility(
        db_session, query=resolution.LeadIntakeResolutionQuery(conversation.id)
    ).eligible
