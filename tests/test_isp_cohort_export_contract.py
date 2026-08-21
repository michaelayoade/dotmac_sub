"""The cohort-isp-01 export contract must be deterministic, minimal and closed.

No database here. Everything under test is a pure reduction: a record to
canonical bytes, bytes to a digest, two digests to a bounded verdict. That is
deliberate — the properties a destination depends on are properties of the
reduction, and proving them against a live database would prove them for one
row rather than for the contract.

Each rejection is paired with the acceptance case for the same code path.
"""

from __future__ import annotations

import unicodedata
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.migration_source import canonical, digest, snapshot
from app.migration_source.cohort import CohortEntityType

TENANT = snapshot.TenantScope(tenant_id=UUID("8c7ae830-51fc-52ae-9818-d84b2a35e568"))
CAPTURED = datetime(2026, 8, 21, 10, 0, tzinfo=UTC)
REVISION = snapshot.SourceRevision(
    schema_revision="546_module_prerequisites",
    application_version="8.6.0",
    snapshot_transaction_id="4321:4321:",
    captured_at=CAPTURED,
)


def _party(
    source_id: str, *, display_name: str = "Ada Lovelace", status: str = "active"
) -> snapshot.PartyRecord:
    return snapshot.PartyRecord(
        source_id=UUID(source_id),
        created_at=CAPTURED,
        updated_at=CAPTURED,
        party_type="person",
        display_name=display_name,
        status=status,
        data_classification="production",
        merged_into_party_id=None,
        merge_reason=None,
        metadata_blob=None,
    )


def _page(
    records: tuple[snapshot.PartyRecord, ...],
    *,
    next_cursor: snapshot.ExportCursor | None = None,
) -> snapshot.SnapshotPage:
    return snapshot.SnapshotPage(
        contract_version=snapshot.ContractVersion.V1,
        tenant=TENANT,
        source_revision=REVISION,
        entity_type=CohortEntityType.PARTY,
        records=records,
        next_cursor=next_cursor,
        completeness=(
            snapshot.Completeness.PARTIAL
            if next_cursor
            else snapshot.Completeness.COMPLETE
        ),
    )


# --------------------------------------------------------------------------
# canonicalisation
# --------------------------------------------------------------------------


def test_a_naive_timestamp_is_refused_not_assumed_utc() -> None:
    with pytest.raises(canonical.NaiveDatetimeError):
        canonical.canonical_datetime(datetime(2026, 8, 21, 10, 0))


def test_the_same_instant_in_two_zones_canonicalises_identically() -> None:
    lagos = timezone(timedelta(hours=1))
    assert canonical.canonical_datetime(
        datetime(2026, 8, 21, 11, 0, tzinfo=lagos)
    ) == canonical.canonical_datetime(datetime(2026, 8, 21, 10, 0, tzinfo=UTC))


def test_equal_decimals_spelled_differently_canonicalise_identically() -> None:
    assert canonical.canonical_decimal(Decimal("1.10")) == canonical.canonical_decimal(
        Decimal("1.1")
    )
    assert canonical.canonical_decimal(Decimal("1E+2")) == canonical.canonical_decimal(
        Decimal("100")
    )
    assert canonical.canonical_decimal(Decimal("-0.00")) == "0"


def test_unequal_decimals_stay_unequal() -> None:
    """The acceptance half: normalisation must not collapse real differences."""

    assert canonical.canonical_decimal(Decimal("1.10")) != canonical.canonical_decimal(
        Decimal("1.11")
    )


def test_two_unicode_spellings_of_one_name_canonicalise_identically() -> None:
    composed = "José"
    decomposed = unicodedata.normalize("NFD", composed)
    assert composed != decomposed
    assert canonical.canonical_string(composed) == canonical.canonical_string(
        decomposed
    )


def test_case_and_leading_space_are_data_and_survive() -> None:
    """Normalising these away would make the digest agree while rows differed."""

    assert canonical.canonical_string(" ada") != canonical.canonical_string("ada")
    assert canonical.canonical_string("Ada") != canonical.canonical_string("ada")


