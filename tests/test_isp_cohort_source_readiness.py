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
# the three classification axes
# --------------------------------------------------------------------------


def _surface(**overrides: object) -> surfaces.SourceSurface:
    """A minimal legal surface, so each test varies exactly one thing."""

    payload: dict[str, object] = {
        "path": "app/services/party.py",
        "family": surfaces.EntryPointFamily.SERVICE,
        "authority": surfaces.AuthorityRole.DECLARED_OWNER,
        "boundary": surfaces.BoundaryRole.PERSISTS,
        "reachability": surfaces.Reachability.INTERNAL_ONLY,
        "disposition": surfaces.Disposition.RETIRE_AFTER_CUTOVER,
        "entity_types": (cohort.CohortEntityType.PARTY,),
        "owning_service": "party.registry",
        "registry_declared": True,
        "open_question": None,
        "note": "the declared native identity owner",
    }
    payload.update(overrides)
    return surfaces.SourceSurface.model_validate(payload)


def test_a_legal_surface_constructs() -> None:
    """The acceptance case every rejection below is measured against."""

    surface = _surface()
    assert surface.writes
    assert surface.production_runtime
    assert surface.classification is (
        surfaces.SurfaceClassification.AUTHORITATIVE_WRITER
    )


def test_an_undeclared_module_cannot_be_called_a_declared_owner() -> None:
    with pytest.raises(ValidationError) as caught:
        _surface(registry_declared=False)
    assert "flattering name" in str(caught.value)


def test_a_reachable_writer_cannot_disclaim_all_authority() -> None:
    """Writing IS authority, whether or not anybody granted it."""

    with pytest.raises(ValidationError) as caught:
        _surface(
            authority=surfaces.AuthorityRole.NO_AUTHORITY,
            registry_declared=False,
            owning_service=None,
        )
    assert "say which" in str(caught.value)


def test_a_disposable_database_writer_may_disclaim_authority() -> None:
    """The acceptance half: a fixture seeder writes real rows and owns nothing."""

    surface = _surface(
        path="scripts/seed/seed_test_fixtures.py",
        family=surfaces.EntryPointFamily.CLI_SCRIPT,
        authority=surfaces.AuthorityRole.NO_AUTHORITY,
        reachability=surfaces.Reachability.NON_PRODUCTION,
        disposition=surfaces.Disposition.NON_PRODUCTION_NO_ACTION,
        registry_declared=False,
        owning_service=None,
        note="a fixture seeder for test databases",
    )
    assert surface.writes
    assert not surface.production_runtime


def test_a_non_writer_cannot_hold_a_writer_authority() -> None:
    with pytest.raises(ValidationError) as caught:
        _surface(boundary=surfaces.BoundaryRole.READS)
    assert "without writing" in str(caught.value)


def test_an_applied_migration_has_only_the_schema_lineage_authority() -> None:
    with pytest.raises(ValidationError) as caught:
        _surface(
            path="alembic/versions/999_thing.py",
            family=surfaces.EntryPointFamily.MIGRATION,
            reachability=surfaces.Reachability.APPLIED_ONCE,
            disposition=surfaces.Disposition.HISTORICAL_NO_ACTION,
        )
    assert "schema lineage and nothing else" in str(caught.value)


def test_an_undetermined_axis_may_not_name_an_owner() -> None:
    with pytest.raises(ValidationError) as caught:
        _surface(
            path="app/services/mystery.py",
            authority=surfaces.AuthorityRole.UNDETERMINED,
            registry_declared=False,
        )
    assert "neither may be guessed" in str(caught.value)


def test_an_undetermined_axis_without_an_owner_is_accepted() -> None:
    surface = _surface(
        path="app/services/mystery.py",
        authority=surfaces.AuthorityRole.UNDETERMINED,
        registry_declared=False,
        owning_service=None,
        note="authority was looked for in the registry and not established",
    )
    assert surface.classification is surfaces.SurfaceClassification.UNKNOWN


def test_a_surface_without_a_rationale_is_refused() -> None:
    with pytest.raises(ValidationError):
        _surface(note="   ")


def test_the_three_axes_are_orthogonal() -> None:
    """Knowing one axis must not determine another, or they are one axis.

    Asserted over the real inventory rather than over the enum definitions: an
    orthogonality claim is about the data, and three vocabularies that always
    move together are one vocabulary written three times. If this ever fails,
    the honest fix is to merge the collapsed axes — not to invent a surface
    that keeps them apart.
    """

    axes = {
        "authority": [surface.authority for surface in surfaces.COHORT_SURFACES],
        "boundary": [surface.boundary for surface in surfaces.COHORT_SURFACES],
        "reachability": [surface.reachability for surface in surfaces.COHORT_SURFACES],
    }
    collapsed: list[str] = []
    for first, first_values in axes.items():
        for second, second_values in axes.items():
            if first == second:
                continue
            observed: dict[object, set[object]] = {}
            for left, right in zip(first_values, second_values, strict=True):
                observed.setdefault(left, set()).add(right)
            if all(len(options) == 1 for options in observed.values()):
                collapsed.append(f"{first} determines {second}")
    assert not collapsed, (
        "these axes are not independent in the inventory, so they are one "
        "classification wearing three names:\n  " + "\n  ".join(collapsed)
    )


