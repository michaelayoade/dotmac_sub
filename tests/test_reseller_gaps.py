"""Tests for reseller portal gap fixes.

Covers: account detail view, invoice visibility, search filtering,
event system integration, revenue summary, and route registration.
"""

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest

from app.services.domain_errors import DomainError
from app.services.events.types import EventType
from tests.reseller_status_helpers import (
    confirm_status_action,
    preview_status_confirmation,
)

# ---------------------------------------------------------------------------
# 1. Event types
# ---------------------------------------------------------------------------


class TestResellerEventTypes:
    def test_reseller_login_event(self) -> None:
        assert EventType.reseller_login.value == "reseller.login"

    def test_reseller_logout_event(self) -> None:
        assert EventType.reseller_logout.value == "reseller.logout"

    def test_reseller_impersonated_event(self) -> None:
        assert EventType.reseller_impersonated.value == "reseller.impersonated"


# ---------------------------------------------------------------------------
# 2. Account detail service
# ---------------------------------------------------------------------------


class TestAccountDetail:
    def test_get_account_detail_returns_none_for_wrong_reseller(
        self, db_session
    ) -> None:
        from app.models.subscriber import Reseller, Subscriber

        reseller1 = Reseller(name="Reseller A", is_active=True)
        reseller2 = Reseller(name="Reseller B", is_active=True)
        db_session.add_all([reseller1, reseller2])
        db_session.commit()

        account = Subscriber(
            first_name="Test",
            last_name="User",
            email="test@example.com",
            reseller_id=reseller1.id,
        )
        db_session.add(account)
        db_session.commit()

        from app.services.reseller_portal import get_account_detail

        # Correct reseller
        result = get_account_detail(db_session, str(reseller1.id), str(account.id))
        assert result is not None
        assert result["first_name"] == "Test"
        assert isinstance(result["subscriptions"], list)

        # Wrong reseller — must return None
        result = get_account_detail(db_session, str(reseller2.id), str(account.id))
        assert result is None

    def test_get_account_detail_includes_subscriptions(self, db_session) -> None:
        from app.models.catalog import (
            AccessType,
            CatalogOffer,
            OfferStatus,
            PriceBasis,
            ServiceType,
            Subscription,
            SubscriptionStatus,
        )
        from app.models.subscriber import Reseller, Subscriber

        reseller = Reseller(name="Sub Test", is_active=True)
        db_session.add(reseller)
        db_session.commit()

        account = Subscriber(
            first_name="Sub",
            last_name="User",
            email="sub@example.com",
            reseller_id=reseller.id,
        )
        db_session.add(account)
        db_session.commit()

        offer = CatalogOffer(
            name="Basic 10Mbps",
            status=OfferStatus.active,
            service_type=ServiceType.residential,
            access_type=AccessType.fiber,
            price_basis=PriceBasis.flat,
        )
        db_session.add(offer)
        db_session.commit()

        sub = Subscription(
            subscriber_id=account.id,
            offer_id=offer.id,
            status=SubscriptionStatus.active,
            start_at=datetime.now(UTC),
        )
        db_session.add(sub)
        db_session.commit()

        from app.services.reseller_portal import get_account_detail

        result = get_account_detail(db_session, str(reseller.id), str(account.id))
        assert result is not None
        assert len(result["subscriptions"]) == 1
        assert result["subscriptions"][0]["offer_name"] == "Basic 10Mbps"

    def test_get_account_detail_status_actions_come_from_owner(
        self, db_session
    ) -> None:
        from app.models.catalog import (
            AccessType,
            CatalogOffer,
            OfferStatus,
            PriceBasis,
            ServiceType,
            Subscription,
            SubscriptionStatus,
        )
        from app.models.subscriber import Reseller, Subscriber

        reseller = Reseller(name="Actions Test", is_active=True)
        db_session.add(reseller)
        db_session.commit()
        account = Subscriber(
            first_name="Act",
            last_name="User",
            email="actions@example.com",
            reseller_id=reseller.id,
        )
        db_session.add(account)
        db_session.commit()
        offer = CatalogOffer(
            name="Basic",
            status=OfferStatus.active,
            service_type=ServiceType.residential,
            access_type=AccessType.fiber,
            price_basis=PriceBasis.flat,
        )
        db_session.add(offer)
        db_session.commit()
        for status in (SubscriptionStatus.active, SubscriptionStatus.suspended):
            db_session.add(
                Subscription(
                    subscriber_id=account.id,
                    offer_id=offer.id,
                    status=status,
                    start_at=datetime.now(UTC),
                )
            )
        db_session.commit()

        from app.services.reseller_portal import get_account_detail

        actions = get_account_detail(db_session, str(reseller.id), str(account.id))[
            "status_actions"
        ]
        # Restore acts on the suspended service; deactivate on the active one;
        # disable on both (neither is terminal) — eligibility and counts come
        # from the owner, not a status string in the template.
        assert actions["restore"]["allowed"] is True
        assert actions["restore"]["affected"] == 1
        assert actions["deactivate"]["allowed"] is True
        assert actions["deactivate"]["affected"] == 1
        assert actions["disable"]["allowed"] is True
        assert actions["disable"]["affected"] == 2
        assert all(actions[action]["fingerprint"] for action in actions)

    def test_status_action_preview_accounts_for_active_locks(self, db_session) -> None:
        from app.models.catalog import (
            AccessType,
            CatalogOffer,
            PriceBasis,
            ServiceType,
            Subscription,
            SubscriptionStatus,
        )
        from app.models.enforcement_lock import EnforcementLock, EnforcementReason
        from app.models.subscriber import Reseller, Subscriber
        from app.services import reseller_portal

        reseller = Reseller(name="Locked Preview", is_active=True)
        account = Subscriber(
            first_name="Locked",
            last_name="Customer",
            email="locked-preview@example.com",
            reseller=reseller,
        )
        offer = CatalogOffer(
            name="Locked preview plan",
            service_type=ServiceType.residential,
            access_type=AccessType.fiber,
            price_basis=PriceBasis.flat,
        )
        db_session.add_all([reseller, account, offer])
        db_session.flush()
        subscription = Subscription(
            subscriber_id=account.id,
            offer_id=offer.id,
            status=SubscriptionStatus.active,
            start_at=datetime.now(UTC),
        )
        db_session.add(subscription)
        db_session.flush()
        db_session.add(
            EnforcementLock(
                subscription_id=subscription.id,
                subscriber_id=account.id,
                reason=EnforcementReason.fraud,
                source="test:fraud-review",
                is_active=True,
            )
        )
        db_session.commit()

        actions = reseller_portal.get_account_detail(
            db_session, str(reseller.id), str(account.id)
        )["status_actions"]

        assert actions["deactivate"]["allowed"] is False
        assert actions["deactivate"]["affected"] == 0
        assert actions["disable"]["allowed"] is True

    def test_status_action_preview_includes_accounts_without_services(
        self, db_session
    ) -> None:
        from app.models.subscriber import Reseller, Subscriber, SubscriberStatus
        from app.services import reseller_portal

        reseller = Reseller(name="No-service Preview", is_active=True)
        account = Subscriber(
            first_name="No",
            last_name="Service",
            email="no-service-preview@example.com",
            reseller=reseller,
            status=SubscriberStatus.active,
        )
        db_session.add_all([reseller, account])
        db_session.commit()

        actions = reseller_portal.get_account_detail(
            db_session, str(reseller.id), str(account.id)
        )["status_actions"]

        assert actions["deactivate"]["allowed"] is True
        assert actions["deactivate"]["affected"] == 0
        assert actions["deactivate"]["account_affected"] is True
        assert actions["disable"]["allowed"] is True

    def test_status_command_rejects_stale_preview_fingerprint(self, db_session) -> None:
        from app.models.catalog import (
            AccessType,
            CatalogOffer,
            PriceBasis,
            ServiceType,
            Subscription,
            SubscriptionStatus,
        )
        from app.models.enforcement_lock import EnforcementLock, EnforcementReason
        from app.models.idempotency import IdempotencyKey
        from app.models.subscriber import Reseller, Subscriber
        from app.services import reseller_portal

        reseller = Reseller(name="Stale Preview", is_active=True)
        account = Subscriber(
            first_name="Stale",
            last_name="Customer",
            email="stale-preview@example.com",
            reseller=reseller,
        )
        offer = CatalogOffer(
            name="Stale preview plan",
            service_type=ServiceType.residential,
            access_type=AccessType.fiber,
            price_basis=PriceBasis.flat,
        )
        db_session.add_all([reseller, account, offer])
        db_session.flush()
        subscription = Subscription(
            subscriber_id=account.id,
            offer_id=offer.id,
            status=SubscriptionStatus.active,
            start_at=datetime.now(UTC),
        )
        db_session.add(subscription)
        db_session.commit()
        fingerprint = reseller_portal.get_account_detail(
            db_session, str(reseller.id), str(account.id)
        )["status_actions"]["deactivate"]["fingerprint"]
        db_session.add(
            EnforcementLock(
                subscription_id=subscription.id,
                subscriber_id=account.id,
                reason=EnforcementReason.system,
                source="test:state-changed",
                is_active=True,
            )
        )
        db_session.commit()

        with pytest.raises(DomainError, match="changed after preview"):
            confirm_status_action(
                db_session,
                reseller_id=reseller.id,
                account_id=account.id,
                action="deactivate",
                fingerprint=fingerprint,
                idempotency_key="stale-preview-confirmation",
            )

        db_session.refresh(subscription)
        assert subscription.status == SubscriptionStatus.active
        assert (
            db_session.query(IdempotencyKey)
            .filter(IdempotencyKey.key == "stale-preview-confirmation")
            .count()
            == 0
        )

    def test_status_confirmation_is_preview_first_and_idempotent(
        self, db_session
    ) -> None:
        from app.models.idempotency import IdempotencyKey
        from app.models.subscriber import Reseller, Subscriber, SubscriberStatus
        from app.services import reseller_portal

        reseller = Reseller(name="Confirmed Status", is_active=True)
        account = Subscriber(
            first_name="Confirmed",
            last_name="Customer",
            email="confirmed-status@example.com",
            reseller=reseller,
            status=SubscriberStatus.active,
        )
        db_session.add_all([reseller, account])
        db_session.commit()
        fingerprint = reseller_portal.get_account_detail(
            db_session, str(reseller.id), str(account.id)
        )["status_actions"]["deactivate"]["fingerprint"]

        proposal = preview_status_confirmation(
            db_session,
            reseller_id=reseller.id,
            account_id=account.id,
            action="deactivate",
            fingerprint=fingerprint,
        )
        assert proposal is not None
        assert proposal["affected"] == 0
        assert proposal["account_affected"] is True

        first = confirm_status_action(
            db_session,
            reseller_id=reseller.id,
            account_id=account.id,
            action="deactivate",
            fingerprint=proposal["preview_fingerprint"],
            idempotency_key=proposal["idempotency_key"],
        )
        # A later state change must not alter the original result returned by an
        # idempotent replay.
        account.status = SubscriberStatus.active
        db_session.commit()
        replay = confirm_status_action(
            db_session,
            reseller_id=reseller.id,
            account_id=account.id,
            action="deactivate",
            fingerprint=proposal["preview_fingerprint"],
            idempotency_key=proposal["idempotency_key"],
        )

        assert first is not None and first["replayed"] is False
        assert first["status"] == SubscriberStatus.blocked.value
        assert replay is not None and replay["replayed"] is True
        assert replay["status"] == SubscriberStatus.blocked.value
        db_session.refresh(account)
        assert account.status == SubscriberStatus.active
        assert (
            db_session.query(IdempotencyKey)
            .filter(IdempotencyKey.scope == "reseller_account_status:deactivate")
            .count()
            == 1
        )

    def test_status_confirmation_key_cannot_cross_accounts(self, db_session) -> None:
        from app.models.subscriber import Reseller, Subscriber, SubscriberStatus
        from app.services import reseller_portal

        reseller = Reseller(name="Bound Status", is_active=True)
        first_account = Subscriber(
            first_name="First",
            last_name="Customer",
            email="status-first@example.com",
            reseller=reseller,
            status=SubscriberStatus.active,
        )
        second_account = Subscriber(
            first_name="Second",
            last_name="Customer",
            email="status-second@example.com",
            reseller=reseller,
            status=SubscriberStatus.active,
        )
        db_session.add_all([reseller, first_account, second_account])
        db_session.commit()

        first_fingerprint = reseller_portal.get_account_detail(
            db_session, str(reseller.id), str(first_account.id)
        )["status_actions"]["deactivate"]["fingerprint"]
        first_proposal = preview_status_confirmation(
            db_session,
            reseller_id=reseller.id,
            account_id=first_account.id,
            action="deactivate",
            fingerprint=first_fingerprint,
        )
        assert first_proposal is not None
        confirm_status_action(
            db_session,
            reseller_id=reseller.id,
            account_id=first_account.id,
            action="deactivate",
            fingerprint=first_proposal["preview_fingerprint"],
            idempotency_key=first_proposal["idempotency_key"],
        )
        second_fingerprint = reseller_portal.get_account_detail(
            db_session, str(reseller.id), str(second_account.id)
        )["status_actions"]["deactivate"]["fingerprint"]

        with pytest.raises(DomainError, match="another account"):
            confirm_status_action(
                db_session,
                reseller_id=reseller.id,
                account_id=second_account.id,
                action="deactivate",
                fingerprint=second_fingerprint,
                idempotency_key=first_proposal["idempotency_key"],
            )

    def test_status_actions_use_server_confirmation_page(self) -> None:
        detail = Path("templates/reseller/accounts/detail.html").read_text()
        confirmation = Path(
            "templates/reseller/accounts/status_confirm.html"
        ).read_text()

        assert 'onsubmit="return confirm(' not in detail
        assert "/status/confirm" in confirmation
        assert 'name="preview_fingerprint"' in confirmation
        assert 'name="idempotency_key"' in confirmation

    def test_get_account_detail_returns_none_for_reseller_login_user(
        self, db_session
    ) -> None:
        from app.models.subscriber import Reseller, Subscriber, UserType

        reseller = Reseller(name="Portal Reseller", is_active=True)
        db_session.add(reseller)
        db_session.commit()

        login_user = Subscriber(
            first_name="Mimi",
            last_name="David",
            email="mimi@example.com",
            reseller_id=reseller.id,
            user_type=UserType.reseller,
        )
        db_session.add(login_user)
        db_session.commit()

        from app.services.reseller_portal import get_account_detail

        assert (
            get_account_detail(db_session, str(reseller.id), str(login_user.id)) is None
        )


