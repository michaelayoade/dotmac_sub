"""The D1 switch needs a dedicated permission on top of control:settings:write."""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import select

from app.models.audit import AuditEvent
from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.rbac import (
    Permission,
    Role,
    RolePermission,
    SystemUserPermission,
    SystemUserRole,
)
from app.models.subscription_engine import SettingValueType
from app.models.system_user import SystemUser
from app.schemas.settings import DomainSettingUpdate
from app.services import domain_settings, settings_spec, web_system_settings_forms
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
    db.flush()
    db.expunge(user)  # keep attributes loaded: no autobegin when read later
    db.commit()
    return user


def _grant(db, user: SystemUser) -> None:
    permission = db.scalars(
        select(Permission).where(Permission.key == PERMISSION)
    ).first()
    if permission is None:
        permission = Permission(key=PERMISSION, description="test")
        db.add(permission)
        db.flush()
    db.add(SystemUserPermission(system_user_id=user.id, permission_id=permission.id))
    db.commit()


def _write(db, actor: str, key: str, value: bool):
    db.commit()  # owner commands need an idle session
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
        _write(
            db_session, "api_key:" + str(uuid4()), "starter_ledger_live_enabled", True
        )
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
    assert starter_policies.starter_ledger_live_enabled(db_session) is True


def test_other_owner_command_keys_are_unaffected(db_session) -> None:
    user = _user(db_session)  # holds no dedicated permission
    outcome = _write(
        db_session, f"user:{user.id}", "sole_approver_exception_enabled", False
    )
    assert outcome.updated_keys == ("billing.sole_approver_exception_enabled",)
    assert all(e.is_success is not False for e in _audits(db_session))


def _role_with(db, user: SystemUser, *, name: str, keys: list[str], active=True):
    role = Role(name=f"{name}-{uuid4().hex[:6]}", is_active=active)
    db.add(role)
    db.flush()
    for key in keys:
        permission = db.scalars(select(Permission).where(Permission.key == key)).first()
        if permission is None:
            permission = Permission(key=key, description="test")
            db.add(permission)
            db.flush()
        db.add(RolePermission(role_id=role.id, permission_id=permission.id))
    db.add(SystemUserRole(system_user_id=user.id, role_id=role.id))
    db.commit()


def _context_for(user: SystemUser) -> CommandContext:
    return CommandContext.system(
        actor=f"user:{user.id}",
        scope=ADMIN_SETTINGS_FORM_WRITE_SCOPE,
        reason="flip switch",
    )


def _switch_spec() -> settings_spec.SettingSpec:
    spec = get_spec(SettingDomain.billing, "starter_ledger_live_enabled")
    assert spec is not None
    return spec


def _refused_count(db) -> int:
    return len([e for e in _audits(db) if e.is_success is False])


def test_billing_tab_save_neither_writes_nor_refuses_the_switch(db_session) -> None:
    user = _user(db_session)  # no dedicated permission
    topup = get_spec(SettingDomain.billing, "topup_max_amount")
    assert topup is not None
    # The tab submits every billing spec; the checkbox is simply absent.
    errors = web_system_settings_forms.upsert_settings_from_specs(
        db=db_session,
        form={topup.key: "2000000"},
        specs=[topup, _switch_spec()],
        service=domain_settings.billing_settings,
        context=_context_for(user),
    )
    assert errors == []
    saved = domain_settings.billing_settings.get_by_key(db_session, topup.key)
    assert saved.value_text == "2000000"
    assert (
        db_session.scalars(
            select(DomainSetting).where(
                DomainSetting.key == "starter_ledger_live_enabled"
            )
        ).first()
        is None
    )
    assert _refused_count(db_session) == 0


def test_tab_preparation_skips_gated_specs_for_every_billing_spec() -> None:
    updates, _errors = web_system_settings_forms._prepare_settings_from_specs(
        form={}, specs=settings_spec.list_specs(SettingDomain.billing)
    )
    assert "starter_ledger_live_enabled" not in {u.key for u in updates}


def test_dedicated_route_service_refuses_without_permission_and_audits(
    db_session,
) -> None:
    user = _user(db_session)
    errors = web_system_settings_forms.set_starter_ledger_live_switch(
        db=db_session, enabled=True, context=_context_for(user)
    )
    assert errors
    assert starter_policies.starter_ledger_live_enabled(db_session) is False
    assert _refused_count(db_session) == 1


def test_dedicated_route_service_allows_granted_user(db_session) -> None:
    user = _user(db_session)
    _grant(db_session, user)
    errors = web_system_settings_forms.set_starter_ledger_live_switch(
        db=db_session, enabled=True, context=_context_for(user)
    )
    assert errors == ()
    assert starter_policies.starter_ledger_live_enabled(db_session) is True


