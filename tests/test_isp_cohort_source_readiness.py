"""The cohort-isp-01 source-readiness contract must refuse what it forbids.

`app/migration_source/` is a governance contract before it is anything else:
it records which Governance revision Sub is bound to, which tables hold cohort
state, and what each writer of that state is. Every one of those is a claim
somebody could later weaken by editing a literal. These tests exist to make
the weakening fail.

Each rejection is paired with the acceptance case for the same code path. A
validator that has only ever been shown bad input passes for the wrong reason:
it would keep passing if the rule under test were deleted and the constructor
simply started refusing everything.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.migration_source import cohort, programme, surfaces

# --------------------------------------------------------------------------
# the Governance binding
# --------------------------------------------------------------------------


def test_the_binding_names_the_accepted_revision() -> None:
    assert programme.BINDING.revision == programme.ACCEPTED_REVISION
    assert programme.BINDING.programme_id == "pgm-dotmac-isp-replacement"
    assert programme.BINDING.cohort_id == "cohort-isp-01"
    assert programme.BINDING.cohort_sequence == 1


def test_a_mutable_revision_cannot_bind_the_programme() -> None:
    """A tag is a pointer its publisher can move after acceptance."""

    with pytest.raises(ValidationError) as caught:
        programme.GovernanceBinding.model_validate(
            programme.BINDING.model_dump() | {"revision": "main"}
        )
    assert "40-character" in str(caught.value)


def test_a_commit_shaped_revision_is_accepted() -> None:
    """The acceptance half: a real commit still constructs."""

    rebound = programme.GovernanceBinding.model_validate(
        programme.BINDING.model_dump() | {"revision": "a" * 40}
    )
    assert rebound.revision == "a" * 40


def test_the_cohort_cannot_be_recorded_as_open() -> None:
    """`CohortState` has no member that spells an opened cohort."""

    assert [member.value for member in programme.CohortState] == ["blocked"]
    assert programme.BINDING.cohort_state is programme.CohortState.BLOCKED


def test_readiness_claims_cannot_spell_adoption_or_cutover() -> None:
    forbidden = {"adopted", "composed", "cut_over", "cutover", "retired", "migrated"}
    spoken = {member.value for member in programme.SourceReadinessClaim}
    assert not spoken & forbidden, (
        "the readiness vocabulary gained a member that can claim an authority "
        "movement this repository is not entitled to record"
    )


def test_sub_is_named_as_an_evidence_producer_for_real_controls() -> None:
    assert programme.BINDING.evidence_control_ids == (
        "ctl-isp-006",
        "ctl-isp-007",
        "ctl-isp-009",
    )


def test_no_control_this_work_feeds_is_already_verified() -> None:
    """Producing an input is not verifying a control."""

    supplied = [
        control
        for control in programme.BINDING.controls
        if control.sub_supplies_evidence
    ]
    assert supplied and all(control.state == "blocked" for control in supplied)


def test_a_binding_with_no_sub_evidence_is_refused() -> None:
    payload = programme.BINDING.model_dump()
    payload["controls"] = [
        control | {"sub_supplies_evidence": False} for control in payload["controls"]
    ]
    with pytest.raises(ValidationError) as caught:
        programme.GovernanceBinding.model_validate(payload)
    assert "evidence producer" in str(caught.value)


# --------------------------------------------------------------------------
# the cohort surface
# --------------------------------------------------------------------------


def test_every_declared_entity_type_maps_to_exactly_one_table() -> None:
    mapping = cohort.cohort_tables_by_entity()
    assert set(mapping) == set(cohort.CohortEntityType)
    assert len(cohort.cohort_table_names()) == len(cohort.COHORT_TABLES)


def test_a_table_outside_sub_cannot_join_the_cohort_surface() -> None:
    """The surface may only name tables this application owns."""

    with pytest.raises(ValidationError):
        cohort.CohortTable(
            entity_type=cohort.CohortEntityType.PARTY,
            table="parties",
            model_class="Party",
            model_module="dotmac_party.models",
            owning_service=None,
            expected_target_component=cohort.CohortComponent.PARTY,
        )


def test_a_sub_table_is_accepted() -> None:
    """The acceptance half of the previous check."""

    declared = cohort.CohortTable(
        entity_type=cohort.CohortEntityType.PARTY,
        table="parties",
        model_class="Party",
        model_module="app.models.party",
        owning_service=None,
        expected_target_component=cohort.CohortComponent.PARTY,
    )
    assert declared.table == "parties"


def test_the_cohort_surface_excludes_later_cohort_tables() -> None:
    """Pulling a later cohort's table forward would erase the boundary."""

    later_cohorts = {
        "resellers",
        "reseller_users",
        "subscriptions",
        "invoices",
        "payments",
        "ip_assignments",
    }
    assert not cohort.cohort_table_names() & later_cohorts


