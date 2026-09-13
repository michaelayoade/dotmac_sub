"""service_intent.offer_access_requirement — admission, immutability, and the
reviewed classification command."""

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
)
from app.services.owner_commands import CommandContext


def _make_offer(db_session):
    return catalog_service.offers.create(
        db_session,
        CatalogOfferCreate(
            name="Fiber 100",
            code=f"FIBER-{uuid4().hex[:8]}",
            service_type=ServiceType.residential,
            access_type=AccessType.fiber,
            price_basis=PriceBasis.flat,
        ),
    )


def _make_version(db_session, offer, *, access_requirement, version_number=1):
    return catalog_service.offer_versions.create(
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


def _context(**overrides):
    command_id = overrides.pop("command_id", uuid4())
    defaults = dict(
        command_id=command_id,
        correlation_id=command_id,
        actor="staff:tester",
        scope=CLASSIFY_PERMISSION,
        reason="reviewed with network ops",
        idempotency_key=f"classify-{uuid4()}",
    )
    defaults.update(overrides)
    return CommandContext(**defaults)


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
    ids = {row.offer_version_id for row in worklist.rows}
    assert unclassified.id in ids
    assert worklist.total_count == len(worklist.rows)
    assert all(
        db_session.get(OfferVersion, row.offer_version_id).access_requirement
        is AccessRequirement.unclassified
        for row in worklist.rows
    )


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
        preview_classify_offer_version_access_requirement(
            db_session,
            PreviewClassifyOfferAccessRequirementQuery(
                offer_version_id=version.id,
                proposed_access_requirement=AccessRequirement.unclassified,
                review_reference="JIRA-1",
            ),
        )
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
    assert excinfo.value.code.endswith("offer_version_not_found")


def test_preview_refuses_an_already_classified_version(db_session):
    offer = _make_offer(db_session)
    version = _make_version(
        db_session, offer, access_requirement=AccessRequirement.network_access
    )
    with pytest.raises(OfferAccessRequirementError) as excinfo:
        preview_classify_offer_version_access_requirement(
            db_session,
            PreviewClassifyOfferAccessRequirementQuery(
                offer_version_id=version.id,
                proposed_access_requirement=AccessRequirement.no_network_access,
                review_reference="JIRA-1",
            ),
        )
    assert excinfo.value.code.endswith("already_classified")


# --------------------------------------------------------------------------
# Reviewed classification command
# --------------------------------------------------------------------------


def _preview(db_session, version, proposed, review_reference="JIRA-42"):
    return preview_classify_offer_version_access_requirement(
        db_session,
        PreviewClassifyOfferAccessRequirementQuery(
            offer_version_id=version.id,
            proposed_access_requirement=proposed,
            review_reference=review_reference,
        ),
    )


def test_classify_requires_the_dedicated_permission(db_session):
    offer = _make_offer(db_session)
    version = _make_version(
        db_session, offer, access_requirement=AccessRequirement.unclassified
    )
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
                permission_granted=False,
            ),
        )
    assert excinfo.value.code.endswith("permission_denied")


def test_classify_applies_the_reviewed_transition(db_session):
    offer = _make_offer(db_session)
    version = _make_version(
        db_session, offer, access_requirement=AccessRequirement.unclassified
    )
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
            permission_granted=True,
        ),
    )
    assert outcome.replayed is False
    assert outcome.new_access_requirement is AccessRequirement.network_access
    db_session.refresh(version)
    assert version.access_requirement is AccessRequirement.network_access
    row = db_session.scalar(
        select(OfferAccessRequirementClassification).where(
            OfferAccessRequirementClassification.offer_version_id == version.id
        )
    )
    assert row is not None
    assert row.new_access_requirement is AccessRequirement.network_access


def test_classify_refuses_a_stale_preview(db_session):
    offer = _make_offer(db_session)
    version = _make_version(
        db_session, offer, access_requirement=AccessRequirement.unclassified
    )
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
                permission_granted=True,
            ),
        )
    assert excinfo.value.code.endswith("stale_preview")


def test_classify_refuses_real_to_real(db_session):
    offer = _make_offer(db_session)
    version = _make_version(
        db_session, offer, access_requirement=AccessRequirement.network_access
    )
    context = _context()
    with pytest.raises(OfferAccessRequirementError) as excinfo:
        classify_offer_version_access_requirement(
            db_session,
            ClassifyOfferAccessRequirementCommand(
                context=context,
                query=PreviewClassifyOfferAccessRequirementQuery(
                    offer_version_id=version.id,
                    proposed_access_requirement=AccessRequirement.no_network_access,
                    review_reference="JIRA-42",
                ),
                expected_preview_fingerprint="0" * 64,
                permission_granted=True,
            ),
        )
    assert excinfo.value.code.endswith("already_classified")


def test_classify_exact_replay_is_idempotent(db_session):
    offer = _make_offer(db_session)
    version = _make_version(
        db_session, offer, access_requirement=AccessRequirement.unclassified
    )
    preview = _preview(db_session, version, AccessRequirement.network_access)
    context = _context(idempotency_key="fixed-key-1")
    query = PreviewClassifyOfferAccessRequirementQuery(
        offer_version_id=version.id,
        proposed_access_requirement=AccessRequirement.network_access,
        review_reference="JIRA-42",
    )
    command = ClassifyOfferAccessRequirementCommand(
        context=context,
        query=query,
        expected_preview_fingerprint=preview.preview_fingerprint,
        permission_granted=True,
    )
    first = classify_offer_version_access_requirement(db_session, command)
    assert first.replayed is False

    replay_context = _context(idempotency_key="fixed-key-1")
    replay_command = ClassifyOfferAccessRequirementCommand(
        context=replay_context,
        query=query,
        expected_preview_fingerprint=preview.preview_fingerprint,
        permission_granted=True,
    )
    second = classify_offer_version_access_requirement(db_session, replay_command)
    assert second.replayed is True
    assert second.new_access_requirement is AccessRequirement.network_access

    rows = db_session.scalars(
        select(OfferAccessRequirementClassification).where(
            OfferAccessRequirementClassification.offer_version_id == version.id
        )
    ).all()
    assert len(rows) == 1


def test_classify_refuses_a_different_target_under_a_reused_idempotency_key(
    db_session,
):
    offer = _make_offer(db_session)
    version = _make_version(
        db_session, offer, access_requirement=AccessRequirement.unclassified
    )
    preview = _preview(db_session, version, AccessRequirement.network_access)
    context = _context(idempotency_key="reused-key")
    query = PreviewClassifyOfferAccessRequirementQuery(
        offer_version_id=version.id,
        proposed_access_requirement=AccessRequirement.network_access,
        review_reference="JIRA-42",
    )
    classify_offer_version_access_requirement(
        db_session,
        ClassifyOfferAccessRequirementCommand(
            context=context,
            query=query,
            expected_preview_fingerprint=preview.preview_fingerprint,
            permission_granted=True,
        ),
    )
    with pytest.raises(OfferAccessRequirementError) as excinfo:
        classify_offer_version_access_requirement(
            db_session,
            ClassifyOfferAccessRequirementCommand(
                context=_context(idempotency_key="reused-key"),
                query=PreviewClassifyOfferAccessRequirementQuery(
                    offer_version_id=version.id,
                    proposed_access_requirement=AccessRequirement.no_network_access,
                    review_reference="JIRA-42",
                ),
                expected_preview_fingerprint="0" * 64,
                permission_granted=True,
            ),
        )
    assert excinfo.value.code.endswith("already_classified")
