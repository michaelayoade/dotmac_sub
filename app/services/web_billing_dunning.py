"""Service helpers for billing dunning web routes."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from fastapi import HTTPException
from sqlalchemy.orm import selectinload

from app.models.collections import (
    DunningActionLog,
    DunningCase,
    DunningCaseStatus,
    FinancialAccessConsequence,
)
from app.services import collections as collections_service
from app.services import web_billing_customers as web_billing_customers_service
from app.services.audit_helpers import log_audit_event

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class BulkDunningActionResult:
    """Outcome for a bulk dunning action."""

    selected_ids: list[str]
    processed_ids: list[str] = field(default_factory=list)
    failed_ids: list[str] = field(default_factory=list)

    @property
    def selected(self) -> int:
        return len(self.selected_ids)

    @property
    def processed(self) -> int:
        return len(self.processed_ids)

    @property
    def failed(self) -> int:
        return len(self.failed_ids)

    @property
    def skipped(self) -> int:
        return max(0, self.selected - self.processed - self.failed)

    def message(self, action: str) -> str:
        label = {
            "pause": "Paused",
            "resume": "Resumed",
            "close": "Closed",
        }.get(action, "Processed")
        noun = "case" if self.selected == 1 else "cases"
        message = f"{label} {self.processed} of {self.selected} selected dunning {noun}"
        details = []
        if self.skipped:
            details.append(f"{self.skipped} skipped")
        if self.failed:
            details.append(f"{self.failed} failed")
        if details:
            message += f"; {', '.join(details)}"
        return message


def build_listing_data(
    db,
    *,
    page: int,
    per_page: int = 50,
    status: str | None,
    customer_ref: str | None,
) -> dict[str, object]:
    """Build paginated listing data and status counts for dunning page."""
    offset = (page - 1) * per_page

    account_ids = []
    customer_filtered = bool(customer_ref)
    if customer_ref:
        account_ids = [
            item["id"]
            for item in web_billing_customers_service.accounts_for_customer(
                db, customer_ref
            )
        ]

    if customer_filtered and not account_ids:
        status_counts = {
            "open": 0,
            "paused": 0,
            "resolved": 0,
            "closed": 0,
        }
    else:
        status_query = db.query(DunningCase)
        if account_ids:
            status_query = status_query.filter(DunningCase.account_id.in_(account_ids))
        status_counts = {
            "open": status_query.filter(
                DunningCase.status == DunningCaseStatus.open
            ).count(),
            "paused": status_query.filter(
                DunningCase.status == DunningCaseStatus.paused
            ).count(),
            "resolved": status_query.filter(
                DunningCase.status == DunningCaseStatus.resolved
            ).count(),
            "closed": status_query.filter(
                DunningCase.status == DunningCaseStatus.closed
            ).count(),
        }

    cases = []
    total = 0
    total_pages = 1
    if account_ids:
        query = db.query(DunningCase).filter(DunningCase.account_id.in_(account_ids))
        if status:
            query = query.filter(DunningCase.status == status)
        total = query.count()
        total_pages = (total + per_page - 1) // per_page if total > 0 else 1
        cases = (
            query.order_by(DunningCase.created_at.desc())
            .offset(offset)
            .limit(per_page)
            .all()
        )
    elif not customer_filtered:
        count_query = db.query(DunningCase)
        if status:
            count_query = count_query.filter(DunningCase.status == status)
        total = count_query.count()
        total_pages = (total + per_page - 1) // per_page if total > 0 else 1
        cases = collections_service.dunning_cases.list(
            db=db,
            account_id=None,
            status=status,
            order_by="created_at",
            order_dir="desc",
            limit=per_page,
            offset=offset,
        )

    return {
        "cases": cases,
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
        "status": status,
        "status_counts": status_counts,
        "customer_ref": customer_ref,
    }


def build_detail_data(db, *, case_id: str) -> dict[str, object]:
    case = collections_service.dunning_cases.get(db, case_id)
    actions = (
        db.query(DunningActionLog)
        .filter(DunningActionLog.case_id == case.id)
        .options(
            selectinload(DunningActionLog.access_consequence).selectinload(
                FinancialAccessConsequence.evidence
            )
        )
        .order_by(DunningActionLog.executed_at.desc())
        .limit(100)
        .all()
    )
    return {
        "case": case,
        "account": case.subscriber,
        "actions": actions,
    }


def apply_case_action(db, *, case_id: str, action: str) -> None:
    """Apply a single dunning-case action."""
    if action == "pause":
        collections_service.dunning_cases.pause(db=db, case_id=case_id)
        return
    if action == "resume":
        collections_service.dunning_cases.resume(db=db, case_id=case_id)
        return
    if action == "close":
        collections_service.dunning_cases.close(db=db, case_id=case_id)
        return
    raise ValueError("Unsupported action")


def apply_bulk_action(db, *, case_ids_csv: str, action: str) -> list[str]:
    """Apply dunning action for many IDs; return IDs successfully processed."""
    processed: list[str] = []
    for case_id in [item.strip() for item in case_ids_csv.split(",") if item.strip()]:
        try:
            apply_case_action(db, case_id=case_id, action=action)
            processed.append(case_id)
        except Exception:
            logger.debug(
                "Skipping dunning bulk action for case %s", case_id, exc_info=True
            )
            continue
    return processed


def apply_bulk_action_result(
    db, *, case_ids_csv: str, action: str
) -> BulkDunningActionResult:
    """Apply a dunning action for many IDs and report per-item outcome counts."""
    case_ids = [item.strip() for item in case_ids_csv.split(",") if item.strip()]
    result = BulkDunningActionResult(selected_ids=case_ids)
    for case_id in case_ids:
        try:
            apply_case_action(db, case_id=case_id, action=action)
            result.processed_ids.append(case_id)
        except HTTPException as exc:
            if exc.status_code >= 500:
                db.rollback()
            logger.debug(
                "Skipping dunning bulk action for case %s", case_id, exc_info=True
            )
            result.failed_ids.append(case_id)
        except Exception:
            db.rollback()
            logger.debug(
                "Skipping dunning bulk action for case %s", case_id, exc_info=True
            )
            result.failed_ids.append(case_id)
    return result


def execute_action(
    db,
    *,
    action: str,
    case_id: str | None = None,
    case_ids_csv: str | None = None,
) -> list[str]:
    """Execute single/bulk dunning action and return processed IDs."""
    if case_id:
        apply_case_action(db, case_id=case_id, action=action)
        return [case_id]
    if case_ids_csv is not None:
        return apply_bulk_action(db, case_ids_csv=case_ids_csv, action=action)
    raise ValueError("case_id or case_ids_csv is required")


def execute_bulk_action_result(
    db,
    *,
    action: str,
    case_ids_csv: str,
) -> BulkDunningActionResult:
    """Execute a bulk dunning action and return a full outcome."""
    return apply_bulk_action_result(db, case_ids_csv=case_ids_csv, action=action)


def execute_action_with_audit(
    db,
    *,
    request,
    action: str,
    actor_id: str | None,
    case_id: str | None = None,
    case_ids_csv: str | None = None,
) -> list[str]:
    processed_ids = execute_action(
        db,
        action=action,
        case_id=case_id,
        case_ids_csv=case_ids_csv,
    )
    for processed_id in processed_ids:
        log_audit_event(
            db=db,
            request=request,
            action=action,
            entity_type="dunning_case",
            entity_id=processed_id,
            actor_id=actor_id,
        )
    return processed_ids


def execute_bulk_action_with_audit_result(
    db,
    *,
    request,
    action: str,
    actor_id: str | None,
    case_ids_csv: str,
) -> BulkDunningActionResult:
    """Execute a bulk dunning action and audit successfully processed cases."""
    result = execute_bulk_action_result(
        db,
        action=action,
        case_ids_csv=case_ids_csv,
    )
    for processed_id in result.processed_ids:
        log_audit_event(
            db=db,
            request=request,
            action=action,
            entity_type="dunning_case",
            entity_id=processed_id,
            actor_id=actor_id,
        )
    return result
