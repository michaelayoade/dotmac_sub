import logging

from app.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.snmp.discover_interfaces")
def discover_interfaces() -> dict[str, int]:
    logger.info("Skipping SNMP interface discovery: monitoring is managed by Zabbix")
    return {"skipped": 1}


@celery_app.task(name="app.tasks.snmp.walk_interfaces")
def walk_interfaces() -> dict[str, int]:
    logger.info("Skipping SNMP interface walk: monitoring is managed by Zabbix")
    return {"skipped": 1}
