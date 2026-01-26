import pytest
from fastapi import HTTPException

from app.services import catalog as catalog_service
from app.services import notification as notification_service
from app.services import subscriber as subscriber_service


def test_catalog_offer_list_invalid_status(db_session):
    with pytest.raises(HTTPException) as exc:
        catalog_service.offers.list(
            db_session,
            service_type=None,
            access_type=None,
            status="invalid",
            is_active=None,
            order_by="created_at",
            order_dir="desc",
            limit=10,
            offset=0,
        )
    assert exc.value.status_code == 400


def test_notification_list_invalid_status(db_session):
    with pytest.raises(HTTPException) as exc:
        notification_service.notifications.list(
            db_session,
            channel=None,
            status="bad",
            is_active=None,
            order_by="created_at",
            order_dir="desc",
            limit=10,
            offset=0,
        )
    assert exc.value.status_code == 400


def test_subscriber_list_invalid_type(db_session):
    with pytest.raises(HTTPException) as exc:
        subscriber_service.subscribers.list(
            db_session,
            subscriber_type="not_a_type",
            person_id=None,
            organization_id=None,
            order_by="created_at",
            order_dir="desc",
            limit=10,
            offset=0,
        )
    assert exc.value.status_code == 400

