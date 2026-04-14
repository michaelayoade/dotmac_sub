"""Tests for customer portal gap fixes.

Covers: ticket creation, password change, event types, route registration.
"""

from datetime import UTC, datetime
from types import SimpleNamespace
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

    def test_save_radius_config_persists_explicit_captive_redirect_toggle(self, db_session) -> None:
        from app.services.domain_settings import radius_settings
        from app.services.web_system_config import save_radius_config

        save_radius_config(
            db_session,
            {
                "captive_redirect_enabled": "true",
                "captive_portal_ip": "203.0.113.10/32",
                "captive_portal_url": "https://example.com/portal",
            },
        )

        setting = radius_settings.get_by_key(db_session, "captive_redirect_enabled")
        assert setting.value_text == "true"

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


class TestPlanChangeUiHelpers:
    def test_offer_price_summary_uses_recurring_price_cycle(self) -> None:
        from types import SimpleNamespace

        from app.models.catalog import PriceType
        from app.services.customer_portal_flow_changes import get_offer_price_summary

        offer = SimpleNamespace(
            prices=[
                SimpleNamespace(
                    is_active=True,
                    price_type=PriceType.recurring,
                    amount="4999.99",
                    currency="NGN",
                    billing_cycle=SimpleNamespace(value="weekly"),
                )
            ],
            billing_cycle=None,
        )

        summary = get_offer_price_summary(offer)

        assert summary.amount == 4999.99
        assert summary.currency == "NGN"
        assert summary.period_label == "/week"

    def test_plan_change_copy_mentions_proration_for_prepaid(self) -> None:
        from types import SimpleNamespace

        from app.services.customer_portal_flow_changes import get_plan_change_copy

        copy = get_plan_change_copy(
            SimpleNamespace(billing_mode=SimpleNamespace(value="prepaid"))
        )

        assert "prorated invoice or credit note" in copy["billing_message"]

    def test_get_fup_status_uses_highest_active_rule_threshold_for_allowance(self) -> None:
        from app.models.fup import FupDataUnit
        from app.services.customer_portal_flow_services import _get_fup_status

        db = MagicMock()
        subscription_id = "00000000-0000-0000-0000-000000000123"
        db.get.return_value = SimpleNamespace(next_billing_at=datetime(2026, 3, 30, tzinfo=UTC))

        query = MagicMock()
        db.query.return_value = query
        query.filter.return_value = query
        query.first.return_value = SimpleNamespace(rx=None, tx=None)

        policy = SimpleNamespace(
            is_active=True,
            rules=[
                SimpleNamespace(is_active=True, sort_order=1, threshold_amount=80, threshold_unit=FupDataUnit.gb),
                SimpleNamespace(is_active=True, sort_order=2, threshold_amount=120, threshold_unit=FupDataUnit.gb),
            ],
            offer=None,
        )

        from unittest.mock import patch

        with patch("app.services.fup.FupPolicies.get_by_offer", return_value=policy):
            status = _get_fup_status(db, "offer-1", subscription_id)

        assert status is not None
        assert status["allowance_gb"] == 120.0

    def test_get_fup_status_prefers_usage_records_over_bandwidth_estimate(
        self, db_session, subscription, catalog_offer
    ) -> None:
        from app.models.fup import FupDataUnit
        from app.models.usage import UsageRecord, UsageSource
        from app.services.customer_portal_flow_services import _get_fup_status

        subscription.offer_id = catalog_offer.id
        subscription.next_billing_at = datetime(2026, 3, 30, tzinfo=UTC)
        db_session.add(
            UsageRecord(
                subscription_id=subscription.id,
                source=UsageSource.radius,
                recorded_at=datetime(2026, 3, 20, 12, 0, tzinfo=UTC),
                input_gb=2,
                output_gb=1,
                total_gb=3,
            )
        )
        db_session.commit()

        policy = SimpleNamespace(
            is_active=True,
            rules=[
                SimpleNamespace(is_active=True, sort_order=1, threshold_amount=10, threshold_unit=FupDataUnit.gb),
            ],
            offer=None,
        )

        from unittest.mock import patch

        with patch("app.services.fup.FupPolicies.get_by_offer", return_value=policy):
            status = _get_fup_status(db_session, str(catalog_offer.id), str(subscription.id))

        assert status is not None
        assert status["usage_gb"] == 3.0


