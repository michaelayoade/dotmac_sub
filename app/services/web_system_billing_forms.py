"""Form/payload helpers for admin system billing settings pages."""

from __future__ import annotations

from uuid import UUID

from fastapi import HTTPException

from app.models.billing import BankAccountType
from app.schemas.billing import BankAccountCreate, BankAccountUpdate
from app.services.common import validate_enum


def _form_bool(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def build_bank_account_create_payload(form) -> BankAccountCreate:
    """Parse form input into BankAccountCreate payload."""
    account_id_str = (form.get("account_id") or "").strip()
    if not account_id_str:
        raise HTTPException(status_code=400, detail="Account is required.")
    try:
        account_id = UUID(account_id_str)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Account id is invalid.") from exc

    account_type = (form.get("account_type") or "").strip() or None
    bank_name = (form.get("bank_name") or "").strip() or None
    account_last4 = (form.get("account_last4") or "").strip() or None
    routing_last4 = (form.get("routing_last4") or "").strip() or None
    token = (form.get("token") or "").strip() or None

    return BankAccountCreate(
        account_id=account_id,
        bank_name=bank_name,
        account_type=validate_enum(account_type, BankAccountType, "account_type")
        if account_type
        else BankAccountType.checking,
        account_last4=account_last4,
        routing_last4=routing_last4,
        token=token,
        is_default=_form_bool(form.get("is_default")),
        is_active=_form_bool(form.get("is_active"))
        if form.get("is_active") is not None
        else True,
    )


def build_bank_account_update_payload(form) -> BankAccountUpdate:
    """Parse partial form input into BankAccountUpdate payload."""
    data: dict[str, object] = {}
    if "bank_name" in form:
        data["bank_name"] = (form.get("bank_name") or "").strip() or None
    if "account_type" in form:
        account_type = (form.get("account_type") or "").strip()
        data["account_type"] = (
            validate_enum(account_type, BankAccountType, "account_type")
            if account_type
            else None
        )
    if "account_last4" in form:
        data["account_last4"] = (form.get("account_last4") or "").strip() or None
    if "routing_last4" in form:
        data["routing_last4"] = (form.get("routing_last4") or "").strip() or None
    if "is_default" in form:
        data["is_default"] = _form_bool(form.get("is_default"))
    if "is_active" in form:
        data["is_active"] = _form_bool(form.get("is_active"))
    token = (form.get("token") or "").strip()
    if token:
        data["token"] = token

    return BankAccountUpdate.model_validate(data)


def build_bank_account_error_context(
    request,
    db,
    *,
    error: str | None,
    message: str,
) -> dict:
    from app.services import web_system_settings_views as web_system_settings_views_service

    settings_context = web_system_settings_views_service.build_settings_context(db, "billing")
    return web_system_settings_views_service.build_settings_page_context(
        request,
        db,
        settings_context=settings_context,
        extra={"bank_account_error": error or message},
    )