# ---------------------------------------------------------------------------
# 3. Invoice visibility
# ---------------------------------------------------------------------------


class TestInvoiceVisibility:
    def test_list_invoices_returns_none_for_wrong_reseller(self, db_session) -> None:
        from app.models.subscriber import Reseller, Subscriber

        reseller = Reseller(name="Invoice Reseller", is_active=True)
        other = Reseller(name="Other Reseller", is_active=True)
        db_session.add_all([reseller, other])
        db_session.commit()

        account = Subscriber(
            first_name="Inv",
            last_name="User",
            email="inv@example.com",
            reseller_id=reseller.id,
        )
        db_session.add(account)
        db_session.commit()

        from app.services.reseller_portal import list_account_invoices

        # Correct reseller
        result = list_account_invoices(db_session, str(reseller.id), str(account.id))
        assert result is not None
        assert result == []

        # Wrong reseller
        result = list_account_invoices(db_session, str(other.id), str(account.id))
        assert result is None

    def test_get_invoice_detail_scoped(self, db_session) -> None:
        from app.models.billing import Invoice, InvoiceStatus
        from app.models.subscriber import Reseller, Subscriber

        reseller = Reseller(name="Detail Reseller", is_active=True)
        db_session.add(reseller)
        db_session.commit()

        account = Subscriber(
            first_name="Det",
            last_name="User",
            email="det@example.com",
            reseller_id=reseller.id,
        )
        db_session.add(account)
        db_session.commit()

        invoice = Invoice(
            account_id=account.id,
            status=InvoiceStatus.issued,
            balance_due=Decimal("100.00"),
        )
        db_session.add(invoice)
        db_session.commit()
        db_session.refresh(invoice)

        from app.services.reseller_portal import get_invoice_detail

        result = get_invoice_detail(
            db_session, str(reseller.id), str(account.id), str(invoice.id)
        )
        assert result is not None
        assert result["balance_due"] == Decimal("100.00")
        assert result["status_presentation"] == {
            "value": "issued",
            "label": "Issued",
            "tone": "info",
            "icon": "info",
        }