class TestPortalServiceVisibility:
    def test_services_page_includes_blocked_subscription(self, db_session, subscription, subscriber) -> None:
        from app.models.catalog import SubscriptionStatus
        from app.services.customer_portal_flow_services import get_services_page

        subscription.status = SubscriptionStatus.blocked
        db_session.commit()

        page = get_services_page(
            db_session,
            {"account_id": subscriber.id},
            page=1,
            per_page=10,
        )

        assert page["total"] == 1
        assert len(page["services"]) == 1
        assert page["services"][0].status == SubscriptionStatus.blocked


class TestPortalNotificationsPage:
    def test_notifications_page_merges_event_queue_and_customer_notification_events(
        self, db_session, subscriber
    ) -> None:
        from app.models.comms import (
            CustomerNotificationEvent,
            CustomerNotificationStatus,
        )
        from app.models.notification import (
            Notification,
            NotificationChannel,
            NotificationStatus,
            NotificationTemplate,
        )
        from app.services.customer_portal_notifications import get_notifications_page

        subscriber.phone = "+2348000000000"
        template = NotificationTemplate(
            name="Invoice Created",
            code="invoice_created",
            channel=NotificationChannel.email,
            body="Invoice body",
            is_active=True,
        )
        db_session.add(template)
        db_session.flush()

        queued = Notification(
            template_id=template.id,
            channel=NotificationChannel.email,
            recipient=subscriber.email,
            body="Invoice ready",
            status=NotificationStatus.delivered,
        )
        direct = CustomerNotificationEvent(
            entity_type="service_order",
            entity_id=subscriber.id,
            channel="sms",
            recipient=subscriber.phone,
            message="Technician dispatched",
            status=CustomerNotificationStatus.sent,
        )
        db_session.add_all([queued, direct])
        db_session.commit()

        page = get_notifications_page(
            db_session,
            {"subscriber_id": str(subscriber.id)},
            page=1,
            per_page=10,
        )

        assert page["total"] == 2
        entity_types = {item.entity_type for item in page["notifications"]}
        assert "invoice_created" in entity_types
        assert "service_order" in entity_types


class TestPaymentSuccessBanner:
    def test_payment_success_only_marks_service_restored_after_post_payment_check(self) -> None:
        from unittest.mock import patch

        from app.web.customer.routes import customer_verify_payment

        request = MagicMock()
        customer = {"subscriber_id": "sub-1", "account_id": "acct-1"}
        result = {
            "payment": SimpleNamespace(receipt_number="RCT-1"),
            "invoice": SimpleNamespace(id="inv-1", invoice_number="INV-1"),
            "amount": 5000,
            "reference": "ref-1",
        }

        template_response = MagicMock(name="template_response")

        with (
            patch("app.web.customer.routes.get_current_customer_from_request", return_value=customer),
            patch("app.web.customer.routes.customer_portal.verify_and_record_payment", return_value=result),
            patch("app.web.customer.routes.is_subscriber_restricted", side_effect=[True, False]),
            patch("app.web.customer.routes.templates.TemplateResponse", return_value=template_response) as render,
        ):
            response = customer_verify_payment(
                request=request,
                reference="ref-1",
                provider="paystack",
                db=MagicMock(),
            )

        assert response is template_response
        context = render.call_args.args[1]
        assert context["was_restricted"] is True
        assert context["service_restored"] is True

    def test_payment_success_does_not_claim_restoration_after_partial_payment(self) -> None:
        from unittest.mock import patch

        from app.web.customer.routes import customer_verify_payment

        request = MagicMock()
        customer = {"subscriber_id": "sub-1", "account_id": "acct-1"}
        result = {
            "payment": SimpleNamespace(receipt_number="RCT-2"),
            "invoice": SimpleNamespace(id="inv-2", invoice_number="INV-2"),
            "amount": 1000,
            "reference": "ref-2",
        }

        template_response = MagicMock(name="template_response")

        with (
            patch("app.web.customer.routes.get_current_customer_from_request", return_value=customer),
            patch("app.web.customer.routes.customer_portal.verify_and_record_payment", return_value=result),
            patch("app.web.customer.routes.is_subscriber_restricted", side_effect=[True, True]),
            patch("app.web.customer.routes.templates.TemplateResponse", return_value=template_response) as render,
        ):
            response = customer_verify_payment(
                request=request,
                reference="ref-2",
                provider="paystack",
                db=MagicMock(),
            )

        assert response is template_response
        context = render.call_args.args[1]
        assert context["was_restricted"] is True
        assert context["service_restored"] is False


