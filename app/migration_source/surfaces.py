"""Who writes, adapts and consumes cohort-isp-01 source state today.

The writer *census* is mechanical — `scripts/architecture/isp_cohort_writers.py`
counts it from the AST, and a two-directional ratchet freezes the result. This
module is the other half: what each counted surface **is**, which no static
scan can decide. A file that assigns `subscriber.status` and a file that
assigns `subscriber.metadata_` are the same shape to a parser and completely
different facts to a migration.

## The classification is a finding, not a plan

`LEGACY_PARALLEL_WRITER` here does not schedule anything. It records that a
second path writes a fact some other owner is declared to own, so the cutover
sequence knows what has to be displaced before authority can move. Nothing in
this module retires, disables or reroutes a writer; Sub remains the sole
production writer for this cohort until a separately authorised sealed switch.

## UNKNOWN means unknown

`SurfaceClassification.UNKNOWN` is reserved for a surface whose ownership was
looked for and not established. It is never a synonym for "none found" and
never a placeholder for "probably fine" — an UNKNOWN row is an open question
carried into the cutover gate, and the validator below refuses one that does
not say what is unknown about it.

The inverse matters just as much. `organizations` and
`organization_memberships` have **no** counted writer, and that is a searched
result rather than an absence of effort: no construction, no tracked mutation,
no set-based DML and no raw statement names them anywhere under `app/`,
`scripts/` or the executable migration lineage. That is recorded as a stated
finding in `TABLES_WITH_NO_COUNTED_WRITER`, with the census's blind spots named
alongside it, so nobody later reads the empty result as UNKNOWN or as proof.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict, model_validator

from app.migration_source.cohort import CohortEntityType


class SurfaceClassification(StrEnum):
    """What one surface is allowed to be, relative to the cohort's facts."""

    #: The declared owner of the fact it writes.
    AUTHORITATIVE_WRITER = "authoritative_writer"
    #: Validates, authorises and delegates to an owner; owns the transaction
    #: but decides nothing. Writes no cohort column itself.
    AUTHORIZED_ADAPTER = "authorized_adapter"
    #: Writes a value derived from another owner's decision onto a cohort row.
    DERIVED_PROJECTION = "derived_projection"
    #: Records an external fact as an observation, deciding nothing.
    OBSERVATION_COLLECTOR = "observation_collector"
    #: Carries data between systems; holds no authority over it.
    TRANSPORT = "transport"
    #: Reads cohort state and never writes it.
    READ_ONLY_CONSUMER = "read_only_consumer"
    #: A second path writing a fact another owner is declared to own.
    LEGACY_PARALLEL_WRITER = "legacy_parallel_writer"
    #: Ownership was looked for and not established. Carried into the gate.
    UNKNOWN = "unknown"


class EntryPointFamily(StrEnum):
    """Mirrors the census families so inventory and ratchet speak one language.

    Duplicated deliberately rather than imported from
    `scripts/architecture/isp_cohort_writers`: `app/` must not import from
    `scripts/`, and a test asserts the two enumerations still have identical
    members, so the duplication cannot drift unnoticed.
    """

    API_ROUTE = "api_route"
    WEB_ROUTE = "web_route"
    WEB_PRESENTER = "web_presenter"
    SERVICE = "service"
    TASK_WORKER = "task_worker"
    SCHEDULED_JOB = "scheduled_job"
    EVENT_HANDLER = "event_handler"
    WEBSOCKET = "websocket"
    IMPORTER = "importer"
    POLLER = "poller"
    CLI_SCRIPT = "cli_script"
    MIGRATION = "migration"
    APP_MODULE = "app_module"
    REPOSITORY_ROOT = "repository_root"


_WRITING_CLASSIFICATIONS: Final[frozenset[SurfaceClassification]] = frozenset(
    {
        SurfaceClassification.AUTHORITATIVE_WRITER,
        SurfaceClassification.DERIVED_PROJECTION,
        SurfaceClassification.LEGACY_PARALLEL_WRITER,
        SurfaceClassification.OBSERVATION_COLLECTOR,
    }
)