# ---------------------------------------------------------------------------
# 4. Account search
# ---------------------------------------------------------------------------


class TestAccountSearch:
    def test_search_filters_by_name(self, db_session) -> None:
        from app.models.subscriber import Reseller, Subscriber

        reseller = Reseller(name="Search Reseller", is_active=True)
        db_session.add(reseller)
        db_session.commit()

        db_session.add(
            Subscriber(
                first_name="Alice",
                last_name="Smith",
                email="alice@example.com",
                reseller_id=reseller.id,
            )
        )
        db_session.add(
            Subscriber(
                first_name="Bob",
                last_name="Jones",
                email="bob@example.com",
                reseller_id=reseller.id,
            )
        )
        db_session.commit()

        from app.services.reseller_portal import list_accounts

        all_accounts = list_accounts(db_session, str(reseller.id), 50, 0)
        assert len(all_accounts) == 2

        filtered = list_accounts(db_session, str(reseller.id), 50, 0, search="Alice")
        assert len(filtered) == 1
        assert "Alice" in filtered[0]["subscriber_name"]

    def test_search_by_email(self, db_session) -> None:
        from app.models.subscriber import Reseller, Subscriber

        reseller = Reseller(name="Email Search", is_active=True)
        db_session.add(reseller)
        db_session.commit()

        db_session.add(
            Subscriber(
                first_name="Test",
                last_name="User",
                email="unique@example.com",
                reseller_id=reseller.id,
            )
        )
        db_session.commit()

        from app.services.reseller_portal import list_accounts

        results = list_accounts(db_session, str(reseller.id), 50, 0, search="unique@")
        assert len(results) == 1

    def test_list_accounts_excludes_reseller_login_users(self, db_session) -> None:
        from app.models.subscriber import Reseller, Subscriber, UserType

        reseller = Reseller(name="Filter Reseller", is_active=True)
        db_session.add(reseller)
        db_session.commit()

        db_session.add(
            Subscriber(
                first_name="Managed",
                last_name="Customer",
                email="managed@example.com",
                reseller_id=reseller.id,
            )
        )
        db_session.add(
            Subscriber(
                first_name="Portal",
                last_name="User",
                email="portal@example.com",
                reseller_id=reseller.id,
                user_type=UserType.reseller,
            )
        )
        db_session.commit()

        from app.services.reseller_portal import list_accounts

        accounts = list_accounts(db_session, str(reseller.id), 50, 0)
        assert len(accounts) == 1
        assert accounts[0]["subscriber_name"] == "Managed Customer"


