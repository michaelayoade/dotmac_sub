"""Staff transport for the Tax owner-backed policy authoring workbench."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Literal
from uuid import UUID, uuid4

from dotmac_tax import TaxConflict, TaxNotFound, TaxRuleViolation
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import TypeAdapter, ValidationError
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from app.csrf import get_csrf_token
from app.db import finish_read_transaction, get_db
from app.services.action_forms import ActionFormSubmission
from app.services.auth_dependencies import require_permission_admission
from app.services.owner_commands import CommandContext
from app.services.permission_admission import PermissionAdmission
from app.services.tax_policy_authoring import (
    READ_PERMISSION,
    WRITE_PERMISSION,
    AuthorTaxPolicy,
    TaxPolicyAuthoringError,
    author_tax_policy,
    preview_policy_details,
)
from app.services.tax_policy_authoring_contracts import (
    AuthorityDraft,
    CodeDraft,
    DependencySelection,
    JurisdictionDraft,
    Operation,
    PolicyIdentity,
    PolicyPreviewOutcome,
    PreviewDraft,
    RuleDraft,
    SupersessionDraft,
)
from app.services.tax_policy_authoring_workbench import (
    BASE_URL,
    DRAFT_TYPES,
    WorkbenchQuery,
    authoring_form,
    browse_policy_catalog,
    policy_form_seed,
    publication_destination,
)
from app.web.request_parsing import parse_form_data

router = APIRouter(prefix="/billing/tax-policy", tags=["web-admin-tax-policy"])
templates = Jinja2Templates(directory="templates")
_read = require_permission_admission(READ_PERMISSION)
_write = require_permission_admission(WRITE_PERMISSION)


def _staff_read(admission: PermissionAdmission = Depends(_read)) -> PermissionAdmission:
    if admission.principal_type != "system_user":
        raise HTTPException(status_code=403, detail="Authorized staff required")
    return admission


def _staff_write(
    admission: PermissionAdmission = Depends(_write),
) -> PermissionAdmission:
    if admission.principal_type != "system_user":
        raise HTTPException(status_code=403, detail="Authorized staff required")
    return admission


def _context(request: Request) -> dict[str, object]:
    from app.web.admin import get_current_user

    return {
        "request": request,
        "current_user": get_current_user(request),
        "active_page": "tax-policy",
        "active_menu": "billing",
        "csrf_token": get_csrf_token(request),
    }


def _submission(
    *,
    operation: Operation,
    form: FormData,
    message: str | None = None,
    errors: dict[str, str] | None = None,
) -> ActionFormSubmission:
    keys = set(DRAFT_TYPES[operation].model_fields)
    return ActionFormSubmission.from_mapping(
        f"tax-policy.{operation.value}",
        {
            key: value
            for key, value in form.items()
            if key in keys and isinstance(value, str)
        },
        field_errors=errors,
        general_error=message,
    )


def _parse(
    *, operation: Operation, form: FormData
) -> (
    AuthorityDraft
    | JurisdictionDraft
    | CodeDraft
    | RuleDraft
    | SupersessionDraft
    | PreviewDraft
):
    model = DRAFT_TYPES[operation]
    permitted = set(model.model_fields) | {"csrf_token", "_csrf_token", "confirmed"}
    if set(form.keys()) - permitted or any(len(form.getlist(key)) != 1 for key in form):
        raise ValueError("Unknown or repeated form fields are not accepted.")
    values: dict[str, object] = {}
    for key, info in model.model_fields.items():
        value = form.get(key)
        if value is not None and not isinstance(value, str):
            raise ValueError("File fields are not accepted.")
        if value == "" or value is None:
            if info.is_required():
                values[key] = value
            continue
        if len(value) > (
            20000
            if key
            in {"bands", "dependency_tax_code_ids", "prior_determination_set_ids"}
            else 1000
        ):
            raise ValueError("A form field exceeds its permitted size.")
        values[key] = (
            json.loads(value, parse_float=Decimal)
            if key
            in {"bands", "dependency_tax_code_ids", "prior_determination_set_ids"}
            else value
        )
    parsed = model.model_validate(values)
    if not isinstance(
        parsed,
        (
            AuthorityDraft,
            JurisdictionDraft,
            CodeDraft,
            RuleDraft,
            SupersessionDraft,
            PreviewDraft,
        ),
    ):
        raise ValueError("Unknown form type.")
    return parsed


def _form_response(
    request: Request,
    *,
    operation: Operation,
    submission: ActionFormSubmission | None = None,
    status_code: int = 200,
    preview: PolicyPreviewOutcome | None = None,
) -> HTMLResponse:
    context = _context(request)
    context.update(
        {
            "action": authoring_form(operation=operation, submission=submission),
            "preview": preview,
        }
    )
    return templates.TemplateResponse(
        "admin/billing/tax_policy/form.html", context, status_code=status_code
    )


@router.get("", response_class=HTMLResponse)
def policy_workbench(
    request: Request,
    catalog: Literal["authorities", "jurisdictions", "codes"] = Query("authorities"),
    cursor: str | None = Query(None, max_length=1024),
    page_size: int = Query(25, ge=1, le=100),
    authority_id: UUID | None = None,
    jurisdiction_id: UUID | None = None,
    tax_code_id: UUID | None = None,
    history_cursor: str | None = Query(None, max_length=1024),
    jurisdiction_code: str | None = Query(None, max_length=100),
    tax_code: str | None = Query(None, max_length=100),
    dependency_predecessor_version: int | None = Query(None, ge=1),
    dependency_target: str | None = Query(None, max_length=2048),
    dependency_selected_codes: str = Query("[]", max_length=5000),
    dependency_prior_jurisdiction_id: UUID | None = None,
    admission: PermissionAdmission = Depends(_staff_read),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    try:
        query = WorkbenchQuery(
            catalog=catalog,
            cursor=cursor,
            page_size=page_size,
            authority_id=authority_id,
            jurisdiction_id=jurisdiction_id,
            tax_code_id=tax_code_id,
            history_cursor=history_cursor,
            jurisdiction_code=jurisdiction_code,
            tax_code=tax_code,
            dependency_predecessor_version=dependency_predecessor_version,
            dependency_target=PolicyIdentity.model_validate_json(dependency_target)
            if dependency_target
            else None,
            dependency_selected_codes=TypeAdapter(tuple[UUID, ...]).validate_json(
                dependency_selected_codes
            ),
            dependency_prior_jurisdiction_id=dependency_prior_jurisdiction_id,
        )
        workbench = browse_policy_catalog(db, admission=admission, query=query)
    except (
        ValueError,
        TaxPolicyAuthoringError,
        TaxConflict,
        TaxNotFound,
        TaxRuleViolation,
    ) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    context = _context(request)
    context["workbench"] = workbench
    return templates.TemplateResponse("admin/billing/tax_policy/index.html", context)


@router.get("/preview", response_class=HTMLResponse)
def policy_preview_form(
    request: Request, admission: PermissionAdmission = Depends(_staff_read)
) -> HTMLResponse:
    return _form_response(request, operation=Operation.preview)


@router.post("/preview", response_class=HTMLResponse)
def policy_preview(
    request: Request,
    form: FormData = Depends(parse_form_data),
    admission: PermissionAdmission = Depends(_staff_read),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    try:
        draft = _parse(operation=Operation.preview, form=form)
        if not isinstance(draft, PreviewDraft):
            raise ValueError("A preview fact is required.")
        preview = preview_policy_details(db, admission=admission, draft=draft)
    except (ValueError, TaxPolicyAuthoringError) as exc:
        return _form_response(
            request,
            operation=Operation.preview,
            submission=_submission(
                operation=Operation.preview, form=form, message=str(exc)
            ),
            status_code=400,
        )
    return _form_response(
        request,
        operation=Operation.preview,
        preview=preview,
        submission=_submission(operation=Operation.preview, form=form),
    )


@router.get("/author/{operation}", response_class=HTMLResponse)
def policy_author_form(
    request: Request,
    operation: Operation,
    tax_code_id: UUID | None = None,
    predecessor_version: int | None = Query(None, ge=1),
    jurisdiction_id: UUID | None = None,
    jurisdiction_code: str | None = Query(None, max_length=100),
    tax_code: str | None = Query(None, max_length=100),
    dependency_selection: DependencySelection = DependencySelection.none,
    dependency_prior_jurisdiction_id: UUID | None = None,
    dependency_tax_code_ids: str = Query("[]", max_length=5000),
    admission: PermissionAdmission = Depends(_staff_write),
    db: Session = Depends(get_db),
) -> Response:
    if operation is Operation.preview:
        return RedirectResponse(f"{BASE_URL}/preview", status_code=303)
    submission = None
    if operation in {Operation.supersede, Operation.initial_rule} and tax_code_id:
        try:
            submission = policy_form_seed(
                db,
                admission=admission,
                operation=operation,
                tax_code_id=tax_code_id,
                predecessor_version=predecessor_version,
                jurisdiction_id=jurisdiction_id,
                jurisdiction_code=jurisdiction_code,
                tax_code=tax_code,
                dependency_selection=dependency_selection,
                dependency_prior_jurisdiction_id=dependency_prior_jurisdiction_id,
                dependency_tax_code_ids=TypeAdapter(tuple[UUID, ...]).validate_json(
                    dependency_tax_code_ids
                ),
            )
        except (TaxNotFound, TaxConflict, TaxRuleViolation, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _form_response(request, operation=operation, submission=submission)


@router.post("/{operation}")
def policy_author(
    request: Request,
    operation: Operation,
    form: FormData = Depends(parse_form_data),
    admission: PermissionAdmission = Depends(_staff_write),
    db: Session = Depends(get_db),
) -> Response:
    if operation is Operation.preview:
        raise HTTPException(status_code=404)
    try:
        draft = _parse(operation=operation, form=form)
        if isinstance(draft, PreviewDraft):
            raise ValueError("A publication command is required.")
        context = CommandContext.system(
            actor=f"system_user:{admission.principal_id}",
            scope=WRITE_PERMISSION,
            reason=draft.reason,
            idempotency_key=f"{draft.source_ref}:{draft.source_version}",
            command_id=uuid4(),
        )
        finish_read_transaction(db)
        outcome = author_tax_policy(
            db,
            command=AuthorTaxPolicy(
                context=context,
                admission=admission,
                draft=draft,
                confirmed=form.get("confirmed") == "yes",
            ),
        )
    except ValidationError as exc:
        field_errors = {
            str(item["loc"][0]): item["msg"]
            for item in exc.errors()
            if item["loc"]
            and str(item["loc"][0]) in DRAFT_TYPES[operation].model_fields
        }
        return _form_response(
            request,
            operation=operation,
            status_code=400,
            submission=_submission(
                operation=operation,
                form=form,
                errors=field_errors,
                message="Review the highlighted values.",
            ),
        )
    except (ValueError, TaxPolicyAuthoringError) as exc:
        return _form_response(
            request,
            operation=operation,
            status_code=409
            if isinstance(exc, TaxPolicyAuthoringError)
            and exc.code.endswith("conflict")
            else 400,
            submission=_submission(operation=operation, form=form, message=str(exc)),
        )
    return RedirectResponse(publication_destination(outcome=outcome), status_code=303)
