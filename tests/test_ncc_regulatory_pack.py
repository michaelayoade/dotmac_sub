"""NCC regulatory-pack aggregator: sub assembles the three returns.

Covers the three divergences from CRM's version — ② is native (no HTTP hop),
③ degrades instead of fabricating, and ① comes from sub's own tickets — plus
the graceful-degradation contract that keeps the pack renderable when an
upstream is down.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import patch

from app.services import ncc_regulatory_pack

_START = datetime(2026, 4, 1, tzinfo=UTC)
_END = datetime(2026, 6, 30, 23, 59, 59, tzinfo=UTC)

_ERP_STAFF = {
    "total_active": 12,
    "by_category": {"MANAGERIAL": {"nigerian": {"male": 7, "female": 5, "other": 0}}},
}
_ERP_FINANCIALS = {"period": "2026", "summary": {"revenue": "1000.00"}}


class _FakeERPClient:
    """Stands in for DotMacERPClient's context-manager + NCC read surface."""

    def __init__(self, *, staff=None, financials=None, exc: Exception | None = None):
        self._staff = staff
        self._financials = financials
        self._exc = exc

    def __enter__(self):
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def get_ncc_staff_headcount(self) -> dict:
        if self._exc:
            raise self._exc
        return self._staff or {}

    def get_ncc_financials(self, **_kwargs) -> dict:
        if self._exc:
            raise self._exc
        return self._financials or {}


def _patch_erp(client):
    """Patch the lazily-imported ERP client factory the sections resolve."""
    return patch("app.services.dotmac_erp.client.build_erp_client", return_value=client)


def _patch_complaints(report):
    return patch.object(
        ncc_regulatory_pack,
        "complaints_section",
        return_value={"available": True, **report},
    )


# ── ② native re-home ────────────────────────────────────────────────────────


def test_subscribers_section_is_native_no_http(db_session):
    """② must be a direct call — CRM fetched it over the /crm bearer API, and
    the whole point of the re-home is that sub owns this return."""
    with patch(
        "httpx.Client",
        side_effect=AssertionError("HTTP client constructed — ② must be native"),
    ):
        section = ncc_regulatory_pack.subscribers_section(db_session)

    assert section["available"] is True
    assert "report" in section
    assert "total_active_subscriptions" in section["report"]


def test_subscribers_section_does_not_build_an_erp_client(db_session):
    with patch("app.services.dotmac_erp.client.build_erp_client") as build:
        ncc_regulatory_pack.subscribers_section(db_session)
    build.assert_not_called()


def test_subscribers_section_degrades_when_the_report_raises(db_session):
    with patch(
        "app.services.ncc_subscriber_report.build_ncc_subscriber_report",
        side_effect=RuntimeError("boom"),
    ):
        section = ncc_regulatory_pack.subscribers_section(db_session)
    assert section == {"available": False, "error": "boom"}


# ── ③G anti-fabrication (the removed _STAFF_HEADCOUNT_FALLBACK) ─────────────


def test_staff_section_returns_erp_headcount(db_session):
    with _patch_erp(_FakeERPClient(staff=_ERP_STAFF)):
        section = ncc_regulatory_pack.staff_section(db_session)
    assert section == {"available": True, "staff": _ERP_STAFF}


def test_staff_section_degrades_when_erp_unreachable(db_session):
    with _patch_erp(_FakeERPClient(exc=RuntimeError("connection refused"))):
        section = ncc_regulatory_pack.staff_section(db_session)
    assert section["available"] is False
    assert "connection refused" in section["error"]


def test_unreachable_erp_never_yields_a_headcount_number(db_session):
    """Regression: CRM substituted a hardcoded 170-person table whenever ERP
    was unreachable, empty, or unclassified — an ERP outage could put invented
    numbers into a regulatory filing. No degraded response may carry a
    head-count sub did not receive from ERP.
    """
    unusable = [
        _FakeERPClient(exc=RuntimeError("connection refused")),  # unreachable
        _FakeERPClient(staff={}),  # empty
        _FakeERPClient(  # present but unclassified — CRM's other fallback path
            staff={
                "total_active": 170,
                "by_category": {"MANAGERIAL": {"nigerian": {"male": 0, "female": 0}}},
            }
        ),
    ]
    for client in unusable:
        with _patch_erp(client):
            section = ncc_regulatory_pack.staff_section(db_session)

        assert section["available"] is False
        assert "staff" not in section
        # Nothing numeric may survive into the payload from a stand-in table.
        assert "170" not in json.dumps(section)


