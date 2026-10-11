"""Admin UI for managed customer connection type definitions."""

from __future__ import annotations

from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import customer_connection_types
from app.services.auth_dependencies import require_permission
from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError
from app.services.owner_commands import CommandContext

templates = Jinja2Templates(directory="templates")
router = APIRouter(
    prefix="/system/customer-connection-types",
    tags=["web-admin-customer-connection-types"],
)


def _context(request: Request, db: Session, **extra: object) -> dict[str, object]:
    from app.web.admin import get_current_user, get_sidebar_stats

    return {
        "request": request,
        "active_page": "settings-hub",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
        **extra,
    }


def _actor(auth: dict[str, object]) -> str:
    return (
        f"{auth.get('principal_type') or 'user'}:"
        f"{auth.get('principal_id') or 'unknown'}"
    )


@router.get(
    "",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission(customer_connection_types.READ_SCOPE))],
)
def customer_connection_types_index(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        "admin/system/customer_connection_types.html",
        _context(
            request,
            db,
            connection_types=customer_connection_types.list_definitions(db),
            editing_type=None,
            error=None,
        ),
    )


@router.get(
    "/{definition_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission(customer_connection_types.READ_SCOPE))],
)
def customer_connection_type_edit(
    definition_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
):
    definition = customer_connection_types.get_definition(
        db, definition_id=definition_id
    )
    if definition is None:
        return RedirectResponse(
            url="/admin/system/customer-connection-types", status_code=303
        )
    return templates.TemplateResponse(
        "admin/system/customer_connection_types.html",
        _context(
            request,
            db,
            connection_types=customer_connection_types.list_definitions(db),
            editing_type=definition,
            error=None,
        ),
    )


@router.post("", response_class=HTMLResponse)
def customer_connection_type_save(
    request: Request,
    name: str = Form(...),
    description: str | None = Form(None),
    sort_order: int = Form(0),
    is_active: str | None = Form(None),
    definition_id: str | None = Form(None),
    db: Session = Depends(get_db),
    auth: dict[str, object] = Depends(
        require_permission(customer_connection_types.WRITE_SCOPE)
    ),
):
    form_values = {
        "definition_id": definition_id or "",
        "name": name,
        "description": description or "",
        "sort_order": sort_order,
        "is_active": is_active is not None,
    }
    try:
        parsed_id = UUID(definition_id) if definition_id else None
        command_id = uuid4()
        db_session_adapter.release_read_transaction(db)
        customer_connection_types.save_definition(
            db,
            customer_connection_types.SaveConnectionTypeCommand(
                context=CommandContext(
                    command_id=command_id,
                    correlation_id=command_id,
                    actor=_actor(auth),
                    scope=customer_connection_types.WRITE_SCOPE,
                    reason=(
                        "Administrator updated a customer connection type"
                        if parsed_id
                        else "Administrator created a customer connection type"
                    ),
                    idempotency_key=f"customer-connection-type:save:{command_id}",
                ),
                definition_id=parsed_id,
                name=name,
                description=description,
                sort_order=sort_order,
                is_active=is_active is not None,
            ),
        )
        return RedirectResponse(
            url="/admin/system/customer-connection-types", status_code=303
        )
    except (DomainError, ValueError, TypeError) as exc:
        db_session_adapter.discard_failed_transaction(db)
        error = exc.message if isinstance(exc, DomainError) else str(exc)
        editing_type = None
        try:
            if definition_id:
                editing_type = customer_connection_types.get_definition(
                    db, definition_id=UUID(definition_id)
                )
        except ValueError:
            editing_type = None
        return templates.TemplateResponse(
            "admin/system/customer_connection_types.html",
            _context(
                request,
                db,
                connection_types=customer_connection_types.list_definitions(db),
                editing_type=editing_type,
                form_values=form_values,
                error=error,
            ),
            status_code=422,
        )


@router.post("/{definition_id}/disable")
def customer_connection_type_disable(
    definition_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    auth: dict[str, object] = Depends(
        require_permission(customer_connection_types.WRITE_SCOPE)
    ),
):
    try:
        command_id = uuid4()
        db_session_adapter.release_read_transaction(db)
        customer_connection_types.disable_definition(
            db,
            customer_connection_types.DisableConnectionTypeCommand(
                context=CommandContext(
                    command_id=command_id,
                    correlation_id=command_id,
                    actor=_actor(auth),
                    scope=customer_connection_types.WRITE_SCOPE,
                    reason="Administrator disabled a customer connection type",
                    idempotency_key=(
                        f"customer-connection-type:disable:{definition_id}"
                    ),
                ),
                definition_id=definition_id,
            ),
        )
    except (DomainError, ValueError, TypeError) as exc:
        db_session_adapter.discard_failed_transaction(db)
        error = exc.message if isinstance(exc, DomainError) else str(exc)
        return templates.TemplateResponse(
            "admin/system/customer_connection_types.html",
            _context(
                request,
                db,
                connection_types=customer_connection_types.list_definitions(db),
                editing_type=None,
                error=error,
            ),
            status_code=422,
        )
    return RedirectResponse(
        url="/admin/system/customer-connection-types", status_code=303
    )
