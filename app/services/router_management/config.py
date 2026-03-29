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
)
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.router_management import (
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
from app.services.router_management.connection import check_dangerous_commands

logger = logging.getLogger(__name__)

_jinja_env = Environment(loader=BaseLoader(), undefined=StrictUndefined)  # noqa: S701


class RouterConfigService:
    @staticmethod
    def store_snapshot(
        db: Session,
        router_id: uuid.UUID,
        config_export: str,
        source: RouterSnapshotSource,
        captured_by: uuid.UUID | None = None,
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
        db.commit()
        db.refresh(snap)
        logger.info("Config snapshot stored for router %s: %s", router_id, snap.id)
        return snap

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
    def get_snapshot(db: Session, snapshot_id: uuid.UUID) -> RouterConfigSnapshot:
        snap = db.get(RouterConfigSnapshot, snapshot_id)
        if not snap:
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
    ) -> RouterConfigPush:
        check_dangerous_commands(commands)

        push = RouterConfigPush(
            template_id=template_id,
            commands=commands,
            variable_values=variable_values,
            initiated_by=initiated_by,
            status=RouterConfigPushStatus.pending,
        )
        db.add(push)
        db.flush()

        for rid in router_ids:
            result = RouterConfigPushResult(
                push_id=push.id,
                router_id=rid,
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
