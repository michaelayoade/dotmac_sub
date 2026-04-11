"""Service helpers for OpenBao secrets management web routes."""

from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import quote_plus

MASKED_SECRET_VALUE = "••••••••"


def build_secrets_index_context(*, status: str | None, message: str | None) -> dict:
    from app.services.secrets import (
        is_openbao_available,
        list_secret_paths,
        read_secret_fields,
        read_secret_metadata,
    )

    available = is_openbao_available()
    paths = list_secret_paths() if available else []
    secrets_list = []
    for path in paths:
        path_clean = path.rstrip("/")
        meta = read_secret_metadata(path_clean) if available else {}
        fields = read_secret_fields(path_clean) if available else {}
        secrets_list.append(
            {
                "path": path_clean,
                "fields": list(fields.keys()),
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
        read_secret_fields,
        read_secret_metadata,
    )

    if not is_openbao_available():
        return {
            "redirect_url": "/admin/system/secrets?status=error&message=OpenBao+not+available"
        }
    return {
        "secret_path": path,
        "fields": read_secret_fields(path),
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

    new_keys = str(form.get("new_field_names") or "").strip()
    new_vals = str(form.get("new_field_values") or "").strip()
    if new_keys:
        for new_key, new_value in zip(
            new_keys.split(","), new_vals.split(","), strict=False
        ):
            key = new_key.strip()
            value = new_value.strip()
            if key:
                updated[key] = value
    return updated


def save_secret(path: str, form: Mapping[str, object]) -> dict:
    from app.services.secrets import write_secret

    updated = _updated_secret_fields(path, form)
    if write_secret(path, updated):
        return {
            "ok": True,
            "redirect_url": f"/admin/system/secrets?status=success&message=Secret+{quote_plus(path)}+updated",
        }
    return {
        "ok": False,
        "secret_path": path,
        "fields": updated,
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


def create_secret(form: Mapping[str, object]) -> dict:
    from app.services.secrets import write_secret

    path = str(form.get("path") or "").strip()
    if not path:
        return {"ok": False, "error": "Secret path is required"}
    data = _new_secret_fields(form)
    if not data:
        return {"ok": False, "error": "At least one field is required"}
    if write_secret(path, data):
        return {
            "ok": True,
            "redirect_url": f"/admin/system/secrets?status=success&message=Secret+{quote_plus(path)}+created",
        }
    return {"ok": False, "error": "Failed to create secret in OpenBao"}


def delete_secret_path(path: str) -> str:
    from app.services.secrets import delete_secret

    delete_secret(path)
    return f"/admin/system/secrets?status=success&message=Secret+{quote_plus(path)}+deleted"
