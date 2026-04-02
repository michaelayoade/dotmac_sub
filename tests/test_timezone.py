from datetime import UTC, datetime
from decimal import Decimal
import json
from types import SimpleNamespace

from app.timezone import (
    APP_TIMEZONE_NAME,
    format_in_app_timezone,
    localize_datetime,
    localize_template_context,
)


def test_localize_datetime_uses_africa_lagos() -> None:
    localized = localize_datetime(datetime(2026, 3, 23, 12, 0, tzinfo=UTC))
    assert localized is not None
    assert localized.tzinfo is not None
    assert localized.tzname() == "WAT"
    assert localized.strftime("%Y-%m-%d %H:%M") == "2026-03-23 13:00"


def test_localize_template_context_wraps_object_attributes() -> None:
    context = localize_template_context(
        {
            "request": object(),
            "ont": SimpleNamespace(
                last_seen_at=datetime(2026, 3, 23, 12, 0, tzinfo=UTC),
                nested={"updated_at": datetime(2026, 3, 23, 14, 30, tzinfo=UTC)},
            ),
        }
    )

    assert context["ont"].last_seen_at.strftime("%Y-%m-%d %H:%M") == "2026-03-23 13:00"
    assert (
        context["ont"].nested["updated_at"].strftime("%Y-%m-%d %H:%M")
        == "2026-03-23 15:30"
    )


def test_format_in_app_timezone_includes_lagos_time() -> None:
    formatted = format_in_app_timezone(
        datetime(2026, 3, 23, 23, 15, tzinfo=UTC),
        "%Y-%m-%d %H:%M",
    )
    assert formatted == "2026-03-24 00:15"
    assert APP_TIMEZONE_NAME == "Africa/Lagos"


def test_localize_template_context_keeps_mappings_json_serializable() -> None:
    context = localize_template_context(
        {
            "request": object(),
            "filter_schema": [
                {
                    "field": "created_at",
                    "label": "Created At",
                    "value": datetime(2026, 3, 23, 12, 0, tzinfo=UTC),
                }
            ],
        }
    )

    encoded = json.dumps(context["filter_schema"], default=str)

    assert '"field": "created_at"' in encoded
    assert "2026-03-23 13:00:00+01:00" in encoded


def test_localize_template_context_keeps_decimals_formatable() -> None:
    context = localize_template_context(
        {
            "request": object(),
            "amount": Decimal("12345.67"),
        }
    )

    assert "{:,.0f}".format(context["amount"]) == "12,346"
