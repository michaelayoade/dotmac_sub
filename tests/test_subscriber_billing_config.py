from decimal import Decimal

from app.schemas.subscriber import SubscriberUpdate
from app.services import subscriber as subscriber_service
from app.services.web_subscriber_details import build_subscriber_detail_page_context


def test_subscriber_category_is_persisted_in_metadata(db_session, subscriber):
    updated = subscriber_service.subscribers.update(
        db_session,
        subscriber_id=str(subscriber.id),
        payload=SubscriberUpdate(category="business"),
    )
    assert updated.category.value == "business"
    assert (updated.metadata_ or {}).get("subscriber_category") == "business"


def test_subscriber_detail_includes_billing_config_snapshot(db_session, subscriber):
    metadata = dict(subscriber.metadata_ or {})
    metadata.update(
        {
            "blocking_period_days": 5,
            "deactivation_period_days": 14,
            "auto_create_invoices": False,
            "send_billing_notifications": True,
        }
    )
    subscriber_service.subscribers.update(
        db_session,
        subscriber_id=str(subscriber.id),
        payload=SubscriberUpdate(
            billing_day=3,
            payment_due_days=7,
            grace_period_days=2,
            min_balance=Decimal("100.00"),
            metadata_=metadata,
        ),
    )

    context = build_subscriber_detail_page_context(db_session, str(subscriber.id))
    cfg = context["billing_config"]

    assert cfg["billing_day"] == 3
    assert cfg["payment_due_days"] == 7
    assert cfg["blocking_period_days"] == 5
    assert cfg["deactivation_period_days"] == 14
    assert cfg["auto_create_invoices"] is False
