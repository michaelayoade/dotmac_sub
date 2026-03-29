import logging
import uuid
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.router_management import (
    JumpHost,
    Router,
    RouterAccessMethod,
    RouterInterface,
    RouterStatus,
)
from app.schemas.router_management import (
    JumpHostCreate,
    JumpHostUpdate,
    RouterCreate,
    RouterUpdate,
)
from app.services.common import apply_ordering, apply_pagination
from app.services.credential_crypto import encrypt_credential
from app.services.response import ListResponseMixin

logger = logging.getLogger(__name__)

ROUTER_CREDENTIAL_FIELDS = ("rest_api_password",)
JUMP_HOST_CREDENTIAL_FIELDS = ("ssh_key", "ssh_password")


class RouterInventory(ListResponseMixin):
    ALLOWED_ORDER_COLUMNS = {
        "name": Router.name,
        "hostname": Router.hostname,
        "management_ip": Router.management_ip,
        "status": Router.status,
        "created_at": Router.created_at,
    }

    @staticmethod
    def create(db: Session, payload: RouterCreate) -> Router:
        data = payload.model_dump(exclude_unset=True)

        for field in ROUTER_CREDENTIAL_FIELDS:
            if field in data and data[field]:
                data[field] = encrypt_credential(data[field])

        if data.get("access_method"):
            data["access_method"] = RouterAccessMethod(data["access_method"])

        if data.get("jump_host_id"):
            jh = db.get(JumpHost, data["jump_host_id"])
            if not jh:
                raise HTTPException(status_code=404, detail="Jump host not found")

        router = Router(**data)
        try:
            db.add(router)
            db.commit()
        except IntegrityError:
            db.rollback()
            raise HTTPException(
                status_code=409,
                detail=f"Router with name '{payload.name}' already exists",
            )
        db.refresh(router)
        logger.info("Router created: %s (%s)", router.name, router.id)
        return router

    @staticmethod
    def get(db: Session, router_id: uuid.UUID) -> Router:
        router = db.execute(
            select(Router).where(Router.id == router_id, Router.is_active.is_(True))
        ).scalar_one_or_none()
        if not router:
            raise HTTPException(status_code=404, detail="Router not found")
        return router

    @staticmethod
    def list(
        db: Session,
        status: str | None = None,
        access_method: str | None = None,
        jump_host_id: uuid.UUID | None = None,
        search: str | None = None,
        order_by: str = "name",
        order_dir: str = "asc",
        limit: int = 50,
        offset: int = 0,
    ) -> list[Router]:
        query = select(Router).where(Router.is_active.is_(True))

        if status:
            query = query.where(Router.status == RouterStatus(status))
        if access_method:
            query = query.where(
                Router.access_method == RouterAccessMethod(access_method)
            )
        if jump_host_id:
            query = query.where(Router.jump_host_id == jump_host_id)
        if search:
            pattern = f"%{search}%"
            query = query.where(
                Router.name.ilike(pattern)
                | Router.hostname.ilike(pattern)
                | Router.management_ip.ilike(pattern)
                | Router.location.ilike(pattern)
            )

        query = apply_ordering(
            query, order_by, order_dir, RouterInventory.ALLOWED_ORDER_COLUMNS
        )
        query = apply_pagination(query, limit, offset)
        return list(db.execute(query).scalars().all())

    @staticmethod
    def count(db: Session, status: str | None = None) -> int:
        query = select(func.count(Router.id)).where(Router.is_active.is_(True))
        if status:
            query = query.where(Router.status == RouterStatus(status))
        return db.execute(query).scalar_one()

    @staticmethod
    def update(db: Session, router_id: uuid.UUID, payload: RouterUpdate) -> Router:
        router = RouterInventory.get(db, router_id)
        data = payload.model_dump(exclude_unset=True)

        for field in ROUTER_CREDENTIAL_FIELDS:
            if field in data and data[field]:
                data[field] = encrypt_credential(data[field])

        if "access_method" in data and data["access_method"]:
            data["access_method"] = RouterAccessMethod(data["access_method"])
        if "status" in data and data["status"]:
            data["status"] = RouterStatus(data["status"])

        for key, value in data.items():
            setattr(router, key, value)

        db.commit()
        db.refresh(router)
        logger.info("Router updated: %s (%s)", router.name, router.id)
        return router

    @staticmethod
    def delete(db: Session, router_id: uuid.UUID) -> None:
        router = RouterInventory.get(db, router_id)
        router.is_active = False
        db.commit()
        logger.info("Router soft-deleted: %s (%s)", router.name, router.id)

    @staticmethod
    def upsert_interfaces(
        db: Session, router: Router, interfaces_data: list[dict]
    ) -> list[RouterInterface]:
        now = datetime.now(UTC)
        existing = {
            iface.name: iface
            for iface in db.execute(
                select(RouterInterface).where(RouterInterface.router_id == router.id)
            )
            .scalars()
            .all()
        }

        seen_names: set[str] = set()
        results: list[RouterInterface] = []

        for data in interfaces_data:
            name = data.get("name", "")
            seen_names.add(name)

            if name in existing:
                iface = existing[name]
                for key, value in data.items():
                    if key != "name":
                        setattr(iface, key, value)
                iface.synced_at = now
            else:
                iface = RouterInterface(router_id=router.id, synced_at=now, **data)
                db.add(iface)
            results.append(iface)

        for name, iface in existing.items():
            if name not in seen_names:
                db.delete(iface)

        db.commit()
        return results