class TestPlanChangeSettingsValidation:
    def test_save_plan_change_rejects_invalid_refund_policy(self) -> None:
        import pytest

        from app.services.web_system_config import save_plan_change

        with pytest.raises(ValueError, match="Refund Policy"):
            save_plan_change(
                MagicMock(),
                {
                    "refund_policy": "bogus",
                    "upgrade_fee": "0",
                    "downgrade_fee": "0",
                    "fee_tax_rate": "0",
                    "invoice_timing": "immediate",
                    "prepaid_rollover": "false",
                    "discount_transfer": "false",
                    "minimum_invoice_amount": "0",
                },
            )

    def test_save_plan_change_normalizes_valid_values(self) -> None:
        from unittest.mock import patch

        from app.services.web_system_config import save_plan_change

        with patch("app.services.web_system_config._save_settings") as save_mock:
            save_plan_change(
                MagicMock(),
                {
                    "refund_policy": "PRORATED",
                    "upgrade_fee": "500.00",
                    "downgrade_fee": "0",
                    "fee_tax_rate": "7.50",
                    "invoice_timing": "Immediate",
                    "prepaid_rollover": "TRUE",
                    "discount_transfer": "false",
                    "minimum_invoice_amount": "100.00",
                },
            )

        saved_payload = save_mock.call_args.args[2]
        assert saved_payload["refund_policy"] == "prorated"
        assert saved_payload["invoice_timing"] == "immediate"
        assert saved_payload["prepaid_rollover"] == "true"


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


class TestSuspendResumeRoutes:
    def test_suspend_get_route_exists(self) -> None:
        from app.web.customer.routes import router

        found = False
        for route in router.routes:
            path = getattr(route, "path", "")
            methods = getattr(route, "methods", set())
            if "/portal/services/" in path and "suspend" in path and "GET" in methods:
                found = True
                break
        assert found, "GET /portal/services/{id}/suspend route not found"

    def test_suspend_post_route_exists(self) -> None:
        from app.web.customer.routes import router

        found = False
        for route in router.routes:
            path = getattr(route, "path", "")
            methods = getattr(route, "methods", set())
            if "/portal/services/" in path and "suspend" in path and "POST" in methods:
                found = True
                break
        assert found, "POST /portal/services/{id}/suspend route not found"

    def test_resume_get_route_exists(self) -> None:
        from app.web.customer.routes import router

        found = False
        for route in router.routes:
            path = getattr(route, "path", "")
            methods = getattr(route, "methods", set())
            if "/portal/services/" in path and "resume" in path and "GET" in methods:
                found = True
                break
        assert found, "GET /portal/services/{id}/resume route not found"

    def test_resume_post_route_exists(self) -> None:
        from app.web.customer.routes import router

        found = False
        for route in router.routes:
            path = getattr(route, "path", "")
            methods = getattr(route, "methods", set())
            if "/portal/services/" in path and "resume" in path and "POST" in methods:
                found = True
                break
        assert found, "POST /portal/services/{id}/resume route not found"


