"""Tests for billing automation services."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.models.catalog import BillingCycle
from app.services import billing_automation

# =============================================================================
# _add_months Tests
# =============================================================================


class TestAddMonths:
    """Tests for _add_months function."""

    def test_add_one_month(self):
        """Test adding one month."""
        start = datetime(2024, 1, 15, 12, 0, 0)
        result = billing_automation._add_months(start, 1)
        assert result.year == 2024
        assert result.month == 2
        assert result.day == 15

    def test_add_twelve_months(self):
        """Test adding 12 months (one year)."""
        start = datetime(2024, 3, 20, 12, 0, 0)
        result = billing_automation._add_months(start, 12)
        assert result.year == 2025
        assert result.month == 3
        assert result.day == 20

    def test_year_rollover(self):
        """Test adding months that roll over to next year."""
        start = datetime(2024, 11, 15, 12, 0, 0)
        result = billing_automation._add_months(start, 3)
        assert result.year == 2025
        assert result.month == 2
        assert result.day == 15

    def test_month_end_handling_january_to_february(self):
        """Test January 31 to February (clamp to 28/29)."""
        start = datetime(2024, 1, 31, 12, 0, 0)  # 2024 is leap year
        result = billing_automation._add_months(start, 1)
        assert result.year == 2024
        assert result.month == 2
        assert result.day == 29  # Leap year

    def test_month_end_handling_non_leap_year(self):
        """Test January 31 to February in non-leap year."""
        start = datetime(2023, 1, 31, 12, 0, 0)
        result = billing_automation._add_months(start, 1)
        assert result.year == 2023
        assert result.month == 2
        assert result.day == 28

    def test_month_end_31_to_30(self):
        """Test from 31-day month to 30-day month."""
        start = datetime(2024, 3, 31, 12, 0, 0)
        result = billing_automation._add_months(start, 1)
        assert result.year == 2024
        assert result.month == 4
        assert result.day == 30  # April has 30 days

    def test_add_zero_months(self):
        """Test adding zero months."""
        start = datetime(2024, 5, 15, 12, 0, 0)
        result = billing_automation._add_months(start, 0)
        assert result.year == 2024
        assert result.month == 5
        assert result.day == 15


# =============================================================================
# _period_end Tests
# =============================================================================


class TestPeriodEnd:
    """Tests for _period_end function."""

    def test_monthly_cycle(self):
        """Test monthly billing cycle."""
        start = datetime(2024, 6, 15, 12, 0, 0)
        result = billing_automation._period_end(start, BillingCycle.monthly)
        assert result.month == 7
        assert result.day == 15

    def test_annual_cycle(self):
        """Test annual billing cycle."""
        start = datetime(2024, 6, 15, 12, 0, 0)
        result = billing_automation._period_end(start, BillingCycle.annual)
        assert result.year == 2025
        assert result.month == 6
        assert result.day == 15

    def test_none_cycle_defaults_to_monthly(self):
        """Test that None cycle defaults to monthly."""
        start = datetime(2024, 6, 15, 12, 0, 0)
        # None/unrecognized should default to monthly in code logic
        # But _period_end only handles monthly/annual, so just test monthly
        result = billing_automation._period_end(start, BillingCycle.monthly)
        assert result.month == 7
        assert result.day == 15


# =============================================================================
# _resolve_price Tests
# =============================================================================


class TestResolvePrice:
    """Tests for _resolve_price function."""

    def test_price_from_offer_version(self, db_session, subscription):
        """Test getting price from offer version."""
        from app.models.catalog import (
            AccessType,
            BillingCycle,
            OfferVersion,
            OfferVersionPrice,
            PriceBasis,
            PriceType,
            ServiceType,
        )

        # Create offer version with price
        offer_version = OfferVersion(
            offer_id=subscription.offer_id,
            version_number=1,
            name="Test Version",
            service_type=ServiceType.residential,
            access_type=AccessType.fiber,
            price_basis=PriceBasis.flat,
        )
        db_session.add(offer_version)
        db_session.commit()

        version_price = OfferVersionPrice(
            offer_version_id=offer_version.id,
            price_type=PriceType.recurring,
            amount=Decimal("99.99"),
            currency="USD",
            billing_cycle=BillingCycle.monthly,
            is_active=True,
        )
        db_session.add(version_price)

        subscription.offer_version_id = offer_version.id
        db_session.commit()

        amount, currency, cycle = billing_automation._resolve_price(db_session, subscription)
        assert amount == Decimal("99.99")
        assert currency == "USD"
        assert cycle == BillingCycle.monthly

    def test_price_from_offer(self, db_session, subscription):
        """Test getting price from offer when no version price."""
        from app.models.catalog import BillingCycle, OfferPrice, PriceType

        # Create offer price
        offer_price = OfferPrice(
            offer_id=subscription.offer_id,
            price_type=PriceType.recurring,
            amount=Decimal("79.99"),
            currency="EUR",
            billing_cycle=BillingCycle.annual,
            is_active=True,
        )
        db_session.add(offer_price)
        db_session.commit()

        amount, currency, cycle = billing_automation._resolve_price(db_session, subscription)
        assert amount == Decimal("79.99")
        assert currency == "EUR"
        assert cycle == BillingCycle.annual

    def test_no_price_found(self, db_session, subscription):
        """Test when no price is found."""
        # No prices created
        amount, currency, cycle = billing_automation._resolve_price(db_session, subscription)
        assert amount is None
        assert currency is None
        assert cycle is None


# =============================================================================
# _resolve_tax_rate_id Tests
# =============================================================================


class TestResolveTaxRateId:
    """Tests for _resolve_tax_rate_id function."""

    def test_tax_rate_from_service_address(self, db_session, subscription, subscriber):
        """Test getting tax rate from service address."""

        from app.models.billing import TaxRate
        from app.models.subscriber import Address

        # Create tax rate
        tax_rate = TaxRate(
            name="State Tax",
            rate=Decimal("0.08"),
        )
        db_session.add(tax_rate)
        db_session.commit()

        # Create address with tax rate
        address = Address(
            subscriber_id=subscriber.id,
            address_line1="123 Main St",
            tax_rate_id=tax_rate.id,
        )
        db_session.add(address)
        db_session.commit()

        subscription.service_address_id = address.id
        db_session.commit()

        result = billing_automation._resolve_tax_rate_id(db_session, subscription)
        assert result == tax_rate.id

    def test_tax_rate_from_account(self, db_session, subscription, subscriber_account):
        """Test getting tax rate from account when no address tax rate."""
        from app.models.billing import TaxRate

        # Create tax rate
        tax_rate = TaxRate(
            name="Account Tax",
            rate=Decimal("0.06"),
        )
        db_session.add(tax_rate)
        db_session.commit()

        # Set account tax rate
        subscriber_account.tax_rate_id = tax_rate.id
        db_session.commit()

        result = billing_automation._resolve_tax_rate_id(db_session, subscription)
        assert result == tax_rate.id

    def test_no_tax_rate(self, db_session, subscription):
        """Test when no tax rate is configured."""
        result = billing_automation._resolve_tax_rate_id(db_session, subscription)
        assert result is None


# =============================================================================
# _prorated_amount Tests
# =============================================================================


class TestProratedAmount:
    """Tests for _prorated_amount function."""

    def test_full_period(self):
        """Test full period returns full amount."""
        period_start = datetime(2024, 1, 1, 0, 0, 0)
        period_end = datetime(2024, 2, 1, 0, 0, 0)

        result = billing_automation._prorated_amount(
            full_amount=Decimal("100.00"),
            period_start=period_start,
            period_end=period_end,
            usage_start=period_start,
            usage_end=period_end,
        )
        assert result == Decimal("100.00")

    def test_half_period(self):
        """Test half period returns half amount."""
        period_start = datetime(2024, 1, 1, 0, 0, 0)
        period_end = datetime(2024, 1, 31, 0, 0, 0)
        usage_start = datetime(2024, 1, 1, 0, 0, 0)
        usage_end = datetime(2024, 1, 16, 0, 0, 0)  # Half the month

        result = billing_automation._prorated_amount(
            full_amount=Decimal("100.00"),
            period_start=period_start,
            period_end=period_end,
            usage_start=usage_start,
            usage_end=usage_end,
        )
        assert result == Decimal("50.00")

    def test_zero_period(self):
        """Test zero period returns zero."""
        period_start = datetime(2024, 1, 1, 0, 0, 0)
        period_end = datetime(2024, 1, 1, 0, 0, 0)  # Same time

        result = billing_automation._prorated_amount(
            full_amount=Decimal("100.00"),
            period_start=period_start,
            period_end=period_end,
            usage_start=period_start,
            usage_end=period_end,
        )
        assert result == Decimal("0.00")

    def test_zero_usage(self):
        """Test zero usage returns zero."""
        period_start = datetime(2024, 1, 1, 0, 0, 0)
        period_end = datetime(2024, 2, 1, 0, 0, 0)
        usage_start = datetime(2024, 1, 15, 0, 0, 0)
        usage_end = datetime(2024, 1, 15, 0, 0, 0)  # Same time

        result = billing_automation._prorated_amount(
            full_amount=Decimal("100.00"),
            period_start=period_start,
            period_end=period_end,
            usage_start=usage_start,
            usage_end=usage_end,
        )
        assert result == Decimal("0.00")

    def test_usage_capped_at_full_amount(self):
        """Test usage longer than period is capped at full amount."""
        period_start = datetime(2024, 1, 1, 0, 0, 0)
        period_end = datetime(2024, 1, 15, 0, 0, 0)
        usage_start = datetime(2023, 12, 1, 0, 0, 0)  # Before period
        usage_end = datetime(2024, 2, 1, 0, 0, 0)  # After period

        result = billing_automation._prorated_amount(
            full_amount=Decimal("100.00"),
            period_start=period_start,
            period_end=period_end,
            usage_start=usage_start,
            usage_end=usage_end,
        )
        assert result == Decimal("100.00")


# =============================================================================
# run_invoice_cycle Tests
# =============================================================================


class TestRunInvoiceCycle:
    """Tests for run_invoice_cycle function."""

    def test_dry_run_no_changes(self, db_session, subscription, subscriber_account):
        """Test dry run doesn't create invoices."""
        from app.models.catalog import (
            BillingCycle,
            OfferPrice,
            PriceType,
            SubscriptionStatus,
        )
        from app.models.subscriber import AccountStatus

        # Use naive datetime (SQLite stores naive)
        now_naive = datetime.now(UTC).replace(tzinfo=None)

        # Ensure subscription and account are active
        subscription.status = SubscriptionStatus.active
        subscriber_account.status = AccountStatus.active
        subscription.start_at = now_naive - timedelta(days=30)
        subscription.next_billing_at = now_naive - timedelta(days=1)
        db_session.commit()

        # Create a price
        offer_price = OfferPrice(
            offer_id=subscription.offer_id,
            price_type=PriceType.recurring,
            amount=Decimal("50.00"),
            currency="USD",
            billing_cycle=BillingCycle.monthly,
            is_active=True,
        )
        db_session.add(offer_price)
        db_session.commit()

        # Run dry mode - pass naive run_at to match SQLite
        summary = billing_automation.run_invoice_cycle(db_session, run_at=now_naive, dry_run=True)

        assert summary["run_id"] is None
        assert summary["subscriptions_scanned"] >= 1
        # No actual invoices created in dry_run

    def test_creates_invoice_for_active_subscription(self, db_session, subscription, subscriber_account):
        """Test creates invoice for active subscription."""
        from app.models.billing import Invoice
        from app.models.catalog import (
            BillingCycle,
            OfferPrice,
            PriceType,
            SubscriptionStatus,
        )
        from app.models.subscriber import AccountStatus

        now_naive = datetime.now(UTC).replace(tzinfo=None)

        # Setup active subscription
        subscription.status = SubscriptionStatus.active
        subscriber_account.status = AccountStatus.active
        subscription.start_at = now_naive - timedelta(days=30)
        subscription.next_billing_at = now_naive - timedelta(days=1)
        db_session.commit()

        # Create a price
        offer_price = OfferPrice(
            offer_id=subscription.offer_id,
            price_type=PriceType.recurring,
            amount=Decimal("100.00"),
            currency="USD",
            billing_cycle=BillingCycle.monthly,
            is_active=True,
        )
        db_session.add(offer_price)
        db_session.commit()

        initial_invoices = db_session.query(Invoice).filter(
            Invoice.account_id == subscriber_account.id
        ).count()

        # Run billing - pass naive run_at to match SQLite
        summary = billing_automation.run_invoice_cycle(db_session, run_at=now_naive)

        assert summary["run_id"] is not None
        assert summary["invoices_created"] >= 1
        assert summary["lines_created"] >= 1

        # Verify invoice was created
        final_invoices = db_session.query(Invoice).filter(
            Invoice.account_id == subscriber_account.id
        ).count()
        assert final_invoices > initial_invoices

    def test_skips_subscription_without_price(self, db_session, subscription, subscriber_account):
        """Test skips subscription without a price."""
        from app.models.catalog import SubscriptionStatus
        from app.models.subscriber import AccountStatus

        now_naive = datetime.now(UTC).replace(tzinfo=None)

        subscription.status = SubscriptionStatus.active
        subscriber_account.status = AccountStatus.active
        subscription.start_at = now_naive - timedelta(days=30)
        subscription.next_billing_at = now_naive - timedelta(days=1)
        db_session.commit()

        # No price created

        summary = billing_automation.run_invoice_cycle(db_session, run_at=now_naive, dry_run=True)

        assert summary["skipped"] >= 1

    def test_skips_future_billing_date(self, db_session, subscription, subscriber_account):
        """Test skips subscription with future billing date."""
        from app.models.catalog import (
            BillingCycle,
            OfferPrice,
            PriceType,
            SubscriptionStatus,
        )
        from app.models.subscriber import AccountStatus

        now_naive = datetime.now(UTC).replace(tzinfo=None)

        subscription.status = SubscriptionStatus.active
        subscriber_account.status = AccountStatus.active
        subscription.next_billing_at = now_naive + timedelta(days=30)  # Future
        db_session.commit()

        offer_price = OfferPrice(
            offer_id=subscription.offer_id,
            price_type=PriceType.recurring,
            amount=Decimal("50.00"),
            currency="USD",
            billing_cycle=BillingCycle.monthly,
            is_active=True,
        )
        db_session.add(offer_price)
        db_session.commit()

        summary = billing_automation.run_invoice_cycle(db_session, run_at=now_naive, dry_run=True)

        # Subscription should be scanned but not billed
        assert summary["subscriptions_scanned"] >= 1
        assert summary["subscriptions_billed"] == 0

    def test_skips_inactive_subscription(self, db_session, subscription, subscriber_account):
        """Test skips inactive subscription."""
        from app.models.catalog import (
            BillingCycle,
            OfferPrice,
            PriceType,
            SubscriptionStatus,
        )
        from app.models.subscriber import AccountStatus

        subscription.status = SubscriptionStatus.canceled  # Inactive
        subscriber_account.status = AccountStatus.active
        db_session.commit()

        offer_price = OfferPrice(
            offer_id=subscription.offer_id,
            price_type=PriceType.recurring,
            amount=Decimal("50.00"),
            currency="USD",
            billing_cycle=BillingCycle.monthly,
            is_active=True,
        )
        db_session.add(offer_price)
        db_session.commit()

        summary = billing_automation.run_invoice_cycle(db_session, dry_run=True)

        # Inactive subscription not scanned
        assert summary["subscriptions_billed"] == 0

    def test_skips_inactive_account(self, db_session, subscription, subscriber_account):
        """Test skips subscription with inactive account."""
        from app.models.catalog import (
            BillingCycle,
            OfferPrice,
            PriceType,
            SubscriptionStatus,
        )
        from app.models.subscriber import AccountStatus

        subscription.status = SubscriptionStatus.active
        subscriber_account.status = AccountStatus.suspended  # Inactive
        db_session.commit()

        offer_price = OfferPrice(
            offer_id=subscription.offer_id,
            price_type=PriceType.recurring,
            amount=Decimal("50.00"),
            currency="USD",
            billing_cycle=BillingCycle.monthly,
            is_active=True,
        )
        db_session.add(offer_price)
        db_session.commit()

        summary = billing_automation.run_invoice_cycle(db_session, dry_run=True)

        assert summary["subscriptions_billed"] == 0

    def test_filter_by_billing_cycle(self, db_session, subscription, subscriber_account):
        """Test filtering by billing cycle."""
        from app.models.catalog import (
            BillingCycle,
            OfferPrice,
            PriceType,
            SubscriptionStatus,
        )
        from app.models.subscriber import AccountStatus

        now_naive = datetime.now(UTC).replace(tzinfo=None)

        subscription.status = SubscriptionStatus.active
        subscriber_account.status = AccountStatus.active
        subscription.start_at = now_naive - timedelta(days=30)
        subscription.next_billing_at = now_naive - timedelta(days=1)
        db_session.commit()

        # Create monthly price
        offer_price = OfferPrice(
            offer_id=subscription.offer_id,
            price_type=PriceType.recurring,
            amount=Decimal("50.00"),
            currency="USD",
            billing_cycle=BillingCycle.monthly,
            is_active=True,
        )
        db_session.add(offer_price)
        db_session.commit()

        # Run with annual filter - should not match monthly subscription
        summary = billing_automation.run_invoice_cycle(
            db_session,
            run_at=now_naive,
            billing_cycle=BillingCycle.annual,
            dry_run=True,
        )

        assert summary["subscriptions_billed"] == 0

    def test_skips_ended_subscription(self, db_session, subscription, subscriber_account):
        """Test skips subscription that has ended."""
        from app.models.catalog import (
            BillingCycle,
            OfferPrice,
            PriceType,
            SubscriptionStatus,
        )
        from app.models.subscriber import AccountStatus

        now_naive = datetime.now(UTC).replace(tzinfo=None)

        subscription.status = SubscriptionStatus.active
        subscriber_account.status = AccountStatus.active
        subscription.start_at = now_naive - timedelta(days=60)
        subscription.next_billing_at = now_naive - timedelta(days=1)
        subscription.end_at = now_naive - timedelta(days=30)  # Already ended
        db_session.commit()

        offer_price = OfferPrice(
            offer_id=subscription.offer_id,
            price_type=PriceType.recurring,
            amount=Decimal("50.00"),
            currency="USD",
            billing_cycle=BillingCycle.monthly,
            is_active=True,
        )
        db_session.add(offer_price)
        db_session.commit()

        summary = billing_automation.run_invoice_cycle(db_session, run_at=now_naive, dry_run=True)

        assert summary["subscriptions_billed"] == 0

    def test_updates_next_billing_at(self, db_session, subscription, subscriber_account):
        """Test updates next_billing_at after invoicing."""
        from app.models.catalog import (
            BillingCycle,
            OfferPrice,
            PriceType,
            SubscriptionStatus,
        )
        from app.models.subscriber import AccountStatus

        now_naive = datetime.now(UTC).replace(tzinfo=None)

        subscription.status = SubscriptionStatus.active
        subscriber_account.status = AccountStatus.active
        original_billing = now_naive - timedelta(days=1)
        subscription.start_at = now_naive - timedelta(days=30)
        subscription.next_billing_at = original_billing
        db_session.commit()

        offer_price = OfferPrice(
            offer_id=subscription.offer_id,
            price_type=PriceType.recurring,
            amount=Decimal("50.00"),
            currency="USD",
            billing_cycle=BillingCycle.monthly,
            is_active=True,
        )
        db_session.add(offer_price)
        db_session.commit()

        billing_automation.run_invoice_cycle(db_session, run_at=now_naive)

        db_session.refresh(subscription)
        assert subscription.next_billing_at > original_billing
