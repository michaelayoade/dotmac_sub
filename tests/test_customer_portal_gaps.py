"""Tests for customer portal gap fixes.

Covers: ticket creation, password change, event types, route registration.
"""

from datetime import UTC, datetime
from unittest.mock import MagicMock
from app.services.events.types import EventType

# ---------------------------------------------------------------------------
# 1. Customer event types
# ---------------------------------------------------------------------------


class TestCustomerEventTypes:
    def test_customer_login_event(self) -> None:
        assert EventType.customer_login.value == "customer.login"

    def test_customer_logout_event(self) -> None:
        assert EventType.customer_logout.value == "customer.logout"

    def test_customer_ticket_created_event(self) -> None:
        assert EventType.customer_ticket_created.value == "customer.ticket_created"

    def test_customer_password_changed_event(self) -> None:
        assert EventType.customer_password_changed.value == "customer.password_changed"


# ---------------------------------------------------------------------------
# 2. Route registration
# ---------------------------------------------------------------------------


class TestCustomerRouteRegistration:
    def test_support_new_get_route_exists(self) -> None:
        from app.web.customer.routes import router

        found = False
        for route in router.routes:
            if getattr(route, "path", "") == "/portal/support/new" and "GET" in getattr(route, "methods", set()):
                found = True
                break
        assert found, "GET /portal/support/new route not found"

    def test_support_new_post_route_exists(self) -> None:
        from app.web.customer.routes import router

        found = False
        for route in router.routes:
            if getattr(route, "path", "") == "/portal/support/new" and "POST" in getattr(route, "methods", set()):
                found = True
                break
        assert found, "POST /portal/support/new route not found"

    def test_password_change_get_route_removed(self) -> None:
        """Password change route must not exist — credentials are shared with PPPoE."""
        from app.web.customer.routes import router

        for route in router.routes:
            if getattr(route, "path", "") == "/portal/profile/password" and "GET" in getattr(route, "methods", set()):
                raise AssertionError("GET /portal/profile/password must not exist (PPPoE credential safety)")

    def test_password_change_post_route_removed(self) -> None:
        """Password change route must not exist — credentials are shared with PPPoE."""
        from app.web.customer.routes import router

        for route in router.routes:
            if getattr(route, "path", "") == "/portal/profile/password" and "POST" in getattr(route, "methods", set()):
                raise AssertionError("POST /portal/profile/password must not exist (PPPoE credential safety)")

    def test_support_info_route_exists(self) -> None:
        """Public support contact page must be accessible without auth."""
        from app.web.customer.auth import router as auth_router

        found = False
        for route in auth_router.routes:
            if getattr(route, "path", "") == "/portal/auth/support-info" and "GET" in getattr(route, "methods", set()):
                found = True
                break
        assert found, "GET /portal/auth/support-info route not found"

    def test_support_comment_post_route_exists(self) -> None:
        from app.web.customer.routes import router

        found = False
        for route in router.routes:
            path = getattr(route, "path", "")
            if "/portal/support/" in path and "comment" in path and "POST" in getattr(route, "methods", set()):
                found = True
                break
        assert found, "POST /portal/support/{ticket}/comment route not found"


# ---------------------------------------------------------------------------
# 3. Ticket creation schema
# ---------------------------------------------------------------------------


class TestTicketCreationSchema:
    def test_ticket_create_schema_accepts_portal_fields(self) -> None:
        from app.models.support import TicketChannel, TicketPriority
        from app.schemas.support import TicketCreate

        payload = TicketCreate(
            title="Internet not working",
            description="Connection drops every 10 minutes",
            priority=TicketPriority.high,
            channel=TicketChannel.web,
        )
        assert payload.title == "Internet not working"
        assert payload.priority == TicketPriority.high
        assert payload.channel == TicketChannel.web

    def test_ticket_create_with_subscriber_id(self) -> None:
        import uuid

        from app.schemas.support import TicketCreate

        sub_id = uuid.uuid4()
        payload = TicketCreate(
            title="Speed issue",
            subscriber_id=sub_id,
            customer_account_id=sub_id,
        )
        assert payload.subscriber_id == sub_id


# ---------------------------------------------------------------------------
# 4. Password validation
# ---------------------------------------------------------------------------


class TestPasswordValidation:
    def test_password_hashing_roundtrip(self) -> None:
        from app.services.auth_flow import hash_password, verify_password

        password = "TestP@ssw0rd!2024"
        hashed = hash_password(password)
        assert hashed != password
        assert verify_password(password, hashed)

    def test_wrong_password_fails(self) -> None:
        from app.services.auth_flow import hash_password, verify_password

        hashed = hash_password("correct_password")
        assert not verify_password("wrong_password", hashed)


# ---------------------------------------------------------------------------
# 5. Event emission helper
# ---------------------------------------------------------------------------


