"""Invoice PDF export service.

Queues invoice PDF exports, tracks status in DB, and renders PDF artifacts.
"""

from __future__ import annotations

import html
import logging
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models.billing import Invoice, InvoicePdfExport, InvoicePdfExportStatus
from app.models.subscription_engine import SettingValueType
from app.schemas.settings import DomainSettingUpdate
from app.services import domain_settings as domain_settings_service
from app.services.file_storage import file_uploads
from app.services.object_storage import (
    ObjectNotFoundError,
    StreamResult,
    get_s3_storage,
)

STALE_EXPORT_SECONDS = 20
INVOICE_PDF_CACHE_METRICS_KEY = "invoice_pdf_cache_metrics"
logger = logging.getLogger(__name__)


def _safe_invoice_number(invoice: Invoice) -> str:
    raw = (invoice.invoice_number or str(invoice.id)).strip()
    return re.sub(r"[^A-Za-z0-9._-]+", "-", raw).strip("-") or str(invoice.id)


def _display_account_name(invoice: Invoice) -> str:
    account = getattr(invoice, "account", None)
    if not account:
        return "Account"
    person = getattr(account, "person", None)
    if person:
        full_name = f"{(person.first_name or '').strip()} {(person.last_name or '').strip()}".strip()
        if full_name:
            return full_name
    organization = getattr(account, "organization", None)
    if organization and organization.name:
        return organization.name
    return "Account"


def _money(value: Decimal | None) -> str:
    amount = Decimal(str(value or Decimal("0.00")))
    return f"{amount:,.2f}"


def _format_date(value: datetime | None) -> str:
    if not value:
        return "N/A"
    return value.strftime("%Y-%m-%d")


def _render_invoice_html(invoice: Invoice) -> str:
    lines = [line for line in (invoice.lines or []) if getattr(line, "is_active", True)]
    rows = "".join(
        (
            "<tr>"
            f"<td>{html.escape(str(line.description or 'Line item'))}</td>"
            f"<td class='num'>{html.escape(str(line.quantity or Decimal('0')))}</td>"
            f"<td class='num'>N{_money(line.unit_price)}</td>"
            f"<td class='num'>N{_money(line.amount)}</td>"
            "</tr>"
        )
        for line in lines
    )
    if not rows:
        rows = "<tr><td colspan='4'>No line items</td></tr>"

    account_name = html.escape(_display_account_name(invoice))
    account_email = html.escape((getattr(invoice.account, "email", None) or "").strip() or "N/A")
    invoice_number = html.escape(invoice.invoice_number or str(invoice.id))
    memo = html.escape((invoice.memo or "").strip() or "-")

    return f"""
<!doctype html>
<html>
<head>
<meta charset=\"utf-8\">
<title>Invoice {invoice_number}</title>
<style>
  @page {{ size: A4; margin: 18mm; }}
  body {{ font-family: DejaVu Sans, Arial, sans-serif; color: #0f172a; font-size: 12px; }}
  .top {{ display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 16px; }}
  .h1 {{ font-size: 22px; font-weight: 700; margin: 0; }}
  .muted {{ color: #475569; }}
  .box {{ border: 1px solid #cbd5e1; border-radius: 8px; padding: 10px; margin-bottom: 12px; }}
  table {{ width: 100%; border-collapse: collapse; margin-top: 8px; }}
  th, td {{ border-bottom: 1px solid #e2e8f0; padding: 8px 6px; text-align: left; }}
  th {{ background: #f8fafc; font-size: 11px; text-transform: uppercase; color: #334155; }}
  .num {{ text-align: right; white-space: nowrap; }}
  .totals {{ margin-top: 10px; width: 45%; margin-left: auto; }}
  .totals td {{ border: none; padding: 4px 0; }}
  .total {{ font-size: 14px; font-weight: 700; }}
</style>
</head>
<body>
  <div class=\"top\">
    <div>
      <p class=\"h1\">Invoice</p>
      <p class=\"muted\">Invoice #: {invoice_number}</p>
      <p class=\"muted\">Status: {html.escape(invoice.status.value if invoice.status else 'draft')}</p>
    </div>
    <div class=\"muted\" style=\"text-align:right\">
      <div>Issued: {_format_date(invoice.issued_at)}</div>
      <div>Due: {_format_date(invoice.due_at)}</div>
      <div>Currency: {html.escape(invoice.currency or 'NGN')}</div>
    </div>
  </div>

  <div class=\"box\">
    <strong>Billed To</strong><br>
    {account_name}<br>
    {account_email}
  </div>

  <table>
    <thead>
      <tr>
        <th>Description</th>
        <th class=\"num\">Qty</th>
        <th class=\"num\">Unit Price</th>
        <th class=\"num\">Amount</th>
      </tr>
    </thead>
    <tbody>
      {rows}
    </tbody>
  </table>

  <table class=\"totals\">
    <tr><td>Subtotal</td><td class=\"num\">N{_money(invoice.subtotal)}</td></tr>
    <tr><td>Tax</td><td class=\"num\">N{_money(invoice.tax_total)}</td></tr>
    <tr class=\"total\"><td>Total</td><td class=\"num\">N{_money(invoice.total)}</td></tr>
    <tr><td>Balance Due</td><td class=\"num\">N{_money(invoice.balance_due)}</td></tr>
  </table>

  <div style=\"margin-top: 16px\" class=\"muted\">
    <strong>Memo:</strong> {memo}
  </div>
</body>
</html>
"""


