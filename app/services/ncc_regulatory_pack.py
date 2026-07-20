"""NCC regulatory-pack aggregator — sub assembles the filing.

Assembles the three NCC (Nigerian Communications Commission) returns into one
payload, so a compliance officer produces the filing from a single view
instead of stitching tools together:

  ① Quarterly Complaints         — native (sub tickets, ``ncc_complaints_report``)
  ② Quarterly Subscriber/Capacity — native (``ncc_subscriber_report``)
  ③ Annual Year-End Section F/G   — dotmac_erp via the ERP API

Ported from ``dotmac_crm/app/services/ncc_regulatory_pack.py`` for the CRM
exit, with the assembler re-homed to sub. Three deliberate divergences:

**② is native, not an HTTP hop.** CRM fetched ② from sub over the ``/crm``
bearer API and then re-applied the pack's presentation adjustments locally.
Sub owns that return, so it is now a direct call — which also removes a latent
double-normalisation: ``build_ncc_subscriber_report`` already applies
``normalize_ncc_pack_subscriber_report`` before returning, so the pack must
not adjust the buckets a second time.

**③G never fabricates.** CRM carried a hardcoded 170-person
``_STAFF_HEADCOUNT_FALLBACK`` and substituted it whenever ERP was unreachable
or returned an unclassified head-count — meaning an ERP outage could put
invented numbers into a regulatory filing. That table is deliberately NOT
ported. ERP is the owner of Section G; if it cannot answer, the section
degrades to ``{"available": False, ...}`` like any other unreachable upstream,
and the officer sees a gap rather than a fiction. The "is the head-count
actually classified?" quality check is kept — but it now degrades instead of
substituting.

**① is owned here, not fetched.** CRM built ① from its own tickets. Sub builds
it from native tickets via ``ncc_complaints_report``.

External/optional sections degrade gracefully — an unavailable upstream carries
``{"available": False, "error": ...}`` rather than failing the whole pack, so
the officer can always see exactly which section is missing.

Only the annual return's narrative pages (③'s free-text) stay manual.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


# ── ① Complaints (native sub tickets) ───────────────────────────────────────
def complaints_section(
    db: Session, start_dt: datetime, end_dt: datetime
) -> dict[str, Any]:
    """Summarise the NCC quarterly complaints return (①) from sub's tickets.

    Delegates to ``ncc_complaints_report.build_report``, which owns the record
    building, NCC categorisation and SLA derivation, so the pack and the filed
    workbook always agree. The report's own keys are merged flat into the
    section (matching the pack's section contract).
    """
    period = {"start": start_dt.isoformat(), "end": end_dt.isoformat()}
    try:
        from app.services import ncc_complaints_report
    except ImportError as exc:  # pragma: no cover - transitional
        logger.warning("NCC pack: complaints section unavailable: %s", exc)
        return {
            "available": False,
            "error": f"native complaints report is not available: {exc}",
            "period": period,
        }

    try:
        report = ncc_complaints_report.build_report(db, start=start_dt, end=end_dt)
    except Exception as exc:
        logger.warning("NCC pack: complaints section unavailable: %s", exc)
        return {"available": False, "error": str(exc), "period": period}

    if not report:
        return {
            "available": False,
            "error": "native complaints report returned no data",
            "period": period,
        }

    section: dict[str, Any] = {"available": True, "period": period}
    if isinstance(report, dict):
        section.update({k: v for k, v in report.items() if k != "available"})
    return section


# ── ② Subscribers & Capacity (native) ───────────────────────────────────────
def subscribers_section(
    db: Session,
    *,
    as_of: str | None = None,
    statuses: str | None = None,
    reseller_id: str | None = None,
    capacity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the NCC subscriber/capacity aggregate (②) from sub's own data.

    Native — no HTTP hop, no second session, and no re-normalisation: the
    report service already applies the pack's state/region adjustments.
    """
    from app.services import ncc_subscriber_report

    try:
        params = ncc_subscriber_report.parse_report_params(
            as_of=as_of,
            statuses=statuses,
            reseller_id=reseller_id,
            capacity=capacity or {},
        )
        report = ncc_subscriber_report.build_ncc_subscriber_report(db, params)
        if not report:
            return {
                "available": False,
                "error": "subscriber report returned no data",
            }
        return {"available": True, "report": report}
    except Exception as exc:
        logger.warning("NCC pack: subscriber section unavailable: %s", exc)
        return {"available": False, "error": str(exc)}


# ── ③ Year-End Section F/G (dotmac_erp) ─────────────────────────────────────
def _erp_client(db: Session):
    """An enabled ERP capability client, or ``None`` when unavailable."""
    from app.services.integrations.erp_capability import capability_client

    try:
        return capability_client(db)
    except Exception:
        return None


def financials_section(db: Session, *, year: int | None = None) -> dict[str, Any]:
    """Fetch the NCC year-end Section F financials (③F) from dotmac_erp."""
    try:
        client = _erp_client(db)
        if client is None:
            return {"available": False, "error": "dotmac_erp is not configured"}
        with client:
            data = client.get_ncc_financials(year=year)
    except Exception as exc:
        logger.warning("NCC pack: financials section unavailable: %s", exc)
        return {"available": False, "error": str(exc)}

    if not data:
        return {"available": False, "error": "erp returned empty financials"}
    return {"available": True, "financials": data}


def _staff_has_classified_headcount(data: dict[str, Any]) -> bool:
    """True when ERP sent a non-zero Nigerian head-count in some category."""
    for nationalities in (data.get("by_category") or {}).values():
        if not isinstance(nationalities, dict):
            continue
        nigerian = nationalities.get("nigerian") or {}
        if not isinstance(nigerian, dict):
            continue
        for gender in ("male", "female", "other"):
            try:
                if int(nigerian.get(gender) or 0) > 0:
                    return True
            except (TypeError, ValueError):
                continue
    return False


def staff_section(db: Session) -> dict[str, Any]:
    """Fetch the NCC year-end Section G staff head-count (③G) from dotmac_erp.

    ERP owns Section G. When it cannot answer — unreachable, unconfigured,
    empty, or an unclassified head-count — this degrades to
    ``available: False``. It never substitutes a stand-in figure: a regulatory
    filing must carry ERP's real numbers or visibly carry none (see the module
    docstring's note on CRM's removed fallback table).
    """
    try:
        client = _erp_client(db)
        if client is None:
            return {"available": False, "error": "dotmac_erp is not configured"}
        with client:
            data = client.get_ncc_staff_headcount()
    except Exception as exc:
        logger.warning("NCC pack: staff section unavailable: %s", exc)
        return {"available": False, "error": str(exc)}

    if not data:
        return {"available": False, "error": "erp returned an empty staff head-count"}
    if not _staff_has_classified_headcount(data):
        return {
            "available": False,
            "error": (
                "erp returned no classified Nigerian head-count; Section G "
                "cannot be filed from this response"
            ),
        }
    return {"available": True, "staff": data}


# ── The pack ────────────────────────────────────────────────────────────────
def build_regulatory_pack(
    db: Session,
    *,
    start_dt: datetime,
    end_dt: datetime,
    as_of: str | None = None,
    year: int | None = None,
    statuses: str | None = None,
    reseller_id: str | None = None,
    capacity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble the full NCC regulatory pack.

    ``start_dt``/``end_dt`` bound the quarterly complaints return; ``as_of`` is
    the subscriber period-end; ``year`` selects the annual financials year.
    Sections degrade individually so the pack always returns, and ``meta``
    reports exactly which ones made it.
    """
    complaints = complaints_section(db, start_dt, end_dt)
    subscribers = subscribers_section(
        db,
        as_of=as_of,
        statuses=statuses,
        reseller_id=reseller_id,
        capacity=capacity,
    )
    financials = financials_section(db, year=year)
    staff = staff_section(db)

    sources = {
        "complaints": complaints.get("available", False),
        "subscribers": subscribers.get("available", False),
        "financials": financials.get("available", False),
        "staff": staff.get("available", False),
    }
    return {
        "meta": {
            "period": {"start": start_dt.isoformat(), "end": end_dt.isoformat()},
            "as_of": as_of,
            "year": year,
            "sources": sources,
            "complete": all(sources.values()),
        },
        # ① Quarterly complaints
        "complaints": complaints,
        # ② Quarterly subscriber & capacity
        "subscribers": subscribers,
        # ③ Annual year-end (financials + staff; narrative pages remain manual)
        "financials": financials,
        "staff": staff,
    }
