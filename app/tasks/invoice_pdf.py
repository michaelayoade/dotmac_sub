"""Celery tasks for invoice PDF exports."""

from __future__ import annotations

from app.celery_app import celery_app
from app.services import billing_invoice_pdf as billing_invoice_pdf_service


@celery_app.task(name="app.tasks.invoice_pdf.generate_invoice_pdf_export")
def generate_invoice_pdf_export(export_id: str):
    return billing_invoice_pdf_service.process_export(export_id)
