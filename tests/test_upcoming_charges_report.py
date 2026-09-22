from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

import pytest
from starlette.requests import Request

from app.models.catalog import BillingCycle
from app.services.billing import reporting
from app.web.admin import reports as report_routes


def test_amount_bands_are_typed_and_open_ended() -> None:
    bands = reporting.parse_upcoming_charge_amount_bands(
        "50000-100000, 100000-500000, 500000-"
    )

    assert [(band.minimum, band.maximum) for band in bands] == [
        (Decimal("50000"), Decimal("100000")),
        (Decimal("100000"), Decimal("500000")),
        (Decimal("500000"), None),
    ]
    assert [band.key for band in bands] == ["band-1", "band-2", "band-3"]


@pytest.mark.parametrize(
    "raw",
    (
        "",
        "100000",
        "100000-50000",
        "100000-500000,400000-",
        "500000-,1000000-",
        "100000-200000,50000-100000",
        "-1-100000",
    ),
)
def test_amount_bands_reject_ambiguous_configuration(raw: str) -> None:
    with pytest.raises(ValueError):
        reporting.parse_upcoming_charge_amount_bands(raw)


def test_upcoming_charges_page_caps_expensive_enrichment_page(monkeypatch) -> None:
    config = reporting.UpcomingChargesConfig(
        postpaid_lead_days=14,
        prepaid_lead_days=7,
        prepaid_amount_bands=reporting.parse_upcoming_charge_amount_bands("50000-"),
        include_funded_prepaid_default=False,
    )
    observed: dict[str, int] = {}

    monkeypatch.setattr(reporting, "get_upcoming_charges_config", lambda _db: config)

    def fake_prepaid(_db, **kwargs):
        observed["per_page"] = kwargs["per_page"]
        return reporting.UpcomingChargesPage(
            rows=(),
            candidate_count=0,
            page=kwargs["page"],
            per_page=kwargs["per_page"],
            has_previous=False,
            has_next=False,
        )

    monkeypatch.setattr(reporting, "_prepaid_upcoming_charges", fake_prepaid)

    _config, page = reporting.get_upcoming_charges_page(
        object(),  # type: ignore[arg-type]
        query=reporting.UpcomingChargesQuery(
            mode=reporting.UpcomingChargeMode.prepaid,
            page=-4,
            per_page=5000,
        ),
    )

    assert observed == {"per_page": 50}
    assert page.page == 1


def test_amount_band_boundaries_do_not_overlap() -> None:
    first, second = reporting.parse_upcoming_charge_amount_bands(
        "50000-100000,100000-500000"
    )

    boundary = Decimal("100000")
    assert first.minimum <= Decimal("99999.99") < first.maximum  # type: ignore[operator]
    assert not (first.minimum <= boundary < first.maximum)  # type: ignore[operator]
    assert second.minimum <= boundary < second.maximum  # type: ignore[operator]


def test_postpaid_summary_uses_confirmed_allocations_for_all_matching_invoices(
    monkeypatch,
) -> None:
    config = reporting.UpcomingChargesConfig(
        postpaid_lead_days=14,
        prepaid_lead_days=7,
        prepaid_amount_bands=reporting.parse_upcoming_charge_amount_bands("50000-"),
        include_funded_prepaid_default=False,
    )
    monkeypatch.setattr(reporting, "get_upcoming_charges_config", lambda _db: config)
    statements = []
    batches = iter([[("NGN", Decimal("300.00"), Decimal("200.00"))], []])

    def execute(statement):
        statements.append(statement)
        return SimpleNamespace(all=lambda: next(batches))

    _config, page = reporting.get_upcoming_charges_page(
        SimpleNamespace(execute=execute),
        query=reporting.UpcomingChargesQuery(
            mode=reporting.UpcomingChargeMode.postpaid,
            include_summary=True,
            as_of=datetime(2026, 9, 22, tzinfo=UTC),
        ),
    )

    assert page.rows == ()
    assert page.summary is not None
    assert page.summary.amounts == (
        reporting.UpcomingChargeCurrencySummary(
            currency="NGN",
            expected=Decimal("500.00"),
            received=Decimal("200.00"),
            not_received=Decimal("300.00"),
        ),
    )
    assert "payment_allocations" in str(statements[0])
    assert "payments.status" in str(statements[0])