class SourceSurface(BaseModel):
    """One classified file in the cohort-isp-01 source surface."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Repository-relative path, matching the census key exactly.
    path: str
    family: EntryPointFamily
    classification: SurfaceClassification
    #: Cohort entity types this surface touches.
    entity_types: tuple[CohortEntityType, ...]
    #: The owner this surface acts as or bypasses. A SOT registry service name
    #: when `registry_declared`; otherwise a plain description of the
    #: authority it runs under, or `None` when there is none to name.
    owning_service: str | None
    #: Whether `app/services/sot_registry/` declares this module as a service.
    registry_declared: bool
    #: False for a surface that cannot write the production database again:
    #: fixture seeders, rehearsal canaries against disposable databases, and
    #: applied migrations Alembic will not re-run. They still count in the
    #: ratchet — a *new* one is exactly what the guard should catch — but
    #: folding them into the production writer count would overstate what a
    #: cutover has to displace, and none of them can be "retired".
    production_runtime: bool
    #: Why this classification, in one sentence a reviewer can disagree with.
    note: str

    @model_validator(mode="after")
    def _check(self) -> SourceSurface:
        if not self.note.strip():
            raise ValueError(f"{self.path} carries no classification rationale")
        if not self.entity_types:
            raise ValueError(
                f"{self.path} claims to be a cohort surface but names no entity"
            )
        if len(set(self.entity_types)) != len(self.entity_types):
            raise ValueError(f"{self.path} repeats an entity type")
        if (
            self.classification is SurfaceClassification.AUTHORITATIVE_WRITER
            and not self.registry_declared
        ):
            raise ValueError(
                f"{self.path} is called an authoritative writer but the SOT "
                "registry does not declare it. An owner nobody declared is a "
                "parallel writer with a flattering name."
            )
        if self.classification is SurfaceClassification.UNKNOWN and (
            self.owning_service is not None
        ):
            raise ValueError(
                f"{self.path} is UNKNOWN yet names an owner; one of the two "
                "statements is wrong and neither may be guessed"
            )
        return self

    @property
    def writes(self) -> bool:
        """Whether this surface mutates cohort state."""

        return self.classification in _WRITING_CLASSIFICATIONS


COHORT_SURFACES: Final[tuple[SourceSurface, ...]] = (
    # ---- declared owners --------------------------------------------------
    SourceSurface(
        path="app/services/party.py",
        family=EntryPointFamily.SERVICE,
        classification=SurfaceClassification.AUTHORITATIVE_WRITER,
        entity_types=(
            CohortEntityType.PARTY,
            CohortEntityType.PARTY_ROLE,
            CohortEntityType.PARTY_RELATIONSHIP,
            CohortEntityType.PARTY_MEMBERSHIP,
            CohortEntityType.PARTY_CONTACT_POINT,
            CohortEntityType.PARTY_EXTERNAL_REFERENCE,
            CohortEntityType.CUSTOMER_ACCOUNT,
            CohortEntityType.CUSTOMER_CONTACT,
        ),
        owning_service="party.registry",
        registry_declared=True,
        production_runtime=True,
        note=(
            "The declared native identity owner. Its writes to `subscribers` "
            "and `subscriber_contacts` are the canonical Party binding "
            "columns, not the legacy identity columns beside them."
        ),
    ),
    SourceSurface(
        path="app/services/subscriber.py",
        family=EntryPointFamily.SERVICE,
        classification=SurfaceClassification.AUTHORITATIVE_WRITER,
        entity_types=(
            CohortEntityType.CUSTOMER_ACCOUNT,
            CohortEntityType.CUSTOMER_ADDRESS,
        ),
        owning_service="customer.accounts",
        registry_declared=True,
        production_runtime=True,
        note=(
            "The declared owner of Subscriber account creation. Its own SOT "
            "note already records that existing direct writers elsewhere are "
            "shrink-only migration debt."
        ),
    ),
    SourceSurface(
        path="app/services/brand_profiles.py",
        family=EntryPointFamily.SERVICE,
        classification=SurfaceClassification.AUTHORITATIVE_WRITER,
        entity_types=(CohortEntityType.BRAND_PROFILE,),
        owning_service="customer.branding",
        registry_declared=True,
        production_runtime=True,
        note="The declared owner of platform, reseller and organization brand profiles.",
    ),
    SourceSurface(
        path="app/services/web_customer_actions.py",
        family=EntryPointFamily.WEB_PRESENTER,
        classification=SurfaceClassification.AUTHORITATIVE_WRITER,
        entity_types=(CohortEntityType.CUSTOMER_ACCOUNT,),
        owning_service="customer.profile_commands",
        registry_declared=True,
        production_runtime=True,
        note=(
            "Named like a presenter, declared as an owner. Kept in the "
            "`web_presenter` family because that is where it lives and how a "
            "reader will find it, and classified as an owner because the "
            "registry says so — family is location, classification is "
            "authority, and conflating them is how a real owner gets "
            "mistaken for a stray adapter."
        ),
    ),
    SourceSurface(
        path="app/services/subscriber_profile_cleanup.py",
        family=EntryPointFamily.SERVICE,
        classification=SurfaceClassification.AUTHORITATIVE_WRITER,
        entity_types=(CohortEntityType.CUSTOMER_ACCOUNT,),
        owning_service="customer.profile_cleanup",
        registry_declared=True,
        production_runtime=True,
        note="Declared owner of evidence-bound gender and date-of-birth repair.",
    ),
    SourceSurface(
        path="app/services/crm_customer_name_repair.py",
        family=EntryPointFamily.SERVICE,
        classification=SurfaceClassification.AUTHORITATIVE_WRITER,
        entity_types=(CohortEntityType.CUSTOMER_ACCOUNT,),
        owning_service="customer.name_remediation",
        registry_declared=True,
        production_runtime=True,
        note="Declared owner of the reviewed legacy customer-name repair manifest.",
    ),
    # ---- projections ------------------------------------------------------
    SourceSurface(
        path="app/services/account_lifecycle.py",
        family=EntryPointFamily.SERVICE,
        classification=SurfaceClassification.DERIVED_PROJECTION,
        entity_types=(CohortEntityType.CUSTOMER_ACCOUNT,),
        owning_service="access.subscription_lifecycle",
        registry_declared=True,
        production_runtime=True,
        note=(
            "Writes the Subscriber lifecycle projection and its override "
            "columns. The customer_context registry already names this a "
            "projection of canonical subscription lifecycle state, so the "
            "account row is downstream of a decision made elsewhere — the "
            "export must carry it as derived, never as the account's own "
            "status."
        ),
    ),
    SourceSurface(
        path="app/services/mrr_snapshot.py",
        family=EntryPointFamily.SERVICE,
        classification=SurfaceClassification.DERIVED_PROJECTION,
        entity_types=(CohortEntityType.CUSTOMER_ACCOUNT,),
        owning_service=None,
        registry_declared=False,
        production_runtime=True,
        note=(
            "Writes `subscribers.mrr_total`, a money figure derived from "
            "subscription state, from a module the registry does not declare. "
            "A derived money column with no named owner is exactly the shape "
            "that survives a migration as an authoritative-looking number, so "
            "the export carries it as derived and the target must recompute "
            "rather than trust it."
        ),
    ),
    # ---- parallel writers -------------------------------------------------
    SourceSurface(
        path="app/services/account_deletion.py",
        family=EntryPointFamily.SERVICE,
        classification=SurfaceClassification.LEGACY_PARALLEL_WRITER,
        entity_types=(CohortEntityType.CUSTOMER_ACCOUNT,),
        owning_service="customer.accounts",
        registry_declared=False,
        production_runtime=True,
        note="Writes the account's `metadata` blob directly rather than through customer.accounts.",
    ),
    SourceSurface(
        path="app/services/billing_cleanup_remediation.py",
        family=EntryPointFamily.SERVICE,
        classification=SurfaceClassification.LEGACY_PARALLEL_WRITER,
        entity_types=(CohortEntityType.CUSTOMER_ACCOUNT,),
        owning_service="customer.accounts",
        registry_declared=False,
        production_runtime=True,
        note="A billing remediation path sets `subscribers.billing_mode` without the account owner.",
    ),
    SourceSurface(
        path="app/services/crm_portal.py",
        family=EntryPointFamily.SERVICE,
        classification=SurfaceClassification.LEGACY_PARALLEL_WRITER,
        entity_types=(CohortEntityType.CUSTOMER_ACCOUNT,),
        owning_service="party.registry",
        registry_declared=False,
        production_runtime=True,
        note=(
            "Stamps `subscribers.crm_subscriber_id` in place. External "
            "identity-reference provenance is declared to party.registry and "
            "belongs in `party_external_references`, so this is a second "
            "provenance store on the authoritative row."
        ),
    ),
    SourceSurface(
        path="app/services/crm_ticket_pull.py",
        family=EntryPointFamily.SERVICE,
        classification=SurfaceClassification.LEGACY_PARALLEL_WRITER,
        entity_types=(CohortEntityType.CUSTOMER_ACCOUNT,),
        owning_service="party.registry",
        registry_declared=False,
        production_runtime=True,
        note=(
            "A ticket importer that also writes the customer account's CRM "
            "provenance id. Collecting an observation is legitimate; writing "
            "it onto the authoritative row is what makes it parallel."
        ),
    ),
    SourceSurface(
        path="app/services/customer_location_requests.py",
        family=EntryPointFamily.SERVICE,
        classification=SurfaceClassification.LEGACY_PARALLEL_WRITER,
        entity_types=(
            CohortEntityType.CUSTOMER_ACCOUNT,
            CohortEntityType.CUSTOMER_ADDRESS,
        ),
        owning_service="customer.accounts",
        registry_declared=False,
        production_runtime=True,
        note=(
            "Constructs `Address` rows and writes the account `metadata` blob "
            "from an undeclared module. Addresses have no declared owner at "
            "all, which is why this one is debt rather than a bypass of "
            "someone."
        ),
    ),
    SourceSurface(
        path="app/services/customer_portal_contacts.py",
        family=EntryPointFamily.SERVICE,
        classification=SurfaceClassification.LEGACY_PARALLEL_WRITER,
        entity_types=(CohortEntityType.CUSTOMER_CONTACT,),
        owning_service="party.registry",
        registry_declared=False,
        production_runtime=True,
        note=(
            "Creates and edits legacy `subscriber_contacts` rows from the "
            "customer portal. party.registry owns the canonical contact-point "
            "lifecycle these rows are projected into."
        ),
    ),
    SourceSurface(
        path="app/services/customer_portal_notifications.py",
        family=EntryPointFamily.SERVICE,
        classification=SurfaceClassification.LEGACY_PARALLEL_WRITER,
        entity_types=(CohortEntityType.CUSTOMER_ACCOUNT,),
        owning_service="customer.accounts",
        registry_declared=True,
        production_runtime=True,
        note=(
            "Declared as communications.customer_read_state, and stores that "
            "read state inside the customer account's `metadata` blob. A "
            "declared owner writing another owner's row is still a parallel "
            "writer of that row."
        ),
    ),
    SourceSurface(
        path="app/services/network_subscriber_bridge.py",
        family=EntryPointFamily.SERVICE,
        classification=SurfaceClassification.LEGACY_PARALLEL_WRITER,
        entity_types=(CohortEntityType.CUSTOMER_ACCOUNT,),
        owning_service="customer.accounts",
        registry_declared=False,
        production_runtime=True,
        note="Constructs `Subscriber` rows from a provisioning bridge instead of the account owner.",
    ),
    SourceSurface(
        path="app/services/nin_verifications.py",
        family=EntryPointFamily.SERVICE,
        classification=SurfaceClassification.LEGACY_PARALLEL_WRITER,
        entity_types=(CohortEntityType.CUSTOMER_ACCOUNT,),
        owning_service="customer.accounts",
        registry_declared=False,
        production_runtime=True,
        note="Writes NIN verification outcome into the account `metadata` blob.",
    ),
    SourceSurface(
        path="app/services/team_inbox_commands.py",
        family=EntryPointFamily.SERVICE,
        classification=SurfaceClassification.LEGACY_PARALLEL_WRITER,
        entity_types=(CohortEntityType.PARTY_RELATIONSHIP,),
        owning_service="party.registry",
        registry_declared=True,
        production_runtime=True,
        note=(
            "Constructs `PartyRelationship` rows from the team inbox. "
            "party.registry is declared to own directional relationships and "
            "their effective-date contract."
        ),
    ),
    SourceSurface(
        path="app/services/web_admin_resellers.py",
        family=EntryPointFamily.WEB_PRESENTER,
        classification=SurfaceClassification.LEGACY_PARALLEL_WRITER,
        entity_types=(CohortEntityType.CUSTOMER_ACCOUNT,),
        owning_service="customer.accounts",
        registry_declared=True,
        production_runtime=True,
        note=(
            "Declared as ui.reseller_list_projection — a list projection — and "
            "assigns `subscribers.reseller_id`. Account ownership is not a "
            "list-rendering concern."
        ),
    ),
    SourceSurface(
        path="app/services/web_customer_details.py",
        family=EntryPointFamily.WEB_PRESENTER,
        classification=SurfaceClassification.LEGACY_PARALLEL_WRITER,
        entity_types=(CohortEntityType.CUSTOMER_ACCOUNT,),
        owning_service="customer.accounts",
        registry_declared=False,
        production_runtime=True,
        note="An undeclared detail presenter initialises the account `metadata` blob in place.",
    ),
    SourceSurface(
        path="app/services/web_system_import_wizard.py",
        family=EntryPointFamily.WEB_PRESENTER,
        classification=SurfaceClassification.LEGACY_PARALLEL_WRITER,
        entity_types=(CohortEntityType.CUSTOMER_ACCOUNT,),
        owning_service="customer.accounts",
        registry_declared=False,
        production_runtime=True,
        note="An admin import wizard constructs `Subscriber` rows directly.",
    ),
    SourceSurface(
        path="app/services/web_system_restore_tool.py",
        family=EntryPointFamily.WEB_PRESENTER,
        classification=SurfaceClassification.LEGACY_PARALLEL_WRITER,
        entity_types=(CohortEntityType.CUSTOMER_ACCOUNT,),
        owning_service="customer.accounts",
        registry_declared=False,
        production_runtime=True,
        note=(
            "A restore tool reactivates accounts and rewrites their `metadata` "
            "blob. Recovery tooling still has to reach the same owner, or the "
            "restored row can disagree with the owner that will be migrated."
        ),
    ),
    # ---- one-off and fixture writers -------------------------------------
    SourceSurface(
        path="scripts/migration/backfill_crm_subscriber_links.py",
        family=EntryPointFamily.CLI_SCRIPT,
        classification=SurfaceClassification.LEGACY_PARALLEL_WRITER,
        entity_types=(CohortEntityType.CUSTOMER_ACCOUNT,),
        owning_service="customer.accounts",
        registry_declared=False,
        production_runtime=True,
        note="A one-off backfill issuing raw DML against `subscribers`.",
    ),
    SourceSurface(
        path="scripts/migration/backfill_party_status.py",
        family=EntryPointFamily.CLI_SCRIPT,
        classification=SurfaceClassification.LEGACY_PARALLEL_WRITER,
        entity_types=(CohortEntityType.CUSTOMER_ACCOUNT, CohortEntityType.PARTY),
        owning_service="party.registry",
        registry_declared=False,
        production_runtime=True,
        note="A one-off backfill issuing raw DML against party and account rows.",
    ),
    SourceSurface(
        path="scripts/migration/import_crm_phase3.py",
        family=EntryPointFamily.CLI_SCRIPT,
        classification=SurfaceClassification.LEGACY_PARALLEL_WRITER,
        entity_types=(CohortEntityType.CUSTOMER_ACCOUNT,),
        owning_service="customer.accounts",
        registry_declared=False,
        production_runtime=True,
        note="A CRM import phase writing `subscribers` directly.",
    ),
    SourceSurface(
        path="scripts/one_off/backfill_crm_subscriber_ids.py",
        family=EntryPointFamily.CLI_SCRIPT,
        classification=SurfaceClassification.LEGACY_PARALLEL_WRITER,
        entity_types=(CohortEntityType.CUSTOMER_ACCOUNT,),
        owning_service="customer.accounts",
        registry_declared=False,
        production_runtime=True,
        note="A one-off script stamping CRM provenance ids onto account rows.",
    ),
    SourceSurface(
        path="scripts/migration/kernel_lineage_rehearsal_canaries.py",
        family=EntryPointFamily.CLI_SCRIPT,
        classification=SurfaceClassification.LEGACY_PARALLEL_WRITER,
        entity_types=(
            CohortEntityType.PARTY,
            CohortEntityType.PARTY_ROLE,
            CohortEntityType.CUSTOMER_ACCOUNT,
        ),
        owning_service=None,
        registry_declared=False,
        production_runtime=False,
        note=(
            "Builds synthetic canaries inside a disposable rehearsal database. "
            "Counted so the ratchet still sees it, marked non-production so it "
            "does not inflate the writer surface a cutover has to displace."
        ),
    ),
    SourceSurface(
        path="scripts/seed/seed_test_fixtures.py",
        family=EntryPointFamily.CLI_SCRIPT,
        classification=SurfaceClassification.LEGACY_PARALLEL_WRITER,
        entity_types=(CohortEntityType.CUSTOMER_ACCOUNT,),
        owning_service=None,
        registry_declared=False,
        production_runtime=False,
        note="A fixture seeder for test databases; same treatment as the rehearsal canaries.",
    ),
    # ---- applied migrations ----------------------------------------------
    SourceSurface(
        path="alembic/versions/045_contact_channels_without_name.py",
        family=EntryPointFamily.MIGRATION,
        classification=SurfaceClassification.LEGACY_PARALLEL_WRITER,
        entity_types=(CohortEntityType.CUSTOMER_CONTACT,),
        owning_service="alembic migration lineage",
        registry_declared=False,
        production_runtime=False,
        note=(
            "An applied data migration wrote contact rows outside the owning "
            "service. Alembic owns the deployed schema, so this ran under a "
            "real authority — but it wrote a fact a service owns, and it "
            "cannot run again against a migrated database, which is why it is "
            "counted and not counted as production."
        ),
    ),
    SourceSurface(
        path="alembic/versions/116_add_billing_accounts.py",
        family=EntryPointFamily.MIGRATION,
        classification=SurfaceClassification.LEGACY_PARALLEL_WRITER,
        entity_types=(CohortEntityType.CUSTOMER_ACCOUNT,),
        owning_service="alembic migration lineage",
        registry_declared=False,
        production_runtime=False,
        note=(
            "An applied data migration over `subscribers`; applied, so it "
            "cannot run again."
        ),
    ),
    SourceSurface(
        path="alembic/versions/208_map_karu_customers_to_bts.py",
        family=EntryPointFamily.MIGRATION,
        classification=SurfaceClassification.LEGACY_PARALLEL_WRITER,
        entity_types=(CohortEntityType.CUSTOMER_ACCOUNT,),
        owning_service="alembic migration lineage",
        registry_declared=False,
        production_runtime=False,
        note=(
            "An applied one-off remap of a named customer cohort; applied, so "
            "it cannot run again."
        ),
    ),
    SourceSurface(
        path="alembic/versions/267_brand_profiles.py",
        family=EntryPointFamily.MIGRATION,
        classification=SurfaceClassification.LEGACY_PARALLEL_WRITER,
        entity_types=(CohortEntityType.BRAND_PROFILE,),
        owning_service="alembic migration lineage",
        registry_declared=False,
        production_runtime=False,
        note=(
            "The migration that created and seeded `brand_profiles`, before "
            "`customer.branding` existed to own them."
        ),
    ),
    SourceSurface(
        path="alembic/versions/277_lifecycle_communications_sot.py",
        family=EntryPointFamily.MIGRATION,
        classification=SurfaceClassification.LEGACY_PARALLEL_WRITER,
        entity_types=(CohortEntityType.CUSTOMER_ACCOUNT,),
        owning_service="alembic migration lineage",
        registry_declared=False,
        production_runtime=False,
        note=(
            "An applied lifecycle-communications migration touching account "
            "rows; applied, so it cannot run again."
        ),
    ),
    SourceSurface(
        path="alembic/versions/383_replaceable_backoffice_boundary.py",
        family=EntryPointFamily.MIGRATION,
        classification=SurfaceClassification.LEGACY_PARALLEL_WRITER,
        entity_types=(CohortEntityType.ORGANIZATION,),
        owning_service="alembic migration lineage",
        registry_declared=False,
        production_runtime=False,
        note=(
            "An applied migration writing back-office reference columns on "
            "`organizations`; applied, so it cannot run again."
        ),
    ),
    # ---- adapters that reach the cohort but write none of it --------------
    SourceSurface(
        path="app/api/subscribers.py",
        family=EntryPointFamily.API_ROUTE,
        classification=SurfaceClassification.AUTHORIZED_ADAPTER,
        entity_types=(CohortEntityType.CUSTOMER_ACCOUNT,),
        owning_service="customer.accounts",
        registry_declared=False,
        production_runtime=True,
        note="The JSON account API. Imports the model for typing and delegates every mutation.",
    ),
    SourceSurface(
        path="app/api/me.py",
        family=EntryPointFamily.API_ROUTE,
        classification=SurfaceClassification.AUTHORIZED_ADAPTER,
        entity_types=(CohortEntityType.CUSTOMER_ACCOUNT,),
        owning_service="customer.accounts",
        registry_declared=False,
        production_runtime=True,
        note="The authenticated self-service API; reads the account and delegates writes.",
    ),
    SourceSurface(
        path="app/api/reseller.py",
        family=EntryPointFamily.API_ROUTE,
        classification=SurfaceClassification.AUTHORIZED_ADAPTER,
        entity_types=(CohortEntityType.CUSTOMER_ACCOUNT,),
        owning_service="customer.accounts",
        registry_declared=False,
        production_runtime=True,
        note="The reseller-scoped API; reaches accounts only through owning services.",
    ),
    SourceSurface(
        path="app/web/admin/customers.py",
        family=EntryPointFamily.WEB_ROUTE,
        classification=SurfaceClassification.AUTHORIZED_ADAPTER,
        entity_types=(CohortEntityType.CUSTOMER_ACCOUNT,),
        owning_service="customer.accounts",
        registry_declared=False,
        production_runtime=True,
        note="The admin customer portal; owns the transaction, decides nothing.",
    ),
    SourceSurface(
        path="app/web/admin/system.py",
        family=EntryPointFamily.WEB_ROUTE,
        classification=SurfaceClassification.AUTHORIZED_ADAPTER,
        entity_types=(CohortEntityType.CUSTOMER_ACCOUNT,),
        owning_service="customer.accounts",
        registry_declared=False,
        production_runtime=True,
        note="Admin system tooling; delegates account changes to presenters and services.",
    ),
    SourceSurface(
        path="app/web/admin/billing_payments.py",
        family=EntryPointFamily.WEB_ROUTE,
        classification=SurfaceClassification.READ_ONLY_CONSUMER,
        entity_types=(CohortEntityType.CUSTOMER_ACCOUNT,),
        owning_service=None,
        registry_declared=False,
        production_runtime=True,
        note="Reads account rows to render payment screens; writes billing rows only.",
    ),
    SourceSurface(
        path="app/web/customer/routes.py",
        family=EntryPointFamily.WEB_ROUTE,
        classification=SurfaceClassification.AUTHORIZED_ADAPTER,
        entity_types=(CohortEntityType.CUSTOMER_ACCOUNT,),
        owning_service="customer.accounts",
        registry_declared=False,
        production_runtime=True,
        note="The customer portal shell; delegates every account mutation.",
    ),
    SourceSurface(
        path="app/web/customer/location.py",
        family=EntryPointFamily.WEB_ROUTE,
        classification=SurfaceClassification.AUTHORIZED_ADAPTER,
        entity_types=(
            CohortEntityType.CUSTOMER_ACCOUNT,
            CohortEntityType.CUSTOMER_ADDRESS,
        ),
        owning_service="customer.location_capture",
        registry_declared=False,
        production_runtime=True,
        note="Portal location confirmation; calls location capture and owns only the commit.",
    ),
    SourceSurface(
        path="app/tasks/nin_tasks.py",
        family=EntryPointFamily.TASK_WORKER,
        classification=SurfaceClassification.AUTHORIZED_ADAPTER,
        entity_types=(CohortEntityType.CUSTOMER_ACCOUNT,),
        owning_service=None,
        registry_declared=False,
        production_runtime=True,
        note=(
            "The NIN verification worker. It persists verification rows, which "
            "are outside this cohort, and reaches account state only through "
            "`nin_verifications`."
        ),
    ),
)


class UnmappedAdjacentTable(BaseModel):
    """A cohort-adjacent Sub table deliberately outside the export contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    table: str
    reason: str

    @model_validator(mode="after")
    def _check(self) -> UnmappedAdjacentTable:
        if len(self.reason.split()) < 4:
            raise ValueError(
                f"{self.table} is excluded without a reason a reviewer could "
                "disagree with; an unexplained exclusion is indistinguishable "
                "from an oversight"
            )
        return self


