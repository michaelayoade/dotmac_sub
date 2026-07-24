from collections.abc import Callable
from typing import Any, TypeVar
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.db import get_db
from app.models.integration_platform import IntegrationInstallationState
from app.schemas.common import ListResponse
from app.schemas.integration import (
    IntegrationCapabilityBindingRead,
    IntegrationCapabilityBindingUpsert,
    IntegrationConfigRevisionCreate,
    IntegrationConfigRevisionRead,
    IntegrationDeliveryRead,
    IntegrationEventSubscriptionCreate,
    IntegrationEventSubscriptionRead,
    IntegrationInboxRead,
    IntegrationInstallationCreate,
    IntegrationInstallationRead,
    IntegrationJobCreate,
    IntegrationJobRead,
    IntegrationJobUpdate,
    IntegrationLifecycleCommand,
    IntegrationManifestAdoptionCommand,
    IntegrationManifestAdoptionPreviewRead,
    IntegrationManifestAdoptionRead,
    IntegrationRunRead,
    IntegrationTargetCreate,
    IntegrationTargetRead,
    IntegrationTargetUpdate,
)
from app.services import integration as integration_service
from app.services.domain_errors import DomainError
from app.services.integrations import delivery as integration_delivery
from app.services.integrations import inbox as integration_inbox
from app.services.integrations import installations
from app.services.integrations.runtime import ValidationResult
from app.services.integrations.runtime_execution import (
    RuntimeExecutionError,
    build_execution_context,
    validate_connection,
)
from app.services.owner_commands import CommandContext

router = APIRouter(prefix="/integrations", tags=["integrations"])
T = TypeVar("T")


def _operator_id(principal: dict[str, Any]) -> str:
    for key in ("sub", "user_id", "username", "email"):
        value = principal.get(key)
        if value:
            return str(value)[:160]
    return "authenticated-admin"


def _installation_command(db: Session, command: Callable[[], T]) -> T:
    try:
        return installations.execute_command(db, command)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except installations.InstallationError as exc:
        code = 404 if "not found" in str(exc) else 400
        raise HTTPException(status_code=code, detail=str(exc)) from exc
    except RuntimeExecutionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _delivery_command(db: Session, command: Callable[[], T]) -> T:
    try:
        return integration_delivery.execute_command(db, command)
    except integration_delivery.DeliveryError as exc:
        code = 404 if "not found" in str(exc) else 409
        raise HTTPException(status_code=code, detail=str(exc)) from exc


def _inbox_command(db: Session, command: Callable[[], T]) -> T:
    try:
        return integration_inbox.execute_command(db, command)
    except integration_inbox.InboxError as exc:
        code = 404 if "not found" in str(exc) else 409
        raise HTTPException(status_code=code, detail=str(exc)) from exc


@router.post(
    "/installations",
    response_model=IntegrationInstallationRead,
    status_code=status.HTTP_201_CREATED,
)
def create_integration_installation(
    payload: IntegrationInstallationCreate,
    db: Session = Depends(get_db),
    principal: dict[str, Any] = Depends(get_current_user),
):
    return _installation_command(
        db,
        lambda: installations.create_draft(
            db,
            connector_key=payload.connector_key,
            name=payload.name,
            environment=payload.environment,
            actor=_operator_id(principal),
        ),
    )


