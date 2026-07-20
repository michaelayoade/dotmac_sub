"""Billing ledger summary tiles are Kpi contracts that drill into their cohort.

The credit/debit/net cards used to be raw display strings the template rendered
with no drill-down. They now come back as ``Kpi`` objects whose ``cohort_url``
filters the ledger to exactly the rows the tile counts (KPI-parity), so a
headline and its list can never disagree.
"""

from __future__ import annotations

from pathlib import Path

from app.services import web_billing_ledger
from app.services.ui_contracts import Kpi


def test_ledger_kpis_are_contracts_that_link_to_their_cohort(db_session):
    data = web_billing_ledger.build_ledger_entries_data(
        db_session, customer_ref=None, entry_type=None
    )
    kpis = data["ledger_kpis"]
    assert set(kpis) == {"credits", "debits", "net"}
    assert all(isinstance(kpi, Kpi) for kpi in kpis.values())

    # Each tile still shows the same figure the ledger_totals carry.
    assert kpis["credits"].value.value == data["ledger_totals"]["credit_display"]
    assert kpis["debits"].value.value == data["ledger_totals"]["debit_display"]
    assert kpis["net"].value.value == data["ledger_totals"]["net_display"]

    # Credits/debits narrow the ledger by entry_type; net keeps the full cohort.
    assert kpis["credits"].cohort_url == "/admin/billing/ledger?entry_type=credit"
    assert kpis["debits"].cohort_url == "/admin/billing/ledger?entry_type=debit"
    assert kpis["net"].cohort_url == "/admin/billing/ledger"


def test_ledger_kpi_cohort_preserves_the_active_filters(db_session):
    data = web_billing_ledger.build_ledger_entries_data(
        db_session,
        customer_ref=None,
        entry_type=None,
        start_date="2026-01-01",
        end_date="2026-03-31",
        category="subscription",
    )
    credits_url = data["ledger_kpis"]["credits"].cohort_url
    assert credits_url.startswith("/admin/billing/ledger?")
    assert "entry_type=credit" in credits_url
    assert "start_date=2026-01-01" in credits_url
    assert "end_date=2026-03-31" in credits_url
    assert "category=subscription" in credits_url


def test_ledger_template_renders_kpi_cohort_links():
    source = (
        Path(__file__).resolve().parents[1] / "templates/admin/billing/ledger.html"
    ).read_text(encoding="utf-8")
    # The tiles deep-link and render the StateValue, not a bare display string.
    assert "ledger_kpis.credits.value.value" in source
    assert "href=ledger_kpis.credits.cohort_url" in source
    assert "href=ledger_kpis.debits.cohort_url" in source
    assert "href=ledger_kpis.net.cohort_url" in source
