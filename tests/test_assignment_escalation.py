"""SUB-1 / G1: ``rbac:assign`` cannot grant what the actor lacks, or self-grant.

One section per grant path: subset allowed, superset refused (no row written),
self-grant refused, admin break-glass unchanged. Documentation-range / example
identities only.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from app.models.audit import AuditActorType
from app.models.auth import ApiKey
from app.models.rbac import (
    Permission,
    Role,
    RolePermission,
    SubscriberPermission,
    SubscriberRole,
    SystemUserPermission,
    SystemUserRole,
)
from app.models.subscriber import Subscriber
from app.models.system_user import SystemUser
from app.services import (
    api_key_administration as keys,
)
from app.services import (
    assignment_escalation,
    staff_provisioning,
    subscriber_assignments,
    system_user_assignments,
)
from app.services.audit_adapter import AuditActor
from app.services.owner_commands import CommandContext
from tests.api_key_actor_fixtures import persist_admin_actor

EXCEEDS = assignment_escalation.EXCEEDS
SELF = assignment_escalation.SELF


# ---- builders ---------------------------------------------------------------


def _idle(db):
    """Owner commands require a transaction-free session at entry."""
    db.commit()
    return db


def _ctx(actor: str, scope: str) -> CommandContext:
    command_id = uuid4()
    return CommandContext(
        command_id=command_id,
        correlation_id=command_id,
        actor=actor,
        scope=scope,
        reason="SUB-1 escalation test",
        idempotency_key=f"sub1:{command_id}",
    )


def _perm(db, key: str) -> UUID:
    row = Permission(key=f"{key}.{uuid4().hex[:6]}", is_active=True)
    db.add(row)
    db.commit()
    return row.id


def _role(db, perm_ids: list[UUID], name: str = "r") -> UUID:
    role = Role(name=f"{name}-{uuid4().hex[:8]}", is_active=True)
    db.add(role)
    db.flush()
    for pid in perm_ids:
        db.add(RolePermission(role_id=role.id, permission_id=pid))
    db.commit()
    return role.id


def _staff_actor(db, held: list[UUID]) -> UUID:
    """A non-admin staff actor holding exactly ``held`` as direct permissions."""
    user = SystemUser(
        first_name="A",
        last_name="B",
        email=f"actor-{uuid4().hex}@example.com",
        is_active=True,
    )
    db.add(user)
    db.flush()
    for pid in held:
        db.add(SystemUserPermission(system_user_id=user.id, permission_id=pid))
    db.commit()
    return user.id


def _subscriber(db, held: list[UUID] | None = None) -> UUID:
    sub = Subscriber(
        first_name="T",
        last_name="U",
        email=f"sub-{uuid4().hex}@example.com",
    )
    db.add(sub)
    db.flush()
    for pid in held or []:
        db.add(SubscriberPermission(subscriber_id=sub.id, permission_id=pid))
    db.commit()
    return sub.id


def _admin_actor(db) -> UUID:
    actor_id = uuid4()
    persist_admin_actor(db, str(actor_id))
    return actor_id


def _grant_role(db, actor: str, subscriber_id: UUID, role_id: UUID):
    return subscriber_assignments.grant_subscriber_role(
        _idle(db),
        subscriber_assignments.GrantSubscriberRoleCommand(
            context=_ctx(actor, subscriber_assignments.ASSIGNMENT_SCOPE),
            subscriber_id=subscriber_id,
            role_id=role_id,
        ),
    )


def _fixture(db):
    """actor holds p1; role_sub = {p1}; role_sup = {p1, p2}."""
    p1, p2 = _perm(db, "sub1:a"), _perm(db, "sub1:b")
    return {
        "p1": p1,
        "p2": p2,
        "role_sub": _role(db, [p1]),
        "role_sup": _role(db, [p1, p2]),
        "actor": _staff_actor(db, [p1]),
    }


# ---- subscriber roles: grant_subscriber_role --------------------------------


def test_grant_role_subset_allowed(db_session):
    f = _fixture(db_session)
    target = _subscriber(db_session)
    out = _grant_role(db_session, f"user:{f['actor']}", target, f["role_sub"])
    assert out.changed is True
    assert db_session.query(SubscriberRole).filter_by(subscriber_id=target).count() == 1


def test_grant_role_superset_refused(db_session):
    f = _fixture(db_session)
    target = _subscriber(db_session)
    with pytest.raises(assignment_escalation.AssignmentEscalationError) as exc:
        _grant_role(db_session, f"user:{f['actor']}", target, f["role_sup"])
    assert exc.value.code == EXCEEDS
    assert len(exc.value.details["permission_keys"]) == 1  # only p2
    assert db_session.query(SubscriberRole).filter_by(subscriber_id=target).count() == 0


def test_grant_role_to_self_refused_even_when_subset(db_session):
    f = _fixture(db_session)
    me = _subscriber(db_session, [f["p1"]])
    with pytest.raises(assignment_escalation.AssignmentEscalationError) as exc:
        _grant_role(db_session, f"user:{me}", me, f["role_sub"])
    assert exc.value.code == SELF
    assert db_session.query(SubscriberRole).filter_by(subscriber_id=me).count() == 0


def test_grant_role_admin_break_glass_unchanged(db_session):
    f = _fixture(db_session)
    admin = _admin_actor(db_session)
    target = _subscriber(db_session)
    assert _grant_role(db_session, f"user:{admin}", target, f["role_sup"]).changed


def test_grant_admin_role_requires_admin(db_session):
    f = _fixture(db_session)
    admin_role = db_session.query(Role).filter_by(name="admin").first()
    if admin_role is None:
        admin_role = Role(name="admin", is_active=True)
        db_session.add(admin_role)
        db_session.commit()
    target = _subscriber(db_session)
    with pytest.raises(assignment_escalation.AssignmentEscalationError):
        _grant_role(db_session, f"user:{f['actor']}", target, admin_role.id)


def test_grant_role_unknown_actor_fails_closed(db_session):
    f = _fixture(db_session)
    target = _subscriber(db_session)
    with pytest.raises(assignment_escalation.AssignmentEscalationError) as exc:
        _grant_role(db_session, f"user:{uuid4()}", target, f["role_sub"])
    assert exc.value.code == assignment_escalation.UNKNOWN_ACTOR


def test_api_key_actor_cannot_grant_role_beyond_its_scopes(db_session):
    f = _fixture(db_session)
    key = ApiKey(key_hash=f"h-{uuid4().hex}", scopes=["rbac:assign"], is_active=True)
    db_session.add(key)
    db_session.commit()
    target = _subscriber(db_session)
    with pytest.raises(assignment_escalation.AssignmentEscalationError) as exc:
        _grant_role(db_session, f"api_key:{key.id}", target, f["role_sub"])
    assert exc.value.code == EXCEEDS


# ---- subscriber roles: update_subscriber_role -------------------------------


def _update_role(db, actor: str, grant_id: UUID, **kw):
    return subscriber_assignments.update_subscriber_role(
        _idle(db),
        subscriber_assignments.UpdateSubscriberRoleCommand(
            context=_ctx(actor, subscriber_assignments.ASSIGNMENT_SCOPE),
            grant_id=grant_id,
            **kw,
        ),
    )


def test_update_role_subset_allowed_superset_refused(db_session):
    f = _fixture(db_session)
    target = _subscriber(db_session)
    seed = _grant_role(db_session, "service:seed", target, _role(db_session, []))
    actor = f"user:{f['actor']}"
    assert _update_role(db_session, actor, seed.id, role_id=f["role_sub"]).changed
    with pytest.raises(assignment_escalation.AssignmentEscalationError) as exc:
        _update_role(db_session, actor, seed.id, role_id=f["role_sup"])
    assert exc.value.code == EXCEEDS
    assert db_session.get(SubscriberRole, seed.id).role_id == f["role_sub"]


def test_update_role_self_refused_in_either_direction(db_session):
    f = _fixture(db_session)
    me = _subscriber(db_session, [f["p1"]])
    other = _subscriber(db_session)
    mine = _grant_role(db_session, "service:seed", me, _role(db_session, []))
    theirs = _grant_role(db_session, "service:seed", other, _role(db_session, []))
    actor = f"user:{me}"
    with pytest.raises(assignment_escalation.AssignmentEscalationError) as exc:
        _update_role(db_session, actor, mine.id, role_id=f["role_sub"])
    assert exc.value.code == SELF
    # Moving someone else's grant onto the actor's own account is also a self-grant.
    with pytest.raises(assignment_escalation.AssignmentEscalationError) as exc:
        _update_role(db_session, actor, theirs.id, subscriber_id=me)
    assert exc.value.code == SELF


# ---- subscriber direct permissions ------------------------------------------


def _grant_perm(db, actor: str, subscriber_id: UUID, permission_id: UUID):
    return subscriber_assignments.grant_subscriber_permission(
        _idle(db),
        subscriber_assignments.GrantSubscriberPermissionCommand(
            context=_ctx(actor, subscriber_assignments.ASSIGNMENT_SCOPE),
            subscriber_id=subscriber_id,
            permission_id=permission_id,
        ),
    )


def test_grant_permission_subset_superset_and_self(db_session):
    f = _fixture(db_session)
    target = _subscriber(db_session)
    actor = f"user:{f['actor']}"
    assert _grant_perm(db_session, actor, target, f["p1"]).changed
    with pytest.raises(assignment_escalation.AssignmentEscalationError) as exc:
        _grant_perm(db_session, actor, target, f["p2"])
    assert exc.value.code == EXCEEDS
    me = _subscriber(db_session, [f["p1"]])
    with pytest.raises(assignment_escalation.AssignmentEscalationError) as exc:
        _grant_perm(db_session, f"user:{me}", me, f["p1"])
    assert exc.value.code == SELF


def test_update_permission_superset_and_self_refused(db_session):
    f = _fixture(db_session)
    target = _subscriber(db_session)
    seed = _grant_perm(db_session, "service:seed", target, f["p1"])
    actor = f"user:{f['actor']}"

    def update(actor_ref, grant_id, **kw):
        return subscriber_assignments.update_subscriber_permission(
            _idle(db_session),
            subscriber_assignments.UpdateSubscriberPermissionCommand(
                context=_ctx(actor_ref, subscriber_assignments.ASSIGNMENT_SCOPE),
                grant_id=grant_id,
                **kw,
            ),
        )

    with pytest.raises(assignment_escalation.AssignmentEscalationError) as exc:
        update(actor, seed.id, permission_id=f["p2"])
    assert exc.value.code == EXCEEDS
    me = _subscriber(db_session, [f["p1"]])
    with pytest.raises(assignment_escalation.AssignmentEscalationError) as exc:
        update(f"user:{me}", seed.id, subscriber_id=me)
    assert exc.value.code == SELF


# ---- subscriber replace -----------------------------------------------------


def _replace_sub(db, actor, subscriber_id, roles=(), perms=()):
    return subscriber_assignments.replace_subscriber_assignments(
        _idle(db),
        subscriber_assignments.ReplaceSubscriberAssignmentsCommand(
            context=_ctx(actor, subscriber_assignments.ASSIGNMENT_SCOPE),
            subscriber_id=subscriber_id,
            role_grants=tuple(
                subscriber_assignments.SubscriberRoleGrantSpec(r) for r in roles
            ),
            direct_permission_ids=tuple(perms),
        ),
    )


def test_replace_subscriber_assignments_rules(db_session):
    f = _fixture(db_session)
    target = _subscriber(db_session)
    actor = f"user:{f['actor']}"
    assert _replace_sub(db_session, actor, target, [f["role_sub"]], [f["p1"]]).changed
    with pytest.raises(assignment_escalation.AssignmentEscalationError) as exc:
        _replace_sub(db_session, actor, target, [f["role_sup"]], [f["p1"]])
    assert exc.value.code == EXCEEDS
    # Retaining what the target already holds is not a grant, even above the actor.
    seeded = _subscriber(db_session)
    _replace_sub(db_session, "service:seed", seeded, [f["role_sup"]], [])
    assert not _replace_sub(db_session, actor, seeded, [f["role_sup"]], []).changed
    me = _subscriber(db_session, [f["p1"]])
    with pytest.raises(assignment_escalation.AssignmentEscalationError) as exc:
        _replace_sub(db_session, f"user:{me}", me, [f["role_sub"]], [])
    assert exc.value.code == SELF


# ---- system-user assignments ------------------------------------------------


def _replace_staff(db, actor, user_id, roles=(), perms=()):
    return system_user_assignments.replace_system_user_assignments(
        _idle(db),
        system_user_assignments.ReplaceSystemUserAssignmentsCommand(
            context=_ctx(actor, system_user_assignments.ASSIGNMENT_SCOPE),
            user_id=user_id,
            role_ids=tuple(roles),
            direct_permission_ids=tuple(perms),
        ),
    )


def test_system_user_replace_subset_superset_self_and_admin(db_session):
    f = _fixture(db_session)
    target = _staff_actor(db_session, [])
    actor = f"user:{f['actor']}"
    assert _replace_staff(db_session, actor, target, [f["role_sub"]], [f["p1"]]).changed
    with pytest.raises(assignment_escalation.AssignmentEscalationError) as exc:
        _replace_staff(db_session, actor, target, [f["role_sup"]], [f["p1"]])
    assert exc.value.code == EXCEEDS
    with pytest.raises(assignment_escalation.AssignmentEscalationError) as exc:
        _replace_staff(db_session, actor, f["actor"], [f["role_sub"]], [f["p1"]])
    assert exc.value.code == SELF
    assert (
        db_session.query(SystemUserRole).filter_by(system_user_id=f["actor"]).count()
        == 0
    )
    admin = _admin_actor(db_session)
    assert _replace_staff(
        db_session, f"user:{admin}", target, [f["role_sup"]], [f["p2"]]
    ).changed


# ---- reseller onboarding collaborator ---------------------------------------


def test_ensure_role_grant_in_transaction_superset_refused(db_session):
    f = _fixture(db_session)
    target = _subscriber(db_session)
    ctx = _ctx(f"user:{f['actor']}", subscriber_assignments.ASSIGNMENT_SCOPE)
    ok = subscriber_assignments.ensure_role_grant_in_transaction(
        db_session, context=ctx, subscriber_id=target, role_id=f["role_sub"]
    )
    assert ok.role_id == f["role_sub"]
    with pytest.raises(assignment_escalation.AssignmentEscalationError):
        subscriber_assignments.ensure_role_grant_in_transaction(
            db_session, context=ctx, subscriber_id=target, role_id=f["role_sup"]
        )


# ---- staff provisioning (interactive actors) --------------------------------


def test_staff_role_sync_and_local_create_rules(db_session):
    f = _fixture(db_session)
    sub_role = db_session.get(Role, f["role_sub"])
    sup_role = db_session.get(Role, f["role_sup"])
    sub_name, sup_name = sub_role.name, sup_role.name
    target = _staff_actor(db_session, [])
    actor = f"user:{f['actor']}"

    def sync(actor_ref, user_id, *names):
        return staff_provisioning.sync_staff_account_roles(
            _idle(db_session),
            staff_provisioning.SyncStaffRolesCommand(
                context=_ctx(actor_ref, staff_provisioning.STAFF_ASSIGN_SCOPE),
                user_id=user_id,
                role_names=tuple(names),
            ),
        )

    assert sync(actor, target, sub_name).changed
    with pytest.raises(assignment_escalation.AssignmentEscalationError) as exc:
        sync(actor, target, sup_name)
    assert exc.value.code == EXCEEDS
    with pytest.raises(assignment_escalation.AssignmentEscalationError) as exc:
        sync(actor, f["actor"], sub_name)
    assert exc.value.code == SELF

    def create(actor_ref, role_id):
        return staff_provisioning.create_local_staff_account(
            _idle(db_session),
            staff_provisioning.CreateLocalStaffAccountCommand(
                context=_ctx(actor_ref, staff_provisioning.STAFF_ASSIGN_SCOPE),
                email=f"new-{uuid4().hex}@example.com",
                first_name="N",
                last_name="U",
                role_id=role_id,
                send_invite=False,
            ),
        )

    with pytest.raises(assignment_escalation.AssignmentEscalationError) as exc:
        create(actor, f["role_sup"])
    assert exc.value.code == EXCEEDS


# ---- API-key scope path ------------------------------------------------------


def _issue_cmd(actor: AuditActor, scopes, *, generate=False):
    cls = keys.GenerateApiKeyCommand if generate else keys.CreateApiKeyCommand
    scope = keys.GENERATE_SCOPE if generate else keys.CREATE_SCOPE
    extra = {} if generate else {"supplied_secret": f"s-{uuid4().hex}"}
    return cls(
        context=CommandContext.system(
            actor=str(actor.actor_id), scope=scope, reason="SUB-1 test"
        ),
        actor=actor,
        scopes=tuple(scopes),
        **extra,
    )


def test_api_key_create_scope_subset_superset_wildcard_and_admin(db_session):
    f = _fixture(db_session)
    p1 = db_session.get(Permission, f["p1"]).key
    p2 = db_session.get(Permission, f["p2"]).key
    actor = AuditActor.user(str(f["actor"]))
    assert keys.create(_idle(db_session), command=_issue_cmd(actor, [p1])).scopes == (
        p1,
    )
    with pytest.raises(keys.ApiKeyIssuanceError) as exc:
        keys.create(_idle(db_session), command=_issue_cmd(actor, [p1, p2]))
    assert exc.value.code == EXCEEDS
    assert exc.value.details["permission_keys"] == [p2]
    with pytest.raises(keys.ApiKeyIssuanceError):
        keys.create(_idle(db_session), command=_issue_cmd(actor, ["*"]))
    with pytest.raises(keys.ApiKeyIssuanceError):
        keys.generate(_idle(db_session), command=_issue_cmd(actor, [p2], generate=True))
    assert db_session.query(ApiKey).filter(ApiKey.scopes.isnot(None)).count() == 1
    admin = AuditActor.user(str(_admin_actor(db_session)))
    assert keys.create(_idle(db_session), command=_issue_cmd(admin, ["*", p2])).scopes


def test_api_key_actor_cannot_mint_wider_key(db_session):
    holder = ApiKey(key_hash=f"h-{uuid4().hex}", scopes=["billing:*"], is_active=True)
    db_session.add(holder)
    db_session.commit()
    actor = AuditActor.api_key(str(holder.id))
    ok = keys.create(
        _idle(db_session), command=_issue_cmd(actor, ["billing:invoice:read"])
    )
    assert ok.scopes == ("billing:invoice:read",)
    with pytest.raises(keys.ApiKeyIssuanceError):
        keys.create(_idle(db_session), command=_issue_cmd(actor, ["network:*"]))


def test_api_key_update_scopes_and_secret_takeover_refused(db_session):
    f = _fixture(db_session)
    p1 = db_session.get(Permission, f["p1"]).key
    p2 = db_session.get(Permission, f["p2"]).key
    strong = ApiKey(key_hash=f"h-{uuid4().hex}", scopes=[p1, p2], is_active=True)
    weak = ApiKey(key_hash=f"h-{uuid4().hex}", scopes=[p1], is_active=True)
    db_session.add_all((strong, weak))
    db_session.commit()
    actor = AuditActor.user(str(f["actor"]))

    def update(key_id, fields, **kw):
        return keys.update(
            _idle(db_session),
            command=keys.UpdateApiKeyCommand(
                context=CommandContext.system(
                    actor=str(actor.actor_id),
                    scope=keys.UPDATE_SCOPE,
                    reason="SUB-1 test",
                ),
                key_id=key_id,
                actor=actor,
                fields=frozenset(fields),
                **kw,
            ),
        )

    F = keys.ApiKeyUpdateField
    with pytest.raises(keys.ApiKeyUpdateError) as exc:
        update(weak.id, [F.SCOPES], scopes=(p1, p2))
    assert exc.value.code == EXCEEDS
    # Replacing the secret of a stronger key would hand the actor its scopes.
    with pytest.raises(keys.ApiKeyUpdateError):
        update(strong.id, [F.KEY_HASH], supplied_secret="takeover")
    # Within the actor's own scopes both are allowed.
    assert update(weak.id, [F.SCOPES], scopes=(p1,)).scopes == (p1,)
    assert update(weak.id, [F.KEY_HASH], supplied_secret="rotated").key_hash


def test_web_create_api_key_scope_superset_refused(db_session):
    f = _fixture(db_session)
    p1 = db_session.get(Permission, f["p1"]).key
    p2 = db_session.get(Permission, f["p2"]).key
    actor = AuditActor.user(str(f["actor"]))

    def web(scopes):
        return keys.web_create(
            _idle(db_session),
            command=keys.WebCreateApiKeyCommand(
                context=CommandContext.system(
                    actor=f"user:{actor.actor_id}",
                    scope=keys.WEB_CREATE_SCOPE,
                    reason="SUB-1 test",
                ),
                actor=actor,
                subscriber_id=None,
                label="k",
                scopes=tuple(scopes),
            ),
        )

    assert web([p1]).secret
    with pytest.raises(keys.WebApiKeyError) as exc:
        web([p1, p2])
    assert exc.value.code == EXCEEDS


def test_internal_system_actor_is_not_evaluated(db_session):
    auth = assignment_escalation.resolve_actor(
        db_session, actor_type=AuditActorType.system, actor_id="scheduler"
    )
    assert auth.exempt


# ---- machine callers on staff provisioning (named widening) -----------------


def test_machine_callers_skip_subset_but_not_admin_admin_only_or_self(db_session):
    from app.services.permission_authorization import ADMIN_ONLY_GRANT_PERMISSION_KEYS

    f = _fixture(db_session)
    admin_role = db_session.query(Role).filter_by(name="admin").first()
    if admin_role is None:
        admin_role = Role(name="admin", is_active=True)
        db_session.add(admin_role)
    gated = Permission(key=sorted(ADMIN_ONLY_GRANT_PERMISSION_KEYS)[0], is_active=True)
    existing = db_session.query(Permission).filter_by(key=gated.key).first()
    if existing is None:
        db_session.add(gated)
        existing = gated
    db_session.commit()
    gated_role = _role(db_session, [existing.id])
    key_owner = _staff_actor(db_session, [])
    key = ApiKey(
        key_hash=f"h-{uuid4().hex}",
        scopes=["rbac:assign"],
        system_user_id=key_owner,
        is_active=True,
    )
    db_session.add(key)
    db_session.commit()
    sub_name = db_session.get(Role, f["role_sub"]).name
    sup_name = db_session.get(Role, f["role_sup"]).name
    admin_name, gated_name = admin_role.name, db_session.get(Role, gated_role).name
    target = _staff_actor(db_session, [])
    actor = f"api_key:{key.id}"

    def sync(user_id, *names):
        return staff_provisioning.sync_staff_account_roles(
            _idle(db_session),
            staff_provisioning.SyncStaffRolesCommand(
                context=_ctx(actor, staff_provisioning.STAFF_ASSIGN_SCOPE),
                user_id=user_id,
                role_names=tuple(names),
            ),
        )

    # Ordinary synced roles (even above the key's own scopes) are allowed.
    assert sync(target, sub_name, sup_name).changed
    for bad in (admin_name, gated_name):
        with pytest.raises(assignment_escalation.AssignmentEscalationError) as exc:
            sync(target, bad)
        assert exc.value.code == assignment_escalation.ADMIN_REFUSED
    with pytest.raises(assignment_escalation.AssignmentEscalationError) as exc:
        sync(key_owner, sub_name)
    assert exc.value.code == SELF


# ---- rbac-covering API keys are admin-only ------------------------------------


def test_only_admins_touch_api_keys_covering_rbac_assign(db_session):
    f = _fixture(db_session)
    p1 = db_session.get(Permission, f["p1"]).key
    # A non-admin who genuinely holds rbac:assign plus the wildcard-free p1.
    rbac_perm = db_session.query(Permission).filter_by(key="rbac:assign").first()
    if rbac_perm is None:
        rbac_perm = Permission(key="rbac:assign", is_active=True)
        db_session.add(rbac_perm)
        db_session.commit()
    holder = _staff_actor(db_session, [f["p1"], rbac_perm.id])
    actor = AuditActor.user(str(holder))
    for scope in ("rbac:assign", "rbac:*", "*"):
        with pytest.raises(keys.ApiKeyIssuanceError) as exc:
            keys.create(_idle(db_session), command=_issue_cmd(actor, [scope]))
        assert exc.value.code == assignment_escalation.ADMIN_REFUSED
    with pytest.raises(keys.ApiKeyIssuanceError):
        keys.generate(
            _idle(db_session), command=_issue_cmd(actor, ["rbac:assign"], generate=True)
        )
    with pytest.raises(keys.WebApiKeyError):
        keys.web_create(
            _idle(db_session),
            command=keys.WebCreateApiKeyCommand(
                context=CommandContext.system(
                    actor=f"user:{holder}", scope=keys.WEB_CREATE_SCOPE, reason="t"
                ),
                actor=actor,
                subscriber_id=None,
                label="k",
                scopes=("rbac:assign",),
            ),
        )
    # Unaffected: a key without rbac scopes.
    assert keys.create(_idle(db_session), command=_issue_cmd(actor, [p1])).scopes == (
        p1,
    )

    priv = ApiKey(key_hash=f"h-{uuid4().hex}", scopes=["rbac:assign"], is_active=True)
    plain = ApiKey(key_hash=f"h-{uuid4().hex}", scopes=[p1], is_active=True)
    db_session.add_all((priv, plain))
    db_session.commit()
    F = keys.ApiKeyUpdateField

    def update(who, key_id, fields, **kw):
        return keys.update(
            _idle(db_session),
            command=keys.UpdateApiKeyCommand(
                context=CommandContext.system(
                    actor=str(who.actor_id), scope=keys.UPDATE_SCOPE, reason="t"
                ),
                key_id=key_id,
                actor=who,
                fields=frozenset(fields),
                **kw,
            ),
        )

    with pytest.raises(keys.ApiKeyUpdateError):  # extend scopes
        update(actor, plain.id, [F.SCOPES], scopes=(p1, "rbac:assign"))
    with pytest.raises(keys.ApiKeyUpdateError):  # re-secret a privileged key
        update(actor, priv.id, [F.KEY_HASH], supplied_secret="x")
    assert update(actor, plain.id, [F.KEY_HASH], supplied_secret="ok").key_hash
    admin = AuditActor.user(str(_admin_actor(db_session)))
    assert keys.create(
        _idle(db_session), command=_issue_cmd(admin, ["rbac:assign"])
    ).scopes == ("rbac:assign",)
    assert update(admin, priv.id, [F.KEY_HASH], supplied_secret="admin-ok").key_hash


# ---- HTTP actor-context builders never emit system:/service: -------------------


def test_http_actor_context_builders_emit_only_user_or_api_key():
    from types import SimpleNamespace

    from app.api import rbac as rbac_api
    from app.api import staff_sync
    from app.web.admin import resellers as reseller_web
    from app.web.admin import system as system_web

    # Builders found: rbac._context, staff_sync._context/_actor,
    # system._system_command_context (staff create, assignments, API-key web
    # create), resellers._command_context.
    for principal_type in ("api_key", "system_user", "subscriber", "service", "system"):
        auth = {"principal_id": str(uuid4()), "principal_type": principal_type}
        request = SimpleNamespace(state=SimpleNamespace(auth=auth, actor_id=None))
        actors = [
            rbac_api._context(auth, scope="rbac:assign", reason="t", key="k").actor,
            staff_sync._context(auth, reason="t", idempotency_key="k").actor,
            staff_sync._actor(auth),
            system_web._system_command_context(
                request, reason="t", idempotency_key="k"
            ).actor,
            reseller_web._command_context(
                auth, scope="rbac:assign", reason="t", idempotency_key="k"
            ).actor,
        ]
        for actor in actors:
            assert actor.partition(":")[0] in {"user", "api_key"}, actor
