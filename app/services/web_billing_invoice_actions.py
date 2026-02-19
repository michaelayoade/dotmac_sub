"""Service helpers for billing invoice action routes."""

from __future__ import annotations

from datetime import datetime, timezone


def html_notice(message: str) -> str:
    return (
        '<div class="rounded-lg border border-slate-200 bg-white px-4 py-3 text-sm text-slate-600 '
        'shadow-sm dark:border-slate-700 dark:bg-slate-800 dark:text-slate-300">'
        f"{message}"
        "</div>"
    )


def pdf_message(invoice_id) -> str:
    return html_notice(f"PDF generation queued for invoice {invoice_id}.")


def send_message(invoice_id) -> str:
    return html_notice(f"Invoice {invoice_id} send queued.")


def void_message(invoice_id) -> str:
    return html_notice(f"Invoice {invoice_id} void queued.")


def batch_today_str() -> str:
    return datetime.now(timezone.utc).date().strftime("%Y-%m-%d")