def _pdf_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _build_simple_pdf(lines: list[str]) -> bytes:
    """Build a minimal single-page PDF with plain text lines."""
    safe_lines = [_pdf_escape(line)[:180] for line in lines[:55]]
    y = 800
    content_lines = ["BT", "/F1 11 Tf"]
    for line in safe_lines:
        content_lines.append(f"1 0 0 1 50 {y} Tm ({line}) Tj")
        y -= 14
        if y < 40:
            break
    content_lines.append("ET")
    content = "\n".join(content_lines).encode("latin-1", errors="replace")

    objects: list[bytes] = []
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>")
    objects.append(
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"
    )
    objects.append(b"<< /Length " + str(len(content)).encode("ascii") + b" >>\nstream\n" + content + b"\nendstream")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    pdf = bytearray()
    pdf.extend(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for i, obj in enumerate(objects, 1):
        offsets.append(len(pdf))
        pdf.extend(f"{i} 0 obj\n".encode("ascii"))
        pdf.extend(obj)
        pdf.extend(b"\nendobj\n")
    xref_start = len(pdf)
    pdf.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    pdf.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        pdf.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    pdf.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_start}\n%%EOF\n"
        ).encode("ascii")
    )
    return bytes(pdf)


def _render_invoice_text_lines(invoice: Invoice) -> list[str]:
    lines = [line for line in (invoice.lines or []) if getattr(line, "is_active", True)]
    out = [
        f"Invoice {invoice.invoice_number or invoice.id}",
        f"Status: {invoice.status.value if invoice.status else 'draft'}",
        f"Issued: {_format_date(invoice.issued_at)}    Due: {_format_date(invoice.due_at)}",
        f"Billed To: {_display_account_name(invoice)}",
        f"Email: {(getattr(invoice.account, 'email', None) or '').strip() or 'N/A'}",
        "",
        "Items:",
    ]
    if not lines:
        out.append("- No line items")
    else:
        for item in lines:
            qty = str(item.quantity or Decimal("0"))
            unit = _money(item.unit_price)
            amount = _money(item.amount)
            desc = str(item.description or "Line item")
            out.append(f"- {desc} | qty {qty} | unit N{unit} | amount N{amount}")
    out.extend(
        [
            "",
            f"Subtotal: N{_money(invoice.subtotal)}",
            f"Tax: N{_money(invoice.tax_total)}",
            f"Total: N{_money(invoice.total)}",
            f"Balance Due: N{_money(invoice.balance_due)}",
            f"Memo: {(invoice.memo or '').strip() or '-'}",
        ]
    )
    return out


def get_latest_export(db: Session, invoice_id: str) -> InvoicePdfExport | None:
    stmt = (
        select(InvoicePdfExport)
        .where(InvoicePdfExport.invoice_id == invoice_id)
        .order_by(InvoicePdfExport.created_at.desc())
        .limit(1)
    )
    return db.scalars(stmt).first()


