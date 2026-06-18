"""Tests for the cross-domain billing-integrity audit (read-only).

Covers the billing-line invariants (disabled-service, duplicate-period,
add-on-without-billable-parent) and the network/lifecycle invariants
(terminal-holds-IP, duplicate-IPv4, missing-RADIUS). See
docs/POST_CUTOVER_HARDENING.md.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

from app.models.billing import Invoice, InvoiceLine, InvoiceStatus
from app.models.catalog import (
    AccessCredential,
    AddOn,
    AddOnPrice,
    AddOnType,
    PriceType,
    Subscription,
    SubscriptionAddOn,
    SubscriptionStatus,
)
from app.models.network import IPAssignment, IPv4Address, IPVersion
from app.models.subscriber import Subscriber
from app.services import billing_integrity_audit as bia

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _sub(db, offer, *, status=SubscriptionStatus.active, canceled_at=None, login=None):
    s = Subscriber(first_name="I", last_name="A", email=f"{id(object())}@e.com")
    db.add(s)
    db.flush()
    sub = Subscription(
        subscriber_id=s.id,
        offer_id=offer.id,
        status=status,
        canceled_at=canceled_at,
        login=login,
    )
    db.add(sub)
    db.flush()
    return s, sub


def _invoice_line(
    db, subscriber, sub, *, period_start, period_end, description, amount="100.00"
):
    inv = Invoice(
        account_id=subscriber.id,
        status=InvoiceStatus.issued,
        currency="NGN",
        billing_period_start=period_start,
        billing_period_end=period_end,
        is_active=True,
    )
    db.add(inv)
    db.flush()
    line = InvoiceLine(
        invoice_id=inv.id,
        subscription_id=sub.id,
        description=description,
        amount=Decimal(amount),
        is_active=True,
    )
    db.add(line)
    db.flush()
    return inv, line


def _recurring_addon(db, sub, *, end_at=None, price_type=PriceType.recurring):
    addon = AddOn(name="Extra IP", addon_type=AddOnType.extra_ip)
    db.add(addon)
    db.flush()
    db.add(
        AddOnPrice(
            add_on_id=addon.id,
            price_type=price_type,
            amount=Decimal("500.00"),
            currency="NGN",
            is_active=True,
        )
    )
    db.add(
        SubscriptionAddOn(
            subscription_id=sub.id, add_on_id=addon.id, quantity=1, end_at=end_at
        )
    )
    db.flush()
    return addon


def _ip_assignment(db, subscriber, ip, *, active=True):
    addr = IPv4Address(address=ip)
    db.add(addr)
    db.flush()
    db.add(
        IPAssignment(
            subscriber_id=subscriber.id,
            ip_version=IPVersion.ipv4,
            ipv4_address_id=addr.id,
            is_active=active,
        )
    )
    db.flush()


class TestBillingLineInvariants:
    def test_disabled_service_line_flagged(self, db_session, catalog_offer):
        sub_owner, sub = _sub(
            db_session,
            catalog_offer,
            status=SubscriptionStatus.canceled,
            canceled_at=T0,
        )
        # period STARTS a month after cancellation → billed-for-dead
        _invoice_line(
            db_session,
            sub_owner,
            sub,
            period_start=T0 + timedelta(days=30),
            period_end=T0 + timedelta(days=60),
            description="Plan (2026-01-31 - 2026-03-01)",
        )
        db_session.commit()
        assert bia.check_billing_disabled_service_lines(db_session)["count"] == 1

    def test_active_service_line_not_flagged(self, db_session, catalog_offer):
        sub_owner, sub = _sub(db_session, catalog_offer)  # active
        _invoice_line(
            db_session,
            sub_owner,
            sub,
            period_start=T0,
            period_end=T0 + timedelta(days=30),
            description="Plan (2026-01-01 - 2026-01-31)",
        )
        db_session.commit()
        assert bia.check_billing_disabled_service_lines(db_session)["count"] == 0

    def test_duplicate_period_line_flagged(self, db_session, catalog_offer):
        sub_owner, sub = _sub(db_session, catalog_offer)
        for _ in range(2):  # same sub + period + description twice
            _invoice_line(
                db_session,
                sub_owner,
                sub,
                period_start=T0,
                period_end=T0 + timedelta(days=30),
                description="Plan (2026-01-01 - 2026-01-31)",
            )
        db_session.commit()
        assert (
            bia.check_billing_duplicate_subscription_period_lines(db_session)["count"]
            == 1
        )

    def test_base_plus_addon_not_duplicate(self, db_session, catalog_offer):
        """Base line + add-on line share sub+period but differ in description —
        legitimate, must not be flagged."""
        sub_owner, sub = _sub(db_session, catalog_offer)
        _invoice_line(
            db_session,
            sub_owner,
            sub,
            period_start=T0,
            period_end=T0 + timedelta(days=30),
            description="Plan (2026-01-01 - 2026-01-31)",
        )
        # add-on line on the same invoice period (reuse a second invoice/period)
        _invoice_line(
            db_session,
            sub_owner,
            sub,
            period_start=T0,
            period_end=T0 + timedelta(days=30),
            description="Extra IP (2026-01-01 - 2026-01-31)",
        )
        db_session.commit()
        assert (
            bia.check_billing_duplicate_subscription_period_lines(db_session)["count"]
            == 0
        )

    def test_addon_on_terminal_parent_flagged(self, db_session, catalog_offer):
        _, sub = _sub(db_session, catalog_offer, status=SubscriptionStatus.canceled)
        _recurring_addon(db_session, sub)  # live (end_at None), recurring price
        db_session.commit()
        assert bia.check_billing_addon_without_billable_parent(db_session)["count"] == 1

    def test_addon_on_active_parent_not_flagged(self, db_session, catalog_offer):
        _, sub = _sub(db_session, catalog_offer)  # active
        _recurring_addon(db_session, sub)
        db_session.commit()
        assert bia.check_billing_addon_without_billable_parent(db_session)["count"] == 0

    def test_one_time_addon_on_terminal_parent_not_flagged(
        self, db_session, catalog_offer
    ):
        _, sub = _sub(db_session, catalog_offer, status=SubscriptionStatus.canceled)
        _recurring_addon(db_session, sub, price_type=PriceType.one_time)
        db_session.commit()
        assert bia.check_billing_addon_without_billable_parent(db_session)["count"] == 0


class TestNetworkLifecycleInvariants:
    def test_terminal_holding_ip_flagged(self, db_session, catalog_offer):
        owner, _ = _sub(db_session, catalog_offer, status=SubscriptionStatus.canceled)
        _ip_assignment(db_session, owner, "10.0.0.1")
        db_session.commit()
        assert (
            bia.check_terminal_subscription_active_ip_assignment(db_session)["count"]
            == 1
        )

    def test_active_holding_ip_not_flagged(self, db_session, catalog_offer):
        owner, _ = _sub(db_session, catalog_offer)  # active
        _ip_assignment(db_session, owner, "10.0.0.2")
        db_session.commit()
        assert (
            bia.check_terminal_subscription_active_ip_assignment(db_session)["count"]
            == 0
        )

    def test_duplicate_active_ipv4_flagged(self, db_session, catalog_offer):
        owner, _ = _sub(db_session, catalog_offer)
        _ip_assignment(db_session, owner, "10.0.0.3")
        _ip_assignment(db_session, owner, "10.0.0.4")
        db_session.commit()
        assert bia.check_duplicate_active_ipv4_assignment(db_session)["count"] == 1

    def test_missing_radius_flagged(self, db_session, catalog_offer):
        _sub(db_session, catalog_offer, login="u1")  # active, login u1
        db_session.commit()
        with patch.object(bia, "_external_ip_state", return_value=({}, set(), 0)):
            assert (
                bia.check_active_subscription_missing_radius(db_session)["count"] == 1
            )

    def test_missing_radius_provisioned_ok(self, db_session, catalog_offer):
        _sub(db_session, catalog_offer, login="u2")
        db_session.commit()
        with patch.object(bia, "_external_ip_state", return_value=({}, {"u2"}, 0)):
            assert (
                bia.check_active_subscription_missing_radius(db_session)["count"] == 0
            )


class TestUnusableRadiusPassword:
    def _cred(self, db, subscriber, login, *, secret_hash, active=True):
        db.add(
            AccessCredential(
                subscriber_id=subscriber.id,
                username=login,
                secret_hash=secret_hash,
                is_active=active,
            )
        )
        db.flush()

    def test_usable_password_not_flagged(self, db_session, catalog_offer):
        s, _ = _sub(db_session, catalog_offer, login="ok1")
        self._cred(db_session, s, "ok1", secret_hash="$6$salt$abcdef")  # crypt
        db_session.commit()
        assert (
            bia.check_active_subscription_with_unusable_radius_password(db_session)[
                "count"
            ]
            == 0
        )

    def test_empty_secret_flagged(self, db_session, catalog_offer):
        s, _ = _sub(db_session, catalog_offer, login="bad1")
        self._cred(db_session, s, "bad1", secret_hash="")
        db_session.commit()
        res = bia.check_active_subscription_with_unusable_radius_password(db_session)
        assert res["count"] == 1
        assert res["samples"] == ["bad1"]

    def test_inactive_credential_flagged(self, db_session, catalog_offer):
        s, _ = _sub(db_session, catalog_offer, login="bad2")
        self._cred(db_session, s, "bad2", secret_hash="$6$salt$x", active=False)
        db_session.commit()
        assert (
            bia.check_active_subscription_with_unusable_radius_password(db_session)[
                "count"
            ]
            == 1
        )

    def test_no_credential_flagged(self, db_session, catalog_offer):
        _sub(db_session, catalog_offer, login="bad3")  # active sub, no credential
        db_session.commit()
        assert (
            bia.check_active_subscription_with_unusable_radius_password(db_session)[
                "count"
            ]
            == 1
        )


class TestAggregation:
    def test_launch_blocked_when_billing_invariant_trips(
        self, db_session, catalog_offer
    ):
        owner, sub = _sub(
            db_session,
            catalog_offer,
            status=SubscriptionStatus.canceled,
            canceled_at=T0,
        )
        _invoice_line(
            db_session,
            owner,
            sub,
            period_start=T0 + timedelta(days=30),
            period_end=T0 + timedelta(days=60),
            description="Plan (post-cancel)",
        )
        db_session.commit()
        with (
            patch.object(
                bia,
                "audit_ip_consistency",
                return_value={"counts": {}, "errors": 0},
            ),
            patch.object(bia, "_external_ip_state", return_value=({}, set(), 0)),
        ):
            result = bia.audit_billing_integrity(db_session)
        assert result["counts"]["billing_disabled_service_lines"] == 1
        assert result["launch_blocked"] is True