def test_the_derived_classification_covers_every_surface() -> None:
    """The retained eight-member view must answer for all 45 rows."""

    grouped = surfaces.surfaces_by_classification()
    assert sum(len(paths) for paths in grouped.values()) == len(
        surfaces.COHORT_SURFACES
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


def test_nothing_observes_into_the_cohort_and_that_is_a_finding() -> None:
    """`OBSERVES` is empty, and the reason is recorded rather than implied.

    Provider payloads terminate in the Integration Inbox, which is outside
    this cohort. If a surface ever starts collecting an observation directly
    into a cohort table, this fails and the finding in
    `docs/ISP_COHORT1_SOURCE_OWNERSHIP.md` has to be rewritten rather than
    silently outgrown.
    """

    assert surfaces.surfaces_by_boundary()[surfaces.BoundaryRole.OBSERVES] == ()


# --------------------------------------------------------------------------
# dispositions
# --------------------------------------------------------------------------


def test_every_surface_carries_a_disposition() -> None:
    grouped = surfaces.surfaces_by_disposition()
    assert sum(len(paths) for paths in grouped.values()) == len(
        surfaces.COHORT_SURFACES
    )


def test_an_undecided_disposition_must_state_its_question() -> None:
    with pytest.raises(ValidationError) as caught:
        _surface(disposition=surfaces.Disposition.UNDECIDED)
    assert "indistinguishable from an unfinished row" in str(caught.value)


def test_a_decided_disposition_may_not_carry_a_question() -> None:
    """The other direction: a decided surface hiding a live doubt."""

    with pytest.raises(ValidationError) as caught:
        _surface(open_question="but is this really settled, or did we assume it?")
    assert "hiding a live doubt" in str(caught.value)


def test_an_undecided_surface_with_a_real_question_is_accepted() -> None:
    surface = _surface(
        disposition=surfaces.Disposition.UNDECIDED,
        open_question=(
            "Does this fact migrate with the cohort or stay in Sub as history?"
        ),
    )
    assert surface.disposition is surfaces.Disposition.UNDECIDED


def test_a_token_open_question_is_refused() -> None:
    with pytest.raises(ValidationError) as caught:
        _surface(
            disposition=surfaces.Disposition.UNDECIDED,
            open_question="unclear",
        )
    assert "too short to act on" in str(caught.value)


def test_the_undecided_surfaces_are_the_ones_we_expect() -> None:
    """Three today. Named, so shrinking or growing the set is a reviewed diff."""

    assert [surface.path for surface in surfaces.undecided_surfaces()] == [
        "app/services/customer_location_requests.py",
        "app/services/mrr_snapshot.py",
        "app/services/web_system_restore_tool.py",
    ]


def test_every_displaced_writer_has_a_disposition_that_removes_it() -> None:
    """A writer a cutover displaces cannot be left as `REMAINS_IN_SUB`.

    `ctl-isp-009` ratchets the displaced set to zero. A surface in that set
    whose disposition says it stays is a contradiction that would be
    discovered when the ratchet refuses to reach zero.
    """

    removing = {
        surfaces.Disposition.RETIRE_AFTER_CUTOVER,
        surfaces.Disposition.ROUTE_THROUGH_OWNER_FIRST,
        surfaces.Disposition.UNDECIDED,
    }
    displaced = set(surfaces.displaced_writer_paths())
    contradictions = sorted(
        surface.path
        for surface in surfaces.COHORT_SURFACES
        if surface.path in displaced and surface.disposition not in removing
    )
    assert not contradictions, "\n  ".join(contradictions)


def test_surfaces_that_only_read_are_never_marked_for_retirement() -> None:
    """A reader is repointed or kept; retiring it would delete working code."""

    misfiled = sorted(
        surface.path
        for surface in surfaces.COHORT_SURFACES
        if not surface.writes
        and surface.disposition is surfaces.Disposition.RETIRE_AFTER_CUTOVER
    )
    assert not misfiled, "\n  ".join(misfiled)


def test_tables_with_no_counted_writer_state_what_was_searched() -> None:
    recorded = {entry.table for entry in surfaces.TABLES_WITH_NO_COUNTED_WRITER}
    assert recorded == {"organizations", "organization_memberships"}
    assert all(
        len(entry.searched.split()) >= 10
        for entry in surfaces.TABLES_WITH_NO_COUNTED_WRITER
    ), "an empty result has to say how hard it looked, or it reads as a conclusion"


def test_displaced_writers_exclude_fixture_and_rehearsal_tooling() -> None:
    production = set(surfaces.displaced_writer_paths())
    assert "scripts/seed/seed_test_fixtures.py" not in production
    assert "scripts/migration/kernel_lineage_rehearsal_canaries.py" not in production
    assert "app/services/party.py" in production
