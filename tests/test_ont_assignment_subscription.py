from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.models.catalog import Subscription
from app.services.network_subscriber_bridge import DefaultSubscriberValidator


def test_subscription_binding_derives_subscriber_owner():
    subscription_id = uuid4()
    subscriber_id = uuid4()
    db = MagicMock()
    db.get.return_value = SimpleNamespace(
        id=subscription_id, subscriber_id=subscriber_id
    )

    resolved = DefaultSubscriberValidator().resolve_assignment_subscription(
        db, subscription_id=subscription_id, subscriber_id=None
    )

    db.get.assert_called_once_with(Subscription, subscription_id)
    assert resolved == (subscription_id, subscriber_id)


def test_subscription_binding_rejects_cross_customer_assignment():
    db = MagicMock()
    db.get.return_value = SimpleNamespace(id=uuid4(), subscriber_id=uuid4())

    with pytest.raises(HTTPException) as exc:
        DefaultSubscriberValidator().resolve_assignment_subscription(
            db, subscription_id=uuid4(), subscriber_id=uuid4()
        )

    assert exc.value.status_code == 400