class TestSuspendResumeServiceLayer:
    def test_get_suspend_page_returns_none_for_non_active_subscription(
        self, db_session, subscription, subscriber
    ) -> None:
        from app.models.catalog import SubscriptionStatus
        from app.services.customer_portal_flow_services import get_suspend_page

        subscription.status = SubscriptionStatus.suspended
        db_session.commit()

        result = get_suspend_page(
            db_session,
            {"account_id": subscriber.id},
            str(subscription.id),
        )

        assert result is None

    def test_get_suspend_page_returns_context_for_active_subscription(
        self, db_session, subscription, subscriber
    ) -> None:
        from app.models.catalog import SubscriptionStatus
        from app.services.customer_portal_flow_services import get_suspend_page

        subscription.status = SubscriptionStatus.active
        db_session.commit()

        result = get_suspend_page(
            db_session,
            {"account_id": subscriber.id},
            str(subscription.id),
        )

        assert result is not None
        assert "subscription" in result
        assert "max_days" in result

    def test_apply_service_suspend_creates_enforcement_lock(
        self, db_session, subscription, subscriber
    ) -> None:
        from app.models.catalog import SubscriptionStatus
        from app.models.enforcement_lock import EnforcementLock, EnforcementReason
        from app.services.customer_portal_flow_services import apply_service_suspend

        subscription.status = SubscriptionStatus.active
        db_session.commit()

        result = apply_service_suspend(
            db_session,
            {"account_id": subscriber.id},
            str(subscription.id),
            days=7,
        )

        assert result["subscription_id"] == str(subscription.id)
        assert result["days"] == 7

        # Verify lock was created
        lock = db_session.query(EnforcementLock).filter(
            EnforcementLock.subscription_id == subscription.id,
            EnforcementLock.reason == EnforcementReason.customer_hold,
        ).first()
        assert lock is not None
        assert lock.is_active is True

    def test_get_resume_page_returns_none_without_customer_hold_lock(
        self, db_session, subscription, subscriber
    ) -> None:
        from app.models.catalog import SubscriptionStatus
        from app.services.customer_portal_flow_services import get_resume_page

        subscription.status = SubscriptionStatus.suspended
        db_session.commit()

        result = get_resume_page(
            db_session,
            {"account_id": subscriber.id},
            str(subscription.id),
        )

        # No customer_hold lock exists, so cannot self-service resume
        assert result is None

    def test_apply_service_resume_restores_subscription(
        self, db_session, subscription, subscriber
    ) -> None:
        from app.models.catalog import SubscriptionStatus
        from app.models.enforcement_lock import EnforcementLock, EnforcementReason
        from app.services.customer_portal_flow_services import (
            apply_service_resume,
            apply_service_suspend,
        )

        # First suspend the subscription
        subscription.status = SubscriptionStatus.active
        db_session.commit()

        apply_service_suspend(
            db_session,
            {"account_id": subscriber.id},
            str(subscription.id),
            days=7,
        )
        db_session.refresh(subscription)
        assert subscription.status == SubscriptionStatus.suspended

        # Now resume it
        result = apply_service_resume(
            db_session,
            {"account_id": subscriber.id},
            str(subscription.id),
        )

        db_session.refresh(subscription)
        assert result["restored"] is True
        assert subscription.status == SubscriptionStatus.active

        # Verify lock was resolved
        lock = db_session.query(EnforcementLock).filter(
            EnforcementLock.subscription_id == subscription.id,
            EnforcementLock.reason == EnforcementReason.customer_hold,
        ).first()
        assert lock is not None
        assert lock.is_active is False