def test_staff_section_degrades_when_erp_unconfigured(db_session):
    with patch(
        "app.services.dotmac_erp.client.build_erp_client",
        side_effect=ValueError("DotMac ERP is not configured"),
    ):
        section = ncc_regulatory_pack.staff_section(db_session)
    assert section == {"available": False, "error": "dotmac_erp is not configured"}


# ── ③F financials ───────────────────────────────────────────────────────────


def test_financials_section_returns_erp_data(db_session):
    with _patch_erp(_FakeERPClient(financials=_ERP_FINANCIALS)):
        section = ncc_regulatory_pack.financials_section(db_session, year=2026)
    assert section == {"available": True, "financials": _ERP_FINANCIALS}


def test_financials_section_degrades_when_erp_unreachable(db_session):
    with _patch_erp(_FakeERPClient(exc=RuntimeError("gateway timeout"))):
        section = ncc_regulatory_pack.financials_section(db_session, year=2026)
    assert section["available"] is False
    assert "gateway timeout" in section["error"]


# ── ① complaints ────────────────────────────────────────────────────────────


def test_complaints_section_uses_the_native_report(db_session):
    report = {"total_complaints": 3, "by_category": {"Billing": 3}}
    with patch(
        "app.services.ncc_complaints_report.build_report",
        lambda db, *, start, end: report,
    ):
        section = ncc_regulatory_pack.complaints_section(db_session, _START, _END)

    assert section["available"] is True
    assert section["total_complaints"] == 3
    assert section["period"]["start"] == _START.isoformat()


def test_complaints_section_degrades_when_the_report_raises(db_session):
    def _raise(db, *, start, end):
        raise RuntimeError("no tickets")

    with patch("app.services.ncc_complaints_report.build_report", _raise):
        section = ncc_regulatory_pack.complaints_section(db_session, _START, _END)
    assert section["available"] is False
    assert "no tickets" in section["error"]


# ── the pack ────────────────────────────────────────────────────────────────


def test_pack_assembles_all_three_sections(db_session):
    report = {"total_complaints": 2, "by_category": {"Billing": 2}}
    client = _FakeERPClient(staff=_ERP_STAFF, financials=_ERP_FINANCIALS)
    with _patch_complaints(report), _patch_erp(client):
        pack = ncc_regulatory_pack.build_regulatory_pack(
            db_session, start_dt=_START, end_dt=_END, year=2026
        )

    assert pack["meta"]["complete"] is True
    assert pack["meta"]["sources"] == {
        "complaints": True,
        "subscribers": True,
        "financials": True,
        "staff": True,
    }
    assert pack["complaints"]["total_complaints"] == 2
    assert pack["subscribers"]["available"] is True
    assert pack["financials"]["financials"] == _ERP_FINANCIALS
    assert pack["staff"]["staff"] == _ERP_STAFF


def test_pack_renders_with_erp_down(db_session):
    """A dead upstream marks its own section unavailable; the pack still
    returns so the officer sees which section is missing."""
    report = {"total_complaints": 1}
    client = _FakeERPClient(exc=RuntimeError("erp down"))
    with _patch_complaints(report), _patch_erp(client):
        pack = ncc_regulatory_pack.build_regulatory_pack(
            db_session, start_dt=_START, end_dt=_END, year=2026
        )

    assert pack["meta"]["complete"] is False
    assert pack["meta"]["sources"]["financials"] is False
    assert pack["meta"]["sources"]["staff"] is False
    # The native sections are unaffected by the ERP outage.
    assert pack["meta"]["sources"]["complaints"] is True
    assert pack["meta"]["sources"]["subscribers"] is True
    assert "staff" not in pack["staff"]
