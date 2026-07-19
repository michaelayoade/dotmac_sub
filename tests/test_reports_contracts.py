"""UI projection contract coverage for the subscriber-analytics report slice.

Owners (``web_reports``) project the customer and churn headline tiles as
``Kpi`` objects whose values carry state and whose ``cohort_url`` drills into
the exact filtered customer-report cohort that produced each number
(KPI-parity). The templates render those contract fields.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlparse

from app.models.subscriber import AccountStatus, Subscriber, UserType
from app.schemas.status_presentation import StatusTone
from app.services import web_reports
from app.services.ui_contracts import Action, Kpi, StateValue

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _make_subscriber(
    db_session,
    status: AccountStatus | None,
    *,
    is_active: bool = True,
) -> Subscriber:
    from app.services.subscriber import _default_reseller_id

    # ``user_type=customer`` so the row satisfies ``visible_subscriber_clause``
    # (which excludes ``system_user``) and is actually counted by the report
    # queries — otherwise every cohort count would be a vacuous 0.
    label = status.value if status is not None else "null"
    sub = Subscriber(
        first_name="Report",
        last_name=label,
        email=f"{label}-{id(object())}@example.test",
        status=status,
        is_active=is_active,
        user_type=UserType.customer,
        reseller_id=_default_reseller_id(db_session),
    )
    db_session.add(sub)
    db_session.commit()
    db_session.refresh(sub)
    return sub


def _cohort_status(url: str) -> str | None:
    values = parse_qs(urlparse(url).query).get("status")
    return values[0] if values else None


def _count_at_cohort(db_session, url: str) -> int:
    """Rows the customer-report drill-down at ``url`` would actually list.

    Parity check: a KPI tile value must equal the size of the cohort its
    ``cohort_url`` links to — the same strict ``_load_report_subscribers``
    filter the destination page applies.
    """
    query = parse_qs(urlparse(url).query)
    return len(
        web_reports._load_report_subscribers(
            db_session,
            status=query.get("status", [None])[0],
            date_from=query.get("date_from", [None])[0],
            date_to=query.get("date_to", [None])[0],
        )
    )


# --------------------------------------------------------------------------
# Cohort URL helper
# --------------------------------------------------------------------------


def test_cohort_url_is_app_relative_and_encodes_filters():
    url = web_reports._customers_report_cohort_url(
        status="active", date_from="2026-01-01", date_to="2026-03-31"
    )
    assert url.startswith("/admin/reports/customers")
    query = parse_qs(urlparse(url).query)
    assert query["status"] == ["active"]
    assert query["date_from"] == ["2026-01-01"]
    assert query["date_to"] == ["2026-03-31"]


def test_cohort_url_omits_empty_filters():
    assert web_reports._customers_report_cohort_url() == "/admin/reports/customers"


# --------------------------------------------------------------------------
# Subscribers report KPIs
# --------------------------------------------------------------------------


def test_subscribers_report_returns_kpi_contracts(db_session):
    _make_subscriber(db_session, AccountStatus.active)
    _make_subscriber(db_session, AccountStatus.active)
    _make_subscriber(db_session, AccountStatus.suspended)

    data = web_reports.get_subscribers_report_data(db_session)
    kpis = data["subscriber_kpis"]

    assert set(kpis) == {"total", "new_this_month", "active", "suspended"}
    for kpi in kpis.values():
        assert isinstance(kpi, Kpi)
        assert isinstance(kpi.value, StateValue)
        # An owner-supplied count is present, never an unknown standing in as 0.
        assert kpi.value.is_present
        assert kpi.cohort_url.startswith("/")

    # KPI-parity: each status tile drills into exactly that status cohort.
    assert _cohort_status(kpis["active"].cohort_url) == AccountStatus.active.value
    assert _cohort_status(kpis["suspended"].cohort_url) == AccountStatus.suspended.value
    # The total tile spans all statuses (no status narrowing).
    assert _cohort_status(kpis["total"].cohort_url) is None
    assert kpis["active"].tone == StatusTone.info
    assert kpis["suspended"].tone == StatusTone.warning

    # KPI-parity: every tile value equals the size of the cohort it links to,
    # counted with the same strict filter the drill-down page applies. This
    # includes "new_this_month", which counts raw created_at within its month
    # window (not the effective/source signup date), matching its drill-down.
    for key in ("total", "new_this_month", "active", "suspended"):
        assert kpis[key].value.value == _count_at_cohort(
            db_session, kpis[key].cohort_url
        )
    assert kpis["total"].value.value == 3
    assert kpis["active"].value.value == 2
    assert kpis["suspended"].value.value == 1


def test_subscribers_kpi_value_unchanged_by_page_status_filter(db_session):
    _make_subscriber(db_session, AccountStatus.active)
    _make_subscriber(db_session, AccountStatus.active)
    _make_subscriber(db_session, AccountStatus.suspended)

    unfiltered = web_reports.get_subscribers_report_data(db_session)["subscriber_kpis"]
    # Operator narrows the table to active-only; the overview tiles must not move.
    filtered = web_reports.get_subscribers_report_data(
        db_session, status=AccountStatus.active.value
    )["subscriber_kpis"]

    for key in ("total", "active", "suspended"):
        assert filtered[key].value.value == unfiltered[key].value.value
        # And each still equals its own cohort — the drill-down the tile links to.
        assert filtered[key].value.value == _count_at_cohort(
            db_session, filtered[key].cohort_url
        )
    # Concretely: Total stays 3 (not the active-only 2), Suspended stays 1
    # (not 0) even though the page filter would hide those rows from the table.
    assert filtered["total"].value.value == 3
    assert filtered["suspended"].value.value == 1


def test_subscribers_kpi_preserves_active_date_filter(db_session):
    _make_subscriber(db_session, AccountStatus.active)

    data = web_reports.get_subscribers_report_data(
        db_session, date_from="2026-01-01", date_to="2026-06-30"
    )
    active_url = data["subscriber_kpis"]["active"].cohort_url
    query = parse_qs(urlparse(active_url).query)
    assert query["date_from"] == ["2026-01-01"]
    assert query["date_to"] == ["2026-06-30"]
    assert query["status"] == [AccountStatus.active.value]


# --------------------------------------------------------------------------
# Churn report KPIs
# --------------------------------------------------------------------------


def test_churn_report_returns_kpi_contracts(db_session):
    _make_subscriber(db_session, AccountStatus.active)
    _make_subscriber(db_session, AccountStatus.canceled)
    _make_subscriber(db_session, AccountStatus.suspended)

    data = web_reports.get_churn_report_data(db_session)
    kpis = data["churn_kpis"]

    assert set(kpis) == {"churn_rate", "cancelled", "at_risk", "retention_rate"}
    for kpi in kpis.values():
        assert isinstance(kpi, Kpi)
        assert isinstance(kpi.value, StateValue)
        assert kpi.value.is_present
        assert kpi.cohort_url.startswith("/")

    assert kpis["churn_rate"].cohort_url == "/admin/reports/churn#churn-summary"
    assert kpis["retention_rate"].cohort_url == "/admin/reports/churn#churn-summary"
    assert _cohort_status(kpis["cancelled"].cohort_url) == AccountStatus.canceled.value
    assert _cohort_status(kpis["at_risk"].cohort_url) == AccountStatus.suspended.value
    # KPI-parity: the count tiles equal the size of the cohort they link to.
    assert kpis["cancelled"].value.value == _count_at_cohort(
        db_session, kpis["cancelled"].cohort_url
    )
    assert kpis["cancelled"].value.value == 1
    assert kpis["at_risk"].value.value == _count_at_cohort(
        db_session, kpis["at_risk"].cohort_url
    )
    assert kpis["at_risk"].value.value == 1
    # Retention is active / total (1 / 3), not 100 - cancelled / total (2 / 3).
    assert kpis["retention_rate"].value.value == "33.3%"
    # Rate tiles render an owner-formatted string, not a raw number the template
    # would have to reinterpret.
    assert kpis["churn_rate"].value.value.endswith("%")
    assert kpis["retention_rate"].tone == StatusTone.positive


def test_churn_cancelled_tile_counts_strictly_like_its_drilldown(db_session):
    """The Cancellations tile must count with the SAME strict rule its
    ``status=canceled`` drill-down applies, not the wider derived-cancelled
    rule. A NULL-status inactive row derives to ``canceled`` but is excluded by
    the strict list filter, so the tile must not count it — otherwise the
    headline would exceed the list it links to.
    """
    _make_subscriber(db_session, AccountStatus.canceled)
    # Derived-cancelled (NULL status + inactive) but NOT a strict ``canceled``.
    _make_subscriber(db_session, None, is_active=False)

    kpis = web_reports.get_churn_report_data(db_session)["churn_kpis"]
    cancelled = kpis["cancelled"]

    # Only the strict ``canceled`` row is counted, matching the drill-down.
    assert cancelled.value.value == 1
    assert cancelled.value.value == _count_at_cohort(db_session, cancelled.cohort_url)


# --------------------------------------------------------------------------
# Contract invariants and state semantics
# --------------------------------------------------------------------------


def test_state_value_unknown_never_renders_as_zero():
    unknown = StateValue.unknown()
    assert not unknown.is_present
    assert unknown.value is None
    assert unknown.placeholder == "Unknown"


def test_action_eligibility_invariants():
    allowed = Action(key="export", label="Export", allowed=True)
    assert allowed.allowed and allowed.reason is None

    blocked = Action(
        key="export",
        label="Export",
        allowed=False,
        reason="Insufficient permission",
    )
    assert not blocked.allowed and blocked.reason


# --------------------------------------------------------------------------
# Templates render the contract fields
# --------------------------------------------------------------------------


def test_subscribers_template_renders_kpi_contract_fields():
    template = (PROJECT_ROOT / "templates/admin/reports/subscribers.html").read_text(
        encoding="utf-8"
    )
    assert "subscriber_kpis.total.value.value" in template
    assert "href=subscriber_kpis.active.cohort_url" in template
    assert "tone=subscriber_kpis.suspended.tone" in template


def test_churn_template_renders_kpi_contract_fields():
    template = (PROJECT_ROOT / "templates/admin/reports/churn.html").read_text(
        encoding="utf-8"
    )
    assert "churn_kpis.churn_rate.value.value" in template
    assert "href=churn_kpis.cancelled.cohort_url" in template
    assert "tone=churn_kpis.retention_rate.tone" in template
