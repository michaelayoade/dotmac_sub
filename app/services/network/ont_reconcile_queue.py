"""Durable queue boundary for targeted Huawei ONT reconciliation."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.network import OLTDevice, OntUnit
from app.models.network_operation import (
    NetworkOperationTargetType,
    NetworkOperationType,
)
from app.services.network_operation_dispatch import (
    NetworkOperationCommand,
    NetworkOperationDispatchError,
    stage_dispatch,
)
from app.services.network_operations import network_operations


def queue_olt_acs_reconciliation(db: Session, olt: OLTDevice) -> dict[str, Any]:
    """Queue tracked read/plan/write/read reconciliation after ACS policy changes."""
    stats: dict[str, Any] = {
        "attempted": 0,
        "queued": 0,
        "duplicates": 0,
        "errors": 0,
        "operation_id": None,
    }
    if not olt.tr069_acs_server_id:
        return stats

    onts = list(
        db.scalars(
            select(OntUnit)
            .where(OntUnit.olt_device_id == olt.id)
            .where(OntUnit.is_active.is_(True))
        ).all()
    )
    stats["attempted"] = len(onts)
    if not onts:
        return stats

    acs_id = str(olt.tr069_acs_server_id)
    try:
        parent = network_operations.start(
            db,
            NetworkOperationType.olt_ont_sync,
            NetworkOperationTargetType.olt,
            str(olt.id),
            correlation_key=f"olt_acs_reconcile:{olt.id}:{acs_id}",
            input_payload={
                "reason": "olt_acs_assignment_changed",
                "acs_server_id": acs_id,
            },
            initiated_by="system",
        )
    except HTTPException as exc:
        if exc.status_code != 409:
            raise
        stats["duplicates"] = len(onts)
        return stats
    stats["operation_id"] = str(parent.id)

    created_children = 0
    for ont in onts:
        correlation_key = f"ont_desired_reconcile:{ont.id}"
        try:
            child = network_operations.start(
                db,
                NetworkOperationType.olt_ont_sync,
                NetworkOperationTargetType.ont,
                str(ont.id),
                correlation_key=correlation_key,
                input_payload={
                    "reason": "olt_acs_assignment_changed",
                    "acs_server_id": acs_id,
                },
                parent_id=str(parent.id),
                initiated_by="system",
            )
        except HTTPException as exc:
            if exc.status_code != 409:
                raise
            stats["duplicates"] += 1
            continue
        created_children += 1
        try:
            stage_dispatch(
                db,
                child,
                NetworkOperationCommand.ont_desired_reconcile_v1,
            )
            stats["queued"] += 1
        except NetworkOperationDispatchError as exc:
            stats["errors"] += 1
            network_operations.mark_failed(
                db,
                str(child.id),
                f"Unable to stage ONT reconciliation: {exc.message}",
            )

    if not created_children:
        network_operations.mark_succeeded(
            db,
            str(parent.id),
            output_payload={"message": "No new ONT reconciliations were queued."},
        )
        db.commit()
        return stats

    network_operations.update_parent_status(db, str(parent.id))
    db.commit()
    return stats
