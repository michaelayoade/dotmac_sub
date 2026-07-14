from __future__ import annotations

import hashlib
import logging
import uuid

from fastapi import HTTPException
from jinja2 import (
    BaseLoader,
    Environment,
    StrictUndefined,
    TemplateSyntaxError,
    UndefinedError,
    select_autoescape,
)
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.network_operation import (
    NetworkOperationTargetType,
    NetworkOperationType,
)
from app.models.router_management import (
    Router,
    RouterConfigPush,
    RouterConfigPushResult,
    RouterConfigPushStatus,
    RouterConfigSnapshot,
    RouterConfigTemplate,
    RouterPushResultStatus,
    RouterSnapshotSource,
    RouterTemplateCategory,
)
from app.schemas.router_management import (
    RouterConfigTemplateCreate,
    RouterConfigTemplateUpdate,
)
from app.services.network_operations import network_operations
from app.services.router_management.connection import check_dangerous_commands
from app.services.router_management.write_adapter import (
    RouterWriteUnsupported,
    parse_routeros_rest_commands,
)

logger = logging.getLogger(__name__)

_jinja_env = Environment(
    loader=BaseLoader(),
    undefined=StrictUndefined,
    autoescape=select_autoescape(
        enabled_extensions=("html", "xml"),
        default_for_string=False,
        default=False,
    ),
)