@router.get("/installations", response_model=list[IntegrationInstallationRead])
def list_integration_installations(
    connector_key: str | None = None,
    state_filter: IntegrationInstallationState | None = Query(
        default=None, alias="state"
    ),
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return installations.list_installations(
        db,
        connector_key=connector_key,
        state=state_filter,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/installations/{installation_id}",
    response_model=IntegrationInstallationRead,
)
def get_integration_installation(
    installation_id: str,
    db: Session = Depends(get_db),
):
    try:
        return installations.get_installation(db, installation_id)
    except installations.InstallationError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/installations/{installation_id}/manifest-adoption",
    response_model=IntegrationManifestAdoptionPreviewRead,
)
def preview_integration_installation_manifest_adoption(
    installation_id: UUID,
    db: Session = Depends(get_db),
):
    try:
        return installations.preview_manifest_adoption(
            db,
            installation_id=installation_id,
        )
    except installations.InstallationError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/installations/{installation_id}/manifest-adoption",
    response_model=IntegrationManifestAdoptionRead,
)
def adopt_integration_installation_manifest(
    installation_id: UUID,
    payload: IntegrationManifestAdoptionCommand,
    db: Session = Depends(get_db),
    principal: dict[str, Any] = Depends(get_current_user),
):
    try:
        return installations.adopt_installation_manifest(
            db,
            installations.AdoptManifestCommand(
                installation_id=installation_id,
                expected_installed_pin=installations.ManifestPin(
                    connector_version=(
                        payload.expected_installed_pin.connector_version
                    ),
                    manifest_digest=payload.expected_installed_pin.manifest_digest,
                ),
                target_pin=installations.ManifestPin(
                    connector_version=payload.target_pin.connector_version,
                    manifest_digest=payload.target_pin.manifest_digest,
                ),
            ),
            context=CommandContext.system(
                actor=_operator_id(principal),
                scope=installations.MANIFEST_ADOPTION_SCOPE,
                reason=payload.reason,
                idempotency_key=payload.idempotency_key,
            ),
        )
    except DomainError as exc:
        status_code = (
            404
            if exc.code.endswith(".not_found")
            else 409
            if exc.code.rsplit(".", 1)[-1]
            in {
                "manifest_adoption_incompatible",
                "stale_manifest_pin",
                "target_manifest_not_deployed",
            }
            else 400
        )
        raise HTTPException(
            status_code=status_code,
            detail={
                "code": exc.code,
                "message": exc.message,
                "details": exc.details,
            },
        ) from exc


@router.post(
    "/installations/{installation_id}/config-revisions",
    response_model=IntegrationConfigRevisionRead,
    status_code=status.HTTP_201_CREATED,
)
def create_integration_config_revision(
    installation_id: str,
    payload: IntegrationConfigRevisionCreate,
    db: Session = Depends(get_db),
    principal: dict[str, Any] = Depends(get_current_user),
):
    return _installation_command(
        db,
        lambda: installations.create_config_revision(
            db,
            installation_id=installation_id,
            config=payload.config,
            secret_refs=payload.secret_refs,
            schema_version=payload.schema_version,
            actor=_operator_id(principal),
        ),
    )


@router.put(
    "/installations/{installation_id}/capabilities/{capability_id}",
    response_model=IntegrationCapabilityBindingRead,
)
def bind_integration_capability(
    installation_id: str,
    capability_id: str,
    payload: IntegrationCapabilityBindingUpsert,
    db: Session = Depends(get_db),
    principal: dict[str, Any] = Depends(get_current_user),
):
    return _installation_command(
        db,
        lambda: installations.bind_capability(
            db,
            installation_id=installation_id,
            capability_id=capability_id,
            scope=payload.scope,
            policy=payload.policy,
            actor=_operator_id(principal),
        ),
    )


@router.post(
    "/installations/{installation_id}/validate-static",
    response_model=ValidationResult,
)
def validate_integration_installation_static(
    installation_id: str,
    db: Session = Depends(get_db),
    principal: dict[str, Any] = Depends(get_current_user),
):
    return _installation_command(
        db,
        lambda: installations.validate_static(
            db,
            installation_id=installation_id,
            actor=_operator_id(principal),
        ),
    )


@router.post(
    "/installations/{installation_id}/validate-connection",
    response_model=ValidationResult,
)
def validate_integration_installation_connection(
    installation_id: str,
    db: Session = Depends(get_db),
    principal: dict[str, Any] = Depends(get_current_user),
):
    actor = _operator_id(principal)

    def command() -> ValidationResult:
        static_result = installations.validate_static(
            db,
            installation_id=installation_id,
            actor=actor,
        )
        if not static_result.valid:
            return static_result
        installation = installations.get_installation(db, installation_id)
        results = []
        for binding in installation.capability_bindings:
            context = build_execution_context(
                db,
                capability_binding_id=binding.id,
                allow_disabled=True,
            )
            results.append(validate_connection(context))
        failed_codes = tuple(
            code
            for result in results
            if not result.valid
            for code in result.error_codes
        )
        if failed_codes:
            return ValidationResult(valid=False, error_codes=failed_codes)
        installations.enable_after_connection_validation(
            db,
            installation_id=installation.id,
            connection_result=ValidationResult(valid=True),
            actor=actor,
        )
        return ValidationResult(valid=True)

    return _installation_command(db, command)


