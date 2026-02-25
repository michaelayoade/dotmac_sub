"""Service helpers for billing invoice batch routes."""

from __future__ import annotations

import csv
from datetime import UTC, datetime
from decimal import Decimal
import io
from uuid import UUID

from app.models.billing import BillingRunSchedule, BillingRunStatus
from app.models.domain_settings import SettingDomain
from app.models.subscription_engine import SettingValueType
from app.schemas.settings import DomainSettingUpdate
from app.services import billing as billing_service
from app.services import billing_automation as billing_automation_service
from app.services import domain_settings as domain_settings_service


def parse_billing_cycle(value: str | None, parse_cycle_fn):
    return parse_cycle_fn(value)


def run_batch(db, *, billing_cycle: str | None, parse_cycle_fn) -> str:
    """Run invoice cycle and return user-facing note."""
    try:
        summary = billing_automation_service.run_invoice_cycle(
            db=db,
            billing_cycle=parse_billing_cycle(billing_cycle, parse_cycle_fn),
            dry_run=False,
        )
        return (
            "Batch run completed. "
            f"Invoices created: {summary.get('invoices_created', 0)} · "
            f"Subscriptions billed: {summary.get('subscriptions_billed', 0)} · "
            f"Skipped: {summary.get('skipped', 0)}."
        )
    except Exception as exc:
        return f"Batch run failed: {exc}"


def retry_batch_run(db, *, run_id: str, parse_cycle_fn) -> str:
    """Retry a previous billing run using its billing cycle."""
    run = billing_service.billing_runs.get(db, run_id)
    cycle_value = run.billing_cycle or None
    return run_batch(
        db,
        billing_cycle=cycle_value,
        parse_cycle_fn=parse_cycle_fn,
    )


def _parse_run_date(billing_date: str | None) -> datetime | None:
    if not billing_date:
        return None
    return datetime.strptime(billing_date, "%Y-%m-%d").replace(tzinfo=UTC)


def preview_batch(
    db,
    *,
    billing_cycle: str | None,
    billing_date: str | None,
    separate_by_partner: bool = False,
    parse_cycle_fn,
) -> dict[str, object]:
    """Run dry-run invoice preview and return JSON payload."""
    run_date = _parse_run_date(billing_date)

    summary = billing_automation_service.run_invoice_cycle(
        db=db,
        billing_cycle=parse_billing_cycle(billing_cycle, parse_cycle_fn),
        dry_run=True,
        run_at=run_date,
    )
    total_amount = summary.get("total_amount", Decimal("0.00"))
    subscriptions = summary.get("subscriptions", [])
    partner_preview: list[dict[str, object]] = []
    if separate_by_partner:
        from app.models.subscriber import Reseller, Subscriber

        account_ids = [s.get("account_id") for s in subscriptions if s.get("account_id")]
        subscribers = (
            db.query(Subscriber)
            .filter(Subscriber.id.in_(account_ids))
            .all()
            if account_ids
            else []
        )
        subscriber_by_id = {str(item.id): item for item in subscribers}
        reseller_ids = {
            str(item.reseller_id) for item in subscribers if getattr(item, "reseller_id", None)
        }
        reseller_by_id = {
            str(item.id): item
            for item in db.query(Reseller).filter(Reseller.id.in_(reseller_ids)).all()
        } if reseller_ids else {}

        grouped: dict[str, dict[str, object]] = {}
        for sub in subscriptions:
            account_id = str(sub.get("account_id") or "")
            subscriber = subscriber_by_id.get(account_id)
            reseller_id = str(getattr(subscriber, "reseller_id", "") or "")
            partner_key = reseller_id or "direct"
            partner_name = (
                getattr(reseller_by_id.get(reseller_id), "name", None)
                if reseller_id
                else "Direct"
            ) or "Direct"
            if partner_key not in grouped:
                grouped[partner_key] = {
                    "partner_id": partner_key,
                    "partner_name": partner_name,
                    "subscription_count": 0,
                    "invoice_count": 0,
                    "total_amount": Decimal("0.00"),
                }
            amount = Decimal(str(sub.get("amount", 0) or 0))
            grouped[partner_key]["subscription_count"] = int(grouped[partner_key]["subscription_count"]) + 1
            grouped[partner_key]["invoice_count"] = int(grouped[partner_key]["invoice_count"]) + 1
            grouped[partner_key]["total_amount"] = Decimal(str(grouped[partner_key]["total_amount"])) + amount

        partner_preview = [
            {
                "partner_id": item["partner_id"],
                "partner_name": item["partner_name"],
                "subscription_count": item["subscription_count"],
                "invoice_count": item["invoice_count"],
                "total_amount": float(item["total_amount"]),
                "total_amount_formatted": f"NGN {Decimal(str(item['total_amount'])):,.2f}",
            }
            for item in sorted(grouped.values(), key=lambda row: float(row["total_amount"]), reverse=True)
        ]

    return {
        "invoice_count": summary.get("invoices_created", 0),
        "account_count": summary.get(
            "accounts_affected",
            len(set(s.get("account_id") for s in subscriptions)),
        ),
        "total_amount": float(total_amount),
        "total_amount_formatted": f"NGN {total_amount:,.2f}",
        "subscriptions": [
            {
                "id": str(s.get("id", "")),
                "offer_name": s.get("offer_name", "Unknown"),
                "amount": float(s.get("amount", 0)),
                "amount_formatted": f"NGN {s.get('amount', 0):,.2f}",
            }
            for s in subscriptions[:50]
        ],
        "partner_preview": partner_preview,
    }


