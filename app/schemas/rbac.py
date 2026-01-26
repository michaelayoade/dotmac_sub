from __future__ import annotations

import re
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# Permission key format: domain:action or domain:entity:action
# Each part: starts with lowercase letter, contains only lowercase letters, numbers, underscores
PERMISSION_KEY_PATTERN = re.compile(r'^[a-z][a-z0-9_]*(?:[:.][a-z][a-z0-9_]*){1,2}$')


class RoleBase(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str | None = None
    is_active: bool = True


class RoleCreate(RoleBase):
    pass


class RoleUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    description: str | None = None
    is_active: bool | None = None


class RoleRead(RoleBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class PermissionBase(BaseModel):
    key: str = Field(min_length=1, max_length=120)
    description: str | None = None
    is_active: bool = True

    @field_validator('key')
    @classmethod
    def validate_key_format(cls, v: str) -> str:
        if not PERMISSION_KEY_PATTERN.match(v):
            raise ValueError(
                'Permission key must be in format domain:action or domain:entity:action '
                '(e.g., billing:read, customer:invoice:create). '
                'Each part must start with a lowercase letter and contain only lowercase letters, numbers, and underscores.'
            )
        return v


class PermissionCreate(PermissionBase):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, exclude=True)

    @model_validator(mode="after")
    def _apply_name(self) -> "PermissionCreate":
        if self.description is None and self.name:
            self.description = self.name
        return self


class PermissionUpdate(BaseModel):
    key: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = None
    is_active: bool | None = None

    @field_validator('key')
    @classmethod
    def validate_key_format(cls, v: str | None) -> str | None:
        if v is not None and not PERMISSION_KEY_PATTERN.match(v):
            raise ValueError(
                'Permission key must be in format domain:action or domain:entity:action '
                '(e.g., billing:read, customer:invoice:create). '
                'Each part must start with a lowercase letter and contain only lowercase letters, numbers, and underscores.'
            )
        return v


class PermissionRead(PermissionBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class RolePermissionBase(BaseModel):
    role_id: UUID
    permission_id: UUID


class RolePermissionCreate(RolePermissionBase):
    pass


class RolePermissionUpdate(BaseModel):
    role_id: UUID | None = None
    permission_id: UUID | None = None


class RolePermissionRead(RolePermissionBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID


class PersonRoleBase(BaseModel):
    person_id: UUID
    role_id: UUID


class PersonRoleCreate(PersonRoleBase):
    pass


class PersonRoleUpdate(BaseModel):
    person_id: UUID | None = None
    role_id: UUID | None = None


class PersonRoleRead(PersonRoleBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    assigned_at: datetime


class PersonPermissionBase(BaseModel):
    person_id: UUID
    permission_id: UUID


class PersonPermissionCreate(PersonPermissionBase):
    pass


class PersonPermissionUpdate(BaseModel):
    person_id: UUID | None = None
    permission_id: UUID | None = None


class PersonPermissionRead(PersonPermissionBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    granted_at: datetime
    granted_by_person_id: UUID | None = None