class TestVacationHoldUsageLimits:
    """Tests for vacation hold usage limits and cooldown periods."""

    def test_get_vacation_hold_usage_counts_holds_this_year(
        self, db_session, subscription, subscriber
    ) -> None:
        from datetime import UTC, datetime

        from app.models.enforcement_lock import EnforcementLock, EnforcementReason
        from app.services.customer_portal_flow_services import _get_vacation_hold_usage

        # Create a resolved hold from this year
        lock = EnforcementLock(
            subscription_id=subscription.id,
            subscriber_id=subscriber.id,
            reason=EnforcementReason.customer_hold,
            source="test",
            is_active=False,
            resolved_at=datetime.now(UTC),
            resolved_by="test",
            created_at=datetime.now(UTC),
        )
        db_session.add(lock)
        db_session.commit()

        usage = _get_vacation_hold_usage(db_session, str(subscription.id))

        assert usage["holds_this_year"] == 1
        assert usage["last_hold_date"] is not None
        assert usage["days_since_last"] is not None
        assert usage["days_since_last"] >= 0

    def test_get_vacation_hold_usage_returns_zero_for_no_holds(
        self, db_session, subscription
    ) -> None:
        from app.services.customer_portal_flow_services import _get_vacation_hold_usage

        usage = _get_vacation_hold_usage(db_session, str(subscription.id))

        assert usage["holds_this_year"] == 0
        assert usage["last_hold_date"] is None
        assert usage["days_since_last"] is None

    def test_apply_service_suspend_rejects_when_max_holds_reached(
        self, db_session, subscription, subscriber
    ) -> None:
        from unittest.mock import patch

        import pytest

        from app.models.catalog import SubscriptionStatus
        from app.services.customer_portal_flow_services import apply_service_suspend

        subscription.status = SubscriptionStatus.active
        db_session.commit()

        # Mock the settings and usage to simulate max holds reached
        def mock_resolve_value(db, domain, key):
            if key == "max_suspend_holds_per_year":
                return 2
            if key == "customer_suspend_enabled":
                return True
            if key == "max_suspend_days":
                return 30
            if key == "suspend_cooldown_days":
                return 0
            return None

        # Mock usage to show 2 holds already used
        mock_usage = {
            "holds_this_year": 2,
            "last_hold_date": None,
            "days_since_last": None,
        }

        with patch(
            "app.services.settings_spec.resolve_value",
            side_effect=mock_resolve_value,
        ):
            with patch(
                "app.services.customer_portal_flow_services._get_vacation_hold_usage",
                return_value=mock_usage,
            ):
                with pytest.raises(ValueError, match="maximum of 2 vacation holds"):
                    apply_service_suspend(
                        db_session,
                        {"account_id": subscriber.id},
                        str(subscription.id),
                        days=7,
                    )

    def test_apply_service_suspend_rejects_during_cooldown(
        self, db_session, subscription, subscriber
    ) -> None:
        from datetime import UTC, datetime
        from unittest.mock import patch

        import pytest

        from app.models.catalog import SubscriptionStatus
        from app.models.enforcement_lock import EnforcementLock, EnforcementReason
        from app.services.customer_portal_flow_services import apply_service_suspend

        subscription.status = SubscriptionStatus.active
        db_session.commit()

        # Create a recent hold (within cooldown period)
        lock = EnforcementLock(
            subscription_id=subscription.id,
            subscriber_id=subscriber.id,
            reason=EnforcementReason.customer_hold,
            source="test",
            is_active=False,
            resolved_at=datetime.now(UTC),
            resolved_by="test",
            created_at=datetime.now(UTC),  # Just created today
        )
        db_session.add(lock)
        db_session.commit()

        # Mock the setting to have 7 day cooldown
        def mock_resolve_value(db, domain, key):
            if key == "suspend_cooldown_days":
                return 7
            if key == "customer_suspend_enabled":
                return True
            if key == "max_suspend_days":
                return 30
            if key == "max_suspend_holds_per_year":
                return 0  # Unlimited
            return None

        with patch(
            "app.services.settings_spec.resolve_value",
            side_effect=mock_resolve_value,
        ):
            with pytest.raises(ValueError, match="wait .* more day"):
                apply_service_suspend(
                    db_session,
                    {"account_id": subscriber.id},
                    str(subscription.id),
                    days=7,
                )

    def test_get_suspend_page_shows_block_reason_when_limit_reached(
        self, db_session, subscription, subscriber
    ) -> None:
        from datetime import UTC, datetime
        from unittest.mock import patch

        from app.models.catalog import SubscriptionStatus
        from app.models.enforcement_lock import EnforcementLock, EnforcementReason
        from app.services.customer_portal_flow_services import get_suspend_page

        subscription.status = SubscriptionStatus.active
        db_session.commit()

        # Create 1 hold (simulating max_holds_per_year=1)
        lock = EnforcementLock(
            subscription_id=subscription.id,
            subscriber_id=subscriber.id,
            reason=EnforcementReason.customer_hold,
            source="test",
            is_active=False,
            resolved_at=datetime.now(UTC),
            resolved_by="test",
            created_at=datetime.now(UTC),
        )
        db_session.add(lock)
        db_session.commit()

        def mock_resolve_value(db, domain, key):
            if key == "max_suspend_holds_per_year":
                return 1
            if key == "customer_suspend_enabled":
                return True
            if key == "max_suspend_days":
                return 30
            if key == "suspend_cooldown_days":
                return 0
            return None

        with patch(
            "app.services.settings_spec.resolve_value",
            side_effect=mock_resolve_value,
        ):
            result = get_suspend_page(
                db_session,
                {"account_id": subscriber.id},
                str(subscription.id),
            )

        assert result is not None
        assert result["can_suspend"] is False
        assert result["block_reason"] is not None
        assert "maximum" in result["block_reason"]


