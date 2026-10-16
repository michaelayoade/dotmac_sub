"""Declared Celery queue names shared with source-only deployment probes."""

from typing import Final

DEPLOYMENT_QUEUE_NAMES: Final[tuple[str, ...]] = (
    "celery",
    "notifications_immediate",
    "notifications",
    "nin",
    "tr069",
    "acs",
    "bandwidth",
    "monitoring",
    "ingestion",
    "crm",
    "billing",
)
