"""Service helpers for OpenBao secrets management web routes."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable, Mapping
from urllib.parse import quote_plus

from sqlalchemy.orm import Session

MASKED_SECRET_VALUE = "••••••••"
_SECRET_FIELD_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")
AUDIT_UNAVAILABLE_MESSAGE = (
    "The audit record could not be written, so the secret was not changed."
)

logger = logging.getLogger(__name__)


class SecretAuditUnavailable(RuntimeError):
    """The required audit record could not be persisted; the write must not run."""


#: ``(action, path, field_names)``; persists the audit record or raises
#: :class:`SecretAuditUnavailable`. Required by every secret write (S12).
SecretAuditor = Callable[[str, str, Iterable[str]], None]


def secret_write_auditor(db: Session, request: object) -> SecretAuditor:
    """Build the auditor that records a secret write BEFORE OpenBao is touched.

    OpenBao is outside the database transaction, so atomicity here means
    audit-first, fail-closed: the record is committed first and the write is
    refused if it cannot be. Only the secret path and field NAMES are recorded,
    never values.
    """

    def _audit(action: str, path: str, field_names: Iterable[str]) -> None:
        from app.services.audit_helpers import log_audit_event

        try:
            log_audit_event(
                db=db,
                request=request,
                action=action,
                entity_type="secret",
                entity_id=path,
                actor_id=None,
                metadata={"field_names": sorted(str(name) for name in field_names)},
            )
        except Exception as exc:
            db.rollback()
            logger.warning(
                "secret_audit_unavailable",
                exc_info=True,
                extra={"event": "secret_audit_unavailable"},
            )
            raise SecretAuditUnavailable(AUDIT_UNAVAILABLE_MESSAGE) from exc

    return _audit


def build_secrets_index_context(*, status: str | None, message: str | None) -> dict:
    from app.services.secrets import (
        is_openbao_available,
        list_secret_field_names,
        list_secret_paths,
        read_secret_metadata,
    )

    available = is_openbao_available()
    paths = list_secret_paths() if available else []
    secrets_list = []
    for path in paths:
        path_clean = path.rstrip("/")
        meta = read_secret_metadata(path_clean) if available else {}
        fields = list_secret_field_names(path_clean) if available else []
        secrets_list.append(
            {
                "path": path_clean,
                "fields": fields,
                "field_count": len(fields),
                "version": meta.get("current_version", "?"),
                "created_time": meta.get("created_time", ""),
                "updated_time": meta.get("updated_time", ""),
            }
        )
    return {
        "openbao_available": available,
        "secrets_list": secrets_list,
        "status": status,
        "message": message,
    }


def build_secret_edit_context(path: str) -> dict:
    from app.services.secrets import (
        is_openbao_available,
        list_secret_field_names,
        read_secret_metadata,
    )

    if not is_openbao_available():
        return {
            "redirect_url": "/admin/system/secrets?status=error&message=OpenBao+not+available"
        }
    return {
        "secret_path": path,
        "fields": dict.fromkeys(list_secret_field_names(path), MASKED_SECRET_VALUE),
        "metadata": read_secret_metadata(path),
        "error": None,
    }


def _updated_secret_fields(path: str, form: Mapping[str, object]) -> dict[str, str]:
    from app.services.secrets import read_secret_fields

    existing = read_secret_fields(path)
    updated: dict[str, str] = {}
    for key in existing:
        form_val = str(form.get(f"field_{key}") or "").strip()
        if form_val and form_val != MASKED_SECRET_VALUE:
            updated[key] = form_val
        else:
            updated[key] = existing[key]

    indexes = sorted(
        {
            int(key.removeprefix("new_key_"))
            for key in form
            if key.startswith("new_key_") and key.removeprefix("new_key_").isdigit()
        }
    )
    for index in indexes:
        key = str(form.get(f"new_key_{index}") or "").strip()
        value = str(form.get(f"new_value_{index}") or "").strip()
        if not key and not value:
            continue
        if not key:
            raise ValueError("Every new secret value requires a field name")
        if not _SECRET_FIELD_NAME.fullmatch(key):
            raise ValueError(
                "Secret field names may contain letters, numbers, dots, "
                "underscores, and hyphens only"
            )
        if key in updated:
            raise ValueError(f"Secret field '{key}' already exists")
        if not value:
            raise ValueError(f"Secret field '{key}' requires a value")
        updated[key] = value
    return updated


def save_secret(path: str, form: Mapping[str, object], *, audit: SecretAuditor) -> dict:
    from app.services.secrets import (
        is_managed_secret_path,
        list_secret_field_names,
        write_secret,
    )

    if is_managed_secret_path(path):
        return {
            "ok": False,
            "secret_path": path,
            "fields": {},
            "metadata": {},
            "error": "This credential is managed by its integration. Use the integration setup page.",
        }

    try:
        updated = _updated_secret_fields(path, form)
    except ValueError as exc:
        return {
            "ok": False,
            "secret_path": path,
            "fields": dict.fromkeys(list_secret_field_names(path), MASKED_SECRET_VALUE),
            "metadata": {},
            "error": str(exc),
        }
    try:
        audit("secret_updated", path, updated)
    except SecretAuditUnavailable as exc:
        return {
            "ok": False,
            "secret_path": path,
            "fields": dict.fromkeys(updated, MASKED_SECRET_VALUE),
            "metadata": {},
            "error": str(exc),
        }
    if write_secret(path, updated):
        return {
            "ok": True,
            "redirect_url": f"/admin/system/secrets?status=success&message=Secret+{quote_plus(path)}+updated",
        }
    return {
        "ok": False,
        "secret_path": path,
        "fields": dict.fromkeys(updated, MASKED_SECRET_VALUE),
        "metadata": {},
        "error": "Failed to save secret to OpenBao",
    }


def build_secret_new_context(error: str | None = None) -> dict:
    return {"error": error}


def _new_secret_fields(form: Mapping[str, object]) -> dict[str, str]:
    data: dict[str, str] = {}
    idx = 0
    while True:
        key = str(form.get(f"key_{idx}") or "").strip()
        value = str(form.get(f"val_{idx}") or "").strip()
        if not key:
            break
        data[key] = value
        idx += 1
    return data


def create_secret(form: Mapping[str, object], *, audit: SecretAuditor) -> dict:
    from app.services.secrets import is_managed_secret_path, write_secret

    path = str(form.get("path") or "").strip()
    if not path:
        return {"ok": False, "error": "Secret path is required"}
    if is_managed_secret_path(path):
        return {
            "ok": False,
            "error": "This credential is managed by its integration. Use the integration setup page.",
        }
    data = _new_secret_fields(form)
    if not data:
        return {"ok": False, "error": "At least one field is required"}
    try:
        audit("secret_created", path, data)
    except SecretAuditUnavailable as exc:
        return {"ok": False, "error": str(exc)}
    if write_secret(path, data):
        return {
            "ok": True,
            "redirect_url": f"/admin/system/secrets?status=success&message=Secret+{quote_plus(path)}+created",
        }
    return {"ok": False, "error": "Failed to create secret in OpenBao"}


def delete_secret_path(path: str, *, audit: SecretAuditor) -> str:
    from app.services.secrets import delete_secret, is_managed_secret_path

    if not is_managed_secret_path(path):
        try:
            audit("secret_deleted", path, ())
        except SecretAuditUnavailable:
            return "/admin/system/secrets?status=error&message=Audit+unavailable%2C+secret+not+deleted"
    if not delete_secret(path):
        return "/admin/system/secrets?status=error&message=Secret+could+not+be+deleted"
    return f"/admin/system/secrets?status=success&message=Secret+{quote_plus(path)}+deleted"
