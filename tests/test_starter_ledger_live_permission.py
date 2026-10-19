"""The D1 switch needs a dedicated permission on top of control:settings:write."""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import select

from app.models.audit import AuditEvent
from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.rbac import Permission, SystemUserPermission
from app.models.subscription_engine import SettingValueType
from app.models.system_user import SystemUser
from app.schemas.settings import DomainSettingUpdate
from app.services.billing import starter_policies
from app.services.domain_settings import (
    ADMIN_SETTINGS_FORM_WRITE_SCOPE,
    AdminSettingsFormUpdateError,
    AdminSettingWrite,
    ApplyAdminSettingsFormCommand,
    apply_admin_settings_form_updates,
)
from app.services.owner_commands import CommandContext
from app.services.settings_spec import get_spec

PERMISSION = "billing:starter_ledger_live:write"


def _user(db) -> SystemUser:
    user = SystemUser(
        id=uuid4(), first_name="A", last_name="B", email=f"a-{uuid4().hex}@x.test"
    )
    db.add(user)
    db.commit()
    return user


def _grant(db, user: SystemUser) -> None:
    permission = db.scalars(select(Permission).where(Permission.key == PERMISSION)).first()
    if permission is None:
        permission = Permission(key=PERMISSION, description="test")
        db.add(permission)
        db.flush()
    db.add(SystemUserPermission(system_user_id=user.id, permission_id=permission.id))
    db.commit()


def _write(db, actor: str, key: str, value: bool):
    return apply_admin_settings_form_updates(
        db,
        ApplyAdminSettingsFormCommand(
            context=CommandContext.system(
                actor=actor,
                scope=ADMIN_SETTINGS_FORM_WRITE_SCOPE,
                reason="flip switch",
            ),
            updates=(
                AdminSettingWrite(
                    domain=SettingDomain.billing,
                    key=key,
                    payload=DomainSettingUpdate(
                        value_type=SettingValueType.boolean,
                        value_text="true" if value else "false",
                        value_json=value,
                    ),
                ),
            ),
        ),
    )


def _audits(db) -> list[AuditEvent]:
    return list(
        db.scalars(
            select(AuditEvent).where(
                AuditEvent.action == "control.settings_form_updated"
            )
        )
    )


def test_spec_requires_dedicated_permission() -> None:
    spec = get_spec(SettingDomain.billing, "starter_ledger_live_enabled")
    assert spec is not None
    assert spec.required_permission == PERMISSION


def test_write_without_permission_is_refused_and_audited(db_session) -> None:
    user = _user(db_session)
    with pytest.raises(AdminSettingsFormUpdateError) as caught:
        _write(db_session, f"user:{user.id}", "starter_ledger_live_enabled", True)
    assert caught.value.code.endswith("permission_denied")
    assert starter_policies.starter_ledger_live_enabled(db_session) is False
    row = db_session.scalars(
        select(DomainSetting).where(DomainSetting.key == "starter_ledger_live_enabled")
    ).first()
    assert row is None
    refused = [e for e in _audits(db_session) if e.is_success is False]
    assert len(refused) == 1
    meta = refused[0].metadata_
    assert meta["outcome"] == "refused"
    assert meta["setting_keys"] == ["billing.starter_ledger_live_enabled"]
    assert meta["required_permissions"] == [PERMISSION]


def test_non_user_actor_is_refused(db_session) -> None:
    with pytest.raises(AdminSettingsFormUpdateError):
        _write(db_session, "api_key:" + str(uuid4()), "starter_ledger_live_enabled", True)
    with pytest.raises(AdminSettingsFormUpdateError):
        _write(db_session, "system:job", "starter_ledger_live_enabled", True)


def test_write_with_permission_succeeds_and_is_audited(db_session) -> None:
    user = _user(db_session)
    _grant(db_session, user)
    outcome = _write(db_session, f"user:{user.id}", "starter_ledger_live_enabled", True)
    assert outcome.updated_keys == ("billing.starter_ledger_live_enabled",)
    audits = _audits(db_session)
    assert len(audits) == 1
    assert audits[0].is_success is True


def test_other_owner_command_keys_are_unaffected(db_session) -> None:
    user = _user(db_session)  # holds no dedicated permission
    outcome = _write(
        db_session, f"user:{user.id}", "sole_approver_exception_enabled", False
    )
    assert outcome.updated_keys == ("billing.sole_approver_exception_enabled",)
    assert all(e.is_success is not False for e in _audits(db_session))