def run_batch_with_date(
    db,
    *,
    billing_cycle: str | None,
    billing_date: str | None,
    parse_cycle_fn,
) -> str:
    """Run invoice cycle honoring billing date when provided."""
    try:
        summary = billing_automation_service.run_invoice_cycle(
            db=db,
            billing_cycle=parse_billing_cycle(billing_cycle, parse_cycle_fn),
            dry_run=False,
            run_at=_parse_run_date(billing_date),
        )
        run_at = summary.get("run_at")
        run_at_text = run_at.strftime("%Y-%m-%d") if isinstance(run_at, datetime) else "today"
        return (
            f"Batch run completed for {run_at_text}. "
            f"Invoices created: {summary.get('invoices_created', 0)} · "
            f"Subscriptions billed: {summary.get('subscriptions_billed', 0)} · "
            f"Skipped: {summary.get('skipped', 0)}."
        )
    except Exception as exc:
        return f"Batch run failed: {exc}"


def preview_error_payload(exc: Exception) -> dict[str, object]:
    return {
        "error": str(exc),
        "invoice_count": 0,
        "account_count": 0,
        "total_amount_formatted": "NGN 0.00",
        "subscriptions": [],
    }


def _status_badge(status: BillingRunStatus | str | None) -> str:
    if isinstance(status, BillingRunStatus):
        status_key = status.value
    else:
        status_key = str(status or "")
    return {
        "success": "success",
        "failed": "danger",
        "running": "warning",
    }.get(status_key, "neutral")


def _run_status_text(status: BillingRunStatus | str | None) -> str:
    if isinstance(status, BillingRunStatus):
        return status.value.title()
    return str(status or "unknown").replace("_", " ").title()


def list_recent_runs(db, *, limit: int = 20) -> list[dict[str, object]]:
    runs = billing_service.billing_runs.list(
        db=db,
        status=None,
        order_by="created_at",
        order_dir="desc",
        limit=limit,
        offset=0,
    )
    return [
        {
            "id": str(run.id),
            "run_at": run.run_at,
            "created_at": run.created_at,
            "billing_cycle": run.billing_cycle or "all",
            "subscriptions_scanned": int(run.subscriptions_scanned or 0),
            "subscriptions_billed": int(run.subscriptions_billed or 0),
            "invoices_created": int(run.invoices_created or 0),
            "lines_created": int(run.lines_created or 0),
            "skipped": int(run.skipped or 0),
            "status": _run_status_text(run.status),
            "status_badge": _status_badge(run.status),
            "status_message": (
                run.error
                if run.error
                else (
                    "Transactions have been created"
                    if _run_status_text(run.status) == "Success"
                    else ("Run is currently processing" if _run_status_text(run.status) == "Running" else "—")
                )
            ),
            "error": run.error,
            "duration_seconds": (
                int((run.finished_at - run.started_at).total_seconds())
                if run.finished_at and run.started_at
                else None
            ),
        }
        for run in runs
    ]


def get_run_row(db, *, run_id: str) -> dict[str, object] | None:
    run = billing_service.billing_runs.get(db, run_id)
    if not run:
        return None
    status_text = _run_status_text(run.status)
    return {
        "id": str(run.id),
        "run_at": run.run_at,
        "created_at": run.created_at,
        "billing_cycle": run.billing_cycle or "all",
        "subscriptions_scanned": int(run.subscriptions_scanned or 0),
        "subscriptions_billed": int(run.subscriptions_billed or 0),
        "invoices_created": int(run.invoices_created or 0),
        "lines_created": int(run.lines_created or 0),
        "skipped": int(run.skipped or 0),
        "status": status_text,
        "status_badge": _status_badge(run.status),
        "status_message": (
            run.error
            if run.error
            else (
                "Transactions have been created"
                if status_text == "Success"
                else ("Run is currently processing" if status_text == "Running" else "—")
            )
        ),
        "error": run.error,
        "duration_seconds": (
            int((run.finished_at - run.started_at).total_seconds())
            if run.finished_at and run.started_at
            else None
        ),
    }


