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
from app.models.rbac import Role, SubscriberRole, SystemUserRole
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

    This replaces the retired AST name-matching guard in
    ``tests/architecture/test_offer_access_requirement_boundary.py`` (see
    the comment left in its place). It injects a sentinel refusal at the
    shared decision the owner makes — an ERP staff leave-write restriction —
    and drives BOTH adapters through it: the route's own admission
    dependency (``app.api.catalog._require_offer_version_admission``,
    called directly, bypassing FastAPI's DI) and a direct
    ``admit_offer_version`` call (bypassing the route entirely). Both must
    refuse the SAME privileged, admin-role staff principal — proving both
    adapters delegate to the one owner rather than each computing an
    independent approximation that could disagree.

    Break condition: this fails if either adapter stops calling
    ``authorize_offer_version_admission`` (or that function stops calling
    ``erp_staff_access.staff_write_restricted``) — regardless of what the
    owner, the guard, or any intermediate helper is named. It cannot be
    satisfied by a rename; only real delegation makes both paths observe
    the sentinel.
    """

    from types import SimpleNamespace

    from fastapi import HTTPException

    from app.api import catalog as api_catalog
    from app.services import erp_staff_access

    user = _admin_system_user(db_session)

    sentinel_restriction = object()
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

    def _fake_audit_denied_write(db, **kwargs):
        return None

    monkeypatch.setattr(
        erp_staff_access, "staff_write_restricted", _fake_staff_write_restricted
    )
    monkeypatch.setattr(
        erp_staff_access, "audit_denied_write", _fake_audit_denied_write
    )

    offer = _make_offer(db_session)

    # Direct-command adapter: bypasses the route entirely.
    with pytest.raises(OfferAccessRequirementError) as command_excinfo:
        admit_offer_version(
            db_session,
            _admit_command(offer, 1, principal=StaffPrincipal(system_user_id=user.id)),
        )
    db_session.rollback()
    assert command_excinfo.value.code.endswith("permission_denied")

    # Route adapter: call the real FastAPI dependency function directly with
    # explicit arguments (bypassing FastAPI's own DI resolution, which is
    # not needed to exercise the function body).
    fake_request = SimpleNamespace(state=SimpleNamespace(), headers={})
    route_auth = {
        "principal_id": str(user.id),
        "principal_type": "system_user",
        "roles": ["admin"],
        "scopes": [],
    }
    with pytest.raises(HTTPException) as route_excinfo:
        api_catalog._require_offer_version_admission(
            request=fake_request, auth=route_auth, db=db_session
        )
    db_session.rollback()
    assert route_excinfo.value.status_code == 403

    # Both paths actually reached the sentinel for THIS principal — proving
    # the refusal observed above came from the injected restriction, not
    # from some unrelated failure.
    assert any(call["principal_id"] == str(user.id) for call in observed_calls)


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