def test_a_null_field_and_a_false_field_are_distinguishable() -> None:
    assert canonical.canonical_form({"x": None}) != canonical.canonical_form(
        {"x": False}
    )
    assert canonical.canonical_form({"x": True}) != canonical.canonical_form({"x": 1})


def test_field_order_does_not_change_the_canonical_form() -> None:
    assert canonical.canonical_form({"b": 1, "a": 2}) == canonical.canonical_form(
        {"a": 2, "b": 1}
    )


def test_a_string_cannot_forge_canonical_structure() -> None:
    """Quoting is what stops content being read as separators."""

    assert canonical.canonical_form({"a": '","b":"'}) != canonical.canonical_form(
        {"a": "", "b": ""}
    )


def test_coordinates_quantise_to_a_portable_precision() -> None:
    assert canonical.canonical_coordinate(
        9.0576500001
    ) == canonical.canonical_coordinate(9.05765)
    assert canonical.canonical_coordinate(9.05765) != canonical.canonical_coordinate(
        9.0576
    )


# --------------------------------------------------------------------------
# version admission
# --------------------------------------------------------------------------


def test_an_unsupported_contract_version_is_refused() -> None:
    with pytest.raises(snapshot.UnsupportedContractVersionError) as caught:
        snapshot.require_contract_version("2")
    assert "not supported" in str(caught.value)


def test_the_supported_contract_version_is_admitted() -> None:
    assert snapshot.require_contract_version("1") is snapshot.ContractVersion.V1


def test_the_contract_version_is_part_of_the_record_digest() -> None:
    """A version change can never look like data that happened not to move."""

    fields = _party("11111111-1111-1111-1111-111111111111").canonical_fields()
    assert fields["contract_version"] == snapshot.ContractVersion.V1.value
    assert fields["schema_version"] == snapshot.SCHEMA_VERSION


# --------------------------------------------------------------------------
# record digests
# --------------------------------------------------------------------------


def test_one_meaningful_field_change_changes_the_digest() -> None:
    original = _party("11111111-1111-1111-1111-111111111111")
    changed = _party("11111111-1111-1111-1111-111111111111", display_name="Ada L.")
    assert original.digest() != changed.digest()


def test_an_identical_record_digests_identically() -> None:
    identifier = "11111111-1111-1111-1111-111111111111"
    assert _party(identifier).digest() == _party(identifier).digest()


def test_row_order_does_not_change_an_entity_type_aggregate() -> None:
    """The property a shadow comparison depends on most."""

    first = _party("11111111-1111-1111-1111-111111111111")
    second = _party("22222222-2222-2222-2222-222222222222", display_name="Grace")

    forward = digest.build_entity_type_digest(
        entity_type=CohortEntityType.PARTY,
        entries=digest.digest_page(_page((first, second))),
        completeness=snapshot.Completeness.COMPLETE,
        resume_from=None,
        contract_version=snapshot.ContractVersion.V1,
    )
    reversed_entries = tuple(reversed(digest.digest_page(_page((first, second)))))
    backward = digest.build_entity_type_digest(
        entity_type=CohortEntityType.PARTY,
        entries=reversed_entries,
        completeness=snapshot.Completeness.COMPLETE,
        resume_from=None,
        contract_version=snapshot.ContractVersion.V1,
    )
    assert forward.aggregate == backward.aggregate


def test_a_changed_row_changes_the_entity_type_aggregate() -> None:
    """The acceptance half of order-independence: it must still notice change."""

    unchanged = digest.build_entity_type_digest(
        entity_type=CohortEntityType.PARTY,
        entries=digest.digest_page(
            _page((_party("11111111-1111-1111-1111-111111111111"),))
        ),
        completeness=snapshot.Completeness.COMPLETE,
        resume_from=None,
        contract_version=snapshot.ContractVersion.V1,
    )
    changed = digest.build_entity_type_digest(
        entity_type=CohortEntityType.PARTY,
        entries=digest.digest_page(
            _page(
                (
                    _party(
                        "11111111-1111-1111-1111-111111111111",
                        display_name="Ada L.",
                    ),
                )
            )
        ),
        completeness=snapshot.Completeness.COMPLETE,
        resume_from=None,
        contract_version=snapshot.ContractVersion.V1,
    )
    assert unchanged.aggregate != changed.aggregate