def render_runs_csv(rows: list[dict[str, object]]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "run_id",
            "run_at",
            "created_at",
            "billing_cycle",
            "subscriptions_scanned",
            "subscriptions_billed",
            "invoices_created",
            "lines_created",
            "skipped",
            "status",
            "status_message",
            "duration_seconds",
            "error",
        ]
    )
    for row in rows:
        writer.writerow(
            [
                row.get("id", ""),
                row.get("run_at").isoformat() if row.get("run_at") else "",
                row.get("created_at").isoformat() if row.get("created_at") else "",
                row.get("billing_cycle", ""),
                row.get("subscriptions_scanned", 0),
                row.get("subscriptions_billed", 0),
                row.get("invoices_created", 0),
                row.get("lines_created", 0),
                row.get("skipped", 0),
                row.get("status", ""),
                row.get("status_message", ""),
                row.get("duration_seconds", ""),
                row.get("error", ""),
            ]
        )
    return buffer.getvalue()


def render_single_run_csv(row: dict[str, object]) -> str:
    return render_runs_csv([row])


def _default_schedule_config() -> dict[str, object]:
    return {
        "enabled": False,
        "run_day": 1,
        "run_time": "02:00",
        "timezone": "UTC",
        "billing_cycle": "monthly",
        "partner_ids": [],
    }


def _coerce_schedule_config(raw: object) -> dict[str, object]:
    default = _default_schedule_config()
    if not isinstance(raw, dict):
        return default
    run_day = raw.get("run_day", default["run_day"])
    try:
        run_day_value = int(run_day)
    except (TypeError, ValueError):
        run_day_value = int(default["run_day"])
    run_day_value = max(1, min(run_day_value, 28))
    partner_ids_raw = raw.get("partner_ids", [])
    if not isinstance(partner_ids_raw, list):
        partner_ids_raw = []
    partner_ids = [str(item).strip() for item in partner_ids_raw if str(item).strip()]
    return {
        "enabled": bool(raw.get("enabled", default["enabled"])),
        "run_day": run_day_value,
        "run_time": str(raw.get("run_time") or default["run_time"]),
        "timezone": str(raw.get("timezone") or default["timezone"]),
        "billing_cycle": str(raw.get("billing_cycle") or default["billing_cycle"]),
        "partner_ids": partner_ids,
    }


def get_billing_run_schedule(db) -> dict[str, object]:
    schedule = db.query(BillingRunSchedule).order_by(BillingRunSchedule.created_at.desc()).first()
    if schedule:
        return _coerce_schedule_config(
            {
                "enabled": bool(schedule.enabled),
                "run_day": int(schedule.run_day or 1),
                "run_time": str(schedule.run_time or "02:00"),
                "timezone": str(schedule.timezone or "UTC"),
                "billing_cycle": str(schedule.billing_cycle or "monthly"),
                "partner_ids": list(schedule.partner_ids or []),
            }
        )
    try:
        setting = domain_settings_service.billing_settings.get_by_key(
            db,
            "billing_run_schedule_config",
        )
        return _coerce_schedule_config(setting.value_json)
    except Exception:
        return _default_schedule_config()


def save_billing_run_schedule(
    db,
    *,
    enabled: bool,
    run_day: str | int | None,
    run_time: str | None,
    timezone: str | None,
    billing_cycle: str | None,
    partner_ids: list[str] | None,
) -> dict[str, object]:
    parsed_partner_ids: list[str] = []
    for raw in partner_ids or []:
        value = (raw or "").strip()
        if not value:
            continue
        try:
            parsed_partner_ids.append(str(UUID(value)))
        except ValueError:
            continue

    try:
        run_day_value = int(str(run_day or "1"))
    except ValueError:
        run_day_value = 1
    run_day_value = max(1, min(run_day_value, 28))

    config = _coerce_schedule_config(
        {
            "enabled": enabled,
            "run_day": run_day_value,
            "run_time": (run_time or "02:00").strip() or "02:00",
            "timezone": (timezone or "UTC").strip() or "UTC",
            "billing_cycle": (billing_cycle or "monthly").strip() or "monthly",
            "partner_ids": parsed_partner_ids,
        }
    )
    schedule = db.query(BillingRunSchedule).order_by(BillingRunSchedule.created_at.desc()).first()
    if not schedule:
        schedule = BillingRunSchedule()
        db.add(schedule)
    schedule.enabled = bool(config["enabled"])
    schedule.run_day = int(config["run_day"])
    schedule.run_time = str(config["run_time"])
    schedule.timezone = str(config["timezone"])
    schedule.billing_cycle = str(config["billing_cycle"])
    schedule.partner_ids = list(config["partner_ids"])  # type: ignore[assignment]
    db.commit()

    domain_settings_service.billing_settings.upsert_by_key(
        db,
        "billing_run_schedule_config",
        DomainSettingUpdate(
            domain=SettingDomain.billing,
            key="billing_run_schedule_config",
            value_type=SettingValueType.json,
            value_json=config,
            is_active=True,
        ),
    )
    return config
