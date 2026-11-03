"""Persisted break-glass actor for API-key owner fixtures (SUB-1).

The owner now resolves the acting principal to evaluate the no-escalation
rule, so a fixture actor that requests scopes must exist as an admin.
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.rbac import Role, SystemUserRole
from app.models.system_user import SystemUser


def persist_admin_actor(db: Session, actor_id: str | None) -> None:
    assert actor_id is not None
    user_id = UUID(actor_id)
    if db.get(SystemUser, user_id) is not None:
        return
    role = db.scalars(select(Role).where(Role.name == "admin")).first()
    if role is None:
        role = Role(name="admin", is_active=True)
        db.add(role)
        db.flush()
    db.add(
        SystemUser(
            id=user_id,
            first_name="Fixture",
            last_name="Admin",
            email=f"fixture-admin-{user_id.hex}@example.test",
        )
    )
    db.flush()
    db.add(SystemUserRole(system_user_id=user_id, role_id=role.id))
    db.commit()


def persist_wildcard_api_key_actor(db: Session, key_id: str | None) -> None:
    """Persist an API-key actor holding every scope (covers any requested)."""
    from app.models.auth import ApiKey

    assert key_id is not None
    if db.get(ApiKey, UUID(key_id)) is not None:
        return
    db.add(
        ApiKey(
            id=UUID(key_id),
            key_hash=f"fixture-actor-{key_id}",
            scopes=["*"],
            is_active=True,
        )
    )
    db.commit()