def _cohort_digest(
    *, generated_at: datetime, party_name: str = "Ada Lovelace"
) -> digest.CohortDigest:
    entity_types = tuple(
        digest.build_entity_type_digest(
            entity_type=entity_type,
            entries=(
                digest.digest_page(
                    _page(
                        (
                            _party(
                                "11111111-1111-1111-1111-111111111111",
                                display_name=party_name,
                            ),
                        )
                    )
                )
                if entity_type is CohortEntityType.PARTY
                else ()
            ),
            completeness=snapshot.Completeness.COMPLETE,
            resume_from=None,
            contract_version=snapshot.ContractVersion.V1,
        )
        for entity_type in sorted(CohortEntityType, key=lambda value: value.value)
    )
    return digest.build_cohort_digest(
        tenant=TENANT,
        source_revision=REVISION,
        entity_types=entity_types,
        contract_version=snapshot.ContractVersion.V1,
        generated_at=generated_at,
    )


def test_generated_at_is_outside_the_aggregate() -> None:
    """Two exports of unchanged data must reconcile, whatever the clock says."""

    early = _cohort_digest(generated_at=CAPTURED)
    late = _cohort_digest(generated_at=CAPTURED + timedelta(hours=3))
    assert early.generated_at != late.generated_at
    assert early.aggregate == late.aggregate


def test_the_source_revision_is_inside_the_aggregate() -> None:
    """A different schema revision is a different snapshot, not a clock."""

    baseline = _cohort_digest(generated_at=CAPTURED)
    other_revision = digest.build_cohort_digest(
        tenant=TENANT,
        source_revision=REVISION.model_copy(update={"schema_revision": "999_later"}),
        entity_types=baseline.entity_types,
        contract_version=snapshot.ContractVersion.V1,
        generated_at=CAPTURED,
    )
    assert baseline.aggregate != other_revision.aggregate


def test_a_content_change_changes_the_cohort_aggregate() -> None:
    assert (
        _cohort_digest(generated_at=CAPTURED).aggregate
        != _cohort_digest(generated_at=CAPTURED, party_name="Grace Hopper").aggregate
    )


# --------------------------------------------------------------------------
# page invariants
# --------------------------------------------------------------------------


def test_unordered_page_records_are_refused() -> None:
    with pytest.raises(ValidationError) as caught:
        _page(
            (
                _party("22222222-2222-2222-2222-222222222222"),
                _party("11111111-1111-1111-1111-111111111111"),
            )
        )
    assert "ordered by source id" in str(caught.value)


def test_ordered_page_records_are_accepted() -> None:
    page = _page(
        (
            _party("11111111-1111-1111-1111-111111111111"),
            _party("22222222-2222-2222-2222-222222222222"),
        )
    )
    assert len(page.records) == 2


def test_a_complete_page_may_not_also_offer_a_continuation() -> None:
    with pytest.raises(ValidationError) as caught:
        snapshot.SnapshotPage(
            contract_version=snapshot.ContractVersion.V1,
            tenant=TENANT,
            source_revision=REVISION,
            entity_type=CohortEntityType.PARTY,
            records=(_party("11111111-1111-1111-1111-111111111111"),),
            next_cursor=snapshot.ExportCursor(
                entity_type=CohortEntityType.PARTY,
                after_source_id=UUID("11111111-1111-1111-1111-111111111111"),
            ),
            completeness=snapshot.Completeness.COMPLETE,
        )
    assert "different conclusion" in str(caught.value)


def test_a_page_may_not_exceed_the_maximum_size() -> None:
    with pytest.raises(ValidationError):
        snapshot.ExportCursor(
            entity_type=CohortEntityType.PARTY,
            after_source_id=None,
            page_size=snapshot.MAX_PAGE_SIZE + 1,
        )