@router.post(
    "/installations/{installation_id}/disable",
    response_model=IntegrationInstallationRead,
)
def disable_integration_installation(
    installation_id: str,
    payload: IntegrationLifecycleCommand,
    db: Session = Depends(get_db),
    principal: dict[str, Any] = Depends(get_current_user),
):
    return _installation_command(
        db,
        lambda: installations.disable_installation(
            db,
            installation_id=installation_id,
            reason=payload.reason,
            actor=_operator_id(principal),
        ),
    )


@router.post(
    "/installations/{installation_id}/quarantine",
    response_model=IntegrationInstallationRead,
)
def quarantine_integration_installation(
    installation_id: str,
    payload: IntegrationLifecycleCommand,
    db: Session = Depends(get_db),
    principal: dict[str, Any] = Depends(get_current_user),
):
    return _installation_command(
        db,
        lambda: installations.quarantine_installation(
            db,
            installation_id=installation_id,
            reason=payload.reason,
            actor=_operator_id(principal),
        ),
    )


@router.post(
    "/installations/{installation_id}/retire",
    response_model=IntegrationInstallationRead,
)
def retire_integration_installation(
    installation_id: str,
    payload: IntegrationLifecycleCommand,
    db: Session = Depends(get_db),
    principal: dict[str, Any] = Depends(get_current_user),
):
    return _installation_command(
        db,
        lambda: installations.retire_installation(
            db,
            installation_id=installation_id,
            reason=payload.reason,
            actor=_operator_id(principal),
        ),
    )


@router.post(
    "/event-subscriptions",
    response_model=IntegrationEventSubscriptionRead,
    status_code=status.HTTP_201_CREATED,
)
def create_integration_event_subscription(
    payload: IntegrationEventSubscriptionCreate,
    db: Session = Depends(get_db),
    principal: dict[str, Any] = Depends(get_current_user),
):
    return _delivery_command(
        db,
        lambda: integration_delivery.create_event_subscription(
            db,
            capability_binding_id=payload.capability_binding_id,
            event_type=payload.event_type,
            filter_json=payload.filter_json,
            payload_policy_json=payload.payload_policy_json,
            actor=_operator_id(principal),
        ),
    )


