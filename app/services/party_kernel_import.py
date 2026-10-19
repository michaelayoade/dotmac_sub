"""Exact rollback-only Party roots and Sub context participant.

Not a runtime adoption API: fixtures carry explicit reviewed no-adoption facts.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError

from app.services.operator_tenant import OPERATOR_TENANT_ID


class PartyImportRefused(RuntimeError):
    """Typed disposition, source freshness or destination contract differs."""

    code = "party.kernel_import.refused"


class NoAdoption(StrEnum):
    EMAIL = "no_email_adoption"
    SUBTYPE = "no_subtype_adoption"
    CUSTOM_FIELDS = "empty_custom_fields_no_adoption"


@dataclass(frozen=True)
class PartySourceFingerprint:
    party_id: UUID
    row_sha256: str


@dataclass(frozen=True)
class PartyFixtureDisposition:
    source: PartySourceFingerprint
    tenant_id: UUID
    is_active: bool
    fixture_provenance: str
    email: NoAdoption
    subtype: NoAdoption
    custom_fields: NoAdoption


@dataclass(frozen=True)
class PartyImportCohort:
    tenant_id: UUID
    dispositions: tuple[PartyFixtureDisposition, ...]


@dataclass(frozen=True)
class PartyImportCount:
    count: int


def observe_party_source(
    connection: Connection, *, escrow: bool = False
) -> tuple[PartySourceFingerprint, ...]:
    """Fingerprint whole rows including exact JSON text, never emit payloads."""
    source = "sub_kernel_entry_fixture_escrow.parties" if escrow else "public.parties"
    return tuple(
        PartySourceFingerprint(row[0], str(row[1]))
        for row in connection.execute(
            text(
                f"SELECT id,encode(sha256(convert_to(row_to_json(p)::text,'UTF8')),'hex') FROM {source} p ORDER BY id"  # noqa: S608 - fixed source names
            )
        )
    )


def validate_party_cohort(
    connection: Connection, *, cohort: PartyImportCohort, escrow: bool = False
) -> PartyImportCount:
    """Exact complete-cohort matching; never derive active from lifecycle."""
    if (
        not isinstance(cohort, PartyImportCohort)
        or cohort.tenant_id != OPERATOR_TENANT_ID
    ):
        raise PartyImportRefused("One exact operator tenant is required")
    observed = observe_party_source(connection, escrow=escrow)
    dispositions = cohort.dispositions
    if len({d.source.party_id for d in dispositions}) != len(dispositions):
        raise PartyImportRefused("Duplicate Party dispositions")
    if {d.source for d in dispositions} != set(observed):
        raise PartyImportRefused("Missing, stale or unused Party dispositions")
    for d in dispositions:
        if (
            d.tenant_id != cohort.tenant_id
            or type(d.is_active) is not bool
            or not d.fixture_provenance.strip()
            or d.email is not NoAdoption.EMAIL
            or d.subtype is not NoAdoption.SUBTYPE
            or d.custom_fields is not NoAdoption.CUSTOM_FIELDS
        ):
            raise PartyImportRefused("Conflicting or incomplete Party disposition")
    return PartyImportCount(len(observed))


def stage_party_roots(
    connection: Connection, *, cohort: PartyImportCohort
) -> PartyImportCount:
    count = validate_party_cohort(connection, cohort=cohort, escrow=True)
    if connection.scalar(text("SELECT count(*) FROM public.parties")):
        raise PartyImportRefused("Canonical Party destination must be empty")
    for d in cohort.dispositions:
        connection.execute(
            text(
                "INSERT INTO public.parties "
                "(id,tenant_id,party_type,display_name,email,is_active,custom_fields,created_at,updated_at) "
                "SELECT id,:tenant,party_type,display_name,NULL,:active,'{}',created_at,updated_at "
                "FROM sub_kernel_entry_fixture_escrow.parties WHERE id=:party"
            ),
            {
                "tenant": cohort.tenant_id,
                "active": d.is_active,
                "party": d.source.party_id,
            },
        )
    return count


def stage_party_context(
    connection: Connection, *, cohort: PartyImportCohort
) -> PartyImportCount:
    count = validate_party_cohort(connection, cohort=cohort, escrow=True)
    if connection.scalar(text("SELECT count(*) FROM public.sub_party_context")):
        raise PartyImportRefused("Sub Party context destination must be empty")
    # Database-side JSON -> JSON; preserves SQL NULL versus JSON null and raw text.
    connection.execute(
        text(
            "INSERT INTO public.sub_party_context "
            "(tenant_id,party_id,status,data_classification,merged_into_party_id,merge_reason,metadata) "
            "SELECT :tenant,id,status,data_classification,merged_into_party_id,merge_reason,metadata "
            "FROM sub_kernel_entry_fixture_escrow.parties"
        ),
        {"tenant": cohort.tenant_id},
    )
    return count


def verify_party_destinations(
    connection: Connection, *, cohort: PartyImportCohort, context_required: bool = True
) -> tuple[PartyImportCount, PartyImportCount]:
    """Verify whole destination keysets and exact fields before any outcome.

    RLS must never hide extra destination rows. row_security=off makes PostgreSQL
    refuse verification when the supplied owner cannot inspect forced-RLS rows.
    Only Boolean mismatches/counts cross this boundary; no payload is returned.
    """
    validate_party_cohort(connection, cohort=cohort, escrow=True)
    previous = connection.scalar(text("SELECT current_setting('row_security')"))
    connection.execute(text("SELECT set_config('row_security','off',true)"))
    parameters = {
        "tenant": cohort.tenant_id,
        "ids": [d.source.party_id for d in cohort.dispositions],
        "active": [d.is_active for d in cohort.dispositions],
    }
    try:
        roots_differ = connection.scalar(
            text("""
            WITH disposition AS (
                SELECT * FROM unnest(CAST(:ids AS uuid[]),CAST(:active AS boolean[]))
                    AS d(id,is_active)
            )
            SELECT EXISTS (
                SELECT 1 FROM sub_kernel_entry_fixture_escrow.parties s
                FULL JOIN public.parties p ON p.id=s.id
                LEFT JOIN disposition d ON d.id=s.id
                WHERE s.id IS NULL OR p.id IS NULL OR d.id IS NULL
                   OR (p.tenant_id,p.party_type,p.display_name,p.created_at,p.updated_at,
                       p.is_active,p.email,p.custom_fields::text)
                      IS DISTINCT FROM
                      (:tenant,s.party_type,s.display_name,s.created_at,s.updated_at,
                       d.is_active,NULL::varchar,'{}'::text)
            ) OR EXISTS (SELECT 1 FROM public.party_persons)
              OR EXISTS (SELECT 1 FROM public.party_organizations)
        """),
            parameters,
        )
        contexts_differ = context_required and connection.scalar(
            text("""
            SELECT EXISTS (
                SELECT 1 FROM sub_kernel_entry_fixture_escrow.parties s
                FULL JOIN public.sub_party_context c
                    ON c.party_id=s.id AND c.tenant_id=:tenant
                WHERE s.id IS NULL OR c.party_id IS NULL
                   OR (c.status,c.data_classification,c.merged_into_party_id,c.merge_reason,
                       c.metadata::text,c.metadata IS NULL)
                      IS DISTINCT FROM
                      (s.status,s.data_classification,s.merged_into_party_id,s.merge_reason,
                       s.metadata::text,s.metadata IS NULL)
            )
        """),
            parameters,
        )
        roots = int(connection.scalar(text("SELECT count(*) FROM public.parties")))
        contexts = (
            int(
                connection.scalar(text("SELECT count(*) FROM public.sub_party_context"))
            )
            if context_required
            else 0
        )
    except DBAPIError:
        # The transaction owner rolls back an aborted/inaccessible verification.
        raise PartyImportRefused(
            "Party destination verification could not inspect all rows"
        ) from None
    connection.execute(
        text("SELECT set_config('row_security',:previous,true)"), {"previous": previous}
    )
    if roots_differ or contexts_differ:
        raise PartyImportRefused("Party destination verification failed")
    return PartyImportCount(roots), PartyImportCount(contexts)