class JumpHostInventory:
    @staticmethod
    def create(db: Session, payload: JumpHostCreate) -> JumpHost:
        data = payload.model_dump(exclude_unset=True)
        for field in JUMP_HOST_CREDENTIAL_FIELDS:
            if field in data and data[field]:
                data[field] = encrypt_credential(data[field])

        jh = JumpHost(**data)
        try:
            db.add(jh)
            db.commit()
        except IntegrityError:
            db.rollback()
            raise HTTPException(
                status_code=409,
                detail=f"Jump host with name '{payload.name}' already exists",
            )
        db.refresh(jh)
        logger.info("Jump host created: %s (%s)", jh.name, jh.id)
        return jh

    @staticmethod
    def get(db: Session, jh_id: uuid.UUID) -> JumpHost:
        jh = db.execute(
            select(JumpHost).where(JumpHost.id == jh_id, JumpHost.is_active.is_(True))
        ).scalar_one_or_none()
        if not jh:
            raise HTTPException(status_code=404, detail="Jump host not found")
        return jh

    @staticmethod
    def list(db: Session, limit: int = 50, offset: int = 0) -> list[JumpHost]:
        query = (
            select(JumpHost)
            .where(JumpHost.is_active.is_(True))
            .order_by(JumpHost.name)
            .limit(limit)
            .offset(offset)
        )
        return list(db.execute(query).scalars().all())

    @staticmethod
    def update(db: Session, jh_id: uuid.UUID, payload: JumpHostUpdate) -> JumpHost:
        jh = JumpHostInventory.get(db, jh_id)
        data = payload.model_dump(exclude_unset=True)
        for field in JUMP_HOST_CREDENTIAL_FIELDS:
            if field in data and data[field]:
                data[field] = encrypt_credential(data[field])

        for key, value in data.items():
            setattr(jh, key, value)

        db.commit()
        db.refresh(jh)
        logger.info("Jump host updated: %s (%s)", jh.name, jh.id)
        return jh

    @staticmethod
    def delete(db: Session, jh_id: uuid.UUID) -> None:
        jh = JumpHostInventory.get(db, jh_id)
        jh.is_active = False
        db.commit()
        logger.info("Jump host soft-deleted: %s (%s)", jh.name, jh.id)
