"""Generic subscription update/PATCH cannot change the served IPv4."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.schemas.catalog import SubscriptionUpdate
from app.services import catalog as catalog_service


def test_update_rejects_ipv4_change_with_pointer_to_replace(db_session, subscription):
    subscription.ipv4_address = "192.0.2.10"
    db_session.commit()

    with pytest.raises(HTTPException) as excinfo:
        catalog_service.subscriptions.update(
            db_session,
            str(subscription.id),
            SubscriptionUpdate(ipv4_address="192.0.2.99"),
        )

    assert excinfo.value.status_code == 409
    assert "Replace IPv4" in str(excinfo.value.detail)
    db_session.refresh(subscription)
    assert subscription.ipv4_address == "192.0.2.10"


def test_update_rejects_clearing_the_served_ipv4(db_session, subscription):
    subscription.ipv4_address = "192.0.2.10"
    db_session.commit()

    with pytest.raises(HTTPException) as excinfo:
        catalog_service.subscriptions.update(
            db_session,
            str(subscription.id),
            SubscriptionUpdate(ipv4_address=None),
        )

    assert excinfo.value.status_code == 409


def test_update_allows_unchanged_ipv4_and_other_fields(db_session, subscription):
    subscription.ipv4_address = "192.0.2.10"
    db_session.commit()

    updated = catalog_service.subscriptions.update(
        db_session,
        str(subscription.id),
        SubscriptionUpdate(ipv4_address="192.0.2.10", service_description="ok"),
    )

    assert updated.ipv4_address == "192.0.2.10"
    assert updated.service_description == "ok"