def _is_export_fresh(invoice: Invoice, export: InvoicePdfExport) -> bool:
    if export.status != InvoicePdfExportStatus.completed:
        return False
    if not export.completed_at:
        return False
    invoice_updated = invoice.updated_at or invoice.created_at
    if invoice_updated and export.completed_at < invoice_updated:
        return False
    return True


def is_export_cache_valid(db: Session, invoice: Invoice, export: InvoicePdfExport | None) -> bool:
    if not export:
        return False
    if not _is_export_fresh(invoice, export):
        return False
    return export_file_exists(db, export)


def queue_export(
    db: Session,
    invoice_id: str,
    requested_by_id: str | None = None,
    *,
    force_new: bool = False,
) -> InvoicePdfExport:
    latest = get_latest_export(db, invoice_id)
    invoice = db.get(Invoice, invoice_id)

    if (
        not force_new
        and latest
        and invoice
        and latest.status == InvoicePdfExportStatus.completed
        and is_export_cache_valid(db, invoice, latest)
    ):
        return latest

    if latest and latest.status == InvoicePdfExportStatus.processing:
        return latest

    from app.tasks.invoice_pdf import generate_invoice_pdf_export

    if latest and latest.status == InvoicePdfExportStatus.queued:
        if not latest.celery_task_id:
            async_result = generate_invoice_pdf_export.delay(str(latest.id))
            latest.celery_task_id = str(async_result.id)
            db.commit()
            db.refresh(latest)
        return latest

    export = InvoicePdfExport(
        invoice_id=invoice_id,
        status=InvoicePdfExportStatus.queued,
        requested_by_id=requested_by_id,
    )
    db.add(export)
    db.commit()
    db.refresh(export)

    async_result = generate_invoice_pdf_export.delay(str(export.id))
    export.celery_task_id = str(async_result.id)
    db.commit()
    db.refresh(export)
    return export


def _get_cache_metrics(db: Session) -> dict[str, int]:
    default = {"hits": 0, "misses": 0, "generated": 0, "regenerated": 0}
    try:
        setting = domain_settings_service.billing_settings.get_by_key(
            db, INVOICE_PDF_CACHE_METRICS_KEY
        )
    except Exception:
        return default
    if isinstance(setting.value_json, dict):
        out = default.copy()
        for key in out:
            try:
                out[key] = int(setting.value_json.get(key) or 0)
            except (TypeError, ValueError):
                out[key] = 0
        return out
    return default


def _save_cache_metrics(db: Session, metrics: dict[str, int]) -> None:
    domain_settings_service.billing_settings.upsert_by_key(
        db,
        INVOICE_PDF_CACHE_METRICS_KEY,
        DomainSettingUpdate(
            value_type=SettingValueType.json,
            value_json=metrics,
            value_text=None,
            is_secret=False,
            is_active=True,
        ),
    )


def record_cache_hit(db: Session) -> None:
    metrics = _get_cache_metrics(db)
    metrics["hits"] = int(metrics.get("hits", 0)) + 1
    _save_cache_metrics(db, metrics)


def record_cache_miss(db: Session) -> None:
    metrics = _get_cache_metrics(db)
    metrics["misses"] = int(metrics.get("misses", 0)) + 1
    _save_cache_metrics(db, metrics)


def record_generated(db: Session, *, regenerated: bool = False) -> None:
    metrics = _get_cache_metrics(db)
    metrics["generated"] = int(metrics.get("generated", 0)) + 1
    if regenerated:
        metrics["regenerated"] = int(metrics.get("regenerated", 0)) + 1
    _save_cache_metrics(db, metrics)


