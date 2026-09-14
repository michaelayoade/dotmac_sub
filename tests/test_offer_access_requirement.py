"""service_intent.offer_access_requirement — admission, immutability, and the
reviewed classification command.

Transaction-boundary discipline: every helper that reads or writes through
this session leaves it transaction-free before returning (an explicit
``db_session.commit()``/``.rollback()``), because ``execute_owner_command``
requires a transaction-free session at entry and rejects an active caller
transaction (``app/services/owner_commands.py``). This mirrors the existing
pattern in ``tests/test_offer_reseller_availability.py``.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.models.auth import ApiKey
from app.models.catalog import (
    AccessRequirement,
    AccessType,
    OfferAccessRequirementClassification,
    OfferVersion,
    PriceBasis,
    ServiceType,
)
from app.models.rbac import (
    Permission,
    Role,
    SubscriberRole,
    SystemUserPermission,
    SystemUserRole,
)
from app.models.subscriber import Subscriber
from app.models.system_user import SystemUser
from app.schemas.catalog import (
    CatalogOfferCreate,
    OfferVersionCreate,
    OfferVersionUpdate,
)
from app.services import catalog as catalog_service
from app.services.catalog import offer_access_requirement
from app.services.catalog.offer_access_requirement import (
    ADMISSION_SCOPE,
    CLASSIFY_PERMISSION,
    AdmitOfferVersionCommand,
    ApiKeyPrincipal,
    ClassifyOfferAccessRequirementCommand,
    OfferAccessRequirementError,
    PreviewClassifyOfferAccessRequirementQuery,
    StaffPrincipal,
    SubscriberPrincipal,
    SystemAdmission,
    admit_offer_version,
    classify_offer_version_access_requirement,
    list_unclassified_offer_versions,
    preview_classify_offer_version_access_requirement,
    principal_label,
)
from app.services.owner_commands import CommandContext


def _make_offer(db_session):
    offer = catalog_service.offers.create(
        db_session,
        CatalogOfferCreate(
            name="Fiber 100",
            code=f"FIBER-{uuid4().hex[:8]}",
            service_type=ServiceType.residential,
            access_type=AccessType.fiber,
            price_basis=PriceBasis.flat,
        ),
    )
    db_session.commit()
    return offer


def _make_version(db_session, offer, *, access_requirement, version_number=1):
    version = catalog_service.offer_versions.create(
        db_session,
        OfferVersionCreate(
            offer_id=offer.id,
            version_number=version_number,
            name=f"Fiber 100 v{version_number}",
            service_type=ServiceType.residential,
            access_type=AccessType.fiber,
            price_basis=PriceBasis.flat,
            access_requirement=access_requirement,
        ),
        principal=SystemAdmission(reason="test fixture"),
    )
    db_session.commit()
    return version


def _admin_system_user(db_session) -> SystemUser:
    """A real, active staff principal with the ``admin`` role.

    ``admin`` satisfies every permission automatically
    (``app/services/auth_dependencies.py::has_permission``) — this is the
    repo's own wildcard/admin convention, not something this change invents.
    """

    user = SystemUser(
        first_name="Test",
        last_name="Admin",
        email=f"admin-{uuid4().hex[:8]}@example.com",
    )
    role = Role(name="admin", is_active=True)
    db_session.add_all([user, role])
    db_session.flush()
    db_session.add(SystemUserRole(system_user_id=user.id, role_id=role.id))
    db_session.commit()
    return user


def _unprivileged_system_user(db_session) -> SystemUser:
    """A real, active staff principal with NO classify permission or role."""

    user = SystemUser(
        first_name="Test",
        last_name="Unprivileged",
        email=f"unprivileged-{uuid4().hex[:8]}@example.com",
    )
    db_session.add(user)
    db_session.commit()
    return user


def _staff_with_direct_permissions(db_session, *permission_keys: str) -> SystemUser:
    """A real, active staff principal holding exactly ``permission_keys`` as
    DIRECT ``SystemUserPermission`` grants — no ``admin`` role, no wildcard,
    no role-mediated grant at all. Lets a test hold a caller to EXACTLY the
    compatibility-leg permissions (e.g. ``catalog:write`` +
    ``catalog:billing_write``, deliberately withholding
    ``catalog:offer_version:admission``) rather than the ``admin`` role's
    blanket bypass, which would satisfy the compound rule for a reason
    unrelated to the specific OR-leg under test."""

    user = SystemUser(
        first_name="Test",
        last_name="DirectGrant",
        email=f"direct-grant-{uuid4().hex[:8]}@example.com",
    )
    db_session.add(user)
    db_session.flush()
    for key in permission_keys:
        permission = db_session.scalar(select(Permission).where(Permission.key == key))
        if permission is None:
            permission = Permission(key=key, description=f"test grant: {key}")
            db_session.add(permission)
            db_session.flush()
        db_session.add(
            SystemUserPermission(system_user_id=user.id, permission_id=permission.id)
        )
    db_session.commit()
    return user


def _make_subscriber(db_session) -> Subscriber:
    from app.services.subscriber import _default_reseller_id

    subscriber = Subscriber(
        first_name="Test",
        last_name="Subscriber",
        email=f"subscriber-{uuid4().hex[:8]}@example.com",
        reseller_id=_default_reseller_id(db_session),
    )
    db_session.add(subscriber)
    db_session.commit()
    db_session.refresh(subscriber)
    return subscriber


def _admin_subscriber(db_session) -> Subscriber:
    """A real, active subscriber mapped to the ``admin`` role — the seeded
    subscriber-admin path Decision 2 preserves
    (``scripts/seed/seed_rbac.py``, ``app/services/subscriber_assignments.py``,
    ``app/services/auth_flow.py``'s login role resolution)."""

    subscriber = _make_subscriber(db_session)
    role = Role(name="admin", is_active=True)
    db_session.add(role)
    db_session.flush()
    db_session.add(SubscriberRole(subscriber_id=subscriber.id, role_id=role.id))
    db_session.commit()
    return subscriber


def _unprivileged_subscriber(db_session) -> Subscriber:
    """A real, active subscriber with NO role granting the compound
    admission permission."""

    return _make_subscriber(db_session)


def _admission_api_key(db_session, *, scopes: list[str]) -> ApiKey:
    api_key = ApiKey(
        key_hash=f"test-hash-{uuid4().hex}",
        scopes=scopes,
        is_active=True,
    )
    db_session.add(api_key)
    db_session.commit()
    db_session.refresh(api_key)
    return api_key


def _context(**overrides):
    command_id = overrides.pop("command_id", uuid4())
    defaults = dict(
        command_id=command_id,
        correlation_id=command_id,
        actor="placeholder",
        scope=CLASSIFY_PERMISSION,
        reason="reviewed with network ops",
        idempotency_key=f"classify-{uuid4()}",
    )
    defaults.update(overrides)
    return CommandContext(**defaults)


def _preview(db_session, version, proposed, review_reference="JIRA-42"):
    preview = preview_classify_offer_version_access_requirement(
        db_session,
        PreviewClassifyOfferAccessRequirementQuery(
            offer_version_id=version.id,
            proposed_access_requirement=proposed,
            review_reference=review_reference,
        ),
    )
    db_session.rollback()
    return preview


# --------------------------------------------------------------------------
# Admission
# --------------------------------------------------------------------------


def test_offer_version_create_requires_explicit_access_requirement(db_session):
    offer = _make_offer(db_session)
    with pytest.raises(ValidationError):
        OfferVersionCreate(
            offer_id=offer.id,
            version_number=1,
            name="Fiber 100 v1",
            service_type=ServiceType.residential,
            access_type=AccessType.fiber,
            price_basis=PriceBasis.flat,
        )


def test_offer_version_create_accepts_unclassified_explicitly(db_session):
    offer = _make_offer(db_session)
    version = _make_version(
        db_session, offer, access_requirement=AccessRequirement.unclassified
    )
    assert version.access_requirement is AccessRequirement.unclassified


def test_offer_version_create_accepts_a_real_classification_directly(db_session):
    offer = _make_offer(db_session)
    version = _make_version(
        db_session, offer, access_requirement=AccessRequirement.network_access
    )
    assert version.access_requirement is AccessRequirement.network_access


def test_offer_version_update_schema_has_no_access_requirement_field():
    assert "access_requirement" not in OfferVersionUpdate.model_fields


def test_offer_version_update_never_mutates_access_requirement(db_session):
    offer = _make_offer(db_session)
    version = _make_version(
        db_session, offer, access_requirement=AccessRequirement.unclassified
    )
    updated = catalog_service.offer_versions.update(
        db_session, str(version.id), OfferVersionUpdate(name="Renamed")
    )
    db_session.commit()
    assert updated.access_requirement is AccessRequirement.unclassified
    assert updated.name == "Renamed"


# --------------------------------------------------------------------------
# Worklist
# --------------------------------------------------------------------------


def test_worklist_reports_only_unclassified_rows(db_session):
    offer = _make_offer(db_session)
    unclassified = _make_version(
        db_session, offer, access_requirement=AccessRequirement.unclassified
    )
    _make_version(
        db_session,
        offer,
        access_requirement=AccessRequirement.network_access,
        version_number=2,
    )
    worklist = list_unclassified_offer_versions(db_session, limit=50, offset=0)
    db_session.rollback()
    ids = {row.offer_version_id for row in worklist.rows}
    assert unclassified.id in ids
    assert worklist.total_count == len(worklist.rows)


def test_worklist_rejects_an_out_of_range_page(db_session):
    with pytest.raises(OfferAccessRequirementError):
        list_unclassified_offer_versions(db_session, limit=0, offset=0)
    with pytest.raises(OfferAccessRequirementError):
        list_unclassified_offer_versions(db_session, limit=10, offset=-1)


# --------------------------------------------------------------------------
# Preview
# --------------------------------------------------------------------------


def test_preview_refuses_a_target_of_unclassified(db_session):
    offer = _make_offer(db_session)
    version = _make_version(
        db_session, offer, access_requirement=AccessRequirement.unclassified
    )
    with pytest.raises(OfferAccessRequirementError) as excinfo:
        _preview(db_session, version, AccessRequirement.unclassified)
    assert excinfo.value.code.endswith("invalid_target_classification")


def test_preview_refuses_a_missing_offer_version(db_session):
    with pytest.raises(OfferAccessRequirementError) as excinfo:
        preview_classify_offer_version_access_requirement(
            db_session,
            PreviewClassifyOfferAccessRequirementQuery(
                offer_version_id=uuid4(),
                proposed_access_requirement=AccessRequirement.network_access,
                review_reference="JIRA-1",
            ),
        )
    db_session.rollback()
    assert excinfo.value.code.endswith("offer_version_not_found")


def test_preview_refuses_an_already_classified_version_with_no_recorded_row(
    db_session,
):
    offer = _make_offer(db_session)
    version = _make_version(
        db_session, offer, access_requirement=AccessRequirement.network_access
    )
    with pytest.raises(OfferAccessRequirementError) as excinfo:
        _preview(db_session, version, AccessRequirement.no_network_access)
    assert excinfo.value.code.endswith("already_classified")


# --------------------------------------------------------------------------
# Reviewed classification command
# --------------------------------------------------------------------------


def test_classify_denies_an_unprivileged_authenticated_principal(db_session):
    offer = _make_offer(db_session)
    version = _make_version(
        db_session, offer, access_requirement=AccessRequirement.unclassified
    )
    user = _unprivileged_system_user(db_session)
    preview = _preview(db_session, version, AccessRequirement.network_access)

    with pytest.raises(OfferAccessRequirementError) as excinfo:
        classify_offer_version_access_requirement(
            db_session,
            ClassifyOfferAccessRequirementCommand(
                context=_context(),
                query=PreviewClassifyOfferAccessRequirementQuery(
                    offer_version_id=version.id,
                    proposed_access_requirement=AccessRequirement.network_access,
                    review_reference="JIRA-42",
                ),
                expected_preview_fingerprint=preview.preview_fingerprint,
                authorized_system_user_id=user.id,
            ),
        )
    db_session.rollback()
    assert excinfo.value.code.endswith("permission_denied")


def test_classify_denies_an_inactive_system_user(db_session):
    offer = _make_offer(db_session)
    version = _make_version(
        db_session, offer, access_requirement=AccessRequirement.unclassified
    )
    user = _admin_system_user(db_session)
    user.is_active = False
    db_session.commit()
    preview = _preview(db_session, version, AccessRequirement.network_access)

    with pytest.raises(OfferAccessRequirementError) as excinfo:
        classify_offer_version_access_requirement(
            db_session,
            ClassifyOfferAccessRequirementCommand(
                context=_context(),
                query=PreviewClassifyOfferAccessRequirementQuery(
                    offer_version_id=version.id,
                    proposed_access_requirement=AccessRequirement.network_access,
                    review_reference="JIRA-42",
                ),
                expected_preview_fingerprint=preview.preview_fingerprint,
                authorized_system_user_id=user.id,
            ),
        )
    db_session.rollback()
    assert excinfo.value.code.endswith("permission_denied")


def test_classify_applies_the_reviewed_transition_and_records_the_real_principal(
    db_session,
):
    offer = _make_offer(db_session)
    version = _make_version(
        db_session, offer, access_requirement=AccessRequirement.unclassified
    )
    user = _admin_system_user(db_session)
    preview = _preview(db_session, version, AccessRequirement.network_access)

    outcome = classify_offer_version_access_requirement(
        db_session,
        ClassifyOfferAccessRequirementCommand(
            context=_context(),
            query=PreviewClassifyOfferAccessRequirementQuery(
                offer_version_id=version.id,
                proposed_access_requirement=AccessRequirement.network_access,
                review_reference="JIRA-42",
            ),
            expected_preview_fingerprint=preview.preview_fingerprint,
            authorized_system_user_id=user.id,
        ),
    )
    assert outcome.replayed is False
    assert outcome.new_access_requirement is AccessRequirement.network_access

    row = db_session.scalar(
        select(OfferAccessRequirementClassification).where(
            OfferAccessRequirementClassification.offer_version_id == version.id
        )
    )
    assert row is not None
    assert row.new_access_requirement is AccessRequirement.network_access
    # The recorded identity is the AUTHENTICATED principal, never a
    # caller-supplied free-text actor string.
    assert row.classified_by == principal_label(user.id)
    db_session.rollback()

    refreshed = db_session.get(OfferVersion, version.id)
    assert refreshed.access_requirement is AccessRequirement.network_access
    db_session.rollback()


def test_classify_refuses_a_stale_preview(db_session):
    offer = _make_offer(db_session)
    version = _make_version(
        db_session, offer, access_requirement=AccessRequirement.unclassified
    )
    user = _admin_system_user(db_session)

    with pytest.raises(OfferAccessRequirementError) as excinfo:
        classify_offer_version_access_requirement(
            db_session,
            ClassifyOfferAccessRequirementCommand(
                context=_context(),
                query=PreviewClassifyOfferAccessRequirementQuery(
                    offer_version_id=version.id,
                    proposed_access_requirement=AccessRequirement.network_access,
                    review_reference="JIRA-42",
                ),
                expected_preview_fingerprint="0" * 64,
                authorized_system_user_id=user.id,
            ),
        )
    db_session.rollback()
    assert excinfo.value.code.endswith("stale_preview")


def test_classify_refuses_real_to_real_with_no_recorded_row(db_session):
    offer = _make_offer(db_session)
    version = _make_version(
        db_session, offer, access_requirement=AccessRequirement.network_access
    )
    user = _admin_system_user(db_session)

    with pytest.raises(OfferAccessRequirementError) as excinfo:
        classify_offer_version_access_requirement(
            db_session,
            ClassifyOfferAccessRequirementCommand(
                context=_context(),
                query=PreviewClassifyOfferAccessRequirementQuery(
                    offer_version_id=version.id,
                    proposed_access_requirement=AccessRequirement.no_network_access,
                    review_reference="JIRA-42",
                ),
                expected_preview_fingerprint="0" * 64,
                authorized_system_user_id=user.id,
            ),
        )
    db_session.rollback()
    assert excinfo.value.code.endswith("already_classified")


def test_classify_exact_replay_is_reachable_through_preview_then_apply(db_session):
    """The real regression this proves: a genuine retry — SAME idempotency
    key, same material inputs — reaches the replay branch instead of being
    blocked at the preview gate, because preview itself recognizes an
    already-recorded matching transition."""

    offer = _make_offer(db_session)
    version = _make_version(
        db_session, offer, access_requirement=AccessRequirement.unclassified
    )
    user = _admin_system_user(db_session)
    fixed_key = f"fixed-key-{uuid4()}"

    first_preview = _preview(db_session, version, AccessRequirement.network_access)
    assert first_preview.already_applied is False
    first_context = _context(idempotency_key=fixed_key)
    first_query = PreviewClassifyOfferAccessRequirementQuery(
        offer_version_id=version.id,
        proposed_access_requirement=AccessRequirement.network_access,
        review_reference="JIRA-42",
    )
    first = classify_offer_version_access_requirement(
        db_session,
        ClassifyOfferAccessRequirementCommand(
            context=first_context,
            query=first_query,
            expected_preview_fingerprint=first_preview.preview_fingerprint,
            authorized_system_user_id=user.id,
        ),
    )
    assert first.replayed is False
    db_session.rollback()

    # Simulate the client losing the response and retrying: preview again
    # (this is exactly what the CLI does before every apply) with the SAME
    # material inputs.
    retry_preview = _preview(db_session, version, AccessRequirement.network_access)
    assert retry_preview.already_applied is True

    retry_context = _context(idempotency_key=fixed_key)
    second = classify_offer_version_access_requirement(
        db_session,
        ClassifyOfferAccessRequirementCommand(
            context=retry_context,
            query=first_query,
            expected_preview_fingerprint=retry_preview.preview_fingerprint,
            authorized_system_user_id=user.id,
        ),
    )
    db_session.rollback()
    assert second.replayed is True
    assert second.new_access_requirement is AccessRequirement.network_access

    rows = db_session.scalars(
        select(OfferAccessRequirementClassification).where(
            OfferAccessRequirementClassification.offer_version_id == version.id
        )
    ).all()
    db_session.rollback()
    assert len(rows) == 1


def test_classify_refuses_a_key_reused_for_a_different_offer_version(db_session):
    offer = _make_offer(db_session)
    first_version = _make_version(
        db_session, offer, access_requirement=AccessRequirement.unclassified
    )
    second_version = _make_version(
        db_session,
        offer,
        access_requirement=AccessRequirement.unclassified,
        version_number=2,
    )
    user = _admin_system_user(db_session)
    shared_key = f"shared-key-{uuid4()}"

    first_preview = _preview(
        db_session, first_version, AccessRequirement.network_access
    )
    classify_offer_version_access_requirement(
        db_session,
        ClassifyOfferAccessRequirementCommand(
            context=_context(idempotency_key=shared_key),
            query=PreviewClassifyOfferAccessRequirementQuery(
                offer_version_id=first_version.id,
                proposed_access_requirement=AccessRequirement.network_access,
                review_reference="JIRA-42",
            ),
            expected_preview_fingerprint=first_preview.preview_fingerprint,
            authorized_system_user_id=user.id,
        ),
    )
    db_session.rollback()

    second_preview = _preview(
        db_session, second_version, AccessRequirement.network_access
    )
    with pytest.raises(OfferAccessRequirementError) as excinfo:
        classify_offer_version_access_requirement(
            db_session,
            ClassifyOfferAccessRequirementCommand(
                context=_context(idempotency_key=shared_key),
                query=PreviewClassifyOfferAccessRequirementQuery(
                    offer_version_id=second_version.id,
                    proposed_access_requirement=AccessRequirement.network_access,
                    review_reference="JIRA-42",
                ),
                expected_preview_fingerprint=second_preview.preview_fingerprint,
                authorized_system_user_id=user.id,
            ),
        )
    db_session.rollback()
    assert excinfo.value.code.endswith("idempotency_conflict")


def test_classify_refuses_a_key_reused_for_a_different_target(db_session):
    offer = _make_offer(db_session)
    version = _make_version(
        db_session, offer, access_requirement=AccessRequirement.unclassified
    )
    user = _admin_system_user(db_session)
    shared_key = f"shared-key-{uuid4()}"

    first_preview = _preview(db_session, version, AccessRequirement.network_access)
    classify_offer_version_access_requirement(
        db_session,
        ClassifyOfferAccessRequirementCommand(
            context=_context(idempotency_key=shared_key),
            query=PreviewClassifyOfferAccessRequirementQuery(
                offer_version_id=version.id,
                proposed_access_requirement=AccessRequirement.network_access,
                review_reference="JIRA-42",
            ),
            expected_preview_fingerprint=first_preview.preview_fingerprint,
            authorized_system_user_id=user.id,
        ),
    )
    db_session.rollback()

    # The row is now real-to-real from this same key's perspective; the
    # already_classified branch is reached first here because the version is
    # no longer unclassified with a DIFFERENT proposed target — proving the
    # key is not what silently overrides target immutability.
    with pytest.raises(OfferAccessRequirementError) as excinfo:
        classify_offer_version_access_requirement(
            db_session,
            ClassifyOfferAccessRequirementCommand(
                context=_context(idempotency_key=shared_key),
                query=PreviewClassifyOfferAccessRequirementQuery(
                    offer_version_id=version.id,
                    proposed_access_requirement=AccessRequirement.no_network_access,
                    review_reference="JIRA-42",
                ),
                expected_preview_fingerprint="0" * 64,
                authorized_system_user_id=user.id,
            ),
        )
    db_session.rollback()
    assert excinfo.value.code in (
        "service_intent.offer_access_requirement.idempotency_conflict",
        "service_intent.offer_access_requirement.already_classified",
    )


def test_classify_refuses_a_different_key_after_a_real_transition(db_session):
    offer = _make_offer(db_session)
    version = _make_version(
        db_session, offer, access_requirement=AccessRequirement.unclassified
    )
    user = _admin_system_user(db_session)

    first_preview = _preview(db_session, version, AccessRequirement.network_access)
    classify_offer_version_access_requirement(
        db_session,
        ClassifyOfferAccessRequirementCommand(
            context=_context(idempotency_key=f"key-one-{uuid4()}"),
            query=PreviewClassifyOfferAccessRequirementQuery(
                offer_version_id=version.id,
                proposed_access_requirement=AccessRequirement.network_access,
                review_reference="JIRA-42",
            ),
            expected_preview_fingerprint=first_preview.preview_fingerprint,
            authorized_system_user_id=user.id,
        ),
    )
    db_session.rollback()

    retry_preview = _preview(db_session, version, AccessRequirement.network_access)
    assert retry_preview.already_applied is True
    with pytest.raises(OfferAccessRequirementError) as excinfo:
        classify_offer_version_access_requirement(
            db_session,
            ClassifyOfferAccessRequirementCommand(
                context=_context(idempotency_key=f"key-two-{uuid4()}"),
                query=PreviewClassifyOfferAccessRequirementQuery(
                    offer_version_id=version.id,
                    proposed_access_requirement=AccessRequirement.network_access,
                    review_reference="JIRA-42",
                ),
                expected_preview_fingerprint=retry_preview.preview_fingerprint,
                authorized_system_user_id=user.id,
            ),
        )
    db_session.rollback()
    assert excinfo.value.code.endswith("already_classified")


def test_classify_reverifies_permission_after_the_row_lock_not_only_at_entry(
    db_session, monkeypatch
):
    """Regression for the permission-revocation race: before this fix,
    ``_verify_classify_permission`` ran exactly ONCE, at the top of
    ``_classify``, before the offer version was locked. This asserts it now
    runs a SECOND time — after the lock, immediately before the write. This
    NARROWS but does NOT ELIMINATE the revoke-during-apply race: no RBAC row
    (system_users, roles, role_permissions, permissions) is locked, so a
    grant revoked in the instant between this second check and the commit is
    still not caught (see the SOT manifest's own honest disclosure,
    ``service_intent_control_plane.py``'s ``locking=`` contract). Fails
    before the fix (call count 1, not 2)."""

    offer = _make_offer(db_session)
    version = _make_version(
        db_session, offer, access_requirement=AccessRequirement.unclassified
    )
    user = _admin_system_user(db_session)
    preview = _preview(db_session, version, AccessRequirement.network_access)

    calls: list[object] = []
    real_verify = offer_access_requirement._verify_classify_permission

    def _counting_verify(db, system_user_id):
        calls.append(system_user_id)
        return real_verify(db, system_user_id)

    monkeypatch.setattr(
        offer_access_requirement, "_verify_classify_permission", _counting_verify
    )

    outcome = classify_offer_version_access_requirement(
        db_session,
        ClassifyOfferAccessRequirementCommand(
            context=_context(),
            query=PreviewClassifyOfferAccessRequirementQuery(
                offer_version_id=version.id,
                proposed_access_requirement=AccessRequirement.network_access,
                review_reference="JIRA-42",
            ),
            expected_preview_fingerprint=preview.preview_fingerprint,
            authorized_system_user_id=user.id,
        ),
    )
    db_session.rollback()
    assert outcome.replayed is False
    assert len(calls) == 2
    assert calls == [user.id, user.id]


def test_classify_refuses_a_replay_with_a_different_review_reference(db_session):
    """Regression for the replay/review_reference gap: previewing and then
    applying with the SAME idempotency key but a DIFFERENT review_reference
    than the one actually recorded must not silently substitute the stored
    reference — it must be refused. Fails before the fix (the preview
    silently returned the stored reference regardless of what was supplied)."""

    offer = _make_offer(db_session)
    version = _make_version(
        db_session, offer, access_requirement=AccessRequirement.unclassified
    )
    user = _admin_system_user(db_session)
    fixed_key = f"fixed-key-{uuid4()}"

    first_preview = _preview(db_session, version, AccessRequirement.network_access)
    first_query = PreviewClassifyOfferAccessRequirementQuery(
        offer_version_id=version.id,
        proposed_access_requirement=AccessRequirement.network_access,
        review_reference="JIRA-42",
    )
    classify_offer_version_access_requirement(
        db_session,
        ClassifyOfferAccessRequirementCommand(
            context=_context(idempotency_key=fixed_key),
            query=first_query,
            expected_preview_fingerprint=first_preview.preview_fingerprint,
            authorized_system_user_id=user.id,
        ),
    )
    db_session.rollback()

    # Preview again with a DIFFERENT review_reference for the same already-
    # applied transition.
    with pytest.raises(OfferAccessRequirementError) as excinfo:
        _preview(db_session, version, AccessRequirement.network_access, "JIRA-99")
    db_session.rollback()
    assert excinfo.value.code.endswith("review_reference_mismatch")


def test_classify_refuses_an_oversized_idempotency_key(db_session):
    offer = _make_offer(db_session)
    version = _make_version(
        db_session, offer, access_requirement=AccessRequirement.unclassified
    )
    user = _admin_system_user(db_session)
    preview = _preview(db_session, version, AccessRequirement.network_access)

    with pytest.raises(OfferAccessRequirementError) as excinfo:
        classify_offer_version_access_requirement(
            db_session,
            ClassifyOfferAccessRequirementCommand(
                context=_context(idempotency_key="x" * 121),
                query=PreviewClassifyOfferAccessRequirementQuery(
                    offer_version_id=version.id,
                    proposed_access_requirement=AccessRequirement.network_access,
                    review_reference="JIRA-42",
                ),
                expected_preview_fingerprint=preview.preview_fingerprint,
                authorized_system_user_id=user.id,
            ),
        )
    db_session.rollback()
    assert excinfo.value.code.endswith("idempotency_key_too_long")


def test_preview_refuses_an_oversized_review_reference(db_session):
    offer = _make_offer(db_session)
    version = _make_version(
        db_session, offer, access_requirement=AccessRequirement.unclassified
    )
    with pytest.raises(OfferAccessRequirementError) as excinfo:
        _preview(db_session, version, AccessRequirement.network_access, "x" * 201)
    db_session.rollback()
    assert excinfo.value.code.endswith("review_reference_too_long")


# --------------------------------------------------------------------------
# Admission authorization and duplicate (offer_id, version_number) refusal
# --------------------------------------------------------------------------


def _admit_command(offer, version_number, *, principal=None):
    command_id = uuid4()
    resolved_principal = principal or SystemAdmission(reason="test admission")
    return AdmitOfferVersionCommand(
        context=CommandContext(
            command_id=command_id,
            correlation_id=command_id,
            actor="system:test",
            scope=ADMISSION_SCOPE,
            reason="test admission",
        ),
        payload=OfferVersionCreate(
            offer_id=offer.id,
            version_number=version_number,
            name=f"Fiber 100 v{version_number}",
            service_type=ServiceType.residential,
            access_type=AccessType.fiber,
            price_basis=PriceBasis.flat,
            access_requirement=AccessRequirement.unclassified,
        ),
        principal=resolved_principal,
    )


def test_admit_requires_an_explicit_principal():
    """Regression for the actor-spoofing gap's real fix: authorization moved
    to the route layer entirely, and the command now requires an explicit,
    typed principal — there is no default that lets a caller omit it."""

    with pytest.raises(TypeError):
        AdmitOfferVersionCommand(
            context=CommandContext(
                command_id=uuid4(),
                correlation_id=uuid4(),
                actor="system:test",
                scope=ADMISSION_SCOPE,
                reason="test admission",
            ),
            payload=OfferVersionCreate(
                offer_id=uuid4(),
                version_number=1,
                name="Fiber 100 v1",
                service_type=ServiceType.residential,
                access_type=AccessType.fiber,
                price_basis=PriceBasis.flat,
                access_requirement=AccessRequirement.unclassified,
            ),
        )


def test_admit_with_system_admission_is_not_rbac_gated(db_session):
    """Existing internal/system-initiated convention is preserved:
    ``SystemAdmission`` — and only ``SystemAdmission`` — is exempt from
    ``_verify_admission_authorization``'s RBAC re-check (see its own
    docstring). This is NOT "the command makes no authorization decision at
    all": round 11 added a real, in-transaction RBAC re-check
    (``_verify_admission_authorization``) for every OTHER principal type —
    see ``test_admit_denies_an_unprivileged_staff_principal`` and its
    siblings below. Matches every pre-existing caller of
    ``offer_versions.create``/``admit_offer_version`` with no actor, e.g.
    ``tests/conftest.py``'s shared ``catalog_offer`` fixture."""

    offer = _make_offer(db_session)
    result = admit_offer_version(db_session, _admit_command(offer, 1))
    db_session.rollback()
    assert result.replayed is False
    assert result.offer_version.access_requirement is AccessRequirement.unclassified


def test_admit_denies_an_unprivileged_staff_principal(db_session):
    """Decision 1 (round 11): the command now re-verifies the claimed
    principal against RBAC itself, inside its own transaction. An
    unprivileged system_user — no catalog:write, no catalog:billing_write
    or catalog:offer_version:admission — is refused here even though this
    call bypasses the route entirely, proving the check is real command-level
    enforcement, not merely the route's."""

    offer = _make_offer(db_session)
    user = _unprivileged_system_user(db_session)

    with pytest.raises(OfferAccessRequirementError) as excinfo:
        admit_offer_version(
            db_session,
            _admit_command(offer, 1, principal=StaffPrincipal(system_user_id=user.id)),
        )
    db_session.rollback()
    assert excinfo.value.code.endswith("permission_denied")


def test_admit_accepts_a_privileged_claimed_staff_principal(db_session):
    offer = _make_offer(db_session)
    user = _admin_system_user(db_session)

    result = admit_offer_version(
        db_session,
        _admit_command(offer, 1, principal=StaffPrincipal(system_user_id=user.id)),
    )
    db_session.rollback()
    assert result.offer_version.offer_id == offer.id


def test_admit_accepts_the_catalog_write_plus_billing_write_compatibility_leg(
    db_session,
):
    """Owner-command-level proof of the BASELINE caller population: a staff
    principal holding exactly ``catalog:write`` + ``catalog:billing_write``
    — no ``catalog:offer_version:admission``, no ``admin`` role, no
    wildcard — is accepted directly by ``admit_offer_version``. Before this
    test, only the ROUTE (``tests/test_offer_version_admission_route_
    permissions.py``) exercised this leg; the owner command's own
    compound-rule re-check had no test proving it accepts the shape every
    pre-existing ``catalog:billing_write`` caller already held prior to
    ``catalog:offer_version:admission`` ever being introduced.

    Break condition: this fails if ``_admission_permission_granted``'s OR
    ever drops (or narrows) the ``catalog:billing_write`` branch — e.g. a
    future edit that requires ``catalog:offer_version:admission``
    unconditionally would leave every OTHER focused test in this file green
    (they all use ``admin``, the narrower admission scope, or refusal cases)
    while silently locking out the entire pre-existing
    ``catalog:billing_write`` caller population this compound rule was
    designed to keep working."""

    offer = _make_offer(db_session)
    user = _staff_with_direct_permissions(
        db_session,
        offer_access_requirement.WRITE_PERMISSION,
        offer_access_requirement.BILLING_WRITE_PERMISSION,
    )

    result = admit_offer_version(
        db_session,
        _admit_command(offer, 1, principal=StaffPrincipal(system_user_id=user.id)),
    )
    db_session.rollback()
    assert result.offer_version.offer_id == offer.id


def test_admit_denies_a_nonexistent_api_key(db_session):
    """A principal naming an api_key_id with no backing row at all is
    refused — distinct from (and previously conflated with) an existing key
    that simply lacks the required scope; see
    ``test_admit_denies_an_api_key_with_no_matching_scope`` below for that
    case."""

    offer = _make_offer(db_session)

    with pytest.raises(OfferAccessRequirementError) as excinfo:
        admit_offer_version(
            db_session,
            _admit_command(offer, 1, principal=ApiKeyPrincipal(api_key_id=uuid4())),
        )
    db_session.rollback()
    assert excinfo.value.code.endswith("permission_denied")


def test_admit_denies_an_api_key_with_no_matching_scope(db_session):
    """An EXISTING, active API key whose scopes do not satisfy the compound
    rule is refused by the command's own re-check for that reason —
    ``catalog:write`` alone is not enough without either
    ``catalog:billing_write`` or ``catalog:offer_version:admission``.

    Previously this test used a nonexistent random ``api_key_id`` (now
    covered separately by ``test_admit_denies_a_nonexistent_api_key`` above)
    and so never actually exercised "a real key with an insufficient scope"
    at all — a denial was observed, but for the wrong reason."""

    offer = _make_offer(db_session)
    api_key = _admission_api_key(db_session, scopes=["catalog:write"])

    with pytest.raises(OfferAccessRequirementError) as excinfo:
        admit_offer_version(
            db_session,
            _admit_command(offer, 1, principal=ApiKeyPrincipal(api_key_id=api_key.id)),
        )
    db_session.rollback()
    assert excinfo.value.code.endswith("permission_denied")


def test_admit_accepts_an_api_key_with_the_compound_scope(db_session):
    """An active API key whose scopes satisfy catalog:write AND
    catalog:offer_version:admission is accepted."""

    offer = _make_offer(db_session)
    api_key = _admission_api_key(db_session, scopes=["catalog:write", ADMISSION_SCOPE])

    result = admit_offer_version(
        db_session,
        _admit_command(offer, 1, principal=ApiKeyPrincipal(api_key_id=api_key.id)),
    )
    db_session.rollback()
    assert result.offer_version.offer_id == offer.id


def test_admit_accepts_a_subscriber_principal_mapped_to_the_admin_role(db_session):
    """Decision 2 (round 11): a subscriber mapped to the ``admin`` role via
    the seeded subscriber-admin path is a preserved, supported caller — not
    silently refused."""

    offer = _make_offer(db_session)
    subscriber = _admin_subscriber(db_session)

    result = admit_offer_version(
        db_session,
        _admit_command(
            offer, 1, principal=SubscriberPrincipal(subscriber_id=subscriber.id)
        ),
    )
    db_session.rollback()
    assert result.offer_version.offer_id == offer.id


def test_admit_denies_an_unprivileged_subscriber_principal(db_session):
    """A subscriber with no role granting the compound admission permission
    is refused by the command's own re-check, matching the other principal
    types' negative tests."""

    offer = _make_offer(db_session)
    subscriber = _unprivileged_subscriber(db_session)

    with pytest.raises(OfferAccessRequirementError) as excinfo:
        admit_offer_version(
            db_session,
            _admit_command(
                offer, 1, principal=SubscriberPrincipal(subscriber_id=subscriber.id)
            ),
        )
    db_session.rollback()
    assert excinfo.value.code.endswith("permission_denied")


def test_authorization_owner_refuses_identically_through_route_and_command(
    db_session, monkeypatch
):
    """Behavioral parity proof for the single authorization owner
    (``offer_access_requirement.authorize_offer_version_admission``).

    Round 12 findings 2 and 3 showed the ORIGINAL version of this test
    masked the two defects it was meant to disprove:

    - It called ``_require_offer_version_admission`` directly, skipping the
      router's own PRE-EXISTING blanket ``catalog:write`` gate
      (``require_method_permission``), which independently applies the
      SAME staff leave-write check via older plumbing BEFORE the admission
      dependency is ever reached. This version drives the REAL graph — the
      router gate included — proving the actual production request path
      refuses, not just the inner function in isolation.
    - It replaced ``erp_staff_access.audit_denied_write`` with a no-op,
      hiding that a direct-command denial's staged audit record was
      deleted by the very rollback the denial causes. This version lets
      the REAL ``audit_denied_write`` run and asserts the durable
      ``auth.erp_staff_leave_write_denied`` row survives.

    Break condition: fails if either adapter stops calling
    ``authorize_offer_version_admission`` (or that function stops calling
    ``erp_staff_access.staff_write_restricted``), if the router gate stops
    independently enforcing the leave restriction ahead of the admission
    dependency, or if the direct-command denial's audit record stops
    surviving the transaction rollback the denial itself triggers. None of
    this can be satisfied by a rename; only real delegation and a real
    committed audit row make it pass.
    """

    from types import SimpleNamespace

    from fastapi import HTTPException
    from starlette.requests import Request

    from app.api import catalog as api_catalog
    from app.models.audit import AuditEvent
    from app.services import auth_dependencies, erp_staff_access

    user = _admin_system_user(db_session)

    sentinel_restriction = SimpleNamespace(
        restriction_id="sentinel-leave-block", source_system="test"
    )
    observed_calls: list[dict] = []

    def _fake_staff_write_restricted(db, auth, *, method, at=None):
        observed_calls.append(
            {"principal_id": auth.get("principal_id"), "method": method}
        )
        if auth.get("principal_type") == "system_user" and auth.get(
            "principal_id"
        ) == str(user.id):
            return sentinel_restriction
        return None

    monkeypatch.setattr(
        erp_staff_access, "staff_write_restricted", _fake_staff_write_restricted
    )
    # audit_denied_write is deliberately NOT mocked — this test asserts on
    # its REAL, durable effect below.

    offer = _make_offer(db_session)

    # --- Direct-command adapter: bypasses the route entirely. ---
    with pytest.raises(OfferAccessRequirementError) as command_excinfo:
        admit_offer_version(
            db_session,
            _admit_command(offer, 1, principal=StaffPrincipal(system_user_id=user.id)),
        )
    db_session.rollback()
    assert command_excinfo.value.code.endswith("permission_denied")

    surviving_audit_rows = (
        db_session.query(AuditEvent)
        .filter(
            AuditEvent.action == "auth.erp_staff_leave_write_denied",
            AuditEvent.entity_id == str(user.id),
        )
        .all()
    )
    assert len(surviving_audit_rows) >= 1, (
        "the direct-command staff-leave denial's audit record did not "
        "survive the transaction rollback the denial itself causes"
    )

    # --- Route adapter: the REAL dependency graph, router gate included. ---
    route_auth = {
        "principal_id": str(user.id),
        "principal_type": "system_user",
        "roles": ["admin"],
        "scopes": [],
    }
    real_request = Request(
        {"type": "http", "method": "POST", "path": "/offer-versions", "headers": []}
    )

    router_gate = auth_dependencies.require_method_permission(
        "catalog:read", offer_access_requirement.WRITE_PERMISSION
    )
    with pytest.raises(HTTPException) as router_excinfo:
        router_gate(request=real_request, auth=dict(route_auth), db=db_session)
    db_session.rollback()
    assert router_excinfo.value.status_code == 403

    # The endpoint dependency also refuses on its own — necessary for any
    # caller that reaches it without the router gate running first (a
    # differently-mounted route, a future refactor), not merely relying on
    # the router gate to be the only thing that ever catches this.
    with pytest.raises(HTTPException) as route_excinfo:
        api_catalog._require_offer_version_admission(
            request=real_request, auth=dict(route_auth), db=db_session
        )
    db_session.rollback()
    assert route_excinfo.value.status_code == 403

    # Every path actually reached the sentinel for THIS principal — proving
    # the refusals observed above came from the injected restriction, not
    # from some unrelated failure.
    assert any(call["principal_id"] == str(user.id) for call in observed_calls)


def test_admit_accepts_a_machine_credential_with_no_satisfying_scope(db_session):
    """Michael's ruling: machine principals stay on a shadowed, inventoried
    migration path with NO immediate refusal. A ``MachineCredentialPrincipal``
    holding scopes that satisfy NOTHING in the compound rule still succeeds —
    admission proceeds under the compatibility path exactly as it did before
    this module had any command-level check at all (``origin/main``
    authorized this same shape). The decision is only logged (shadow/
    would-refuse), never enforced.

    Break condition: this fails the moment ``MachineCredentialPrincipal`` is
    ever enforced against — i.e. the exact uncensused, silent access
    retirement Michael's ruling explicitly forbids landing without an
    inventory, a scope migration, and a reviewed enforcement switch first."""

    offer = _make_offer(db_session)

    result = admit_offer_version(
        db_session,
        _admit_command(
            offer,
            1,
            principal=offer_access_requirement.MachineCredentialPrincipal(
                credential_id=uuid4(), scopes=()
            ),
        ),
    )
    db_session.rollback()
    assert result.offer_version.offer_id == offer.id


def test_machine_credential_is_the_only_shadow_mode_admission_principal(db_session):
    """Exact, non-growing compatibility path proof.

    Constructs a deliberately unauthorized instance of EVERY member of the
    closed ``AdmissionPrincipal`` union and asserts exactly two bypass real
    enforcement: ``SystemAdmission`` (has no RBAC identity to check at all)
    and ``MachineCredentialPrincipal`` (shadow/would-refuse migration,
    Michael's ruling). Every other member — ``StaffPrincipal``,
    ``ApiKeyPrincipal``, ``SubscriberPrincipal`` — is refused.

    Break condition: this fails if the compatibility path silently widens to
    exempt a THIRD principal type (e.g. someone adding ``ApiKeyPrincipal`` to
    the same isinstance branch as a shortcut), and it fails if
    ``MachineCredentialPrincipal`` itself starts being enforced against
    (collapsing the shadow migration back into an immediate, uncensused
    refusal) — either direction of drift trips it."""

    offer = _make_offer(db_session)
    bypassed: set[type] = set()
    refused: set[type] = set()

    unauthorized_principals = (
        offer_access_requirement.StaffPrincipal(system_user_id=uuid4()),
        offer_access_requirement.ApiKeyPrincipal(api_key_id=uuid4()),
        offer_access_requirement.SubscriberPrincipal(subscriber_id=uuid4()),
        offer_access_requirement.MachineCredentialPrincipal(
            credential_id=uuid4(), scopes=()
        ),
        offer_access_requirement.SystemAdmission(reason="non-growth probe"),
    )
    for index, principal in enumerate(unauthorized_principals):
        try:
            admit_offer_version(
                db_session,
                _admit_command(offer, 100 + index, principal=principal),
            )
        except OfferAccessRequirementError as exc:
            assert exc.code.endswith("permission_denied")
            refused.add(type(principal))
        else:
            bypassed.add(type(principal))
        finally:
            db_session.rollback()

    assert bypassed == {
        offer_access_requirement.SystemAdmission,
        offer_access_requirement.MachineCredentialPrincipal,
    }
    assert refused == {
        offer_access_requirement.StaffPrincipal,
        offer_access_requirement.ApiKeyPrincipal,
        offer_access_requirement.SubscriberPrincipal,
    }


def test_admit_stages_a_machine_admission_with_api_key_actor_type_not_system(
    db_session,
):
    """Forensic-attribution proof (round 12, finding 4). A billing-governance
    audit entry only ever carries a strictly-enumerated ``AuditActorType``
    (``system``/``user``/``api_key``/``service``): ``app.models.audit`` has
    no ``machine_credential`` member, so
    ``catalog_billing_governance._actor_type`` silently falls back to
    ``AuditActorType.system`` for anything it doesn't recognize. Returning
    the free-text string ``"machine_credential"`` as this evidence's
    ``actor_type`` would therefore record an AUTHENTICATED, AUTHORIZED
    machine admission as an anonymous system action — losing the exact
    credential-attribution class this principal held before this module
    ever distinguished it from a legacy local API key.

    Break condition: this fails if ``_admission_actor_evidence`` ever
    returns anything other than ``"api_key"`` for a
    ``MachineCredentialPrincipal`` — including its own now-more-descriptive
    but WRONG former value, ``"machine_credential"``, which is not a member
    of ``AuditActorType`` at all."""

    from app.services.catalog_billing_governance import _actor_type

    principal = offer_access_requirement.MachineCredentialPrincipal(
        credential_id=uuid4(), scopes=(offer_access_requirement.ADMISSION_SCOPE,)
    )
    actor_id, actor_type = offer_access_requirement._admission_actor_evidence(principal)
    assert actor_id == str(principal.credential_id)
    assert actor_type == "api_key"
    # And the ENUM this actually feeds must resolve to the real api_key
    # class, never the system fallback that swallows anything unrecognized.
    resolved = _actor_type(actor_type)
    assert resolved.value == "api_key"


def test_admit_refuses_a_duplicate_offer_id_and_version_number(db_session):
    """Regression: before this fix, retrying an admission with the SAME
    (offer_id, version_number) silently created a second row — there was no
    uniqueness check anywhere in ``_admit`` and no database constraint
    either. This must now be a typed conflict, and no second row is
    created."""

    offer = _make_offer(db_session)
    admit_offer_version(db_session, _admit_command(offer, 1))
    db_session.rollback()

    with pytest.raises(OfferAccessRequirementError) as excinfo:
        admit_offer_version(db_session, _admit_command(offer, 1))
    db_session.rollback()
    assert excinfo.value.code.endswith("duplicate_version_number")

    rows = db_session.scalars(
        select(OfferVersion).where(
            OfferVersion.offer_id == offer.id,
            OfferVersion.version_number == 1,
        )
    ).all()
    db_session.rollback()
    assert len(rows) == 1


def test_admit_replay_with_matching_key_and_payload_returns_the_original_row(
    db_session,
):
    """End-to-end admission idempotency: an exact-key, exact-payload retry
    returns the ORIGINAL row (``replayed=True``) instead of a
    ``duplicate_version_number`` conflict."""

    offer = _make_offer(db_session)
    key = f"admit-{uuid4()}"
    command_id = uuid4()

    def _command():
        return AdmitOfferVersionCommand(
            context=CommandContext(
                command_id=command_id,
                correlation_id=command_id,
                actor="system:test",
                scope=ADMISSION_SCOPE,
                reason="test admission",
                idempotency_key=key,
            ),
            payload=OfferVersionCreate(
                offer_id=offer.id,
                version_number=1,
                name="Fiber 100 v1",
                service_type=ServiceType.residential,
                access_type=AccessType.fiber,
                price_basis=PriceBasis.flat,
                access_requirement=AccessRequirement.unclassified,
            ),
            principal=SystemAdmission(reason="test admission"),
        )

    first = admit_offer_version(db_session, _command())
    db_session.rollback()
    assert first.replayed is False

    second = admit_offer_version(db_session, _command())
    db_session.rollback()
    assert second.replayed is True
    assert second.offer_version.id == first.offer_version.id


def test_admit_replay_with_matching_key_but_different_payload_is_a_typed_conflict(
    db_session,
):
    """End-to-end admission idempotency: the SAME key reused for a
    DIFFERENT admission payload is a typed ``idempotency_conflict``, never a
    silent substitution or a raw database error."""

    offer = _make_offer(db_session)
    key = f"admit-{uuid4()}"

    admit_offer_version(
        db_session,
        AdmitOfferVersionCommand(
            context=CommandContext(
                command_id=uuid4(),
                correlation_id=uuid4(),
                actor="system:test",
                scope=ADMISSION_SCOPE,
                reason="test admission",
                idempotency_key=key,
            ),
            payload=OfferVersionCreate(
                offer_id=offer.id,
                version_number=1,
                name="Fiber 100 v1",
                service_type=ServiceType.residential,
                access_type=AccessType.fiber,
                price_basis=PriceBasis.flat,
                access_requirement=AccessRequirement.unclassified,
            ),
            principal=SystemAdmission(reason="test admission"),
        ),
    )
    db_session.rollback()

    with pytest.raises(OfferAccessRequirementError) as excinfo:
        admit_offer_version(
            db_session,
            AdmitOfferVersionCommand(
                context=CommandContext(
                    command_id=uuid4(),
                    correlation_id=uuid4(),
                    actor="system:test",
                    scope=ADMISSION_SCOPE,
                    reason="test admission",
                    idempotency_key=key,
                ),
                payload=OfferVersionCreate(
                    offer_id=offer.id,
                    version_number=2,
                    name="Fiber 100 v2 (different)",
                    service_type=ServiceType.residential,
                    access_type=AccessType.fiber,
                    price_basis=PriceBasis.flat,
                    access_requirement=AccessRequirement.unclassified,
                ),
                principal=SystemAdmission(reason="test admission"),
            ),
        )
    db_session.rollback()
    assert excinfo.value.code.endswith("idempotency_conflict")


def test_admission_fingerprint_distinguishes_omitted_from_explicit_default(
    db_session,
):
    """Regression for round 7's fix: an omitted optional field (e.g.
    ``billing_cycle``, left for ``_admit``'s settings-resolved default to
    decide) must fingerprint DIFFERENTLY from an explicit value that happens
    to equal that same schema default — ``model_dump()`` alone makes them
    indistinguishable, since Pydantic fills every omitted field with its
    schema default before ``_admission_fingerprint`` ever sees it."""

    offer = _make_offer(db_session)
    base_kwargs = dict(
        offer_id=offer.id,
        version_number=1,
        name="Fiber 100 v1",
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
        access_requirement=AccessRequirement.unclassified,
    )
    omitted = OfferVersionCreate(**base_kwargs)
    explicit_default = OfferVersionCreate(
        **base_kwargs, billing_cycle=omitted.billing_cycle
    )
    assert "billing_cycle" not in omitted.model_fields_set
    assert "billing_cycle" in explicit_default.model_fields_set

    omitted_fingerprint = offer_access_requirement._admission_fingerprint(omitted)
    explicit_fingerprint = offer_access_requirement._admission_fingerprint(
        explicit_default
    )
    assert omitted_fingerprint != explicit_fingerprint


def test_admit_stages_the_admitted_event_only_for_a_fresh_admission(
    db_session, monkeypatch
):
    """Regression for the typed-outcome/event fix: a genuinely NEW admission
    stages ``catalog.offer_version_admitted`` exactly once; an idempotent
    replay of the SAME command does not re-emit it."""

    from app.services.events import types as event_types

    emitted: list[object] = []
    real_emit_event = offer_access_requirement.emit_event

    def _recording_emit_event(db, event_type, payload, **kwargs):
        emitted.append(event_type)
        return real_emit_event(db, event_type, payload, **kwargs)

    monkeypatch.setattr(offer_access_requirement, "emit_event", _recording_emit_event)

    offer = _make_offer(db_session)
    key = f"admit-event-{uuid4()}"

    def _command():
        return AdmitOfferVersionCommand(
            context=CommandContext(
                command_id=uuid4(),
                correlation_id=uuid4(),
                actor="system:test",
                scope=ADMISSION_SCOPE,
                reason="test admission",
                idempotency_key=key,
            ),
            payload=OfferVersionCreate(
                offer_id=offer.id,
                version_number=1,
                name="Fiber 100 v1",
                service_type=ServiceType.residential,
                access_type=AccessType.fiber,
                price_basis=PriceBasis.flat,
                access_requirement=AccessRequirement.unclassified,
            ),
            principal=SystemAdmission(reason="test admission"),
        )

    admit_offer_version(db_session, _command())
    db_session.rollback()
    admit_offer_version(db_session, _command())
    db_session.rollback()

    assert emitted == [event_types.EventType.catalog_offer_version_admitted]


def test_admit_command_rejects_a_principal_outside_the_closed_union():
    """Regression for the runtime-validation gap: the closed union
    (``StaffPrincipal | ApiKeyPrincipal | SystemAdmission``) is only a
    static type hint — passing ``principal=None`` or any arbitrary object
    must raise here, not just when the argument is omitted entirely."""

    def _build(principal):
        return AdmitOfferVersionCommand(
            context=CommandContext(
                command_id=uuid4(),
                correlation_id=uuid4(),
                actor="system:test",
                scope=ADMISSION_SCOPE,
                reason="test admission",
            ),
            payload=OfferVersionCreate(
                offer_id=uuid4(),
                version_number=1,
                name="Fiber 100 v1",
                service_type=ServiceType.residential,
                access_type=AccessType.fiber,
                price_basis=PriceBasis.flat,
                access_requirement=AccessRequirement.unclassified,
            ),
            principal=principal,
        )

    with pytest.raises(TypeError):
        _build(None)
    with pytest.raises(TypeError):
        _build("system_user:not-a-real-principal")