def test_a_page_keeps_its_records_typed() -> None:
    """The discriminated union must not flatten a record to its base class."""

    page = _page((_party("11111111-1111-1111-1111-111111111111"),))
    assert isinstance(page.records[0], snapshot.PartyRecord)
    assert page.records[0].display_name == "Ada Lovelace"


def test_a_page_carries_one_entity_type() -> None:
    """A page declaring one type may not carry a record of another."""

    membership = snapshot.OrganizationMembershipRecord(
        source_id=UUID("33333333-3333-3333-3333-333333333333"),
        created_at=CAPTURED,
        updated_at=CAPTURED,
        organization_id=UUID("44444444-4444-4444-4444-444444444444"),
        person_id=UUID("55555555-5555-5555-5555-555555555555"),
        party_membership_id=None,
        party_bound_at=None,
        party_binding_source=None,
        party_binding_reason=None,
        role="member",
        is_active=True,
    )
    with pytest.raises(ValidationError) as caught:
        snapshot.SnapshotPage(
            contract_version=snapshot.ContractVersion.V1,
            tenant=TENANT,
            source_revision=REVISION,
            entity_type=CohortEntityType.PARTY,
            records=(membership,),
            next_cursor=None,
            completeness=snapshot.Completeness.COMPLETE,
        )
    assert "disagree with its declared type" in str(caught.value)


def test_tenant_scope_cannot_be_omitted() -> None:
    with pytest.raises(ValidationError):
        snapshot.TenantScope.model_validate({})


# --------------------------------------------------------------------------
# minimisation
# --------------------------------------------------------------------------

_SECRET_TOKENS = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "credential",
    "private_key",
    "salt",
    "hash",
    "otp",
    "session",
)


def test_no_exported_field_names_credential_material() -> None:
    """Structural, not incidental: the cohort's tables hold no credentials.

    Asserted anyway, because the next field added to one of these records is
    the one that could.
    """

    offenders = sorted(
        f"{record.__name__}.{field}"
        for record in snapshot.RECORD_TYPES.values()
        for field in record.model_fields
        for token in _SECRET_TOKENS
        if token in field.lower()
    )
    assert not offenders, "\n  ".join(offenders)


def test_regulated_personal_values_cross_as_presence_only() -> None:
    fields = snapshot.CustomerAccountRecord.model_fields
    assert "nin" not in fields
    assert "date_of_birth" not in fields
    assert "nin_present" in fields
    assert "date_of_birth_present" in fields


def test_free_text_operator_notes_are_not_exported() -> None:
    for record in snapshot.RECORD_TYPES.values():
        assert "notes" not in record.model_fields, record.__name__


def test_an_unclassified_blob_crosses_as_keys_and_a_digest() -> None:
    blob = snapshot.opaque_blob({"zeta": {"secret": "value"}, "alpha": 1})
    assert blob is not None
    assert blob.keys == ("alpha", "zeta")
    assert len(blob.digest) == 64
    assert set(snapshot.OpaqueBlob.model_fields) == {"keys", "digest"}, (
        "an opaque blob gained a field; the only two it may have are the key "
        "inventory a reviewer needs and the digest a comparison needs"
    )


def test_two_spellings_of_one_blob_digest_identically() -> None:
    assert snapshot.opaque_blob({"a": 1, "b": 2}) == snapshot.opaque_blob(
        {"b": 2, "a": 1}
    )


def test_a_changed_blob_value_changes_its_digest() -> None:
    first = snapshot.opaque_blob({"a": 1})
    second = snapshot.opaque_blob({"a": 2})
    assert first is not None and second is not None
    assert first.keys == second.keys
    assert first.digest != second.digest


def test_derived_fields_are_declared_rather_than_filtered() -> None:
    derived = snapshot.CustomerAccountRecord.DERIVED_FIELDS
    assert "mrr_total" in derived
    assert "status" in derived
    assert derived <= set(snapshot.CustomerAccountRecord.model_fields)


# --------------------------------------------------------------------------
# the export decides nothing about the destination
# --------------------------------------------------------------------------

