from unittest.mock import MagicMock, patch


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

    def test_notifications_preview_returns_recent_items_and_total(
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
        )
        from app.services.customer_portal_notifications import get_notifications_preview

        subscriber.phone = "+2348000000000"
        db_session.add_all(
            [
                Notification(
                    subscriber_id=subscriber.id,
                    channel=NotificationChannel.email,
                    recipient=subscriber.email,
                    body="Billing reminder",
                    status=NotificationStatus.delivered,
                ),
                CustomerNotificationEvent(
                    entity_type="service_order",
                    entity_id=subscriber.id,
                    subscriber_id=subscriber.id,
                    channel="sms",
                    recipient=subscriber.phone,
                    message="Technician dispatched",
                    status=CustomerNotificationStatus.sent,
                ),
            ]
        )
        db_session.commit()

        preview = get_notifications_preview(
            db_session,
            {"subscriber_id": str(subscriber.id)},
            limit=1,
        )

        assert preview["recent_notifications_total"] == 2
        assert preview["unread_notifications_count"] == 2
        assert preview["has_recent_notifications"] is True
        assert len(preview["recent_notifications"]) == 1

    def test_notifications_page_prefers_subscriber_id_and_hides_non_visible_statuses(
        self, db_session, subscriber
    ) -> None:
        from app.models.notification import (
            Notification,
            NotificationChannel,
            NotificationStatus,
        )
        from app.models.subscriber import Subscriber
        from app.services.customer_portal_notifications import get_notifications_page

        other_subscriber = Subscriber(
            first_name="Other",
            last_name="User",
            email="other@example.com",
            phone=subscriber.phone,
        )
        db_session.add(other_subscriber)
        db_session.flush()

        db_session.add_all(
            [
                Notification(
                    subscriber_id=subscriber.id,
                    channel=NotificationChannel.email,
                    recipient="old-email@example.com",
                    event_type="invoice_paid",
                    category="billing",
                    body="Delivered to owned subscriber",
                    status=NotificationStatus.delivered,
                ),
                Notification(
                    subscriber_id=subscriber.id,
                    channel=NotificationChannel.email,
                    recipient=subscriber.email,
                    event_type="invoice_paid",
                    category="billing",
                    body="Queued row should stay hidden",
                    status=NotificationStatus.queued,
                ),
                Notification(
                    subscriber_id=other_subscriber.id,
                    channel=NotificationChannel.email,
                    recipient=subscriber.email,
                    event_type="invoice_paid",
                    category="billing",
                    body="Recipient collision should stay hidden",
                    status=NotificationStatus.delivered,
                ),
            ]
        )
        db_session.commit()

        page = get_notifications_page(
            db_session,
            {"subscriber_id": str(subscriber.id)},
            page=1,
            per_page=10,
        )

        assert page["total"] == 1
        assert page["notifications"][0].message == "Delivered to owned subscriber"

    def test_notifications_page_respects_billing_and_sms_preferences(
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
        )
        from app.services.customer_portal_notifications import get_notifications_page

        subscriber.metadata_ = {
            "billing_notifications": False,
            "sms_updates": False,
        }
        subscriber.phone = "+2348000000011"
        db_session.add_all(
            [
                Notification(
                    subscriber_id=subscriber.id,
                    channel=NotificationChannel.email,
                    recipient=subscriber.email,
                    event_type="invoice_paid",
                    category="billing",
                    body="Billing email",
                    status=NotificationStatus.delivered,
                ),
                CustomerNotificationEvent(
                    entity_type="service_order_completed",
                    entity_id=subscriber.id,
                    subscriber_id=subscriber.id,
                    channel="sms",
                    recipient=subscriber.phone,
                    message="Service order completed",
                    status=CustomerNotificationStatus.sent,
                ),
            ]
        )
        db_session.commit()

        page = get_notifications_page(
            db_session,
            {"subscriber_id": str(subscriber.id)},
            page=1,
            per_page=10,
        )

        assert page["total"] == 0


class TestCustomerProfileNotifications:
    def test_update_customer_profile_persists_preferences_and_emits_subscriber_updated(
        self, db_session, subscriber
    ) -> None:
        from app.services.events.types import EventType
        from app.services.web_customer_actions import update_customer_profile

        with patch("app.services.subscriber.emit_event") as emit_event_mock:
            updated = update_customer_profile(
                db_session,
                subscriber_id=str(subscriber.id),
                name="Updated Customer",
                email="updated@example.com",
                phone="+2348000000012",
                billing_notifications=False,
                sms_updates=True,
            )

        assert updated is not None
        assert updated.email == "updated@example.com"
        assert updated.phone == "+2348000000012"
        assert (updated.metadata_ or {}).get("billing_notifications") is False
        assert (updated.metadata_ or {}).get("sms_updates") is True
        assert emit_event_mock.call_args.args[1] == EventType.subscriber_updated

    def test_customer_update_profile_route_passes_notification_preferences(
        self,
    ) -> None:
        from app.web.customer.routes import customer_update_profile

        request = MagicMock()
        customer = {"subscriber_id": "sub-1"}

        with (
            patch(
                "app.web.customer.routes.get_current_customer_from_request",
                return_value=customer,
            ),
            patch(
                "app.services.web_customer_actions.update_customer_profile"
            ) as update_mock,
        ):
            response = customer_update_profile(
                request=request,
                name="Updated Customer",
                email="updated@example.com",
                phone="+2348000000012",
                billing_notifications=False,
                sms_updates=True,
                db=MagicMock(),
            )

        assert response.status_code == 303
        kwargs = update_mock.call_args.kwargs
        assert kwargs["billing_notifications"] is False
        assert kwargs["sms_updates"] is True
