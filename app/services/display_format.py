"""Canonical money + timestamp display helpers.

One place for currency resolution, currency-symbol mapping, money formatting,
and display-timezone-aware timestamp formatting. The scattered
``_default_currency`` copies and ``vas_wallet.currency_symbol`` delegate here so
display behavior is defined once.

Default behavior is byte-identical to the historical hardcoded values when the
relevant settings are unset: currency ``NGN`` / symbol ``₦`` and display
timezone ``Africa/Lagos`` (``WAT``).

Import-safe: ``settings_spec`` is imported lazily inside functions to avoid
circular imports.
"""

from __future__ import annotations

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


def default_currency(db) -> str:
    """Resolve ``billing.default_currency`` (fallback ``NGN``)."""
    from app.models.domain_settings import SettingDomain
    from app.services import settings_spec

    value = settings_spec.resolve_value(db, SettingDomain.billing, "default_currency")
    code = str(value or _DEFAULT_CURRENCY).strip().upper()
    return code or _DEFAULT_CURRENCY


def currency_symbol(currency: str) -> str:
    """Map a currency code to its display symbol; unknown → the code itself."""
    code = str(currency or "").strip().upper()
    return _CURRENCY_SYMBOLS.get(code, code or _DEFAULT_CURRENCY)


def currency_symbol_for(db) -> str:
    """Display symbol for the db-resolved default currency."""
    return currency_symbol(default_currency(db))


def format_money(amount, *, db=None, currency: str | None = None) -> str:
    """Format ``amount`` as ``{symbol}{value:,.2f}``.

    ``currency`` wins over the db-resolved default; when neither is given the
    NGN symbol is used. A ``None``/invalid amount renders as ``{symbol}0.00``.
    """
    if currency is not None:
        symbol = currency_symbol(currency)
    elif db is not None:
        symbol = currency_symbol_for(db)
    else:
        symbol = _CURRENCY_SYMBOLS[_DEFAULT_CURRENCY]

    try:
        value = Decimal(str(amount)) if amount not in (None, "") else Decimal("0")
    except (InvalidOperation, ValueError, TypeError):
        value = Decimal("0")
    return f"{symbol}{value:,.2f}"


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


def format_timestamp(value, db, *, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """Format a datetime in the display timezone with a tz-abbreviation label.

    Naive datetimes are treated as UTC. ``None`` renders as an empty string.
    """
    if value is None:
        return ""
    if not isinstance(value, datetime):
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    tz = display_timezone(db)
    local = value.astimezone(tz)
    label = local.tzname() or ""
    suffix = f" {label}" if label else ""
    return f"{local.strftime(fmt)}{suffix}"