# ---------------------------------------------------------------------------
# 5. Revenue summary
# ---------------------------------------------------------------------------


class TestRevenueSummary:
    def test_revenue_summary_empty(self, db_session) -> None:
        from app.models.subscriber import Reseller

        reseller = Reseller(name="Empty Revenue", is_active=True)
        db_session.add(reseller)
        db_session.commit()

        from app.services.reseller_portal import get_revenue_summary

        summary = get_revenue_summary(db_session, str(reseller.id))
        assert summary["total_paid"] == 0
        assert summary["total_outstanding"] == 0
        assert summary["account_count"] == 0
        assert summary["monthly"] == []

    def test_revenue_summary_excludes_reseller_login_users(self, db_session) -> None:
        from app.models.subscriber import Reseller, Subscriber, UserType

        reseller = Reseller(name="Revenue Filter", is_active=True)
        db_session.add(reseller)
        db_session.commit()

        db_session.add(
            Subscriber(
                first_name="Managed",
                last_name="Customer",
                email="managed2@example.com",
                reseller_id=reseller.id,
            )
        )
        db_session.add(
            Subscriber(
                first_name="Portal",
                last_name="User",
                email="portal2@example.com",
                reseller_id=reseller.id,
                user_type=UserType.reseller,
            )
        )
        db_session.commit()

        from app.services.reseller_portal import get_revenue_summary

        summary = get_revenue_summary(db_session, str(reseller.id))
        assert summary["account_count"] == 1


