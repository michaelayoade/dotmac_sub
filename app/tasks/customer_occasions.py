"""Session and clock adapter for bounded annual greeting admission."""

import logging
from datetime import UTC, datetime

from app.celery_app import celery_app
from app.services import customer_occasions
from app.services.customer_occasion_contracts import (
    CustomerOccasion,
    ProcessOccasionCommand,
)
from app.services.db_session_adapter import db_session_adapter
from app.services.owner_commands import CommandContext

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.customer_occasions.process_due_occasions")
def process_due_occasions() -> None:
    now = datetime.now(UTC)
    failures: list[Exception] = []
    for occasion in CustomerOccasion:
        try:
            with db_session_adapter.owner_command_session() as db:
                customer_occasions.process(
                    db=db,
                    command=ProcessOccasionCommand(
                        context=CommandContext.system(
                            actor="scheduler:customer_occasions",
                            scope=occasion.value,
                            reason="Daily birthday and anniversary evaluation",
                        ),
                        occasion=occasion,
                        now=now,
                    ),
                )
        except Exception as exc:
            failures.append(exc)
            logger.exception(
                "Customer occasion admission failed", extra={"occasion": occasion.value}
            )
    if failures:
        raise ExceptionGroup("Customer occasion admission failed", failures)