_TARGET_VOCABULARY = (
    "disposition",
    "should_migrate",
    "migrate",
    "quarantine",
    "accepted",
    "rejected",
    "target_status",
    "adopted",
    "cutover",
)


def test_no_record_field_speaks_the_destination_vocabulary() -> None:
    offenders = sorted(
        f"{record.__name__}.{field}"
        for record in snapshot.RECORD_TYPES.values()
        for field in record.model_fields
        for word in _TARGET_VOCABULARY
        if word in field.lower()
    )
    assert not offenders, (
        "a source snapshot that carries a disposition has already made the "
        "destination's resolver redundant:\n  " + "\n  ".join(offenders)
    )


def test_every_declared_entity_type_has_a_record_type() -> None:
    assert set(snapshot.RECORD_TYPES) == set(CohortEntityType)


# --------------------------------------------------------------------------
# comparison verdicts
# --------------------------------------------------------------------------


def test_the_verdict_vocabulary_is_closed_at_six() -> None:
    assert [member.value for member in digest.MismatchCategory] == [
        "missing-from-target",
        "unexpected-in-target",
        "divergent",
        "unsupported-version",
        "source-unknown",
        "target-unknown",
    ]


def test_identical_digests_produce_no_mismatch() -> None:
    source = _cohort_digest(generated_at=CAPTURED)
    target = _cohort_digest(generated_at=CAPTURED + timedelta(hours=1))
    assert digest.compare(source, target) == ()


def test_a_row_absent_from_the_target_is_reported_as_missing() -> None:
    source = _cohort_digest(generated_at=CAPTURED)
    empty = digest.build_cohort_digest(
        tenant=TENANT,
        source_revision=REVISION,
        entity_types=tuple(
            digest.build_entity_type_digest(
                entity_type=item.entity_type,
                entries=(),
                completeness=snapshot.Completeness.COMPLETE,
                resume_from=None,
                contract_version=snapshot.ContractVersion.V1,
            )
            for item in source.entity_types
        ),
        contract_version=snapshot.ContractVersion.V1,
        generated_at=CAPTURED,
    )
    verdicts = digest.compare(source, empty)
    assert [verdict.category for verdict in verdicts] == [
        digest.MismatchCategory.MISSING_FROM_TARGET
    ]
    assert verdicts[0].identity == "party:11111111-1111-1111-1111-111111111111"


def test_a_row_only_in_the_target_is_reported_as_unexpected() -> None:
    source_empty = digest.build_cohort_digest(
        tenant=TENANT,
        source_revision=REVISION,
        entity_types=tuple(
            digest.build_entity_type_digest(
                entity_type=entity_type,
                entries=(),
                completeness=snapshot.Completeness.COMPLETE,
                resume_from=None,
                contract_version=snapshot.ContractVersion.V1,
            )
            for entity_type in sorted(CohortEntityType, key=lambda value: value.value)
        ),
        contract_version=snapshot.ContractVersion.V1,
        generated_at=CAPTURED,
    )
    verdicts = digest.compare(source_empty, _cohort_digest(generated_at=CAPTURED))
    assert [verdict.category for verdict in verdicts] == [
        digest.MismatchCategory.UNEXPECTED_IN_TARGET
    ]


def test_a_changed_row_is_reported_as_divergent() -> None:
    verdicts = digest.compare(
        _cohort_digest(generated_at=CAPTURED),
        _cohort_digest(generated_at=CAPTURED, party_name="Grace Hopper"),
    )
    assert [verdict.category for verdict in verdicts] == [
        digest.MismatchCategory.DIVERGENT
    ]


def test_a_version_difference_stops_the_comparison() -> None:
    source = _cohort_digest(generated_at=CAPTURED)
    target = source.model_copy(update={"schema_version": source.schema_version + 1})
    verdicts = digest.compare(source, target)
    assert {verdict.category for verdict in verdicts} == {
        digest.MismatchCategory.UNSUPPORTED_VERSION
    }
    assert len(verdicts) == len(CohortEntityType)


