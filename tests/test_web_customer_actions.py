from app.models.catalog import SubscriptionStatus
from app.services import web_customer_actions as web_customer_actions_service


def _update_person_status(db_session, subscriber, status: str) -> None:
    web_customer_actions_service.update_person_customer(
        db=db_session,
        customer_id=str(subscriber.id),
        first_name=subscriber.first_name,
        last_name=subscriber.last_name,
        display_name=None,
        avatar_url=None,
        email=subscriber.email,
        email_verified="false",
        phone=None,
        date_of_birth=None,
        gender=subscriber.gender.value if subscriber.gender else "unknown",
        preferred_contact_method=None,
        locale=None,
        timezone_value=None,
        address_line1=None,
        address_line2=None,
        city=None,
        region=None,
        postal_code=None,
        country_code=None,
        status=status,
        is_active=None,
        marketing_opt_in="false",
        notes=None,
        account_start_date=None,
        metadata_json=None,
    )


def test_blocked_status_suspends_pending_subscription(db_session, subscriber, subscription):
    assert subscription.status == SubscriptionStatus.pending
    _update_person_status(db_session, subscriber, "blocked")
    db_session.refresh(subscription)
    assert subscription.status == SubscriptionStatus.suspended


def test_blocked_status_suspends_active_subscription(db_session, subscriber, subscription):
    subscription.status = SubscriptionStatus.active
    db_session.commit()
    _update_person_status(db_session, subscriber, "blocked")
    db_session.refresh(subscription)
    assert subscription.status == SubscriptionStatus.suspended


def test_non_blocked_status_does_not_force_subscription_suspension(db_session, subscriber, subscription):
    subscription.status = SubscriptionStatus.active
    db_session.commit()
    _update_person_status(db_session, subscriber, "active")
    db_session.refresh(subscription)
    assert subscription.status == SubscriptionStatus.active
