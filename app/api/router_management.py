import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas.router_management import (
    ConnectionTestResult,
    JumpHostCreate,
    JumpHostRead,
    JumpHostUpdate,
    RouterConfigPushCreate,
    RouterConfigPushRead,
    RouterConfigPushResultRead,
    RouterConfigSnapshotRead,
    RouterConfigTemplateCreate,
    RouterConfigTemplateRead,
    RouterConfigTemplateUpdate,
    RouterCreate,
    RouterHealthRead,
    RouterInterfaceRead,
    RouterRead,
    RouterUpdate,
)
from app.services.auth_dependencies import require_permission
from app.services.router_management.config import (
    RouterConfigService,
    RouterTemplateService,
)
from app.services.router_management.connection import RouterConnectionService
from app.services.router_management.inventory import (
    JumpHostInventory,
    RouterInventory,
)
from app.services.router_management.monitoring import RouterMonitoringService

router = APIRouter(prefix="/network/routers", tags=["router-management"])


# --- Router CRUD ---


@router.get(
    "",
    dependencies=[Depends(require_permission("router:read"))],
)
def list_routers(
    status: str | None = None,
    access_method: str | None = None,
    jump_host_id: uuid.UUID | None = None,
    search: str | None = None,
    order_by: str = "name",
    order_dir: str = "asc",
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> dict:
    items = RouterInventory.list(
        db,
        status=status,
        access_method=access_method,
        jump_host_id=jump_host_id,
        search=search,
        order_by=order_by,
        order_dir=order_dir,
        limit=limit,
        offset=offset,
    )
    count = RouterInventory.count(db, status=status)
    return {
        "items": [RouterRead.model_validate(r) for r in items],
        "count": count,
        "limit": limit,
        "offset": offset,
    }


@router.post(
    "",
    response_model=RouterRead,
    dependencies=[Depends(require_permission("router:write"))],
)
def create_router(
    payload: RouterCreate,
    db: Session = Depends(get_db),
) -> RouterRead:
    r = RouterInventory.create(db, payload)
    return RouterRead.model_validate(r)


@router.get(
    "/{router_id}",
    response_model=RouterRead,
    dependencies=[Depends(require_permission("router:read"))],
)
def get_router(
    router_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> RouterRead:
    r = RouterInventory.get(db, router_id)
    return RouterRead.model_validate(r)


@router.patch(
    "/{router_id}",
    response_model=RouterRead,
    dependencies=[Depends(require_permission("router:write"))],
)
def update_router(
    router_id: uuid.UUID,
    payload: RouterUpdate,
    db: Session = Depends(get_db),
) -> RouterRead:
    r = RouterInventory.update(db, router_id, payload)
    return RouterRead.model_validate(r)


@router.delete(
    "/{router_id}",
    dependencies=[Depends(require_permission("router:write"))],
)
def delete_router(
    router_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> dict:
    RouterInventory.delete(db, router_id)
    return {"detail": "Router deleted"}


# --- Router Actions ---


@router.post(
    "/{router_id}/test-connection",
    response_model=ConnectionTestResult,
    dependencies=[Depends(require_permission("router:read"))],
)
def test_router_connection(
    router_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> ConnectionTestResult:
    r = RouterInventory.get(db, router_id)
    return RouterConnectionService.test_connection(r)


@router.post(
    "/{router_id}/sync",
    dependencies=[Depends(require_permission("router:write"))],
)
def sync_router(
    router_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> dict:
    r = RouterInventory.get(db, router_id)
    try:
        RouterInventory.sync_system_info(db, r)
        RouterInventory.sync_interfaces(db, r)
        return {"detail": "Sync complete", "version": r.routeros_version}
    except Exception as exc:
        RouterInventory.update(db, r.id, RouterUpdate(status="unreachable"))
        raise HTTPException(status_code=502, detail=str(exc))


@router.get(
    "/{router_id}/health",
    response_model=RouterHealthRead,
    dependencies=[Depends(require_permission("router:read"))],
)
def get_router_health(
    router_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> RouterHealthRead:
    r = RouterInventory.get(db, router_id)
    data = RouterConnectionService.execute(r, "GET", "/system/resource")
    parsed = RouterMonitoringService.parse_health_response(data)
    return RouterHealthRead(**parsed)


@router.get(
    "/{router_id}/interfaces",
    dependencies=[Depends(require_permission("router:read"))],
)
def list_router_interfaces(
    router_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> dict:
    RouterInventory.get(db, router_id)
    interfaces = RouterInventory.list_interfaces(db, router_id)
    return {"items": [RouterInterfaceRead.model_validate(i) for i in interfaces]}


# --- Config Snapshots ---


@router.get(
    "/{router_id}/snapshots",
    dependencies=[Depends(require_permission("router:read"))],
)
def list_snapshots(
    router_id: uuid.UUID,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> dict:
    RouterInventory.get(db, router_id)
    snaps = RouterConfigService.list_snapshots(
        db, router_id, limit=limit, offset=offset
    )
    return {"items": [RouterConfigSnapshotRead.model_validate(s) for s in snaps]}


@router.post(
    "/{router_id}/snapshots",
    response_model=RouterConfigSnapshotRead,
    dependencies=[Depends(require_permission("router:write"))],
)
def capture_snapshot(
    router_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> RouterConfigSnapshotRead:
    r = RouterInventory.get(db, router_id)
    try:
        snap = RouterConfigService.capture_from_router(db, r)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return RouterConfigSnapshotRead.model_validate(snap)


@router.get(
    "/{router_id}/snapshots/{snapshot_id}",
    response_model=RouterConfigSnapshotRead,
    dependencies=[Depends(require_permission("router:read"))],
)
def get_snapshot(
    router_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> RouterConfigSnapshotRead:
    RouterInventory.get(db, router_id)
    snap = RouterConfigService.get_snapshot(db, snapshot_id)
    return RouterConfigSnapshotRead.model_validate(snap)


# --- Config Templates ---


@router.get(
    "/config-templates",
    dependencies=[Depends(require_permission("router:read"))],
)
def list_templates(
    category: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> dict:
    templates = RouterTemplateService.list(
        db, category=category, limit=limit, offset=offset
    )
    return {"items": [RouterConfigTemplateRead.model_validate(t) for t in templates]}


@router.post(
    "/config-templates",
    response_model=RouterConfigTemplateRead,
    dependencies=[Depends(require_permission("router:write"))],
)
def create_template(
    payload: RouterConfigTemplateCreate,
    db: Session = Depends(get_db),
) -> RouterConfigTemplateRead:
    tmpl = RouterTemplateService.create(db, payload)
    return RouterConfigTemplateRead.model_validate(tmpl)


@router.patch(
    "/config-templates/{template_id}",
    response_model=RouterConfigTemplateRead,
    dependencies=[Depends(require_permission("router:write"))],
)
def update_template(
    template_id: uuid.UUID,
    payload: RouterConfigTemplateUpdate,
    db: Session = Depends(get_db),
) -> RouterConfigTemplateRead:
    tmpl = RouterTemplateService.update(db, template_id, payload)
    return RouterConfigTemplateRead.model_validate(tmpl)


@router.post(
    "/config-templates/{template_id}/preview",
    dependencies=[Depends(require_permission("router:read"))],
)
def preview_template(
    template_id: uuid.UUID,
    variables: dict,
    db: Session = Depends(get_db),
) -> dict:
    tmpl = RouterTemplateService.get(db, template_id)
    try:
        rendered = RouterConfigService.render_template(tmpl.template_body, variables)
        return {"rendered": rendered}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


# --- Config Pushes ---


@router.post(
    "/config-pushes",
    response_model=RouterConfigPushRead,
    dependencies=[Depends(require_permission("router:push_config"))],
)
def create_push(
    payload: RouterConfigPushCreate,
    initiated_by_header: str | None = Header(default=None, alias="X-Initiated-By"),
    db: Session = Depends(get_db),
) -> RouterConfigPushRead:
    try:
        user_id = (
            uuid.UUID(initiated_by_header) if initiated_by_header else uuid.uuid4()
        )
    except (ValueError, AttributeError):
        user_id = uuid.uuid4()

    try:
        push = RouterConfigService.create_push(
            db,
            commands=payload.commands,
            router_ids=payload.router_ids,
            initiated_by=user_id,
            template_id=payload.template_id,
            variable_values=payload.variable_values,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    try:
        from app.tasks.router_sync import execute_config_push

        execute_config_push.delay(str(push.id))
    except ImportError:
        pass

    return RouterConfigPushRead.model_validate(push)


@router.get(
    "/config-pushes",
    dependencies=[Depends(require_permission("router:read"))],
)
def list_pushes(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> dict:
    pushes = RouterConfigService.list_pushes(db, limit=limit, offset=offset)
    return {"items": [RouterConfigPushRead.model_validate(p) for p in pushes]}


@router.get(
    "/config-pushes/{push_id}",
    dependencies=[Depends(require_permission("router:read"))],
)
def get_push(
    push_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> dict:
    push = RouterConfigService.get_push(db, push_id)
    results = [RouterConfigPushResultRead.model_validate(r) for r in push.results]
    push_data = RouterConfigPushRead.model_validate(push)
    return {"push": push_data, "results": results}


# --- Jump Hosts ---

jump_host_router = APIRouter(prefix="/network/jump-hosts", tags=["router-management"])


@jump_host_router.get(
    "",
    dependencies=[Depends(require_permission("router:read"))],
)
def list_jump_hosts(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> dict:
    hosts = JumpHostInventory.list(db, limit=limit, offset=offset)
    return {"items": [JumpHostRead.model_validate(h) for h in hosts]}


@jump_host_router.post(
    "",
    response_model=JumpHostRead,
    dependencies=[Depends(require_permission("router:admin"))],
)
def create_jump_host(
    payload: JumpHostCreate,
    db: Session = Depends(get_db),
) -> JumpHostRead:
    jh = JumpHostInventory.create(db, payload)
    return JumpHostRead.model_validate(jh)


@jump_host_router.patch(
    "/{jh_id}",
    response_model=JumpHostRead,
    dependencies=[Depends(require_permission("router:admin"))],
)
def update_jump_host(
    jh_id: uuid.UUID,
    payload: JumpHostUpdate,
    db: Session = Depends(get_db),
) -> JumpHostRead:
    jh = JumpHostInventory.update(db, jh_id, payload)
    return JumpHostRead.model_validate(jh)


@jump_host_router.delete(
    "/{jh_id}",
    dependencies=[Depends(require_permission("router:admin"))],
)
def delete_jump_host(
    jh_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> dict:
    JumpHostInventory.delete(db, jh_id)
    return {"detail": "Jump host deleted"}


@jump_host_router.post(
    "/{jh_id}/test",
    dependencies=[Depends(require_permission("router:admin"))],
)
def test_jump_host(
    jh_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> dict:
    return JumpHostInventory.test_connection(db, jh_id)


# --- Dashboard ---


@router.get(
    "/dashboard",
    dependencies=[Depends(require_permission("router:read"))],
)
def router_dashboard(db: Session = Depends(get_db)) -> dict:
    return RouterMonitoringService.get_dashboard_summary(db)