#: Tables a reader might expect in "party and customer" and will not find.
#: Recorded so their absence is a decision on the record rather than a gap
#: someone discovers at cutover.
UNMAPPED_ADJACENT_TABLES: Final[tuple[UnmappedAdjacentTable, ...]] = (
    UnmappedAdjacentTable(
        table="resellers",
        reason=(
            "Reseller Management is Governance cohort 7; exporting the channel "
            "record with cohort 1 would move a later cohort's fact early."
        ),
    ),
    UnmappedAdjacentTable(
        table="reseller_users",
        reason="Same cohort-7 boundary as `resellers`, plus it carries login identity.",
    ),
    UnmappedAdjacentTable(
        table="customer_identity_index",
        reason=(
            "A rebuildable search index over identity, not a source fact; the "
            "target rebuilds its own from imported parties."
        ),
    ),
    UnmappedAdjacentTable(
        table="subscriber_channels",
        reason="Communication channel preference belongs with cohort 6 communications.",
    ),
    UnmappedAdjacentTable(
        table="subscriber_nin_verifications",
        reason=(
            "Regulatory identity-verification evidence with its own retention "
            "rules; it needs a disposition decision before any export carries it."
        ),
    ),
    UnmappedAdjacentTable(
        table="subscriber_custom_fields",
        reason=(
            "Operator-defined fields with no declared schema; exporting them "
            "typed would invent a contract nobody owns."
        ),
    ),
    UnmappedAdjacentTable(
        table="carried_source_identity_adjudications",
        reason=(
            "Reviewed adjudication decisions about carried identity, not "
            "identity itself; they describe the migration rather than migrate."
        ),
    ),
    UnmappedAdjacentTable(
        table="party_identity_backfill_receipts",
        reason=(
            "PII-free receipts proving a past backfill replayed idempotently; "
            "evidence about the source, not source state."
        ),
    ),
    UnmappedAdjacentTable(
        table="subscriber_contact_relationship_projections",
        reason=(
            "A reviewed projection from legacy contacts into party "
            "relationships; the target rebuilds it from the exported originals."
        ),
    ),
    UnmappedAdjacentTable(
        table="subscriber_contact_point_projections",
        reason="Same projection rationale as the relationship projections beside it.",
    ),
)


