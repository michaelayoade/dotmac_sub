"""Celery tasks for invoice PDF exports."""

from __future__ import annotations

import logging

from app.celery_app import celery_app
from app.services import billing_invoice_pdf as billing_invoice_pdf_service

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.invoice_pdf.generate_invoice_pdf_export")
def generate_invoice_pdf_export(export_id: str) -> dict:
    logger.info("Starting generate_invoice_pdf_export for export_id=%s", export_id)
    result = billing_invoice_pdf_service.process_export(export_id)
    logger.info("Completed generate_invoice_pdf_export for export_id=%s", export_id)
    return result
