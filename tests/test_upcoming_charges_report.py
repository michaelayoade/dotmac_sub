from __future__ import annotations

from decimal import Decimal

import pytest

from app.services.billing import reporting


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