class TestVacationHoldCeleryTask:
    """Tests for the vacation hold auto-resume Celery task."""

    def test_resume_expired_holds_processes_expired_locks(
        self, db_session, subscription, subscriber
    ) -> None:
        from datetime import UTC, datetime, timedelta
        from unittest.mock import MagicMock, patch

        from app.models.catalog import SubscriptionStatus
        from app.models.enforcement_lock import EnforcementReason
        from app.services.account_lifecycle import suspend_subscription

        # First suspend the subscription properly
        subscription.status = SubscriptionStatus.active
        db_session.commit()

        lock = suspend_subscription(
            db_session,
            str(subscription.id),
            reason=EnforcementReason.customer_hold,
            source="test",
        )
        # Set resume_at to the past
        lock.resume_at = datetime.now(UTC) - timedelta(hours=1)
        db_session.commit()

        db_session.refresh(subscription)
        assert subscription.status == SubscriptionStatus.suspended

        # Import and run the task function directly
        from app.tasks.vacation_holds import resume_expired_holds

        # Create a mock session that delegates to db_session but doesn't close
        mock_session = MagicMock(wraps=db_session)
        mock_session.close = MagicMock()  # Don't close test session
        mock_session.commit = db_session.commit
        mock_session.rollback = db_session.rollback
        mock_session.scalars = db_session.scalars

        with patch(
            "app.tasks.vacation_holds.SessionLocal", return_value=mock_session
        ):
            result = resume_expired_holds()

        assert result["total"] >= 1
        assert result["resumed"] >= 1

        db_session.refresh(subscription)
        assert subscription.status == SubscriptionStatus.active

    def test_resume_expired_holds_skips_non_expired_locks(
        self, db_session, subscription, subscriber
    ) -> None:
        from datetime import UTC, datetime, timedelta
        from unittest.mock import MagicMock, patch

        from app.models.catalog import SubscriptionStatus
        from app.models.enforcement_lock import EnforcementReason
        from app.services.account_lifecycle import suspend_subscription

        # Suspend the subscription
        subscription.status = SubscriptionStatus.active
        db_session.commit()

        lock = suspend_subscription(
            db_session,
            str(subscription.id),
            reason=EnforcementReason.customer_hold,
            source="test",
        )
        # Set resume_at to the future
        lock.resume_at = datetime.now(UTC) + timedelta(days=7)
        db_session.commit()

        from app.tasks.vacation_holds import resume_expired_holds

        mock_session = MagicMock(wraps=db_session)
        mock_session.close = MagicMock()
        mock_session.commit = db_session.commit
        mock_session.rollback = db_session.rollback
        mock_session.scalars = db_session.scalars

        with patch(
            "app.tasks.vacation_holds.SessionLocal", return_value=mock_session
        ):
            result = resume_expired_holds()

        # Should not resume since resume_at is in the future
        assert result["resumed"] == 0

        db_session.refresh(subscription)
        assert subscription.status == SubscriptionStatus.suspended

    def test_resume_expired_holds_handles_errors_gracefully(
        self, db_session, subscription, subscriber
    ) -> None:
        from datetime import UTC, datetime, timedelta
        from unittest.mock import MagicMock, patch

        from app.models.catalog import SubscriptionStatus
        from app.models.enforcement_lock import EnforcementReason
        from app.services.account_lifecycle import suspend_subscription

        # Suspend the subscription
        subscription.status = SubscriptionStatus.active
        db_session.commit()

        lock = suspend_subscription(
            db_session,
            str(subscription.id),
            reason=EnforcementReason.customer_hold,
            source="test",
        )
        lock.resume_at = datetime.now(UTC) - timedelta(hours=1)
        db_session.commit()

        from app.tasks.vacation_holds import resume_expired_holds

        mock_session = MagicMock(wraps=db_session)
        mock_session.close = MagicMock()
        mock_session.commit = db_session.commit
        mock_session.rollback = db_session.rollback
        mock_session.scalars = db_session.scalars

        # Mock restore_subscription to raise an error
        with patch(
            "app.tasks.vacation_holds.SessionLocal", return_value=mock_session
        ):
            with patch(
                "app.tasks.vacation_holds.restore_subscription",
                side_effect=Exception("Test error"),
            ):
                result = resume_expired_holds()

        # Should count as failed but not crash
        assert result["failed"] >= 1
        assert result["resumed"] == 0


class TestPlaywrightPortalRoutes:
    def test_usage_page_object_uses_canonical_portal_route(self) -> None:
        from tests.playwright.pages.customer.usage_page import CustomerUsagePage

        defaults = CustomerUsagePage.goto.__defaults__
        assert defaults is not None
        assert defaults[0] == "/portal/usage"
