from __future__ import annotations

from pathlib import Path

from app.services.brand_theme import (
    CATEGORICAL_COLOR_ROLES,
    COLOR_SCALE_STEPS,
    DEFAULT_SEMANTIC_COLORS,
    LEGACY_TAILWIND_PALETTE_ROLES,
    MIN_SEMANTIC_TEXT_CONTRAST,
    semantic_color_contrast_ratios,
)

ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_default_brand_semantic_roles_meet_wcag_aa_in_both_themes() -> None:
    for color in DEFAULT_SEMANTIC_COLORS.values():
        light_ratio, dark_ratio = semantic_color_contrast_ratios(color)
        assert light_ratio >= MIN_SEMANTIC_TEXT_CONTRAST
        assert dark_ratio >= MIN_SEMANTIC_TEXT_CONTRAST


def test_shared_web_renderer_resolves_tones_through_theme_tokens() -> None:
    css = _read("static/css/design-system.css")
    macros = _read("templates/components/ui/macros.html")
    status_macro = macros.split("{% macro status_badge", 1)[1].split(
        "{% endmacro %}", 1
    )[0]

    for tone in ("positive", "info", "warning", "negative", "neutral"):
        assert f"--color-semantic-{tone}-600" in css
        assert f".status-tone-{tone}" in css
    assert "status-tone-{{ v.tone }}" in status_macro
    assert "text-emerald" not in status_macro
    assert "text-red" not in status_macro
    assert "text-amber" not in status_macro


def test_runtime_brand_theme_owns_legacy_and_categorical_palettes() -> None:
    branding_route = _read("app/web/public/branding.py")
    base = _read("templates/base.html")

    assert "LEGACY_TAILWIND_PALETTE_ROLES" in branding_route
    assert "CATEGORICAL_COLOR_ROLES" in branding_route
    assert "window.themeColor" in base
    assert COLOR_SCALE_STEPS == (
        50,
        100,
        200,
        300,
        400,
        500,
        600,
        700,
        800,
        900,
        950,
    )
    assert set(CATEGORICAL_COLOR_ROLES) == {
        "primary",
        "accent",
        "semantic-info",
        "semantic-positive",
        "semantic-warning",
        "semantic-negative",
        "semantic-neutral",
    }
    assert LEGACY_TAILWIND_PALETTE_ROLES["red"] == "semantic-negative"
    assert LEGACY_TAILWIND_PALETTE_ROLES["emerald"] == "semantic-positive"
    assert LEGACY_TAILWIND_PALETTE_ROLES["amber"] == "semantic-warning"
    assert LEGACY_TAILWIND_PALETTE_ROLES["blue"] == "semantic-info"


def test_migrated_web_slices_do_not_keep_local_semantic_color_maps() -> None:
    connection = _read("templates/customer/connection/index.html")
    noc_kpis = _read("templates/admin/network/monitoring/_kpi_partial.html")
    monitoring_service = _read("app/services/network_monitoring.py")
    device_list = _read("templates/admin/network/devices/index.html")
    payments = _read("templates/admin/billing/payments.html")
    customer_detail = _read("templates/admin/customers/detail.html")
    tax_accounting = _read("templates/admin/billing/tax_accounting.html")

    assert "status-panel-${this.tone}" in connection
    assert "border-emerald" not in connection
    assert "border-red" not in connection
    for literal in ("emerald", "rose", "amber", "blue"):
        assert literal not in noc_kpis
    assert '"tones": ["positive", "warning", "negative"' in monitoring_service
    assert '"colors": ["#10b981"' not in monitoring_service
    assert 'tone="positive"' in device_list
    assert 'tone="negative"' in device_list
    assert 'tone="warning"' in payments
    assert "--color-semantic-positive-600" in payments
    assert "card_conn_state" not in customer_detail
    assert "sub_conn_state" not in customer_detail
    assert "status_presentation_badge" in tax_accounting
    for literal in ("border-red", "text-red", "border-amber", "text-emerald"):
        assert literal not in tax_accounting


def test_flutter_status_renderers_use_brand_semantic_palettes() -> None:
    customer_theme = _read("mobile/lib/src/core/semantic_colors.dart")
    customer_chip = _read("mobile/lib/src/widgets/status_chip.dart")
    field_theme = _read("field_mobile/lib/app/theme.dart")

    for getter in (
        "Brand.semanticPositiveColor",
        "Brand.semanticInfoColor",
        "Brand.semanticWarningColor",
        "Brand.semanticNegativeColor",
        "Brand.semanticNeutralColor",
    ):
        assert getter in customer_theme
    for field in ("success", "info", "warning", "negative", "neutral"):
        assert f"semantic.{field}" in customer_chip
    for field in (
        "semanticPositive",
        "semanticInfo",
        "semanticWarning",
        "semanticNegative",
        "semanticNeutral",
    ):
        assert field in field_theme
    assert "StatusTone.positive => green" not in field_theme
    assert "StatusTone.warning => accent" not in field_theme


def test_migrated_chart_map_and_mobile_slices_have_no_local_color_palette() -> None:
    web_paths = (
        "templates/admin/billing/index.html",
        "templates/admin/customers/detail.html",
        "templates/admin/network/detected_outages.html",
        "templates/admin/network/map.html",
        "templates/admin/network/monitoring/index.html",
        "app/services/subscriber.py",
        "app/services/web_billing_invoices.py",
    )
    retired_literals = (
        "#10b981",
        "#8b5cf6",
        "#14b8a6",
        "#f97316",
        "#ef4444",
        "#f59e0b",
        "#3b82f6",
    )
    for path in web_paths:
        source = _read(path).lower()
        for literal in retired_literals:
            assert literal not in source, f"{path} still owns {literal}"

    field_paths = (
        "field_mobile/lib/features/expenses/expenses_screen.dart",
        "field_mobile/lib/features/manager/manager_screen.dart",
        "field_mobile/lib/features/today/map_screen.dart",
        "field_mobile/lib/features/today/today_screen.dart",
        "mobile/lib/src/features/support/ticket_detail_screen.dart",
    )
    for path in field_paths:
        source = _read(path)
        for material_color in (
            "Colors.amber",
            "Colors.blue",
            "Colors.green",
            "Colors.orange",
            "Colors.red",
            "Colors.teal",
        ):
            assert material_color not in source, f"{path} still owns {material_color}"
