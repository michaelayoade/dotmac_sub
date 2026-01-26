from __future__ import annotations

from typing import List

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.subscriber import Subscriber
from app.models.rbac import Permission, SubscriberPermission, SubscriberRole, Role, RolePermission
from app.schemas.rbac import (
    PermissionCreate,
    PermissionUpdate,
    SubscriberPermissionCreate,
    SubscriberPermissionUpdate,
    SubscriberRoleCreate,
    SubscriberRoleUpdate,
    RoleCreate,
    RolePermissionCreate,
    RolePermissionUpdate,
    RoleUpdate,
)
from app.services.common import apply_ordering, apply_pagination, coerce_uuid
from app.services.response import ListResponseMixin


class Roles(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: RoleCreate):
        role = Role(**payload.model_dump())
        db.add(role)
        db.commit()
        db.refresh(role)
        return role

    @staticmethod
    def get(db: Session, role_id: str):
        role = db.get(Role, coerce_uuid(role_id))
        if not role:
            raise HTTPException(status_code=404, detail="Role not found")
        return role

    @staticmethod
    def list(
        db: Session,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(Role)
        if is_active is None:
            query = query.filter(Role.is_active.is_(True))
        else:
            query = query.filter(Role.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": Role.created_at, "name": Role.name},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, role_id: str, payload: RoleUpdate):
        role = db.get(Role, coerce_uuid(role_id))
        if not role:
            raise HTTPException(status_code=404, detail="Role not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(role, key, value)
        db.commit()
        db.refresh(role)
        return role

    @staticmethod
    def delete(db: Session, role_id: str):
        role = db.get(Role, coerce_uuid(role_id))
        if not role:
            raise HTTPException(status_code=404, detail="Role not found")
        role.is_active = False
        db.commit()


class Permissions(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: PermissionCreate):
        permission = Permission(**payload.model_dump())
        db.add(permission)
        db.commit()
        db.refresh(permission)
        return permission

    @staticmethod
    def get(db: Session, permission_id: str):
        permission = db.get(Permission, coerce_uuid(permission_id))
        if not permission:
            raise HTTPException(status_code=404, detail="Permission not found")
        return permission

    @staticmethod
    def list(
        db: Session,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(Permission)
        if is_active is None:
            query = query.filter(Permission.is_active.is_(True))
        else:
            query = query.filter(Permission.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": Permission.created_at, "key": Permission.key},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, permission_id: str, payload: PermissionUpdate):
        permission = db.get(Permission, coerce_uuid(permission_id))
        if not permission:
            raise HTTPException(status_code=404, detail="Permission not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(permission, key, value)
        db.commit()
        db.refresh(permission)
        return permission

    @staticmethod
    def delete(db: Session, permission_id: str):
        permission = db.get(Permission, coerce_uuid(permission_id))
        if not permission:
            raise HTTPException(status_code=404, detail="Permission not found")
        permission.is_active = False
        db.commit()


class RolePermissions(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: RolePermissionCreate):
        role = db.get(Role, coerce_uuid(payload.role_id))
        if not role:
            raise HTTPException(status_code=404, detail="Role not found")
        permission = db.get(Permission, coerce_uuid(payload.permission_id))
        if not permission:
            raise HTTPException(status_code=404, detail="Permission not found")
        link = RolePermission(**payload.model_dump())
        db.add(link)
        db.commit()
        db.refresh(link)
        return link

    @staticmethod
    def get(db: Session, link_id: str):
        link = db.get(RolePermission, coerce_uuid(link_id))
        if not link:
            raise HTTPException(status_code=404, detail="Role permission not found")
        return link

    @staticmethod
    def list(
        db: Session,
        role_id: str | None,
        permission_id: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(RolePermission)
        if role_id:
            query = query.filter(RolePermission.role_id == role_id)
        if permission_id:
            query = query.filter(RolePermission.permission_id == permission_id)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"role_id": RolePermission.role_id},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, link_id: str, payload: RolePermissionUpdate):
        link = db.get(RolePermission, coerce_uuid(link_id))
        if not link:
            raise HTTPException(status_code=404, detail="Role permission not found")
        data = payload.model_dump(exclude_unset=True)
        if "role_id" in data:
            role = db.get(Role, data["role_id"])
            if not role:
                raise HTTPException(status_code=404, detail="Role not found")
        if "permission_id" in data:
            permission = db.get(Permission, data["permission_id"])
            if not permission:
                raise HTTPException(status_code=404, detail="Permission not found")
        for key, value in data.items():
            setattr(link, key, value)
        db.commit()
        db.refresh(link)
        return link

    @staticmethod
    def delete(db: Session, link_id: str):
        link = db.get(RolePermission, coerce_uuid(link_id))
        if not link:
            raise HTTPException(status_code=404, detail="Role permission not found")
        db.delete(link)
        db.commit()


class SubscriberRoles(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: SubscriberRoleCreate):
        subscriber = db.get(Subscriber, coerce_uuid(payload.subscriber_id))
        if not subscriber:
            raise HTTPException(status_code=404, detail="Subscriber not found")
        role = db.get(Role, coerce_uuid(payload.role_id))
        if not role:
            raise HTTPException(status_code=404, detail="Role not found")
        link = SubscriberRole(**payload.model_dump())
        db.add(link)
        db.commit()
        db.refresh(link)
        return link

    @staticmethod
    def get(db: Session, link_id: str):
        link = db.get(SubscriberRole, coerce_uuid(link_id))
        if not link:
            raise HTTPException(status_code=404, detail="Subscriber role not found")
        return link

    @staticmethod
    def list(
        db: Session,
        subscriber_id: str | None,
        role_id: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(SubscriberRole)
        if subscriber_id:
            query = query.filter(SubscriberRole.subscriber_id == subscriber_id)
        if role_id:
            query = query.filter(SubscriberRole.role_id == role_id)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"assigned_at": SubscriberRole.assigned_at},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, link_id: str, payload: SubscriberRoleUpdate):
        link = db.get(SubscriberRole, coerce_uuid(link_id))
        if not link:
            raise HTTPException(status_code=404, detail="Subscriber role not found")
        data = payload.model_dump(exclude_unset=True)
        if "subscriber_id" in data:
            subscriber = db.get(Subscriber, data["subscriber_id"])
            if not subscriber:
                raise HTTPException(status_code=404, detail="Subscriber not found")
        if "role_id" in data:
            role = db.get(Role, data["role_id"])
            if not role:
                raise HTTPException(status_code=404, detail="Role not found")
        for key, value in data.items():
            setattr(link, key, value)
        db.commit()
        db.refresh(link)
        return link

    @staticmethod
    def delete(db: Session, link_id: str):
        link = db.get(SubscriberRole, coerce_uuid(link_id))
        if not link:
            raise HTTPException(status_code=404, detail="Subscriber role not found")
        db.delete(link)
        db.commit()


class SubscriberPermissions(ListResponseMixin):
    """Service for managing direct user-permission assignments."""

    @staticmethod
    def create(
        db: Session, payload: SubscriberPermissionCreate, granted_by: str | None = None
    ):
        subscriber = db.get(Subscriber, coerce_uuid(payload.subscriber_id))
        if not subscriber:
            raise HTTPException(status_code=404, detail="Subscriber not found")
        permission = db.get(Permission, coerce_uuid(payload.permission_id))
        if not permission:
            raise HTTPException(status_code=404, detail="Permission not found")
        link = SubscriberPermission(
            subscriber_id=payload.subscriber_id,
            permission_id=payload.permission_id,
            granted_by_subscriber_id=coerce_uuid(granted_by) if granted_by else None,
        )
        db.add(link)
        db.commit()
        db.refresh(link)
        return link

    @staticmethod
    def get(db: Session, link_id: str):
        link = db.get(SubscriberPermission, coerce_uuid(link_id))
        if not link:
            raise HTTPException(status_code=404, detail="Subscriber permission not found")
        return link

    @staticmethod
    def list(
        db: Session,
        subscriber_id: str | None,
        permission_id: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(SubscriberPermission)
        if subscriber_id:
            query = query.filter(SubscriberPermission.subscriber_id == coerce_uuid(subscriber_id))
        if permission_id:
            query = query.filter(
                SubscriberPermission.permission_id == coerce_uuid(permission_id)
            )
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"granted_at": SubscriberPermission.granted_at},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def list_for_subscriber(db: Session, subscriber_id: str) -> List[SubscriberPermission]:
        """Get all direct permissions for a subscriber."""
        return (
            db.query(SubscriberPermission)
            .filter(SubscriberPermission.subscriber_id == coerce_uuid(subscriber_id))
            .all()
        )

    @staticmethod
    def update(db: Session, link_id: str, payload: SubscriberPermissionUpdate):
        link = db.get(SubscriberPermission, coerce_uuid(link_id))
        if not link:
            raise HTTPException(status_code=404, detail="Subscriber permission not found")
        data = payload.model_dump(exclude_unset=True)
        if "subscriber_id" in data:
            subscriber = db.get(Subscriber, data["subscriber_id"])
            if not subscriber:
                raise HTTPException(status_code=404, detail="Subscriber not found")
        if "permission_id" in data:
            permission = db.get(Permission, data["permission_id"])
            if not permission:
                raise HTTPException(status_code=404, detail="Permission not found")
        for key, value in data.items():
            setattr(link, key, value)
        db.commit()
        db.refresh(link)
        return link

    @staticmethod
    def delete(db: Session, link_id: str):
        link = db.get(SubscriberPermission, coerce_uuid(link_id))
        if not link:
            raise HTTPException(status_code=404, detail="Subscriber permission not found")
        db.delete(link)
        db.commit()

    @staticmethod
    def sync_for_person(
        db: Session,
        subscriber_id: str,
        desired_permission_ids: set[str],
        granted_by: str | None = None,
    ):
        """Sync direct permissions for a subscriber - add new, remove unselected."""
        subscriber_uuid = coerce_uuid(subscriber_id)
        existing = (
            db.query(SubscriberPermission)
            .filter(SubscriberPermission.subscriber_id == subscriber_uuid)
            .all()
        )
        existing_map = {str(pp.permission_id): pp for pp in existing}

        # Remove permissions not in desired set
        for perm_id, subscriber_perm in existing_map.items():
            if perm_id not in desired_permission_ids:
                db.delete(subscriber_perm)

        # Add new permissions
        for perm_id in desired_permission_ids:
            if perm_id not in existing_map:
                db.add(
                    SubscriberPermission(
                        subscriber_id=subscriber_uuid,
                        permission_id=coerce_uuid(perm_id),
                        granted_by_subscriber_id=coerce_uuid(granted_by)
                        if granted_by
                        else None,
                    )
                )

        db.commit()


roles = Roles()
permissions = Permissions()
role_permissions = RolePermissions()
subscriber_roles = SubscriberRoles()
subscriber_permissions = SubscriberPermissions()
