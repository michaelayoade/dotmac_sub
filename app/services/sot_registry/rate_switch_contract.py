"""Governed switch for the single service rate owner."""

from app.services.sot_manifest import (
    AuthorityInput,
    AuthorityKind,
    AuthorityMigrationState,
    ConcernContract,
    ErrorContract,
    MigrationContract,
    OwnerRole,
    ServiceContract,
    SOTService,
    TransactionContract,
    TransactionMode,
    owner_command_boundary_error_codes,
)

SWITCH = "access.rate_profile_switch"

SERVICE = SOTService(
    name=SWITCH,
    module="app.services.access_rate_switch",
    owns=("rate owner switch",),
    depends_on=(
        "access.radius_projection",
        "control.settings_spec",
        "observability.audit_log",
    ),
    notes=(
        "The only way to turn radius.rate_owner_enabled on. Under one advisory "
        "lock it re-runs the read-only rate preflight in the same transaction "
        "and refuses on any divergence outside an approved, digest-bound list of "
        "expected changes (the live set must equal the list exactly), flips the setting with an audit "
        "record, then runs a full projection through access.radius_projection "
        "and reads every target's radreply rate back against the derived rate. "
        "Any projection failure or mismatch is reported with the final state; this "
        "release has no legacy precedence, so switching off is refused and a "
        "rollback is a redeploy of the previous release."
    ),
    contract=ServiceContract(
        concerns=(
            ConcernContract(
                name="rate owner switch",
                role=OwnerRole.APPLICATION_COORDINATOR,
                input_names=(
                    "attributable switch command",
                    "rate divergence preflight",
                    "full RADIUS projection",
                    "observed radreply rates",
                ),
            ),
        ),
        authoritative_inputs=(
            AuthorityInput(
                name="attributable switch command",
                owner=SWITCH,
                kind=AuthorityKind.CONTROL_INPUT,
                source="actor, reason and correlation carried by the command context",
            ),
            AuthorityInput(
                name="rate divergence preflight",
                owner="access.radius_projection",
                kind=AuthorityKind.DERIVED_PROJECTION,
                source=(
                    "access_rate_profile.preflight: derived rate versus the "
                    "current projection per provisioned login"
                ),
            ),
            AuthorityInput(
                name="full RADIUS projection",
                owner="access.radius_projection",
                kind=AuthorityKind.DERIVED_PROJECTION,
                source="radius_population.populate, the single projection writer",
            ),
            AuthorityInput(
                name="observed radreply rates",
                owner="external:freeradius",
                kind=AuthorityKind.EXTERNAL_OBSERVATION,
                source="Mikrotik-Rate-Limit rows read back from each target",
            ),
        ),
        transaction=TransactionContract(
            mode=TransactionMode.COORDINATOR_MANAGED,
            boundary=(
                "Each flip is one owner command: lock, preflight, setting write "
                "and audit commit together. The projection and the read-back run "
                "after commit and never inside it."
            ),
            locking="Transaction advisory lock serialises switch commands.",
            idempotency=(
                "Switching on when already on is refused; switching off is "
                "idempotent and re-projects."
            ),
            retries=(
                "No automatic retry; a failed switch-on switches off again and "
                "the operator re-runs the preflight."
            ),
        ),
        errors=ErrorContract(
            domain_codes=(
                f"{SWITCH}.already_enabled",
                f"{SWITCH}.preflight_divergence",
                f"{SWITCH}.read_back_mismatch",
                f"{SWITCH}.projection_failed",
                f"{SWITCH}.no_target",
                f"{SWITCH}.baseline_not_clean",
                "{SWITCH}.switch_off_unavailable",
                f"{SWITCH}.moved_offer_unverified",
                f"{SWITCH}.owner_not_active",
                f"{SWITCH}.approved_list_digest_mismatch",
                f"{SWITCH}.approved_list_invalid",
                *owner_command_boundary_error_codes(SWITCH),
            ),
            mapping_owner="app.services.access_rate_switch",
            fail_closed_on=(
                "any preflight divergence",
                "an incomplete projection",
                "any radreply rate that differs from the derived rate",
            ),
        ),
        migration=MigrationContract(
            state=AuthorityMigrationState.NATIVE,
            new_owner=SWITCH,
        ),
        steward="network access",
        design_refs=("docs/SOT_RELATIONSHIP_MAP.md",),
        test_refs=("tests/test_access_rate_switch.py",),
    ),
)

SERVICES = (SERVICE,)