def get_cache_dashboard_stats(db: Session) -> dict[str, Any]:
    completed = (
        db.query(InvoicePdfExport)
        .filter(InvoicePdfExport.status == InvoicePdfExportStatus.completed)
        .all()
    )
    unique_invoice_ids = {str(row.invoice_id) for row in completed}
    total_size_bytes = sum(int(row.file_size_bytes or 0) for row in completed)
    oldest_cached = min((row.completed_at for row in completed if row.completed_at), default=None)

    durations = [
        (row.completed_at - row.created_at).total_seconds()
        for row in completed
        if row.completed_at and row.created_at and row.completed_at >= row.created_at
    ]
    avg_generation_seconds = (sum(durations) / len(durations)) if durations else 0.0

    metrics = _get_cache_metrics(db)
    hits = int(metrics.get("hits", 0))
    misses = int(metrics.get("misses", 0))
    total_lookups = hits + misses
    hit_rate_pct = (hits / total_lookups * 100.0) if total_lookups > 0 else 0.0

    return {
        "total_cached_invoices": len(unique_invoice_ids),
        "cache_size_bytes": total_size_bytes,
        "oldest_cached_at": oldest_cached,
        "avg_generation_seconds": avg_generation_seconds,
        "hit_rate_pct": hit_rate_pct,
        "hits": hits,
        "misses": misses,
    }


def _invalidate_export_file(db: Session, export: InvoicePdfExport) -> None:
    record = file_uploads.get_active_entity_file(db, "invoice_pdf_export", str(export.id))
    if record:
        file_uploads.soft_delete(db=db, file=record, hard_delete_object=True)
    export.file_path = None
    export.file_size_bytes = None
    export.completed_at = None
    export.status = InvoicePdfExportStatus.failed
    export.error = "Cache invalidated"


def regenerate_invoice_cache(
    db: Session,
    *,
    invoice_id: str,
    requested_by_id: str | None = None,
) -> InvoicePdfExport:
    exports = (
        db.query(InvoicePdfExport)
        .filter(InvoicePdfExport.invoice_id == invoice_id)
        .order_by(InvoicePdfExport.created_at.desc())
        .all()
    )
    for row in exports:
        if row.status == InvoicePdfExportStatus.completed and row.file_path:
            _invalidate_export_file(db, row)
    db.commit()
    export = queue_export(
        db,
        invoice_id=invoice_id,
        requested_by_id=requested_by_id,
        force_new=True,
    )
    record_generated(db, regenerated=True)
    return export


def clear_cache(
    db: Session,
    *,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    account_id: str | None = None,
) -> dict[str, int]:
    query = db.query(InvoicePdfExport).join(Invoice, Invoice.id == InvoicePdfExport.invoice_id)
    query = query.filter(InvoicePdfExport.status == InvoicePdfExportStatus.completed)
    if date_from:
        query = query.filter(InvoicePdfExport.completed_at >= date_from)
    if date_to:
        query = query.filter(InvoicePdfExport.completed_at < date_to)
    if account_id:
        query = query.filter(Invoice.account_id == account_id)

    rows = query.all()
    invalidated = 0
    bytes_cleared = 0
    for row in rows:
        bytes_cleared += int(row.file_size_bytes or 0)
        _invalidate_export_file(db, row)
        invalidated += 1
    db.commit()
    return {"invalidated": invalidated, "bytes_cleared": bytes_cleared}


def export_file_exists(db: Session, export: InvoicePdfExport | None) -> bool:
    if not export or export.status != InvoicePdfExportStatus.completed or not export.file_path:
        return False
    record = file_uploads.get_active_entity_file(db, "invoice_pdf_export", str(export.id))
    if record:
        try:
            file_uploads.stream_file(record)
            return True
        except ObjectNotFoundError:
            return False
    local_path = Path(export.file_path)
    if local_path.exists():
        return True
    try:
        return get_s3_storage().exists(export.file_path)
    except Exception:
        return False


def maybe_finalize_stalled_export(
    db: Session,
    export: InvoicePdfExport | None,
) -> InvoicePdfExport | None:
    """Best-effort inline completion for stale or missing export artifacts."""
    if not export:
        return None

    should_process_inline = False
    if export.status == InvoicePdfExportStatus.completed and not export_file_exists(db, export):
        should_process_inline = True
    elif export.status in (InvoicePdfExportStatus.queued, InvoicePdfExportStatus.processing):
        marker = export.updated_at or export.created_at
        if marker:
            age_seconds = (datetime.now(UTC) - marker).total_seconds()
            should_process_inline = age_seconds >= STALE_EXPORT_SECONDS

    if not should_process_inline:
        return export

    logger.warning(
        "INLINE_INVOICE_PDF_FALLBACK export_id=%s status=%s",
        export.id,
        export.status.value,
    )
    process_export(str(export.id))
    db.expire_all()
    return db.get(InvoicePdfExport, export.id)