class RouterConfigService:
    @staticmethod
    def store_snapshot(
        db: Session,
        router_id: uuid.UUID,
        config_export: str,
        source: RouterSnapshotSource,
        captured_by: uuid.UUID | None = None,
        *,
        commit: bool = True,
    ) -> RouterConfigSnapshot:
        config_hash = hashlib.sha256(config_export.encode()).hexdigest()

        snap = RouterConfigSnapshot(
            router_id=router_id,
            config_export=config_export,
            config_hash=config_hash,
            source=source,
            captured_by=captured_by,
        )
        db.add(snap)
        if commit:
            db.commit()
        else:
            db.flush()
        db.refresh(snap)
        logger.info("Config snapshot stored for router %s: %s", router_id, snap.id)
        return snap

    @staticmethod
    def capture_from_router(
        db: Session,
        router: Router,
    ) -> RouterConfigSnapshot:
        """Connect to the router, export its config, and store the snapshot."""
        from app.services.router_management.config_export import fetch_config_export

        try:
            config_text = fetch_config_export(router)
        except Exception as exc:
            raise RuntimeError(f"Failed to export config: {exc}") from exc

        return RouterConfigService.store_snapshot(
            db,
            router_id=router.id,
            config_export=config_text,
            source=RouterSnapshotSource.manual,
        )

    @staticmethod
    def list_snapshots(
        db: Session,
        router_id: uuid.UUID,
        limit: int = 50,
        offset: int = 0,
    ) -> list[RouterConfigSnapshot]:
        query = (
            select(RouterConfigSnapshot)
            .where(RouterConfigSnapshot.router_id == router_id)
            .order_by(RouterConfigSnapshot.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(db.execute(query).scalars().all())

    @staticmethod
    def get_snapshot(
        db: Session,
        snapshot_id: uuid.UUID,
        router_id: uuid.UUID | None = None,
    ) -> RouterConfigSnapshot:
        snap = db.get(RouterConfigSnapshot, snapshot_id)
        # When a router scope is supplied, a snapshot belonging to a different
        # router must read as "not found" — otherwise any snapshot id is
        # fetchable under any router URL.
        if not snap or (router_id is not None and snap.router_id != router_id):
            raise HTTPException(status_code=404, detail="Snapshot not found")
        return snap

    @staticmethod
    def render_template(template_body: str, variables: dict) -> str:
        try:
            template = _jinja_env.from_string(template_body)
            return template.render(**variables)
        except (TemplateSyntaxError, UndefinedError, TypeError) as exc:
            raise ValueError(f"Template rendering failed: {exc}") from exc

    @staticmethod
    def create_push(
        db: Session,
        commands: list[str],
        router_ids: list[uuid.UUID],
        initiated_by: uuid.UUID,
        template_id: uuid.UUID | None = None,
        variable_values: dict | None = None,
        dry_run: bool = False,
        failure_policy: str = "continue",
        allow_dangerous_commands: bool = False,
    ) -> RouterConfigPush:
        if failure_policy not in {"continue", "abort"}:
            raise ValueError("Failure policy must be 'continue' or 'abort'.")
        if allow_dangerous_commands:
            raise ValueError(
                "Blocked-command override is disabled; use a reviewed typed operation."
            )
        check_dangerous_commands(commands)
        try:
            parse_routeros_rest_commands(commands)
        except RouterWriteUnsupported as exc:
            raise ValueError(str(exc)) from exc

        unique_router_ids = list(dict.fromkeys(router_ids))
        routers = list(
            db.scalars(select(Router).where(Router.id.in_(unique_router_ids))).all()
        )
        found_ids = {router.id for router in routers}
        missing = [
            str(router_id)
            for router_id in unique_router_ids
            if router_id not in found_ids
        ]
        if missing:
            raise ValueError(f"Router targets not found: {', '.join(missing)}")
        inactive = [router.name for router in routers if not router.is_active]
        if inactive:
            raise ValueError(f"Router targets are inactive: {', '.join(inactive)}")

        push = RouterConfigPush(
            template_id=template_id,
            commands=commands,
            variable_values=variable_values,
            dry_run=dry_run,
            failure_policy=failure_policy,
            allow_dangerous_commands=allow_dangerous_commands,
            initiated_by=initiated_by,
            status=RouterConfigPushStatus.pending,
        )
        db.add(push)
        db.flush()

        parent_operation = network_operations.start(
            db,
            NetworkOperationType.router_bulk_push,
            NetworkOperationTargetType.system,
            str(push.id),
            correlation_key=f"router-bulk-push:{push.id}",
            input_payload={
                "push_id": str(push.id),
                "router_ids": [str(router_id) for router_id in unique_router_ids],
                "command_count": len(commands),
                "dry_run": dry_run,
                "failure_policy": failure_policy,
            },
            initiated_by=str(initiated_by),
        )
        push.operation_id = parent_operation.id

        for rid in unique_router_ids:
            child_operation = network_operations.start(
                db,
                NetworkOperationType.router_config_push,
                NetworkOperationTargetType.router,
                str(rid),
                correlation_key=f"router-config-push:{push.id}:{rid}",
                input_payload={
                    "push_id": str(push.id),
                    "command_count": len(commands),
                    "dry_run": dry_run,
                },
                parent_id=str(parent_operation.id),
                initiated_by=str(initiated_by),
            )
            result = RouterConfigPushResult(
                push_id=push.id,
                router_id=rid,
                operation_id=child_operation.id,
                status=RouterPushResultStatus.pending,
            )
            db.add(result)

        db.commit()
        db.refresh(push)
        logger.info(
            "Config push created: %s (%d routers, %d commands)",
            push.id,
            len(router_ids),
            len(commands),
        )
        return push

    @staticmethod
    def get_push(db: Session, push_id: uuid.UUID) -> RouterConfigPush:
        push = db.get(RouterConfigPush, push_id)
        if not push:
            raise HTTPException(status_code=404, detail="Config push not found")
        return push

    @staticmethod
    def list_pushes(
        db: Session, limit: int = 50, offset: int = 0
    ) -> list[RouterConfigPush]:
        query = (
            select(RouterConfigPush)
            .order_by(RouterConfigPush.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(db.execute(query).scalars().all())

    @staticmethod
    def list_push_results(
        db: Session, router_id: uuid.UUID, limit: int = 20
    ) -> list[RouterConfigPushResult]:
        """Return recent push results for the given router, newest first."""
        query = (
            select(RouterConfigPushResult)
            .where(RouterConfigPushResult.router_id == router_id)
            .order_by(RouterConfigPushResult.created_at.desc())
            .limit(limit)
        )
        return list(db.execute(query).scalars().all())


class RouterTemplateService:
    @staticmethod
    def create(
        db: Session, payload: RouterConfigTemplateCreate
    ) -> RouterConfigTemplate:
        data = payload.model_dump(exclude_unset=True)
        if "category" in data:
            data["category"] = RouterTemplateCategory(data["category"])

        tmpl = RouterConfigTemplate(**data)
        try:
            db.add(tmpl)
            db.commit()
        except IntegrityError:
            db.rollback()
            raise HTTPException(
                status_code=409,
                detail=f"Template with name '{payload.name}' already exists",
            )
        db.refresh(tmpl)
        logger.info("Config template created: %s (%s)", tmpl.name, tmpl.id)
        return tmpl

    @staticmethod
    def get(db: Session, template_id: uuid.UUID) -> RouterConfigTemplate:
        tmpl = db.get(RouterConfigTemplate, template_id)
        if not tmpl:
            raise HTTPException(status_code=404, detail="Template not found")
        return tmpl

    @staticmethod
    def list(
        db: Session,
        category: str | None = None,
        active_only: bool = True,
        limit: int = 50,
        offset: int = 0,
    ) -> list[RouterConfigTemplate]:
        query = select(RouterConfigTemplate)
        if active_only:
            query = query.where(RouterConfigTemplate.is_active.is_(True))
        if category:
            query = query.where(
                RouterConfigTemplate.category == RouterTemplateCategory(category)
            )
        query = query.order_by(RouterConfigTemplate.name).limit(limit).offset(offset)
        return list(db.execute(query).scalars().all())

    @staticmethod
    def update(
        db: Session,
        template_id: uuid.UUID,
        payload: RouterConfigTemplateUpdate,
    ) -> RouterConfigTemplate:
        tmpl = RouterTemplateService.get(db, template_id)
        data = payload.model_dump(exclude_unset=True)
        if "category" in data and data["category"]:
            data["category"] = RouterTemplateCategory(data["category"])

        for key, value in data.items():
            setattr(tmpl, key, value)

        db.commit()
        db.refresh(tmpl)
        logger.info("Config template updated: %s (%s)", tmpl.name, tmpl.id)
        return tmpl

    @staticmethod
    def delete(db: Session, template_id: uuid.UUID) -> None:
        tmpl = RouterTemplateService.get(db, template_id)
        tmpl.is_active = False
        db.commit()
        logger.info("Config template soft-deleted: %s (%s)", tmpl.name, tmpl.id)
