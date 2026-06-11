"""Customer/reseller portal session lifecycle: revoke-all epochs and the
absolute cap on sliding refreshes (in-memory fallback store under pytest)."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.services import customer_portal_session as cps
from app.services import reseller_portal
from app.services.session_store import store_session


def test_revoke_customer_sessions_invalidates_existing_tokens():
    subscriber_id = uuid4()
    token = cps.create_customer_session(
        username="cust@example.com",
        account_id=subscriber_id,
        subscriber_id=subscriber_id,
    )
    assert cps.get_customer_session(token) is not None

    cps.revoke_customer_sessions_for_subscriber(str(subscriber_id))
    assert cps.get_customer_session(token) is None


def test_revoke_customer_sessions_leaves_other_subscribers_alone():
    subscriber_a, subscriber_b = uuid4(), uuid4()
    token_b = cps.create_customer_session(
        username="other@example.com",
        account_id=subscriber_b,
        subscriber_id=subscriber_b,
    )
    cps.revoke_customer_sessions_for_subscriber(str(subscriber_a))
    assert cps.get_customer_session(token_b) is not None


def test_customer_refresh_capped_at_absolute_lifetime():
    subscriber_id = uuid4()
    token = cps.create_customer_session(
        username="cap@example.com",
        account_id=subscriber_id,
        subscriber_id=subscriber_id,
    )
    # Backdate creation past the absolute cap; the next refresh must end the
    # session instead of sliding it forward.
    session = cps.get_customer_session(token)
    session["created_at"] = (datetime.now(UTC) - timedelta(days=31)).isoformat()
    store_session(
        cps._CUSTOMER_SESSION_PREFIX,  # noqa: SLF001
        token,
        session,
        3600,
        cps._CUSTOMER_SESSIONS,  # noqa: SLF001
    )

    assert cps.refresh_customer_session(token) is None
    assert cps.get_customer_session(token) is None


def test_customer_refresh_inside_absolute_lifetime_slides():
    subscriber_id = uuid4()
    token = cps.create_customer_session(
        username="slide@example.com",
        account_id=subscriber_id,
        subscriber_id=subscriber_id,
    )
    refreshed = cps.refresh_customer_session(token)
    assert refreshed is not None
    # Expiry never exceeds creation + absolute cap.
    created = datetime.fromisoformat(refreshed["created_at"])
    expires = datetime.fromisoformat(refreshed["expires_at"])
    assert expires <= created + timedelta(days=30, seconds=5)


def test_revoke_reseller_sessions_invalidates_existing_tokens():
    subscriber_id = str(uuid4())
    token = reseller_portal._create_session(  # noqa: SLF001
        username="reseller@example.com",
        reseller_id=str(uuid4()),
        remember=False,
        subscriber_id=subscriber_id,
    )
    assert reseller_portal._get_session(token) is not None  # noqa: SLF001

    reseller_portal.revoke_reseller_sessions_for_subscriber(subscriber_id)
    assert reseller_portal._get_session(token) is None  # noqa: SLF001