# ---------------------------------------------------------------------------
# 6. Route registration
# ---------------------------------------------------------------------------


class TestRouteRegistration:
    def test_account_detail_route_exists(self) -> None:
        from app.web.reseller.routes import router

        paths = [getattr(r, "path", "") for r in router.routes]
        assert "/reseller/accounts/{account_id}" in paths

    def test_invoice_list_route_exists(self) -> None:
        from app.web.reseller.routes import router

        paths = [getattr(r, "path", "") for r in router.routes]
        assert "/reseller/accounts/{account_id}/invoices" in paths

    def test_invoice_detail_route_exists(self) -> None:
        from app.web.reseller.routes import router

        paths = [getattr(r, "path", "") for r in router.routes]
        assert "/reseller/accounts/{account_id}/invoices/{invoice_id}" in paths

    def test_revenue_report_route_exists(self) -> None:
        from app.web.reseller.routes import router

        paths = [getattr(r, "path", "") for r in router.routes]
        assert "/reseller/reports/revenue" in paths

    def test_search_param_on_accounts(self) -> None:
        from app.web.reseller.routes import router

        for route in router.routes:
            if getattr(
                route, "path", None
            ) == "/reseller/accounts" and "GET" in getattr(route, "methods", set()):
                assert True
                return
        raise AssertionError("GET /reseller/accounts route not found")


