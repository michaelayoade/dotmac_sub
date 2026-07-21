"""Read-only CRM customer provenance resolution tests."""

from __future__ import annotations

from app.models.subscriber import Subscriber
from app.services.crm_customers import (
    CRMCustomerObservation,
    CRMCustomerObservationStatus,
    observe_customer,
)


def test_person_provenance_takes_priority_over_other_identifiers(db_session) -> None:
    person = Subscriber(
        first_name="Person",
        last_name="Match",
        email="person@example.com",
        metadata_={"crm_person_id": "person-1"},
    )
    order = Subscriber(
        first_name="Order",
        last_name="Match",
        email="order@example.com",
        metadata_={"crm_sales_order_id": "order-1"},
    )
    db_session.add_all([person, order])
    db_session.commit()

    outcome = observe_customer(
        db_session,
        CRMCustomerObservation(
            crm_person_id="person-1",
            crm_quote_id=None,
            crm_sales_order_id="order-1",
        ),
    )

    assert outcome.status is CRMCustomerObservationStatus.MATCHED
    assert outcome.subscriber_id == str(person.id)
    assert outcome.matched_via == ("crm_person_id",)


def test_quote_and_order_must_resolve_to_one_account(db_session) -> None:
    subscriber = Subscriber(
        first_name="Exact",
        last_name="Match",
        email="exact@example.com",
        metadata_={"crm_quote_id": "quote-1", "crm_sales_order_id": "order-1"},
    )
    db_session.add(subscriber)
    db_session.commit()

    outcome = observe_customer(
        db_session,
        CRMCustomerObservation(
            crm_person_id=None,
            crm_quote_id="quote-1",
            crm_sales_order_id="order-1",
        ),
    )

    assert outcome.status is CRMCustomerObservationStatus.MATCHED
    assert outcome.subscriber_id == str(subscriber.id)
    assert outcome.matched_via == ("crm_sales_order_id", "crm_quote_id")


def test_conflicting_quote_and_order_are_ambiguous(db_session) -> None:
    db_session.add_all(
        [
            Subscriber(
                first_name="Quote",
                last_name="Account",
                email="quote@example.com",
                metadata_={"crm_quote_id": "quote-1"},
            ),
            Subscriber(
                first_name="Order",
                last_name="Account",
                email="order@example.com",
                metadata_={"crm_sales_order_id": "order-1"},
            ),
        ]
    )
    db_session.commit()

    outcome = observe_customer(
        db_session,
        CRMCustomerObservation(
            crm_person_id=None,
            crm_quote_id="quote-1",
            crm_sales_order_id="order-1",
        ),
    )

    assert outcome.status is CRMCustomerObservationStatus.AMBIGUOUS
    assert outcome.subscriber_id is None


def test_observer_never_writes_or_commits(db_session, monkeypatch) -> None:
    monkeypatch.setattr(
        db_session,
        "commit",
        lambda: (_ for _ in ()).throw(AssertionError("observer committed")),
    )

    outcome = observe_customer(
        db_session,
        CRMCustomerObservation(
            crm_person_id="missing",
            crm_quote_id=None,
            crm_sales_order_id=None,
        ),
    )

    assert outcome.status is CRMCustomerObservationStatus.UNMATCHED
    assert not db_session.new
    assert not db_session.dirty
