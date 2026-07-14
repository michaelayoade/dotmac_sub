from __future__ import annotations

import uuid

from app.models.subscriber import Subscriber, SubscriberStatus, UserType
from app.services.web_subscriber_lists import (
    build_subscriber_list_page,
    build_subscriber_list_query,
)


def _subscriber(
    db_session,
    *,
    marker: str,
    index: int,
    status: SubscriberStatus = SubscriberStatus.active,
) -> Subscriber:
    subscriber = Subscriber(
        first_name=marker,
        last_name="Same",
        email=f"{marker.lower()}-{index}@example.com",
        status=status,
        is_active=status == SubscriberStatus.active,
        user_type=UserType.customer,
        billing_enabled=True,
        marketing_opt_in=False,
    )
    db_session.add(subscriber)
    db_session.flush()
    return subscriber


def test_subscriber_list_filters_before_pagination(db_session):
    marker = f"SubscriberFilter{uuid.uuid4().hex[:8]}"
    for index in range(11):
        _subscriber(
            db_session,
            marker=marker,
            index=index,
            status=SubscriberStatus.active,
        )
    suspended = [
        _subscriber(
            db_session,
            marker=marker,
            index=index + 20,
            status=SubscriberStatus.suspended,
        )
        for index in range(11)
    ]
    db_session.commit()

    query = build_subscriber_list_query(
        search=marker,
        status="suspended",
        subscriber_type=None,
        sort_by="name",
        sort_dir="asc",
        page=2,
        per_page=10,
    )
    page = build_subscriber_list_page(db_session, list_query=query)
    rows = page.query.all()

    assert page.page_meta.total_items == 11
    assert page.page_meta.total_pages == 2
    assert len(rows) == 1
    assert rows[0].id in {subscriber.id for subscriber in suspended}


def test_subscriber_list_uses_id_as_stable_sort_tie_breaker(db_session):
    marker = f"SubscriberStable{uuid.uuid4().hex[:8]}"
    subscribers = [
        _subscriber(db_session, marker=marker, index=index) for index in range(12)
    ]
    db_session.commit()

    def page_ids(page_number: int) -> list[uuid.UUID]:
        query = build_subscriber_list_query(
            search=marker,
            status=None,
            subscriber_type=None,
            sort_by="name",
            sort_dir="asc",
            page=page_number,
            per_page=10,
        )
        page = build_subscriber_list_page(db_session, list_query=query)
        return [row.id for row in page.query.all()]

    first_page = page_ids(1)
    second_page = page_ids(2)

    assert len(first_page) == 10
    assert len(second_page) == 2
    assert set(first_page).isdisjoint(second_page)
    assert first_page + second_page == sorted(
        (subscriber.id for subscriber in subscribers),
        key=str,
    )