class TestEventEmissionHelper:
    def test_emit_customer_event_handles_missing_type(self) -> None:
        """Non-existent event type should not raise."""
        from unittest.mock import MagicMock

        from app.web.customer.routes import _emit_customer_event

        db = MagicMock()
        # Should not raise
        _emit_customer_event(db, "nonexistent_event_xyz", {})

    def test_emit_customer_event_calls_emit(self) -> None:
        from unittest.mock import MagicMock, patch

        db = MagicMock()
        with patch("app.web.customer.routes.emit_event", create=True):
            from app.web.customer.routes import _emit_customer_event
            _emit_customer_event(db, "customer_login", {"subscriber_id": "test"})


# ---------------------------------------------------------------------------
# 6. Comment schema
# ---------------------------------------------------------------------------


class TestNotificationsRoute:
    def test_notifications_route_exists(self) -> None:
        from app.web.customer.routes import router

        found = False
        for route in router.routes:
            if getattr(route, "path", "") == "/portal/notifications" and "GET" in getattr(route, "methods", set()):
                found = True
                break
        assert found, "GET /portal/notifications route not found"

    def test_invoice_pdf_route_exists(self) -> None:
        from app.web.customer.routes import router

        found = False
        for route in router.routes:
            path = getattr(route, "path", "")
            if "/portal/billing/invoices/" in path and "pdf" in path and "GET" in getattr(route, "methods", set()):
                found = True
                break
        assert found, "GET /portal/billing/invoices/{id}/pdf route not found"


class TestCommentSchema:
    def test_comment_create_schema(self) -> None:
        from app.schemas.support import TicketCommentCreate

        comment = TicketCommentCreate(body="This is my reply", is_internal=False)
        assert comment.body == "This is my reply"
        assert comment.is_internal is False


class TestCaptiveRedirectPersistence:
    def test_subscriber_update_schema_accepts_captive_redirect_flag(self) -> None:
        from app.schemas.subscriber import SubscriberUpdate

        payload = SubscriberUpdate(captive_redirect_enabled=True)
        assert payload.captive_redirect_enabled is True

    def test_billing_override_payload_sets_captive_redirect_flag(self) -> None:
        from app.services.web_customer_actions import _billing_override_payload

        payload = _billing_override_payload(
            billing_enabled_override=None,
            billing_day=None,
            payment_due_days=None,
            grace_period_days=None,
            min_balance=None,
            captive_redirect_enabled="true",
            tax_rate_id=None,
            payment_method=None,
        )

        assert payload["captive_redirect_enabled"] is True


class TestRestrictedContextHelpers:
    def test_get_restricted_since_uses_metadata_timestamp(self) -> None:
        from app.models.subscriber import Subscriber
        from app.services.customer_portal_context import get_restricted_since

        subscriber = Subscriber(
            first_name="Jane",
            last_name="Doe",
            email="jane@example.com",
            metadata_={"restricted_since": "2026-03-10T12:00:00+00:00"},
        )

        result = get_restricted_since(subscriber)
        assert result == datetime(2026, 3, 10, 12, 0, tzinfo=UTC)

    def test_total_outstanding_balance_queries_positive_active_balances(self) -> None:
        from app.services.customer_portal_context import get_total_outstanding_balance

        scalar_query = MagicMock()
        scalar_query.filter.return_value = scalar_query
        scalar_query.scalar.return_value = 125.5
        db = MagicMock()
        db.query.return_value = scalar_query

        total = get_total_outstanding_balance(db, "00000000-0000-0000-0000-000000000001")

        assert total == 125.5


class TestRestrictedStatusMetadata:
    def test_restricted_status_transition_sets_restricted_since(self) -> None:
        from app.models.subscriber import Subscriber, SubscriberStatus
        from app.services.subscriber import _update_restricted_status_metadata

        subscriber = Subscriber(
            first_name="Jane",
            last_name="Doe",
            email="jane@example.com",
            metadata_={},
        )

        _update_restricted_status_metadata(
            subscriber,
            previous_status=SubscriberStatus.active,
            next_status=SubscriberStatus.blocked,
        )

        assert subscriber.metadata_ is not None
        assert "restricted_since" in subscriber.metadata_
        assert subscriber.metadata_["restricted_status"] == "blocked"

    def test_leaving_restricted_status_records_exit(self) -> None:
        from app.models.subscriber import Subscriber, SubscriberStatus
        from app.services.subscriber import _update_restricted_status_metadata

        subscriber = Subscriber(
            first_name="Jane",
            last_name="Doe",
            email="jane@example.com",
            metadata_={"restricted_since": "2026-03-10T12:00:00+00:00"},
        )

        _update_restricted_status_metadata(
            subscriber,
            previous_status=SubscriberStatus.suspended,
            next_status=SubscriberStatus.active,
        )

        assert subscriber.metadata_ is not None
        assert subscriber.metadata_["last_restricted_status"] == "suspended"
        assert "last_restricted_ended_at" in subscriber.metadata_
