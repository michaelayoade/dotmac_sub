from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.models.subscriber import Subscriber
from app.web.admin import customers as customers_web
from app.web.admin import subscribers as subscribers_web


def test_customer_geocode_route_rejects_invalid_address_id(db_session):
    with pytest.raises(HTTPException) as exc:
        customers_web.geocode_address(
            address_id="None",
            latitude=6.5,
            longitude=3.3,
            db=db_session,
        )
    assert exc.value.status_code == 400


def test_subscriber_geocode_route_rejects_invalid_address_id(db_session):
    with pytest.raises(HTTPException) as exc:
        subscribers_web.geocode_address(
            address_id="None",
            latitude=6.5,
            longitude=3.3,
            db=db_session,
        )
    assert exc.value.status_code == 400


def test_customer_geocode_primary_creates_address_when_missing(db_session):
    subscriber = Subscriber(
        first_name="Geo",
        last_name="Customer",
        email="geo-customer@example.com",
        address_line1="8 Ikot Ekpene Close",
        city="AMAC",
        region="FCT",
        country_code="NG",
    )
    db_session.add(subscriber)
    db_session.commit()

    response = customers_web.geocode_primary_address(
        customer_id=str(subscriber.id),
        latitude=9.0765,
        longitude=7.3986,
        db=db_session,
    )
    payload = response.body.decode("utf-8")
    assert response.status_code == 200
    assert '"created_address":true' in payload


def test_subscriber_geocode_primary_creates_address_when_missing(db_session):
    subscriber = Subscriber(
        first_name="Geo",
        last_name="Subscriber",
        email="geo-subscriber@example.com",
        address_line1="8 Ikot Ekpene Close",
        city="AMAC",
        region="FCT",
        country_code="NG",
    )
    db_session.add(subscriber)
    db_session.commit()

    response = subscribers_web.geocode_primary_address(
        subscriber_id=subscriber.id,
        latitude=9.0765,
        longitude=7.3986,
        db=db_session,
    )
    payload = response.body.decode("utf-8")
    assert response.status_code == 200
    assert '"created_address":true' in payload
