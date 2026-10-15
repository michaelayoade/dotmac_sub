from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from jinja2 import Environment, FileSystemLoader, select_autoescape


def _template_environment() -> Environment:
    environment = Environment(
        loader=FileSystemLoader("templates"),
        autoescape=select_autoescape(("html", "xml")),
    )
    environment.filters["app_datetime"] = lambda value, *_args, **_kwargs: value
    environment.globals.update(
        admin_workflow_guidance_for_path=lambda _path: None,
        app_version=lambda: "test",
        can=lambda _request, _permission: False,
        dotmac_ui_stylesheet_url="/static/dotmac-ui.css",
    )
    return environment


def test_ar_aging_template_serializes_only_chart_safe_debtor_fields() -> None:
    request = SimpleNamespace(
        url=SimpleNamespace(path="/admin/billing/ar-aging"),
        state=SimpleNamespace(auth={}, csrf_token=""),
    )
    template = _template_environment().get_template("admin/billing/ar_aging.html")

    html = template.render(
        request=request,
        current_user=None,
        sidebar_stats={},
        active_page="ar-aging",
        active_menu="billing",
        selected_period="all",
        selected_bucket=None,
        selected_partner_id=None,
        selected_location=None,
        selected_debtor_period="all",
        partner_options=[],
        location_options=[],
        bucket_order=[],
        bucket_rows={},
        bucket_invoice_ids={},
        counts={},
        totals={
            "current": 0.0,
            "1_30": 0.0,
            "31_60": 0.0,
            "61_90": 0.0,
            "90_plus": 0.0,
        },
        top_debtors=[
            {
                "account_id": "account-1",
                "account_label": "Ada A",
                "amount": 60.0,
                "amounts": {"NGN": Decimal("60.00")},
                "display": "NGN 60.00",
            }
        ],
        aging_trend={
            "labels": [],
            "series": {
                "current": [],
                "1_30": [],
                "31_60": [],
                "61_90": [],
                "90_plus": [],
            },
        },
        visible_bucket_keys=[],
        can_send_reminders=False,
    )

    assert 'account_label: "Ada A"' in html
    assert "amount: 60.0" in html
    assert '"amounts"' not in html
