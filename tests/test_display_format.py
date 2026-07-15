"""Tests for the canonical money + timestamp display helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from app.models.subscription_engine import SettingValueType
from app.schemas.settings import DomainSettingUpdate
from app.services import display_format
from app.services.domain_settings import billing_settings, scheduler_settings


def _set_currency(db, code: str) -> None:
    billing_settings.upsert_by_key(
        db,
        "default_currency",
        DomainSettingUpdate(value_type=SettingValueType.string, value_text=code),
    )


def _set_timezone(db, tz: str) -> None:
    scheduler_settings.upsert_by_key(
        db,
        "timezone",
        DomainSettingUpdate(value_type=SettingValueType.string, value_text=tz),
    )


class TestDefaultCurrency:
    def test_fallback_is_ngn(self, db_session):
        assert display_format.default_currency(db_session) == "NGN"

    def test_setting_override(self, db_session):
        _set_currency(db_session, "usd")
        assert display_format.default_currency(db_session) == "USD"


class TestCurrencySymbol:
    def test_known_symbols(self):
        assert display_format.currency_symbol("NGN") == "₦"
        assert display_format.currency_symbol("USD") == "$"
        assert display_format.currency_symbol("EUR") == "€"
        assert display_format.currency_symbol("GBP") == "£"
        assert display_format.currency_symbol("KES") == "KSh"
        assert display_format.currency_symbol("GHS") == "₵"
        assert display_format.currency_symbol("ZAR") == "R"

    def test_unknown_returns_code(self):
        assert display_format.currency_symbol("JPY") == "JPY"

    def test_symbol_for_resolves_setting(self, db_session):
        _set_currency(db_session, "EUR")
        assert display_format.currency_symbol_for(db_session) == "€"


class TestCurrencyCode:
    def test_normalizes_case_and_whitespace(self):
        assert display_format.currency_code(" usd ") == "USD"

    def test_missing_value_uses_declared_fallback(self):
        assert display_format.currency_code(None, fallback="kes") == "KES"


class TestFormatMoney:
    def test_normal(self):
        assert display_format.format_money(1234.5) == "₦1,234.50"

    def test_none_amount(self):
        assert display_format.format_money(None) == "—"

    def test_invalid_amount(self):
        assert display_format.format_money("not-a-number") == "—"

    def test_non_finite_amount(self):
        assert display_format.format_money(Decimal("NaN")) == "—"

    def test_missing_marker_can_be_selected_by_projection(self):
        assert display_format.format_money(None, missing="Unavailable") == "Unavailable"

    def test_explicit_currency_wins_over_db(self, db_session):
        _set_currency(db_session, "NGN")
        assert (
            display_format.format_money(10, db=db_session, currency="USD") == "$10.00"
        )

    def test_db_resolved_symbol(self, db_session):
        _set_currency(db_session, "GBP")
        assert display_format.format_money(99.9, db=db_session) == "£99.90"


class TestFinanceSummaryFormatting:
    def test_amount_uses_explicit_iso_code(self):
        assert (
            display_format.format_currency_amount(Decimal("1250"), "ngn")
            == "NGN 1,250.00"
        )

    def test_groups_are_normalized_combined_and_sorted(self):
        amounts = {
            "usd": Decimal("25"),
            " NGN ": Decimal("100"),
            "ngn": Decimal("50"),
        }

        assert display_format.format_currency_groups(amounts) == "NGN 150.00, USD 25.00"

    def test_empty_group_uses_explicit_aggregate_currency(self):
        assert (
            display_format.format_currency_groups({}, empty_currency="kes")
            == "KES 0.00"
        )


class TestDisplayTimezone:
    def test_fallback_is_lagos(self, db_session):
        tz = display_format.display_timezone(db_session)
        assert str(tz) == "Africa/Lagos"

    def test_setting_override(self, db_session):
        _set_timezone(db_session, "UTC")
        assert str(display_format.display_timezone(db_session)) == "UTC"


class TestFormatTimestamp:
    def test_naive_utc_to_wat_with_label(self, db_session):
        # Naive datetime is treated as UTC; Lagos is UTC+1 (WAT), no DST.
        value = datetime(2026, 7, 3, 10, 0)
        assert (
            display_format.format_timestamp(value, db_session) == "2026-07-03 11:00 WAT"
        )

    def test_aware_datetime(self, db_session):
        value = datetime(2026, 7, 3, 10, 0, tzinfo=UTC)
        assert (
            display_format.format_timestamp(value, db_session) == "2026-07-03 11:00 WAT"
        )

    def test_none_uses_shared_missing_marker(self, db_session):
        assert display_format.format_timestamp(None, db_session) == "—"

    def test_invalid_timestamp_uses_shared_missing_marker(self, db_session):
        assert display_format.format_timestamp("not-a-date", db_session) == "—"


class TestConvergedWrappers:
    def test_wrappers_return_setting_value(self, db_session):
        from app.services import (
            web_billing_accounts,
            web_billing_collection_accounts,
            web_billing_credits,
        )
        from app.web.admin import billing_consolidated

        _set_currency(db_session, "KES")
        assert web_billing_accounts._default_currency(db_session) == "KES"
        assert web_billing_collection_accounts._default_currency(db_session) == "KES"
        assert web_billing_credits._default_currency(db_session) == "KES"
        assert billing_consolidated._default_currency(db_session) == "KES"
