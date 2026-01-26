from __future__ import annotations

from typing import List

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.person import Person
from app.models.rbac import Permission, PersonPermission, PersonRole, Role, RolePermission
from app.schemas.rbac import (
    PermissionCreate,
    PermissionUpdate,
    PersonPermissionCreate,
    PersonPermissionUpdate,
    PersonRoleCreate,
    PersonRoleUpdate,
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


class PersonRoles(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: PersonRoleCreate):
        person = db.get(Person, coerce_uuid(payload.person_id))
        if not person:
            raise HTTPException(status_code=404, detail="Person not found")
        role = db.get(Role, coerce_uuid(payload.role_id))
        if not role:
            raise HTTPException(status_code=404, detail="Role not found")
        link = PersonRole(**payload.model_dump())
        db.add(link)
        db.commit()
        db.refresh(link)
        return link

    @staticmethod
    def get(db: Session, link_id: str):
        link = db.get(PersonRole, coerce_uuid(link_id))
        if not link:
            raise HTTPException(status_code=404, detail="Person role not found")
        return link

    @staticmethod
    def list(
        db: Session,
        person_id: str | None,
        role_id: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(PersonRole)
        if person_id:
            query = query.filter(PersonRole.person_id == person_id)
        if role_id:
            query = query.filter(PersonRole.role_id == role_id)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"assigned_at": PersonRole.assigned_at},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, link_id: str, payload: PersonRoleUpdate):
        link = db.get(PersonRole, coerce_uuid(link_id))
        if not link:
            raise HTTPException(status_code=404, detail="Person role not found")
        data = payload.model_dump(exclude_unset=True)
        if "person_id" in data:
            person = db.get(Person, data["person_id"])
            if not person:
                raise HTTPException(status_code=404, detail="Person not found")
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
        link = db.get(PersonRole, coerce_uuid(link_id))
        if not link:
            raise HTTPException(status_code=404, detail="Person role not found")
        db.delete(link)
        db.commit()


class PersonPermissions(ListResponseMixin):
    """Service for managing direct user-permission assignments."""

    @staticmethod
    def create(
        db: Session, payload: PersonPermissionCreate, granted_by: str | None = None
    ):
        person = db.get(Person, coerce_uuid(payload.person_id))
        if not person:
            raise HTTPException(status_code=404, detail="Person not found")
        permission = db.get(Permission, coerce_uuid(payload.permission_id))
        if not permission:
            raise HTTPException(status_code=404, detail="Permission not found")
        link = PersonPermission(
            person_id=payload.person_id,
            permission_id=payload.permission_id,
            granted_by_person_id=coerce_uuid(granted_by) if granted_by else None,
        )
        db.add(link)
        db.commit()
        db.refresh(link)
        return link

    @staticmethod
    def get(db: Session, link_id: str):
        link = db.get(PersonPermission, coerce_uuid(link_id))
        if not link:
            raise HTTPException(status_code=404, detail="Person permission not found")
        return link

    @staticmethod
    def list(
        db: Session,
        person_id: str | None,
        permission_id: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(PersonPermission)
        if person_id:
            query = query.filter(PersonPermission.person_id == coerce_uuid(person_id))
        if permission_id:
            query = query.filter(
                PersonPermission.permission_id == coerce_uuid(permission_id)
            )
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"granted_at": PersonPermission.granted_at},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def list_for_person(db: Session, person_id: str) -> List[PersonPermission]:
        """Get all direct permissions for a person."""
        return (
            db.query(PersonPermission)
            .filter(PersonPermission.person_id == coerce_uuid(person_id))
            .all()
        )

    @staticmethod
    def update(db: Session, link_id: str, payload: PersonPermissionUpdate):
        link = db.get(PersonPermission, coerce_uuid(link_id))
        if not link:
            raise HTTPException(status_code=404, detail="Person permission not found")
        data = payload.model_dump(exclude_unset=True)
        if "person_id" in data:
            person = db.get(Person, data["person_id"])
            if not person:
                raise HTTPException(status_code=404, detail="Person not found")
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
        link = db.get(PersonPermission, coerce_uuid(link_id))
        if not link:
            raise HTTPException(status_code=404, detail="Person permission not found")
        db.delete(link)
        db.commit()

    @staticmethod
    def sync_for_person(
        db: Session,
        person_id: str,
        desired_permission_ids: set[str],
        granted_by: str | None = None,
    ):
        """Sync direct permissions for a person - add new, remove unselected."""
        person_uuid = coerce_uuid(person_id)
        existing = (
            db.query(PersonPermission)
            .filter(PersonPermission.person_id == person_uuid)
            .all()
        )
        existing_map = {str(pp.permission_id): pp for pp in existing}

        # Remove permissions not in desired set
        for perm_id, person_perm in existing_map.items():
            if perm_id not in desired_permission_ids:
                db.delete(person_perm)

        # Add new permissions
        for perm_id in desired_permission_ids:
            if perm_id not in existing_map:
                db.add(
                    PersonPermission(
                        person_id=person_uuid,
                        permission_id=coerce_uuid(perm_id),
                        granted_by_person_id=coerce_uuid(granted_by)
                        if granted_by
                        else None,
                    )
                )

        db.commit()


roles = Roles()
permissions = Permissions()
role_permissions = RolePermissions()
person_roles = PersonRoles()
person_permissions = PersonPermissions()
