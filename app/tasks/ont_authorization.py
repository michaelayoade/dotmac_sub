"""Background ONT authorization follow-up tasks."""

from __future__ import annotations

import logging

from app.celery_app import celery_app
from app.services.db_session_adapter import db_session_adapter
from app.services.task_idempotency import idempotent_task

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.ont_authorization.run_post_authorization_follow_up")
@idempotent_task(key_func=lambda operation_id, ont_unit_id, **kw: f"{ont_unit_id}")
def run_post_authorization_follow_up_task(
    operation_id: str,
    ont_unit_id: str,
    olt_id: str,
    fsp: str,
    serial_number: str,
    ont_id_on_olt: int,
) -> dict[str, object]:
    """Run non-critical reconciliation after foreground OLT authorization succeeds."""
    from app.services.network.ont_authorization import (
        run_post_authorization_follow_up,
    )
    from app.services.network_operations import network_operations

    db = db_session_adapter.create_session()
    try:
        network_operations.mark_running(db, operation_id)
        db.commit()

        try:
            success, message, steps = run_post_authorization_follow_up(
                db,
                ont_unit_id=ont_unit_id,
                olt_id=olt_id,
                fsp=fsp,
                serial_number=serial_number,
                ont_id_on_olt=ont_id_on_olt,
            )
            payload = {"message": message, "steps": steps}
            if success:
                network_operations.mark_succeeded(
                    db,
                    operation_id,
                    output_payload=payload,
                )
            else:
                network_operations.mark_failed(
                    db,
                    operation_id,
                    message,
                    output_payload=payload,
                )
            db.commit()
            return {"success": success, "message": message, "steps": steps}
        except Exception as exc:
            logger.error(
                "Post-authorization follow-up failed for ONT %s: %s",
                ont_unit_id,
                exc,
                exc_info=True,
            )
            network_operations.mark_failed(db, operation_id, str(exc))
            db.commit()
            raise
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

