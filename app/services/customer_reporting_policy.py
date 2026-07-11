"""Shared customer-counting predicates for reporting and dashboards."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.catalog import Subscription
from app.models.subscriber import Subscriber, UserType
from app.services.customer_service_state import active_customer_subscription_filters


def customer_account_filters(subscriber_model) -> tuple:
    return (
        subscriber_model.user_type == UserType.customer,
        subscriber_model.is_active.is_(True),
    )


def active_customer_subscription_query():
    return (
        select(Subscription.id, Subscription.subscriber_id)
        .join(Subscriber, Subscriber.id == Subscription.subscriber_id)
        .where(
            *customer_account_filters(Subscriber),
            *active_customer_subscription_filters(Subscription, Subscriber),
        )
    )


def active_customer_subscription_count(db: Session) -> int:
    return int(
        db.execute(
            select(func.count()).select_from(
                active_customer_subscription_query().subquery()
            )
        ).scalar()
        or 0
    )