@router.get("/deliveries", response_model=list[IntegrationDeliveryRead])
def list_integration_deliveries(
    state_filter: str | None = Query(default=None, alias="state", max_length=40),
    capability_binding_id: UUID | None = None,
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return integration_delivery.list_deliveries(
        db,
        state=state_filter,
        capability_binding_id=capability_binding_id,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/deliveries/{delivery_id}/replay",
    response_model=IntegrationDeliveryRead,
)
def replay_integration_delivery(
    delivery_id: UUID,
    db: Session = Depends(get_db),
):
    delivery = _delivery_command(
        db,
        lambda: integration_delivery.replay_delivery(
            db,
            delivery_id=delivery_id,
        ),
    )
    from app.services.queue_adapter import enqueue_task
    from app.tasks.integration_delivery import deliver_integration_event

    enqueue_task(
        deliver_integration_event,
        args=[str(delivery.id)],
        correlation_id=f"integration-delivery-replay:{delivery.id}",
        source="integration.delivery.operator",
    )
    return delivery


@router.get("/inbox", response_model=list[IntegrationInboxRead])
def list_integration_inbox(
    state_filter: str | None = Query(default=None, alias="state", max_length=24),
    capability_binding_id: UUID | None = None,
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    _principal: dict[str, Any] = Depends(get_current_user),
):
    return _inbox_command(
        db,
        lambda: integration_inbox.list_receipts(
            db,
            state=state_filter,
            capability_binding_id=capability_binding_id,
            limit=limit,
            offset=offset,
        ),
    )


@router.get("/inbox/{receipt_id}", response_model=IntegrationInboxRead)
def get_integration_inbox_receipt(
    receipt_id: UUID,
    db: Session = Depends(get_db),
    _principal: dict[str, Any] = Depends(get_current_user),
):
    return _inbox_command(
        db,
        lambda: integration_inbox.get_receipt(db, receipt_id=receipt_id),
    )


@router.post("/inbox/{receipt_id}/replay", response_model=IntegrationInboxRead)
def replay_integration_inbox_receipt(
    receipt_id: UUID,
    db: Session = Depends(get_db),
    _principal: dict[str, Any] = Depends(get_current_user),
):
    """Authorize a failed receipt for the provider's next idempotent redelivery."""
    return _inbox_command(
        db,
        lambda: integration_inbox.replay_receipt(db, receipt_id=receipt_id),
    )


@router.post(
    "/targets",
    response_model=IntegrationTargetRead,
    status_code=status.HTTP_201_CREATED,
)
def create_integration_target(
    payload: IntegrationTargetCreate, db: Session = Depends(get_db)
):
    return integration_service.integration_targets.create(db, payload)


@router.get("/targets/{target_id}", response_model=IntegrationTargetRead)
def get_integration_target(target_id: str, db: Session = Depends(get_db)):
    return integration_service.integration_targets.get(db, target_id)


@router.get("/targets", response_model=ListResponse[IntegrationTargetRead])
def list_integration_targets(
    target_type: str | None = None,
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return integration_service.integration_targets.list_response(
        db, target_type, is_active, order_by, order_dir, limit, offset
    )


@router.patch("/targets/{target_id}", response_model=IntegrationTargetRead)
def update_integration_target(
    target_id: str, payload: IntegrationTargetUpdate, db: Session = Depends(get_db)
):
    return integration_service.integration_targets.update(db, target_id, payload)


@router.delete("/targets/{target_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_integration_target(target_id: str, db: Session = Depends(get_db)):
    integration_service.integration_targets.delete(db, target_id)


@router.post(
    "/jobs",
    response_model=IntegrationJobRead,
    status_code=status.HTTP_201_CREATED,
)
def create_integration_job(
    payload: IntegrationJobCreate, db: Session = Depends(get_db)
):
    return integration_service.integration_jobs.create(db, payload)


@router.get("/jobs/{job_id}", response_model=IntegrationJobRead)
def get_integration_job(job_id: str, db: Session = Depends(get_db)):
    return integration_service.integration_jobs.get(db, job_id)


@router.get("/jobs", response_model=ListResponse[IntegrationJobRead])
def list_integration_jobs(
    target_id: str | None = None,
    job_type: str | None = None,
    schedule_type: str | None = None,
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return integration_service.integration_jobs.list_response(
        db,
        target_id,
        job_type,
        schedule_type,
        is_active,
        order_by,
        order_dir,
        limit,
        offset,
    )


@router.patch("/jobs/{job_id}", response_model=IntegrationJobRead)
def update_integration_job(
    job_id: str, payload: IntegrationJobUpdate, db: Session = Depends(get_db)
):
    return integration_service.integration_jobs.update(db, job_id, payload)


@router.delete("/jobs/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_integration_job(job_id: str, db: Session = Depends(get_db)):
    integration_service.integration_jobs.delete(db, job_id)


@router.post("/jobs/{job_id}/run", response_model=IntegrationRunRead)
def run_integration_job(job_id: str, db: Session = Depends(get_db)):
    return integration_service.integration_jobs.run(db, job_id)


@router.post("/jobs/refresh-schedule", status_code=status.HTTP_200_OK)
def refresh_integration_schedule(db: Session = Depends(get_db)):
    return integration_service.refresh_schedule(db)


@router.get("/runs/{run_id}", response_model=IntegrationRunRead)
def get_integration_run(run_id: str, db: Session = Depends(get_db)):
    return integration_service.integration_runs.get(db, run_id)


@router.get("/runs", response_model=ListResponse[IntegrationRunRead])
def list_integration_runs(
    job_id: str | None = None,
    status: str | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return integration_service.integration_runs.list_response(
        db, job_id, status, order_by, order_dir, limit, offset
    )