def test_excluded_adjacent_tables_are_not_silently_absent() -> None:
    excluded = {entry.table for entry in surfaces.UNMAPPED_ADJACENT_TABLES}
    assert "resellers" in excluded
    assert not excluded & cohort.cohort_table_names(), (
        "a table cannot be both exported and deliberately excluded"
    )


def test_an_exclusion_without_a_reason_is_refused() -> None:
    with pytest.raises(ValidationError) as caught:
        surfaces.UnmappedAdjacentTable(table="resellers", reason="later")
    assert "oversight" in str(caught.value)


# --------------------------------------------------------------------------
# the surface classification
# --------------------------------------------------------------------------


def test_an_undeclared_module_cannot_be_called_an_authoritative_writer() -> None:
    with pytest.raises(ValidationError) as caught:
        surfaces.SourceSurface(
            path="app/services/whatever.py",
            family=surfaces.EntryPointFamily.SERVICE,
            classification=surfaces.SurfaceClassification.AUTHORITATIVE_WRITER,
            entity_types=(cohort.CohortEntityType.PARTY,),
            owning_service="party.registry",
            registry_declared=False,
            production_runtime=True,
            note="claims to own party identity",
        )
    assert "flattering name" in str(caught.value)


def test_a_declared_module_may_be_an_authoritative_writer() -> None:
    """The acceptance half: the rule admits a real owner."""

    surface = surfaces.SourceSurface(
        path="app/services/party.py",
        family=surfaces.EntryPointFamily.SERVICE,
        classification=surfaces.SurfaceClassification.AUTHORITATIVE_WRITER,
        entity_types=(cohort.CohortEntityType.PARTY,),
        owning_service="party.registry",
        registry_declared=True,
        production_runtime=True,
        note="the declared native identity owner",
    )
    assert surface.writes


def test_an_unknown_surface_may_not_name_an_owner() -> None:
    with pytest.raises(ValidationError) as caught:
        surfaces.SourceSurface(
            path="app/services/mystery.py",
            family=surfaces.EntryPointFamily.SERVICE,
            classification=surfaces.SurfaceClassification.UNKNOWN,
            entity_types=(cohort.CohortEntityType.PARTY,),
            owning_service="party.registry",
            registry_declared=True,
            production_runtime=True,
            note="ownership was not established",
        )
    assert "neither may be guessed" in str(caught.value)


def test_an_unknown_surface_without_an_owner_is_accepted() -> None:
    surface = surfaces.SourceSurface(
        path="app/services/mystery.py",
        family=surfaces.EntryPointFamily.SERVICE,
        classification=surfaces.SurfaceClassification.UNKNOWN,
        entity_types=(cohort.CohortEntityType.PARTY,),
        owning_service=None,
        registry_declared=False,
        production_runtime=True,
        note="ownership was looked for in the registry and not established",
    )
    assert surface.classification is surfaces.SurfaceClassification.UNKNOWN


def test_a_surface_without_a_rationale_is_refused() -> None:
    with pytest.raises(ValidationError):
        surfaces.SourceSurface(
            path="app/services/party.py",
            family=surfaces.EntryPointFamily.SERVICE,
            classification=surfaces.SurfaceClassification.READ_ONLY_CONSUMER,
            entity_types=(cohort.CohortEntityType.PARTY,),
            owning_service=None,
            registry_declared=False,
            production_runtime=True,
            note="   ",
        )


def test_no_inventoried_surface_is_left_unknown() -> None:
    """Today's inventory has no UNKNOWN row, and that is a claim, not a default.

    If one appears later this test should be updated to assert the specific
    open question rather than deleted: an UNKNOWN row is meant to be visible.
    """

    unknown = surfaces.surfaces_by_classification()[
        surfaces.SurfaceClassification.UNKNOWN
    ]
    assert unknown == ()


def test_tables_with_no_counted_writer_state_what_was_searched() -> None:
    recorded = {entry.table for entry in surfaces.TABLES_WITH_NO_COUNTED_WRITER}
    assert recorded == {"organizations", "organization_memberships"}
    assert all(
        len(entry.searched.split()) >= 10
        for entry in surfaces.TABLES_WITH_NO_COUNTED_WRITER
    ), "an empty result has to say how hard it looked, or it reads as a conclusion"


def test_production_writers_exclude_fixture_and_rehearsal_tooling() -> None:
    production = set(surfaces.production_writer_paths())
    assert "scripts/seed/seed_test_fixtures.py" not in production
    assert "scripts/migration/kernel_lineage_rehearsal_canaries.py" not in production
    assert "app/services/party.py" in production
