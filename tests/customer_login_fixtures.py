"""Explicit customer classification for the customer-authentication test lane."""

import pytest
from sqlalchemy.orm import Session

from app.models.subscriber import Subscriber, UserType
from app.services.radius_auth import (
    ObserveRadiusAuthentication,
    RadiusAuthenticationObservation,
)


@pytest.fixture
def customer_subscriber(db_session: Session, subscriber: Subscriber) -> Subscriber:
    # Direct ORM construction uses a legacy staff default. Match the explicit
    # classification assigned by the real customer account-creation owner.
    subscriber.user_type = UserType.customer
    db_session.commit()
    db_session.refresh(subscriber)
    return subscriber


def radius_not_accepted(
    db: Session, *, query: ObserveRadiusAuthentication
) -> RadiusAuthenticationObservation:
    return RadiusAuthenticationObservation.NOT_ACCEPTED


def radius_accepted(
    db: Session, *, query: ObserveRadiusAuthentication
) -> RadiusAuthenticationObservation:
    return RadiusAuthenticationObservation.ACCEPTED