class NoCountedWriter(BaseModel):
    """A cohort table the census found no writer for, and how hard it looked."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    table: str
    #: Stated so the empty result is a bounded claim rather than a conclusion.
    searched: str


#: Cohort tables with zero counted writers. This is a *searched* result, and
#: it is not UNKNOWN: construction, tracked mutation, set-based DML and raw
#: statements were all looked for across every scanned family.
#:
#: The census's blind spots, stated once so they are not rediscovered as a
#: surprise: a mutation through a local this module could not bind to a cohort
#: model, a generic `setattr` helper, a SQLAlchemy event hook, and any writer
#: living outside the scanned roots. None of those would be caught, which is
#: why this reads "no counted writer" rather than "no writer".
TABLES_WITH_NO_COUNTED_WRITER: Final[tuple[NoCountedWriter, ...]] = (
    NoCountedWriter(
        table="organizations",
        searched=(
            "No `Organization(...)` construction, tracked mutation, set-based "
            "DML or raw statement anywhere under app/, scripts/ or the "
            "executable migration lineage, apart from one applied migration "
            "writing back-office reference columns."
        ),
    ),
    NoCountedWriter(
        table="organization_memberships",
        searched=(
            "No construction, tracked mutation, set-based DML or raw statement "
            "in any scanned family."
        ),
    ),
)


def surfaces_by_classification() -> dict[SurfaceClassification, tuple[str, ...]]:
    """Group inventoried paths by what they were classified as."""

    grouped: dict[SurfaceClassification, list[str]] = {
        classification: [] for classification in SurfaceClassification
    }
    for surface in COHORT_SURFACES:
        grouped[surface.classification].append(surface.path)
    return {
        classification: tuple(sorted(paths))
        for classification, paths in grouped.items()
    }


def production_writer_paths() -> tuple[str, ...]:
    """Paths that mutate cohort state in a production runtime.

    The number a cutover cares about. Fixture seeders and rehearsal canaries
    write too, and are excluded here rather than reclassified, so both counts
    stay available and neither has to be reconstructed later.
    """

    return tuple(
        sorted(
            surface.path
            for surface in COHORT_SURFACES
            if surface.writes and surface.production_runtime
        )
    )


def inventoried_paths() -> frozenset[str]:
    """Every path this inventory classifies."""

    return frozenset(surface.path for surface in COHORT_SURFACES)


__all__ = [
    "COHORT_SURFACES",
    "TABLES_WITH_NO_COUNTED_WRITER",
    "UNMAPPED_ADJACENT_TABLES",
    "EntryPointFamily",
    "NoCountedWriter",
    "SourceSurface",
    "SurfaceClassification",
    "UnmappedAdjacentTable",
    "inventoried_paths",
    "production_writer_paths",
    "surfaces_by_classification",
]
