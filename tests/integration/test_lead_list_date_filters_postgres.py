"""Created-date predicates preserve row/count/summary parity on migrated PostgreSQL."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from app.models.party import Party, PartyContactPoint
from app.models.sales import Lead
from app.services import sales


@pytest.mark.parametrize(
    ("preset", "expected_count"),
    [("custom", 14), ("last_7_days", 14), ("last_30_days", 15)],
)
def test_created_date_scope_rows_count_summary_and_paging(
    db_session: Session, preset: str, expected_count: int
) -> None:
    start = datetime(2026, 9, 9, tzinfo=UTC)
    end = datetime(2026, 9, 16, tzinfo=UTC)
    middle = datetime(2026, 9, 15, 12, tzinfo=UTC)
    rows: list[Lead] = []
    instants = [start] + [middle] * 12 + [end - timedelta(microseconds=1)]
    instants += [start - timedelta(microseconds=1), end, middle]
    for index, instant in enumerate(instants):
        party = Party(
            display_name=f"PG date cohort {uuid4()}",
            party_type="person",
            status="active",
        )
        db_session.add(party)
        db_session.flush()
        # Multiple matching contact records must not multiply Lead rows or compare JSON.
        for suffix in ("first", "second"):
            value = f"pg-date-cohort-{suffix}-{uuid4()}@example.com"
            db_session.add(
                PartyContactPoint(
                    party_id=party.id,
                    channel_type="email",
                    normalized_value=value,
                    display_value=value,
                    is_active=True,
                )
            )
        lead = Lead(
            party_id=party.id,
            party_bound_at=middle,
            party_binding_source="pytest-postgresql",
            party_binding_reason="Created date query regression",
            title=f"PG date cohort {index}",
            status="won" if index == 13 else "new",
            created_at=instant,
            updated_at=middle,
            is_active=index != 16,
            estimated_value=Decimal("100.00"),
            currency="NGN",
            metadata_={"cohort": {"index": index}},
        )
        db_session.add(lead)
        rows.append(lead)
    db_session.flush()
    with patch("app.services.sales.service.datetime", wraps=datetime) as clock:
        clock.now.return_value = middle
        pages = [
            sales.leads.query(
                db_session,
                sales.LeadListQueryInput(
                    search_term="pg-date-cohort",
                    date_preset=preset,
                    date_from="2026-09-09",
                    date_to="2026-09-15",
                    page=page,
                    page_size=10,
                ),
            )
            for page in (1, 2)
        ]
    first, second = pages
    assert len(first.items) == 10
    assert len(second.items) == expected_count - 10
    expected = rows[:expected_count]
    assert set(first.items + second.items) == set(expected)
    assert len({lead.id for lead in first.items + second.items}) == expected_count
    for page in pages:
        assert page.total_count == page.summary.total_leads == expected_count
        assert page.summary.won_leads == 1
        assert page.summary.open_leads == expected_count - 1
        assert page.summary.pipeline_value == Decimal("100.00") * (expected_count - 1)