def test_prepaid_summary_counts_wallet_funding_once_across_batches(monkeypatch) -> None:
    from app.services import customer_financial_position, prepaid_service_renewals

    account_id = UUID(int=1)
    subscriptions = [
        SimpleNamespace(id=UUID(int=index + 2), subscriber_id=account_id)
        for index in range(101)
    ]
    subscriptions_to_exclude = subscriptions[0].id
    candidate_rows = [
        (
            subscription,
            datetime(2026, 9, 25, tzinfo=UTC),
            None,
            "Plan",
            False,
            UUID(int=index + 200),
        )
        for index, subscription in enumerate(subscriptions)
    ]
    batches = iter([candidate_rows[:100], candidate_rows[100:], []])
    monkeypatch.setattr(
        prepaid_service_renewals,
        "resolve_prepaid_monthly_charges",
        lambda _db, subscriptions, _now: {
            subscription.id: (Decimal("1.00"), "NGN", BillingCycle.monthly)
            for subscription in subscriptions
            if subscription.id != subscriptions_to_exclude
        },
    )
    monkeypatch.setattr(
        customer_financial_position,
        "prepaid_available_balances",
        lambda _db, _account_ids: {account_id: Decimal("50.00")},
    )
    config = reporting.UpcomingChargesConfig(
        postpaid_lead_days=14,
        prepaid_lead_days=7,
        prepaid_amount_bands=reporting.parse_upcoming_charge_amount_bands("50000-"),
        include_funded_prepaid_default=False,
    )
    monkeypatch.setattr(reporting, "get_upcoming_charges_config", lambda _db: config)

    _config, page = reporting.get_upcoming_charges_page(
        SimpleNamespace(
            execute=lambda _statement: SimpleNamespace(all=lambda: next(batches))
        ),
        query=reporting.UpcomingChargesQuery(
            mode=reporting.UpcomingChargeMode.prepaid,
            include_funded=True,
            include_summary=True,
            as_of=datetime(2026, 9, 22, tzinfo=UTC),
        ),
    )

    assert page.rows == ()
    assert page.summary is not None
    assert page.summary.amounts == (
        reporting.UpcomingChargeCurrencySummary(
            currency="NGN",
            expected=Decimal("100.00"),
            received=Decimal("50.00"),
            not_received=Decimal("50.00"),
        ),
    )
    assert page.summary.unpriced_count == 1


@pytest.mark.parametrize(
    ("mode", "received_label"),
    (("postpaid", "Payments received"), ("prepaid", "Already funded")),
)
def test_upcoming_charge_totals_render_in_report(mode, received_label) -> None:
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/admin/reports/upcoming-charges",
            "query_string": b"",
            "headers": [],
            "scheme": "http",
            "server": ("testserver", 80),
        }
    )
    html = report_routes.templates.env.get_template(
        "admin/reports/upcoming_charges.html"
    ).render(
        request=request,
        current_user=None,
        sidebar_stats={},
        active_page="reports-upcoming-charges",
        active_menu="reports",
        mode=mode,
        state="all",
        selected_band="",
        include_funded=False,
        amount_bands=(),
        postpaid_lead_days=14,
        prepaid_lead_days=7,
        charges=(),
        candidate_count=0,
        page=1,
        per_page=25,
        has_previous=False,
        has_next=False,
        summary_amounts=(
            {
                "expected": "NGN 500.00",
                "received": "NGN 200.00",
                "not_received": "NGN 300.00",
            },
        ),
        summary_unpriced_count=0,
    )

    assert 'aria-label="Charge totals"' in html
    assert "xl:grid-cols-3" in html
    assert received_label in html
    assert "NGN 500.00" in html
    assert "NGN 200.00" in html
    assert "NGN 300.00" in html
