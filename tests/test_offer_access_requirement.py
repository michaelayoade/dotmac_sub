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

from app.models.catalog import (
    AccessRequirement,
    AccessType,
    OfferAccessRequirementClassification,
    OfferVersion,
    PriceBasis,
    ServiceType,
)
from app.models.rbac import Role, SystemUserRole
from app.models.system_user import SystemUser
from app.schemas.catalog import (
    CatalogOfferCreate,
    OfferVersionCreate,
    OfferVersionUpdate,
)
from app.services import catalog as catalog_service
from app.services.catalog.offer_access_requirement import (
    CLASSIFY_PERMISSION,
    ClassifyOfferAccessRequirementCommand,
    OfferAccessRequirementError,
    PreviewClassifyOfferAccessRequirementQuery,
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
