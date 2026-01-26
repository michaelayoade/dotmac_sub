from __future__ import annotations

from fastapi import HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.imports.loader import load_csv_content
from app.models.domain_settings import DomainSetting, SettingDomain
from app.schemas.imports import SubscriberCustomFieldImportRow
from app.schemas.subscriber import SubscriberCustomFieldCreate
from app.services import subscriber as subscriber_service

_DEFAULT_MAX_FILE_BYTES = 5 * 1024 * 1024
_DEFAULT_MAX_ROWS = 5000


def _imports_int_setting(db: Session, key: str, default: int) -> int:
    setting = (
        db.query(DomainSetting)
        .filter(DomainSetting.domain == SettingDomain.imports)
        .filter(DomainSetting.key == key)
        .filter(DomainSetting.is_active.is_(True))
        .first()
    )
    if not setting:
        return default
    value = setting.value_text if setting.value_text is not None else setting.value_json
    if value is None:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed


def import_subscriber_custom_fields_from_csv(
    db: Session, content: str, max_rows: int | None = None
) -> tuple[int, list[dict[str, str | int]]]:
    created = 0
    errors: list[dict[str, str | int]] = []
    rows, row_errors = load_csv_content(
        content, SubscriberCustomFieldImportRow, max_rows=max_rows
    )
    errors.extend(
        {"index": err.index, "detail": err.detail} for err in row_errors
    )
    for idx, import_row in rows:
        try:
            payload = SubscriberCustomFieldCreate(**import_row.model_dump())
            subscriber_service.subscriber_custom_fields.create(db, payload)
            created += 1
        except Exception as exc:
            db.rollback()
            errors.append({"index": idx, "detail": str(exc)})
    return created, errors


def import_subscriber_custom_fields_upload(
    db: Session, file: UploadFile
) -> dict[str, int | list[dict[str, str | int]]]:
    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="CSV file required")
    payload = file.file.read()
    max_file_bytes = _imports_int_setting(db, "max_file_bytes", _DEFAULT_MAX_FILE_BYTES)
    if len(payload) > max_file_bytes:
        raise HTTPException(status_code=413, detail="CSV file too large")
    try:
        content = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail="Invalid UTF-8 CSV content") from exc
    max_rows = _imports_int_setting(db, "max_rows", _DEFAULT_MAX_ROWS)
    created, errors = import_subscriber_custom_fields_from_csv(
        db, content, max_rows=max_rows
    )
    if any(err.get("detail") == "Row limit exceeded" for err in errors):
        raise HTTPException(status_code=400, detail="CSV row limit exceeded")
    return {"created": created, "errors": errors, "error_count": len(errors)}