def test_a_partial_source_drain_reports_source_unknown_not_drift() -> None:
    """Treating an unread page as read would report unread rows as missing."""

    partial = digest.build_cohort_digest(
        tenant=TENANT,
        source_revision=REVISION,
        entity_types=tuple(
            digest.build_entity_type_digest(
                entity_type=entity_type,
                entries=(),
                completeness=(
                    snapshot.Completeness.PARTIAL
                    if entity_type is CohortEntityType.PARTY
                    else snapshot.Completeness.COMPLETE
                ),
                resume_from=(
                    snapshot.ExportCursor(entity_type=entity_type, after_source_id=None)
                    if entity_type is CohortEntityType.PARTY
                    else None
                ),
                contract_version=snapshot.ContractVersion.V1,
            )
            for entity_type in sorted(CohortEntityType, key=lambda value: value.value)
        ),
        contract_version=snapshot.ContractVersion.V1,
        generated_at=CAPTURED,
    )
    verdicts = digest.compare(partial, _cohort_digest(generated_at=CAPTURED))
    assert [verdict.category for verdict in verdicts] == [
        digest.MismatchCategory.SOURCE_UNKNOWN
    ]


def test_a_partial_target_drain_reports_target_unknown() -> None:
    source = _cohort_digest(generated_at=CAPTURED)
    partial_target = digest.build_cohort_digest(
        tenant=TENANT,
        source_revision=REVISION,
        entity_types=tuple(
            digest.build_entity_type_digest(
                entity_type=item.entity_type,
                entries=(),
                completeness=(
                    snapshot.Completeness.PARTIAL
                    if item.entity_type is CohortEntityType.PARTY
                    else snapshot.Completeness.COMPLETE
                ),
                resume_from=(
                    snapshot.ExportCursor(
                        entity_type=item.entity_type, after_source_id=None
                    )
                    if item.entity_type is CohortEntityType.PARTY
                    else None
                ),
                contract_version=snapshot.ContractVersion.V1,
            )
            for item in source.entity_types
        ),
        contract_version=snapshot.ContractVersion.V1,
        generated_at=CAPTURED,
    )
    verdicts = digest.compare(source, partial_target)
    assert [verdict.category for verdict in verdicts] == [
        digest.MismatchCategory.TARGET_UNKNOWN
    ]


def test_a_mismatch_carries_no_field_value() -> None:
    """An adjudicator reads mismatch lists; they must not hand out customer data."""

    verdicts = digest.compare(
        _cohort_digest(generated_at=CAPTURED),
        _cohort_digest(generated_at=CAPTURED, party_name="Grace Hopper"),
    )
    rendered = " ".join(f"{verdict.identity} {verdict.detail}" for verdict in verdicts)
    assert "Grace" not in rendered
    assert "Ada" not in rendered


def test_a_cohort_digest_cannot_claim_completeness_over_a_partial_type() -> None:
    with pytest.raises(ValidationError) as caught:
        digest.CohortDigest(
            contract_version=snapshot.ContractVersion.V1,
            tenant=TENANT,
            source_revision=REVISION,
            entity_types=(
                digest.build_entity_type_digest(
                    entity_type=CohortEntityType.PARTY,
                    entries=(),
                    completeness=snapshot.Completeness.PARTIAL,
                    resume_from=snapshot.ExportCursor(
                        entity_type=CohortEntityType.PARTY, after_source_id=None
                    ),
                    contract_version=snapshot.ContractVersion.V1,
                ),
            ),
            total_count=0,
            aggregate="0" * 64,
            completeness=snapshot.Completeness.COMPLETE,
            generated_at=CAPTURED,
        )
    assert "cannot be complete" in str(caught.value)


def test_an_entity_type_digest_refuses_a_foreign_identity() -> None:
    with pytest.raises(ValidationError) as caught:
        digest.EntityTypeDigest(
            entity_type=CohortEntityType.PARTY,
            count=1,
            entries=(
                digest.EntityDigest(
                    identity="customer_account:11111111-1111-1111-1111-111111111111",
                    digest="0" * 64,
                ),
            ),
            completeness=snapshot.Completeness.COMPLETE,
            resume_from=None,
            aggregate="0" * 64,
        )
    assert "another type" in str(caught.value)