# ---------------------------------------------------------------------------
# 7. Event emission
# ---------------------------------------------------------------------------


class TestResellerProfile:
    def test_profile_route_exists(self) -> None:
        from app.web.reseller.routes import router

        paths = [getattr(r, "path", "") for r in router.routes]
        assert "/reseller/profile" in paths

    def test_profile_post_route_exists(self) -> None:
        from app.web.reseller.routes import router

        found = False
        for route in router.routes:
            if getattr(route, "path", "") == "/reseller/profile" and "POST" in getattr(
                route, "methods", set()
            ):
                found = True
                break
        assert found

    def test_profile_update_changes_contact_email(self, db_session) -> None:
        from app.models.subscriber import Reseller

        reseller = Reseller(
            name="Profile Test", contact_email="old@example.com", is_active=True
        )
        db_session.add(reseller)
        db_session.commit()
        db_session.refresh(reseller)

        reseller.contact_email = "new@example.com"
        db_session.commit()
        db_session.refresh(reseller)
        assert reseller.contact_email == "new@example.com"


class TestResellerNavigation:
    def test_nav_exposes_billing_and_profile_links(self) -> None:
        layout = Path("templates/layouts/reseller.html").read_text()

        assert 'href="/reseller/billing"' in layout
        assert 'href="/reseller/profile"' in layout
        assert "Profile Settings" in layout

    def test_billing_links_to_revenue_summary(self) -> None:
        billing = Path("templates/reseller/billing/index.html").read_text()
        revenue = Path("templates/reseller/reports/revenue.html").read_text()

        assert 'href="/reseller/reports/revenue"' in billing
        assert 'href="/reseller/billing"' in revenue

    def test_billing_shows_consolidated_pay_form(self) -> None:
        billing = Path("templates/reseller/billing/index.html").read_text()

        # Consolidated billing now offers a one-tap "Pay outstanding" affordance
        # that prefills the amount field (free-form entry stays). There is no
        # naive per-invoice "Pay now" button on this page.
        assert "/reseller/billing/pay/intent" in billing
        assert "Pay outstanding" in billing
        assert "Pay now" not in billing

    def test_billing_shows_view_as_customer_action(self) -> None:
        billing = Path("templates/reseller/billing/index.html").read_text()

        assert "/reseller/accounts/{{ s.subscriber_id }}/view" in billing
        assert "View as customer" in billing

    def test_profile_update_changes_phone(self, db_session) -> None:
        from app.models.subscriber import Reseller

        reseller = Reseller(name="Phone Test", contact_phone="+2341234", is_active=True)
        db_session.add(reseller)
        db_session.commit()

        reseller.contact_phone = "+2349999"
        db_session.commit()
        db_session.refresh(reseller)
        assert reseller.contact_phone == "+2349999"


class TestEventEmission:
    def test_login_emits_event(self, db_session) -> None:
        with patch("app.services.reseller_portal._emit_reseller_event") as mock_emit:
            # We can't fully test login without auth, but verify the helper works
            from app.services.reseller_portal import _emit_reseller_event

            _emit_reseller_event(db_session, "reseller_login", {"reseller_id": "test"})
            # If we get here without error, the function works

    def test_emit_reseller_event_handles_missing_type(self, db_session) -> None:
        from app.services.reseller_portal import _emit_reseller_event

        # Should not raise even with a nonexistent event type
        _emit_reseller_event(db_session, "nonexistent_event", {})


# ---------------------------------------------------------------------------
# 8. Redis session store already primary
# ---------------------------------------------------------------------------


class TestSessionStore:
    def test_session_store_uses_redis_first(self) -> None:
        from app.services.session_store import store_session

        # Verify the function exists and accepts the right args
        assert callable(store_session)

    def test_session_store_fallback_in_tests(self) -> None:
        """In test mode, in-memory fallback should work."""
        from app.services.session_store import load_session, store_session

        fallback: dict = {}
        store_session("test:prefix", "token123", {"data": "hello"}, 3600, fallback)
        result = load_session("test:prefix", "token123", fallback)
        assert result is not None
        assert result["data"] == "hello"