def download_filename(invoice: Invoice) -> str:
    return f"invoice-{_safe_invoice_number(invoice)}.pdf"


def _invoice_org_id(invoice: Invoice):
    account = getattr(invoice, "account", None)
    return getattr(account, "organization_id", None) if account else None


def _build_pdf_bytes(invoice: Invoice) -> bytes:
    html_content = _render_invoice_html(invoice)
    try:
        from weasyprint import HTML

        return HTML(string=html_content).write_pdf()
    except Exception as exc:
        logger.warning(
            "WeasyPrint export failed for invoice %s; using simple PDF fallback: %s",
            invoice.id,
            exc,
        )
        return _build_simple_pdf(_render_invoice_text_lines(invoice))


def _stream_local_file(path: Path) -> StreamResult:
    def _chunks() -> Iterator[bytes]:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                yield chunk

    return StreamResult(
        chunks=_chunks(),
        content_type="application/pdf",
        content_length=path.stat().st_size,
    )


def stream_export(db: Session, export: InvoicePdfExport) -> StreamResult:
    if not export.file_path:
        raise ObjectNotFoundError("missing export file path")
    local_path = Path(export.file_path)
    if local_path.exists():
        return _stream_local_file(local_path)

    record = file_uploads.get_active_entity_file(db, "invoice_pdf_export", str(export.id))
    if record:
        return file_uploads.stream_file(record)

    return get_s3_storage().stream(export.file_path)


def process_export(export_id: str) -> dict[str, Any]:
    db = SessionLocal()
    export: InvoicePdfExport | None = None
    started_at = datetime.now(UTC)
    try:
        export = db.get(InvoicePdfExport, export_id)
        if not export:
            return {"status": "missing", "export_id": export_id}

        export.status = InvoicePdfExportStatus.processing
        export.error = None
        export.completed_at = None
        db.commit()

        invoice = db.get(Invoice, export.invoice_id)
        if not invoice:
            export.status = InvoicePdfExportStatus.failed
            export.error = "Invoice not found"
            export.completed_at = datetime.now(UTC)
            db.commit()
            return {"status": "failed", "reason": "invoice_not_found", "export_id": export_id}

        # Load related data used by renderer.
        _ = invoice.account
        _ = invoice.lines

        existing_record = file_uploads.get_active_entity_file(
            db, "invoice_pdf_export", str(export.id)
        )
        if existing_record:
            file_uploads.soft_delete(db=db, file=existing_record, hard_delete_object=True)

        pdf_bytes = _build_pdf_bytes(invoice)
        uploaded = file_uploads.upload(
            db=db,
            domain="generated_docs",
            entity_type="invoice_pdf_export",
            entity_id=str(export.id),
            original_filename=download_filename(invoice),
            content_type="application/pdf",
            data=pdf_bytes,
            uploaded_by=str(export.requested_by_id) if export.requested_by_id else None,
            organization_id=_invoice_org_id(invoice),
        )

        export.status = InvoicePdfExportStatus.completed
        export.file_path = uploaded.storage_key_or_relative_path
        export.file_size_bytes = uploaded.file_size
        export.completed_at = datetime.now(UTC)
        export.error = None
        db.commit()
        record_generated(db)
        return {
            "status": "completed",
            "export_id": export_id,
            "invoice_id": str(invoice.id),
            "file_path": uploaded.storage_key_or_relative_path,
            "duration_ms": int((datetime.now(UTC) - started_at).total_seconds() * 1000),
        }
    except Exception as exc:
        try:
            db.rollback()
        except Exception:
            logger.exception("Failed to rollback PDF export transaction.")

        try:
            failed_export = db.get(InvoicePdfExport, export_id)
            if failed_export is not None:
                failed_export.status = InvoicePdfExportStatus.failed
                failed_export.error = str(exc)
                failed_export.completed_at = datetime.now(UTC)
                db.commit()
        except Exception:
            logger.exception("Failed to persist invoice PDF export failure state.")
            try:
                db.rollback()
            except Exception:
                logger.exception("Failed to rollback after failure-state write error.")
        return {
            "status": "failed",
            "export_id": export_id,
            "reason": str(exc),
        }
    finally:
        db.close()
