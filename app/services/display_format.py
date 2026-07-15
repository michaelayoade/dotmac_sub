"""Canonical money + timestamp display contracts.

One place for currency resolution, currency-code normalization, currency-symbol
mapping, single-value money formatting, multi-currency summary formatting, and
display-timezone-aware timestamp formatting. Domain services own the underlying
amount, currency, and timestamp facts; this module owns their display projection.

Valid-value behavior is byte-identical to the historical hardcoded values when
the relevant settings are unset: currency ``NGN`` / symbol ``₦`` and display
timezone ``Africa/Lagos`` (``WAT``). Missing or invalid scalar facts render an
em dash rather than silently becoming zero.

Import-safe: ``settings_spec`` is imported lazily inside functions to avoid
circular imports.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

# NGN/USD/EUR/GBP must match app.services.vas_wallet.currency_symbol exactly so
# VAS output is unchanged when it delegates here; the rest extend the map.
_CURRENCY_SYMBOLS: dict[str, str] = {
    "NGN": "₦",
    "USD": "$",
    "EUR": "€",
    "GBP": "£",
    "KES": "KSh",
    "GHS": "₵",
    "ZAR": "R",
}

_DEFAULT_CURRENCY = "NGN"
_DEFAULT_TIMEZONE = "Africa/Lagos"
MISSING_DISPLAY = "—"


def currency_code(
    value: object | None,
    *,
    fallback: str = _DEFAULT_CURRENCY,
) -> str:
    """Return one normalized ISO-style currency code for display grouping."""

    fallback_code = str(fallback or _DEFAULT_CURRENCY).strip().upper()
    fallback_code = fallback_code or _DEFAULT_CURRENCY
    code = str(value or fallback_code).strip().upper()
    return code or fallback_code


def default_currency(db) -> str:
    """Resolve ``billing.default_currency`` (fallback ``NGN``)."""
    from app.models.domain_settings import SettingDomain
    from app.services import settings_spec

    value = settings_spec.resolve_value(db, SettingDomain.billing, "default_currency")
    return currency_code(value)


def currency_symbol(currency: str) -> str:
    """Map a currency code to its display symbol; unknown → the code itself."""
    code = currency_code(currency)
    return _CURRENCY_SYMBOLS.get(code, code)


def currency_symbol_for(db) -> str:
    """Display symbol for the db-resolved default currency."""
    return currency_symbol(default_currency(db))


def format_money(
    amount,
    *,
    db=None,
    currency: str | None = None,
    missing: str = MISSING_DISPLAY,
) -> str:
    """Format ``amount`` as ``{symbol}{value:,.2f}``.

    ``currency`` wins over the db-resolved default; when neither is given the
    NGN symbol is used. A ``None``/invalid/non-finite amount renders the
    explicit ``missing`` marker; only aggregate formatters treat absence as a
    declared zero.
    """
    if currency is not None:
        symbol = currency_symbol(currency)
    elif db is not None:
        symbol = currency_symbol_for(db)
    else:
        symbol = _CURRENCY_SYMBOLS[_DEFAULT_CURRENCY]

    if amount in (None, ""):
        return missing
    try:
        value = Decimal(str(amount))
    except (InvalidOperation, ValueError, TypeError):
        return missing
    if not value.is_finite():
        return missing
    return f"{symbol}{value:,.2f}"


def format_currency_amount(amount: object, currency: object | None) -> str:
    """Format one finance value with an explicit ISO-style currency code.

    Finance worklists use this form instead of a symbol when rows or summaries
    can contain multiple currencies. ``None`` is an explicit aggregate zero;
    callers must not pass unknown domain facts as ``None``.
    """

    code = currency_code(currency)
    value = Decimal(str(amount or 0))
    return f"{code} {value:,.2f}"


def format_currency_groups(
    amounts: Mapping[str, object],
    *,
    empty_currency: str = _DEFAULT_CURRENCY,
) -> str:
    """Format normalized, alphabetically ordered multi-currency totals.

    Values are grouped by normalized currency code and are never summed across
    currencies. An empty aggregate renders an explicit zero in
    ``empty_currency`` (historically NGN unless the caller declares otherwise).
    """

    if not amounts:
        return format_currency_amount(0, empty_currency)
    normalized: dict[str, Decimal] = {}
    for raw_currency, raw_amount in amounts.items():
        code = currency_code(raw_currency, fallback=empty_currency)
        normalized[code] = normalized.get(code, Decimal("0")) + Decimal(
            str(raw_amount or 0)
        )
    return ", ".join(
        format_currency_amount(normalized[code], code) for code in sorted(normalized)
    )


def display_timezone(db) -> ZoneInfo:
    """The canonical app display timezone (``scheduler.timezone``; fallback WAT).

    Celery, the enforcement window guard, and cron schedules all read
    ``scheduler.timezone``; this is the display sibling.
    """
    from app.models.domain_settings import SettingDomain
    from app.services import settings_spec

    value = settings_spec.resolve_value(db, SettingDomain.scheduler, "timezone")
    name = str(value).strip() if value else ""
    try:
        return ZoneInfo(name or _DEFAULT_TIMEZONE)
    except Exception:
        return ZoneInfo(_DEFAULT_TIMEZONE)


def format_timestamp(
    value,
    db,
    *,
    fmt: str = "%Y-%m-%d %H:%M",
    missing: str = MISSING_DISPLAY,
) -> str:
    """Format a datetime in the display timezone with a tz-abbreviation label.

    Naive datetimes are treated as UTC. Missing or invalid values render the
    explicit ``missing`` marker.
    """
    if value is None:
        return missing
    if not isinstance(value, datetime):
        return missing
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    tz = display_timezone(db)
    local = value.astimezone(tz)
    label = local.tzname() or ""
    suffix = f" {label}" if label else ""
    return f"{local.strftime(fmt)}{suffix}"
