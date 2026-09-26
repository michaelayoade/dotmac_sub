"""Typed API adapter for registered custom-field target values."""

from __future__ import annotations

from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas.custom_fields import (
    CustomFieldValueItem,
    CustomFieldValueList,
    CustomFieldValueResult,
    CustomFieldValueWrite,
)
from app.services import custom_field_access, custom_fields
from app.services.auth_dependencies import load_permission_keys, require_permission
from app.services.db_session_adapter import db_session_adapter
from app.services.operator_tenant import OPERATOR_TENANT_ID
from app.services.owner_commands import CommandContext

router = APIRouter(prefix="/custom-fields", tags=["custom-fields"])


def _actor(auth: dict) -> str:
    principal_id = str(auth.get("principal_id") or "").strip()
    if not principal_id:
        raise HTTPException(status_code=403, detail="Authorized actor is missing")
    principal_type = "api_key" if auth.get("principal_type") == "api_key" else "user"
    return f"{principal_type}:{principal_id}"


def _status(exc: custom_fields.CustomFieldError) -> int:
    suffix = exc.code.rsplit(".", 1)[-1]
    if suffix == "permission_denied":
        return 403
    if suffix in {"not_found", "target_not_found"}:
        return 404
    if suffix in {"status_conflict", "active_definition_locked", "key_conflict"}:
        return 409
    return 422


@router.get(
    "/{target_type}/{target_id}",
    response_model=CustomFieldValueList,
)
def list_custom_field_values(
    target_type: str,
    target_id: UUID,
    db: Session = Depends(get_db),
    auth: dict = Depends(require_permission(custom_fields.VALUE_READ_PERMISSION)),
) -> CustomFieldValueList:
    if not custom_field_access.target_access_allowed(
        db, auth=auth, target_type=target_type, target_id=target_id, write=False
    ):
        raise HTTPException(status_code=403, detail="Forbidden for this record")
    try:
        rows = custom_fields.list_target_values(
            db,
            tenant_id=OPERATOR_TENANT_ID,
            target_type=target_type,
            target_id=target_id,
            permission_keys=load_permission_keys(auth, db),
        )
    except custom_fields.CustomFieldError as exc:
        raise HTTPException(status_code=_status(exc), detail=exc.message) from exc
    return CustomFieldValueList(
        target_type=target_type,
        target_id=target_id,
        items=[
            CustomFieldValueItem(
                definition_id=row.definition.id,
                key=row.definition.key,
                label=row.definition.label,
                field_type=row.definition.field_type,
                section=row.definition.section,
                required=row.definition.required,
                sensitive=row.definition.sensitive,
                redacted=row.redacted,
                value=row.value,
            )
            for row in rows
        ],
    )


@router.put(
    "/{target_type}/{target_id}",
    response_model=CustomFieldValueResult,
)
def set_custom_field_value(
    target_type: str,
    target_id: UUID,
    payload: CustomFieldValueWrite,
    db: Session = Depends(get_db),
    auth: dict = Depends(require_permission(custom_fields.VALUE_WRITE_PERMISSION)),
) -> CustomFieldValueResult:
    if not custom_field_access.target_access_allowed(
        db, auth=auth, target_type=target_type, target_id=target_id, write=True
    ):
        raise HTTPException(status_code=403, detail="Forbidden for this record")
    permissions = load_permission_keys(auth, db)
    command_id = uuid4()
    db_session_adapter.release_read_transaction(db)
    try:
        outcome = custom_fields.set_value(
            db,
            custom_fields.SetCustomFieldValueCommand(
                tenant_id=OPERATOR_TENANT_ID,
                definition_id=payload.definition_id,
                target_type=target_type,
                target_id=target_id,
                value=payload.value,
                permission_keys=permissions,
                context=CommandContext(
                    command_id=command_id,
                    correlation_id=command_id,
                    actor=_actor(auth),
                    scope=custom_fields.VALUE_WRITE_PERMISSION,
                    reason=f"Set custom-field value for {target_type}:{target_id}",
                    idempotency_key=(
                        f"custom-field-value:{target_type}:{target_id}:"
                        f"{payload.definition_id}:{command_id}"
                    ),
                ),
            ),
        )
    except custom_fields.CustomFieldError as exc:
        raise HTTPException(status_code=_status(exc), detail=exc.message) from exc
    return CustomFieldValueResult(
        definition_id=outcome.definition_id,
        target_id=outcome.target_id,
        cleared=outcome.cleared,
    )


__all__ = ["router"]
