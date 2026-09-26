"""Permission-gated Custom Fields Center administration routes."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from urllib.parse import quote_plus
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.csrf import get_csrf_token
from app.db import get_db
from app.models.custom_fields import CustomFieldDefinition, CustomFieldType
from app.services import custom_field_access, custom_fields, web_custom_fields
from app.services.auth_dependencies import (
    has_permission,
    load_permission_keys,
    require_permission,
)
from app.services.db_session_adapter import db_session_adapter
from app.services.operator_tenant import OPERATOR_TENANT_ID
from app.services.owner_commands import CommandContext

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/custom-fields", tags=["web-admin-custom-fields"])


def _base_context(request: Request, db: Session) -> dict[str, object]:
    from app.web.admin import get_current_user, get_sidebar_stats

    return {
        "request": request,
        "active_page": "custom-fields-center",
        "active_menu": "custom-fields-center",
        "page_title": "Custom Fields",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
        "csrf_token": get_csrf_token(request),
    }


def _permissions(auth: dict, db: Session) -> frozenset[str]:
    return load_permission_keys(auth, db)


def _actor(auth: dict) -> str:
    principal_id = str(auth.get("principal_id") or "").strip()
    if not principal_id:
        raise HTTPException(status_code=403, detail="Authorized actor is missing")
    principal_type = "api_key" if auth.get("principal_type") == "api_key" else "user"
    return f"{principal_type}:{principal_id}"


def _context(auth: dict, *, scope: str, reason: str) -> CommandContext:
    command_id = uuid4()
    return CommandContext(
        command_id=command_id,
        correlation_id=command_id,
        actor=_actor(auth),
        scope=scope,
        reason=reason,
        idempotency_key=f"custom-field:{scope}:{command_id}",
    )


def _can(auth: dict, db: Session, permission: str) -> bool:
    return has_permission(auth, db, permission)


def _center_state(
    request: Request,
    db: Session,
    auth: dict,
    *,
    selected_target: str | None = None,
    page_error: str | None = None,
    success: str | None = None,
) -> dict[str, object]:
    state = web_custom_fields.build_custom_fields_center_data(
        db,
        can_read_definitions=_can(auth, db, custom_fields.DEFINITION_READ_PERMISSION),
        can_create_definitions=_can(
            auth, db, custom_fields.DEFINITION_CREATE_PERMISSION
        ),
        can_update_definitions=_can(
            auth, db, custom_fields.DEFINITION_UPDATE_PERMISSION
        ),
        can_activate_definitions=_can(
            auth, db, custom_fields.DEFINITION_ACTIVATE_PERMISSION
        ),
        can_retire_definitions=_can(
            auth, db, custom_fields.DEFINITION_RETIRE_PERMISSION
        ),
        selected_target=selected_target,
    )
    return {
        **_base_context(request, db),
        **state,
        "page_error": page_error,
        "success": success,
    }


def _definition_row(db: Session, definition_id: UUID) -> CustomFieldDefinition:
    matches = custom_fields.list_definitions(
        db, tenant_id=OPERATOR_TENANT_ID, include_retired=True
    )
    row = next((item for item in matches if item.id == definition_id), None)
    if row is None:
        raise HTTPException(status_code=404, detail="Custom-field definition not found")
    return row


def _optional_decimal(value: str | None) -> Decimal | None:
    if value is None or not value.strip():
        return None
    try:
        return Decimal(value.strip())
    except InvalidOperation as exc:
        raise ValueError("Minimum and maximum values must be valid numbers.") from exc


def _definition_input(
    *,
    label: str,
    description: str | None,
    field_type: CustomFieldType,
    options: str | None,
    default_value: str | None,
    required: str | None,
    sensitive: str | None,
    section: str,
    display_order: int,
    show_in_list: str | None,
    show_in_form: str | None,
    show_in_detail: str | None,
    min_length: int | None,
    max_length: int | None,
    minimum: str | None,
    maximum: str | None,
    pattern: str | None,
    validation_message: str | None,
) -> custom_fields.CustomFieldDefinitionInput:
    return custom_fields.CustomFieldDefinitionInput(
        label=label,
        description=description,
        field_type=field_type,
        options=tuple((options or "").replace("\r", "").replace("\n", ",").split(",")),
        validation=custom_fields.CustomFieldValidation(
            min_length=min_length,
            max_length=max_length,
            minimum=_optional_decimal(minimum),
            maximum=_optional_decimal(maximum),
            pattern=(pattern or "").strip() or None,
            message=(validation_message or "").strip() or None,
        ),
        default_value=(default_value or "").strip() or None,
        required=required == "on",
        sensitive=sensitive == "on",
        section=section,
        display_order=display_order,
        show_in_list=show_in_list == "on",
        show_in_form=show_in_form == "on",
        show_in_detail=show_in_detail == "on",
    )


def _form_context(
    request: Request,
    db: Session,
    *,
    definition: CustomFieldDefinition | None = None,
    page_error: str | None = None,
) -> dict[str, object]:
    from app.services import custom_field_capabilities

    return {
        **_base_context(request, db),
        "definition": definition,
        "field_types": tuple(CustomFieldType),
        "targets": tuple(
            target
            for manifest in custom_field_capabilities.registered_module_manifests()
            for target in manifest.targets
        ),
        "page_error": page_error,
    }


@router.get("", response_class=HTMLResponse)
def custom_fields_index(
    request: Request,
    target: str | None = Query(default=None),
    status: str | None = Query(default=None),
    db: Session = Depends(get_db),
    auth: dict = Depends(require_permission(custom_fields.HUB_READ_PERMISSION)),
) -> HTMLResponse:
    return templates.TemplateResponse(
        "admin/custom_fields/index.html",
        _center_state(
            request,
            db,
            auth,
            selected_target=target,
            success="Custom field saved." if status == "saved" else None,
        ),
    )


@router.get("/new", response_class=HTMLResponse)
def custom_field_new(
    request: Request,
    db: Session = Depends(get_db),
    _auth: dict = Depends(
        require_permission(custom_fields.DEFINITION_CREATE_PERMISSION)
    ),
) -> HTMLResponse:
    return templates.TemplateResponse(
        "admin/custom_fields/form.html", _form_context(request, db)
    )


@router.post("", response_class=HTMLResponse)
def custom_field_create(
    request: Request,
    target_type: str = Form(...),
    key: str = Form(...),
    label: str = Form(...),
    field_type: CustomFieldType = Form(...),
    description: str | None = Form(None),
    options: str | None = Form(None),
    default_value: str | None = Form(None),
    required: str | None = Form(None),
    sensitive: str | None = Form(None),
    section: str = Form("Additional information"),
    display_order: int = Form(0),
    show_in_list: str | None = Form(None),
    show_in_form: str | None = Form(None),
    show_in_detail: str | None = Form(None),
    min_length: int | None = Form(None),
    max_length: int | None = Form(None),
    minimum: str | None = Form(None),
    maximum: str | None = Form(None),
    pattern: str | None = Form(None),
    validation_message: str | None = Form(None),
    db: Session = Depends(get_db),
    auth: dict = Depends(
        require_permission(custom_fields.DEFINITION_CREATE_PERMISSION)
    ),
) -> Response:
    try:
        definition_input = _definition_input(
            label=label,
            description=description,
            field_type=field_type,
            options=options,
            default_value=default_value,
            required=required,
            sensitive=sensitive,
            section=section,
            display_order=display_order,
            show_in_list=show_in_list,
            show_in_form=show_in_form,
            show_in_detail=show_in_detail,
            min_length=min_length,
            max_length=max_length,
            minimum=minimum,
            maximum=maximum,
            pattern=pattern,
            validation_message=validation_message,
        )
        permissions = _permissions(auth, db)
        db_session_adapter.release_read_transaction(db)
        custom_fields.create_definition(
            db,
            custom_fields.CreateCustomFieldDefinitionCommand(
                tenant_id=OPERATOR_TENANT_ID,
                target_type=target_type,
                key=key,
                definition=definition_input,
                permission_keys=permissions,
                context=_context(
                    auth,
                    scope=custom_fields.DEFINITION_CREATE_PERMISSION,
                    reason=f"Create custom-field draft {key.strip()}",
                ),
            ),
        )
    except (custom_fields.CustomFieldError, ValueError) as exc:
        message = (
            exc.message if isinstance(exc, custom_fields.CustomFieldError) else str(exc)
        )
        return templates.TemplateResponse(
            "admin/custom_fields/form.html",
            _form_context(request, db, page_error=message),
            status_code=422,
        )
    return RedirectResponse("/admin/custom-fields?status=saved", status_code=303)


@router.get("/{definition_id}/edit", response_class=HTMLResponse)
def custom_field_edit(
    definition_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    _auth: dict = Depends(
        require_permission(custom_fields.DEFINITION_UPDATE_PERMISSION)
    ),
) -> HTMLResponse:
    return templates.TemplateResponse(
        "admin/custom_fields/form.html",
        _form_context(request, db, definition=_definition_row(db, definition_id)),
    )


@router.post("/{definition_id}", response_class=HTMLResponse)
def custom_field_update(
    definition_id: UUID,
    request: Request,
    label: str = Form(...),
    field_type: CustomFieldType = Form(...),
    description: str | None = Form(None),
    options: str | None = Form(None),
    default_value: str | None = Form(None),
    required: str | None = Form(None),
    sensitive: str | None = Form(None),
    section: str = Form("Additional information"),
    display_order: int = Form(0),
    show_in_list: str | None = Form(None),
    show_in_form: str | None = Form(None),
    show_in_detail: str | None = Form(None),
    min_length: int | None = Form(None),
    max_length: int | None = Form(None),
    minimum: str | None = Form(None),
    maximum: str | None = Form(None),
    pattern: str | None = Form(None),
    validation_message: str | None = Form(None),
    db: Session = Depends(get_db),
    auth: dict = Depends(
        require_permission(custom_fields.DEFINITION_UPDATE_PERMISSION)
    ),
) -> Response:
    try:
        definition_input = _definition_input(
            label=label,
            description=description,
            field_type=field_type,
            options=options,
            default_value=default_value,
            required=required,
            sensitive=sensitive,
            section=section,
            display_order=display_order,
            show_in_list=show_in_list,
            show_in_form=show_in_form,
            show_in_detail=show_in_detail,
            min_length=min_length,
            max_length=max_length,
            minimum=minimum,
            maximum=maximum,
            pattern=pattern,
            validation_message=validation_message,
        )
        permissions = _permissions(auth, db)
        db_session_adapter.release_read_transaction(db)
        custom_fields.update_definition(
            db,
            custom_fields.UpdateCustomFieldDefinitionCommand(
                tenant_id=OPERATOR_TENANT_ID,
                definition_id=definition_id,
                definition=definition_input,
                permission_keys=permissions,
                context=_context(
                    auth,
                    scope=custom_fields.DEFINITION_UPDATE_PERMISSION,
                    reason=f"Update custom-field definition {definition_id}",
                ),
            ),
        )
    except (custom_fields.CustomFieldError, ValueError) as exc:
        message = (
            exc.message if isinstance(exc, custom_fields.CustomFieldError) else str(exc)
        )
        return templates.TemplateResponse(
            "admin/custom_fields/form.html",
            _form_context(
                request,
                db,
                definition=_definition_row(db, definition_id),
                page_error=message,
            ),
            status_code=422,
        )
    return RedirectResponse("/admin/custom-fields?status=saved", status_code=303)


def _change_status(
    *,
    definition_id: UUID,
    operation: custom_fields.CustomFieldDefinitionOperation,
    auth: dict,
    db: Session,
) -> Response:
    permissions = _permissions(auth, db)
    db_session_adapter.release_read_transaction(db)
    try:
        custom_fields.change_definition_status(
            db,
            custom_fields.ChangeCustomFieldDefinitionStatusCommand(
                tenant_id=OPERATOR_TENANT_ID,
                definition_id=definition_id,
                operation=operation,
                permission_keys=permissions,
                context=_context(
                    auth,
                    scope=(
                        custom_fields.DEFINITION_ACTIVATE_PERMISSION
                        if operation
                        is custom_fields.CustomFieldDefinitionOperation.activate
                        else custom_fields.DEFINITION_RETIRE_PERMISSION
                    ),
                    reason=f"{operation.value.title()} custom-field definition {definition_id}",
                ),
            ),
        )
    except custom_fields.CustomFieldError as exc:
        raise HTTPException(status_code=409, detail=exc.message) from exc
    return RedirectResponse("/admin/custom-fields?status=saved", status_code=303)


@router.post("/{definition_id}/activate")
def custom_field_activate(
    definition_id: UUID,
    db: Session = Depends(get_db),
    auth: dict = Depends(
        require_permission(custom_fields.DEFINITION_ACTIVATE_PERMISSION)
    ),
) -> Response:
    return _change_status(
        definition_id=definition_id,
        operation=custom_fields.CustomFieldDefinitionOperation.activate,
        auth=auth,
        db=db,
    )


@router.post("/{definition_id}/retire")
def custom_field_retire(
    definition_id: UUID,
    db: Session = Depends(get_db),
    auth: dict = Depends(
        require_permission(custom_fields.DEFINITION_RETIRE_PERMISSION)
    ),
) -> Response:
    return _change_status(
        definition_id=definition_id,
        operation=custom_fields.CustomFieldDefinitionOperation.retire,
        auth=auth,
        db=db,
    )


@router.post("/targets/{target_type}/{target_id}/{definition_id}")
async def custom_field_value_update(
    target_type: str,
    target_id: UUID,
    definition_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    auth: dict = Depends(require_permission(custom_fields.VALUE_WRITE_PERMISSION)),
) -> Response:
    from app.services import custom_field_capabilities, custom_field_targets

    form = await request.form()
    definition = _definition_row(db, definition_id)
    raw_value: object | None
    if definition.field_type == CustomFieldType.multiselect.value:
        raw_value = list(form.getlist("value"))
    else:
        raw_value = form.get("value")
    target = custom_field_capabilities.target_capability(target_type)
    if not custom_field_access.target_access_allowed(
        db, auth=auth, target_type=target_type, target_id=target_id, write=True
    ):
        raise HTTPException(status_code=403, detail="Forbidden for this record")
    destination = custom_field_targets.target_detail_path(
        db, target_type=target.key, target_id=target_id
    )
    permissions = _permissions(auth, db)
    db_session_adapter.release_read_transaction(db)
    try:
        custom_fields.set_value(
            db,
            custom_fields.SetCustomFieldValueCommand(
                tenant_id=OPERATOR_TENANT_ID,
                definition_id=definition_id,
                target_type=target_type,
                target_id=target_id,
                value=raw_value,
                permission_keys=permissions,
                context=_context(
                    auth,
                    scope=custom_fields.VALUE_WRITE_PERMISSION,
                    reason=f"Set custom-field value for {target_type}:{target_id}",
                ),
            ),
        )
    except custom_fields.CustomFieldError as exc:
        return RedirectResponse(
            f"{destination}?custom_fields_error={quote_plus(exc.message)}",
            status_code=303,
        )
    return RedirectResponse(
        f"{destination}?custom_fields_status=saved", status_code=303
    )


__all__ = ["router"]