@pytest.mark.parametrize(
    "role_name,keys",
    [("admin", []), ("wild-all", ["*"]), ("wild-billing", ["billing:*"])],
)
def test_admin_role_and_wildcards_do_not_satisfy_the_grant(
    db_session, role_name, keys
) -> None:
    user = _user(db_session)
    _role_with(db_session, user, name=role_name, keys=keys)
    errors = web_system_settings_forms.set_starter_ledger_live_switch(
        db=db_session, enabled=True, context=_context_for(user)
    )
    assert errors
    assert starter_policies.starter_ledger_live_enabled(db_session) is False
    assert _refused_count(db_session) == 1


def test_inactive_role_grant_does_not_count(db_session) -> None:
    user = _user(db_session)
    _role_with(db_session, user, name="inactive", keys=[PERMISSION], active=False)
    errors = web_system_settings_forms.set_starter_ledger_live_switch(
        db=db_session, enabled=True, context=_context_for(user)
    )
    assert errors
    assert starter_policies.starter_ledger_live_enabled(db_session) is False


def test_backstop_refuses_when_grant_is_revoked_mid_write(
    db_session, monkeypatch
) -> None:
    user = _user(db_session)  # holds nothing: the "revoked" state
    real = domain_settings._missing_required_permissions
    calls: list[int] = []

    def _pre_check_passes_once(db, command):
        calls.append(1)
        return {} if len(calls) == 1 else real(db, command)

    monkeypatch.setattr(
        domain_settings, "_missing_required_permissions", _pre_check_passes_once
    )
    errors = web_system_settings_forms.set_starter_ledger_live_switch(
        db=db_session, enabled=True, context=_context_for(user)
    )
    assert len(calls) == 2
    assert errors
    assert starter_policies.starter_ledger_live_enabled(db_session) is False


def test_turning_the_switch_off_needs_only_settings_write_and_is_audited(
    db_session,
) -> None:
    user = _user(db_session)  # no dedicated permission
    errors = web_system_settings_forms.set_starter_ledger_live_switch(
        db=db_session, enabled=False, context=_context_for(user)
    )
    assert errors == ()
    assert starter_policies.starter_ledger_live_enabled(db_session) is False
    audits = _audits(db_session)
    assert len(audits) == 1
    assert audits[0].is_success is True


def test_turning_the_switch_on_without_permission_is_refused_and_audited(
    db_session,
) -> None:
    user = _user(db_session)
    errors = web_system_settings_forms.set_starter_ledger_live_switch(
        db=db_session, enabled=True, context=_context_for(user)
    )
    assert errors
    assert _refused_count(db_session) == 1


def test_off_write_through_owner_command_needs_no_dedicated_permission(
    db_session,
) -> None:
    user = _user(db_session)
    outcome = _write(
        db_session, f"user:{user.id}", "starter_ledger_live_enabled", False
    )
    assert outcome.updated_keys == ("billing.starter_ledger_live_enabled",)


def test_admin_only_grant_keys_match_required_permissions() -> None:
    from app.services.permission_authorization import (
        ADMIN_ONLY_GRANT_PERMISSION_KEYS,
    )

    declared = {
        spec.required_permission
        for spec in settings_spec.SETTINGS_SPECS
        if spec.required_permission
    }
    assert declared <= ADMIN_ONLY_GRANT_PERMISSION_KEYS


def test_direct_only_keys_are_admin_only_keys() -> None:
    from app.services.permission_authorization import (
        ADMIN_ONLY_GRANT_PERMISSION_KEYS,
        DIRECT_ONLY_PERMISSION_KEYS,
    )

    assert DIRECT_ONLY_PERMISSION_KEYS <= ADMIN_ONLY_GRANT_PERMISSION_KEYS
    assert PERMISSION in DIRECT_ONLY_PERMISSION_KEYS


def test_disable_decision_uses_the_coerced_value(db_session) -> None:
    user = _user(db_session)  # no dedicated permission
    # A truthy spelling is ON, not a disable write: refused without the grant.
    db_session.commit()
    with pytest.raises(AdminSettingsFormUpdateError):
        apply_admin_settings_form_updates(
            db_session,
            ApplyAdminSettingsFormCommand(
                context=CommandContext.system(
                    actor=f"user:{user.id}",
                    scope=ADMIN_SETTINGS_FORM_WRITE_SCOPE,
                    reason="flip",
                ),
                updates=(
                    AdminSettingWrite(
                        domain=SettingDomain.billing,
                        key="starter_ledger_live_enabled",
                        payload=DomainSettingUpdate(
                            value_type=SettingValueType.boolean, value_text="on"
                        ),
                    ),
                ),
            ),
        )
