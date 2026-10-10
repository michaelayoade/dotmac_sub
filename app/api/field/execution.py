"""Transport identity, command evidence, and domain error mapping for field work."""

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from uuid import UUID, uuid4

from fastapi import HTTPException

from app.services.domain_errors import DomainError
from app.services.field.work_order_access import FieldAccessError
from app.services.owner_commands import CommandContext


def field_system_user_id(auth: Mapping[str, object]) -> UUID:
    if auth.get("principal_type") != "system_user":
        raise HTTPException(
            status_code=403, detail="Field execution requires a system user"
        )
    try:
        return UUID(str(auth["principal_id"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=403, detail="Invalid field principal") from exc


def field_command_context(
    principal_id: UUID, *, reason: str, request_id: UUID | None = None
) -> CommandContext:
    command_id = request_id or uuid4()
    return CommandContext(
        command_id=command_id,
        correlation_id=command_id,
        actor=f"user:{principal_id}",
        scope="field:work_orders:write",
        reason=reason,
        idempotency_key=str(command_id),
    )


@contextmanager
def field_domain_errors() -> Iterator[None]:
    try:
        yield
    except DomainError as exc:
        if exc.code.endswith("not_found"):
            status_code = 404
        elif exc.code.endswith(("denied", "forbidden")):
            status_code = 403
        elif exc.code.endswith("invalid_request"):
            status_code = 422
        else:
            status_code = 409
        raise HTTPException(
            status_code=status_code,
            detail={"code": exc.code, "message": exc.message, "details": exc.details},
        ) from exc


def field_access_error_boundary() -> Iterator[None]:
    """Map canonical field access failures from ancillary field collaborators."""
    try:
        yield
    except FieldAccessError:
        with field_domain_errors():
            raise
