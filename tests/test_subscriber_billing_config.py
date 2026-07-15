from decimal import Decimal

from app.schemas.subscriber import SubscriberUpdate
from app.services import subscriber as subscriber_service
from app.services import web_customer_actions as web_customer_actions_service
from app.services.subscriber import _apply_billing_defaults
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
    assert cfg["auto_create_invoices"] is False
    assert "blocking_period_days" not in cfg
    assert "deactivation_period_days" not in cfg
    assert "next_block_at" not in cfg
    assert "next_block_label" not in cfg


def test_billing_defaults_do_not_materialize_inherited_grace(
    db_session, subscriber, monkeypatch
):
    subscriber.grace_period_days = None
    monkeypatch.setattr(
        "app.services.subscriber.settings_spec.resolve_value",
        lambda _db, _domain, key: {
            "prepaid_default_billing_day": "1",
            "prepaid_default_payment_due_days": "0",
            "prepaid_default_min_balance": "0",
        }.get(key),
    )

    _apply_billing_defaults(db_session, subscriber)

    assert subscriber.grace_period_days is None


def test_billing_form_preserves_explicit_zero_grace(subscriber):
    subscriber.grace_period_days = 0

    values = web_customer_actions_service.billing_form_defaults(subscriber)

    assert values["grace_period_days"] == "0"
