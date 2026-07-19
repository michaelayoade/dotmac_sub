"""Reseller portal headline tiles and account actions are shared UI contracts.

The reseller dashboard/revenue tiles and the account "danger zone" buttons used
to be raw dicts the template re-interpreted. They now project through the shared
``Kpi`` / ``StateValue`` / ``Action`` contracts: each KPI carries a
``cohort_url`` that drills into exactly the accounts/invoices it counts, an
unreachable CRM shows a StateValue placeholder instead of a false ``0``, and
action eligibility (allowed/reason) is owned by the backend, never re-derived
from a status string in the template.

Commission is untouched: ``get_revenue_summary`` figures are customer BILLING
money (invoices paid to / owed to Dotmac), and these projections only wrap the
already-computed values for display.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from pathlib import Path

from app.models.subscriber import Reseller, Subscriber, SubscriberStatus
from app.schemas.status_presentation import StatusTone
from app.services import reseller_portal
from app.services.ui_contracts import Action, Kpi, StateValue

_TEMPLATES = Path(__file__).resolve().parents[1] / "templates"


def _make_account(db_session, reseller, status):
    sub = Subscriber(
        first_name="Acc",
        last_name=status.value,
        email=f"{uuid.uuid4()}@example.com",
        reseller_id=reseller.id,
        status=status,
        is_active=(status != SubscriberStatus.canceled),
    )
    db_session.add(sub)
    db_session.commit()
    db_session.refresh(sub)
    return sub


def test_dashboard_kpis_are_contracts_that_drill_into_their_cohort():
    summary = {
        "totals": {
            "accounts": 12,
            "open_balance": Decimal("450.00"),
            "open_invoices": 4,
        }
    }
    kpis = reseller_portal.dashboard_kpis(summary)
    assert set(kpis) == {"accounts", "open_balance", "open_invoices"}
    assert all(isinstance(kpi, Kpi) for kpi in kpis.values())
    assert all(isinstance(kpi.value, StateValue) for kpi in kpis.values())

    # Each tile shows the already-computed total, never a re-derived number.
    assert kpis["accounts"].value.value == 12
    assert kpis["open_balance"].value.value == Decimal("450.00")
    assert kpis["open_invoices"].value.value == 4

    # Cohort URLs are application-relative and point at the exact list.
    assert kpis["accounts"].cohort_url == "/reseller/accounts"
    assert kpis["open_balance"].cohort_url == "/reseller/billing#total-outstanding"
    assert kpis["open_invoices"].cohort_url == "/reseller/billing#open-invoices"
    assert all(kpi.cohort_url.startswith("/") for kpi in kpis.values())


def test_revenue_kpis_wrap_customer_billing_figures():
    summary = {
        "total_paid": Decimal("10000.00"),
        "total_outstanding": Decimal("250.00"),
        "account_count": 7,
        "currency": "NGN",
    }
    kpis = reseller_portal.revenue_kpis(summary)
    assert set(kpis) == {"total_paid", "total_outstanding", "account_count"}
    assert all(isinstance(kpi, Kpi) for kpi in kpis.values())

    assert kpis["total_paid"].value.value == Decimal("10000.00")
    assert kpis["total_outstanding"].value.value == Decimal("250.00")
    assert kpis["account_count"].value.value == 7

    assert kpis["total_paid"].tone is StatusTone.positive
    assert kpis["total_paid"].cohort_url == "/reseller/billing#customer-paid"
    assert (
        kpis["total_outstanding"].cohort_url
        == "/reseller/billing#total-outstanding"
    )
    assert kpis["account_count"].cohort_url == "/reseller/accounts"
    assert all(kpi.cohort_url.startswith("/") for kpi in kpis.values())


def test_account_status_action_contracts_own_eligibility_and_reason():
    previews = {
        "restore": {"allowed": True, "affected": 2, "fingerprint": "a"},
        "deactivate": {"allowed": False, "affected": 0, "fingerprint": "b"},
        "disable": {"allowed": True, "affected": 3, "fingerprint": "c"},
    }
    actions = reseller_portal.account_status_action_contracts(previews)
    assert set(actions) == {"restore", "deactivate", "disable"}
    assert all(isinstance(action, Action) for action in actions.values())

    # Allowed action carries no blocked reason (Action invariant).
    assert actions["restore"].allowed is True
    assert actions["restore"].reason is None
    assert actions["restore"].affected == 2

    # Blocked action MUST carry a non-empty reason (Action invariant).
    assert actions["deactivate"].allowed is False
    assert actions["deactivate"].reason
    assert actions["deactivate"].affected == 0

    # No preview_url is set, so requires_confirmation stays False — the confirm
    # step remains a CSRF POST driven by the raw preview fingerprint.
    assert actions["disable"].preview_url is None
    assert actions["disable"].requires_confirmation is False
    assert actions["disable"].tone is StatusTone.negative


def test_account_status_action_contracts_default_missing_previews_to_blocked():
    actions = reseller_portal.account_status_action_contracts({})
    for action in actions.values():
        assert action.allowed is False
        assert action.reason  # blocked → reason present, never a bare disabled button
        assert action.affected == 0


def test_dashboard_template_renders_kpi_and_state_contracts():
    source = (_TEMPLATES / "reseller/dashboard/index.html").read_text(encoding="utf-8")
    assert "kpis.accounts.value.value" in source
    assert "kpis.accounts.cohort_url" in source
    assert "kpis.open_balance.cohort_url" in source
    assert "kpis.open_invoices.cohort_url" in source
    # Open tickets: an unreachable CRM renders the StateValue placeholder, not 0.
    assert "open_tickets_state.is_present" in source
    assert "open_tickets_state.value.value" in source
    assert "open_tickets_state.placeholder" in source


def test_revenue_template_renders_kpi_cohort_links():
    source = (_TEMPLATES / "reseller/reports/revenue.html").read_text(encoding="utf-8")
    assert "kpis.total_paid.value.value" in source
    assert "kpis.total_paid.cohort_url" in source
    assert "kpis.total_outstanding.cohort_url" in source
    assert "kpis.account_count.cohort_url" in source


def test_accounts_kpi_value_matches_its_cohort_url_list(db_session):
    """KPI-parity: the Accounts tile counts exactly the cohort /reseller/accounts
    lists. A 'disabled' (deactivated-but-not-canceled) account is hidden from the
    default list, so it must NOT inflate the headline number above that list."""
    reseller = Reseller(name="Parity Co", code="PARITY")
    db_session.add(reseller)
    db_session.commit()
    db_session.refresh(reseller)
    rid = str(reseller.id)

    _make_account(db_session, reseller, SubscriberStatus.active)
    _make_account(db_session, reseller, SubscriberStatus.active)
    _make_account(db_session, reseller, SubscriberStatus.disabled)  # hidden by default
    _make_account(db_session, reseller, SubscriberStatus.canceled)  # soft-deleted

    # The set the cohort_url ("/reseller/accounts", default filter) actually shows.
    default_list_count = len(reseller_portal.list_accounts(db_session, rid, 50, 0))
    cohort_count = reseller_portal.count_accounts(db_session, rid)
    assert default_list_count == cohort_count == 2  # excludes disabled + canceled

    # Dashboard tile value equals the count at its cohort_url's filter.
    summary = reseller_portal.get_dashboard_summary(db_session, rid, 50, 0)
    assert summary["totals"]["accounts"] == cohort_count
    dash_kpis = reseller_portal.dashboard_kpis(summary)
    assert dash_kpis["accounts"].value.value == cohort_count
    assert dash_kpis["accounts"].cohort_url == "/reseller/accounts"

    # Revenue tile value equals the same cohort count (consistent projection).
    revenue = reseller_portal.get_revenue_summary(db_session, rid)
    assert revenue["account_count"] == cohort_count
    rev_kpis = reseller_portal.revenue_kpis(revenue)
    assert rev_kpis["account_count"].value.value == cohort_count
    assert rev_kpis["account_count"].cohort_url == "/reseller/accounts"


def test_accounts_kpi_value_is_independent_of_a_page_status_filter(db_session):
    """A KPI tile is a fixed overview number: filtering the accounts list below it
    (e.g. status_filter=suspended) must NOT shrink the headline Accounts count.
    The overview value is computed over the unfiltered default cohort regardless."""
    reseller = Reseller(name="Filter Co", code="FILTER")
    db_session.add(reseller)
    db_session.commit()
    db_session.refresh(reseller)
    rid = str(reseller.id)

    _make_account(db_session, reseller, SubscriberStatus.active)
    _make_account(db_session, reseller, SubscriberStatus.active)
    _make_account(db_session, reseller, SubscriberStatus.suspended)

    summary = reseller_portal.get_dashboard_summary(db_session, rid, 50, 0)
    overview_value = reseller_portal.dashboard_kpis(summary)["accounts"].value.value

    # The default (unfiltered) cohort the tile links to has all 3 non-deleted rows.
    assert overview_value == reseller_portal.count_accounts(db_session, rid) == 3
    # A page status filter narrows the list but the overview tile is unchanged.
    assert (
        reseller_portal.count_accounts(db_session, rid, status_filter="suspended") == 1
    )
    assert overview_value == 3


def test_account_detail_template_uses_action_contracts_for_eligibility():
    source = (_TEMPLATES / "reseller/accounts/detail.html").read_text(encoding="utf-8")
    # Eligibility/affected/reason come from the Action contract...
    assert "status_action_contracts.restore.allowed" in source
    assert "status_action_contracts.deactivate.allowed" in source
    assert "status_action_contracts.disable.allowed" in source
    assert "action_permitted(request, status_action_contracts.restore)" in source
    assert "action_permitted(request, status_action_contracts.deactivate)" in source
    assert "action_permitted(request, status_action_contracts.disable)" in source
    assert "status_action_contracts.deactivate.reason" in source
    # A blocked restore renders its reason instead of hiding the whole block.
    assert "status_action_contracts.restore.reason" in source
    # ...while the load-bearing fingerprint still flows through the raw preview
    # dict so the confirm POST can re-check it.
    assert "account.status_actions.restore.fingerprint" in source
    assert "account.status_actions.disable.fingerprint" in source
