"""Privacy-safe comparison digests, and the bounded verdicts a comparison may reach.

A shadow importer has to answer "did cohort-isp-01 arrive intact?" without
holding Sub credentials and without a second copy of Sub's data. A digest
artifact is how: identities and hashes, never field values. It can be handed
to whoever needs to run the comparison, kept as evidence, and attached to a
control record, because there is nothing in it to leak.

## What is in a digest, and what deliberately is not

Present: the cohort code, both version numbers, the tenant, the source
revision, the cursor, a count per declared entity type, every source
identity in sorted order, one canonical digest per row, and one aggregate
digest over all of it.

Absent: every field value. A digest is a one-way function over a whole record
including its UUID primary key, so it cannot be walked back to a name, an
address or a phone number the way a hash of one short field could.

## `generated_at` sits outside the digest

Deliberately, and it is the one field that has to. Two exports of unchanged
data taken a minute apart describe the same facts; if the moment of capture
entered the aggregate they would compare as different, and every comparison
would report drift that is really a clock. The capture instant is still
recorded — it is evidence — it just is not part of what is compared.

The *source revision* is inside the digest, because that is not a clock: two
snapshots at different schema revisions genuinely are different snapshots and
should not silently reconcile.

## Six verdicts, and no seventh

`MismatchCategory` is closed. A comparison may say a row is missing from the
target, unexpected in the target, or divergent; that the versions do not
permit comparison at all; or that one side did not know enough to be compared.
It may not invent a category that sounds like a decision — "acceptable",
"expected difference", "ignore" — because those are adjudications, and an
adjudication belongs to a human reading a control record, not to a comparator.

The two `*_unknown` verdicts carry the weight here. When either side reports a
partial drain, the honest answer is that the comparison could not conclude for
that entity type. Treating a partial page as a complete one would report
missing rows that were merely unread, and the natural response to a wave of
false missing-row reports is to stop believing the comparison.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict, model_validator

from app.migration_source.canonical import CanonicalField, canonical_digest
from app.migration_source.cohort import CohortEntityType
from app.migration_source.programme import COHORT_ID
from app.migration_source.snapshot import (
    SCHEMA_VERSION,
    Completeness,
    ContractVersion,
    ExportCursor,
    SnapshotPage,
    SourceRevision,
    TenantScope,
)

_SHA256_HEX_WIDTH: Final[int] = 64


class MismatchCategory(StrEnum):
    """The closed set of verdicts a cohort comparison may reach."""

    #: Present in the source digest, absent from the target's.
    MISSING_FROM_TARGET = "missing-from-target"
    #: Present in the target digest, absent from the source's.
    UNEXPECTED_IN_TARGET = "unexpected-in-target"
    #: Same identity on both sides, different canonical digest.
    DIVERGENT = "divergent"
    #: The two digests do not share a contract or schema version.
    UNSUPPORTED_VERSION = "unsupported-version"
    #: The source did not drain this entity type; it cannot be compared.
    SOURCE_UNKNOWN = "source-unknown"
    #: The target did not drain this entity type; it cannot be compared.
    TARGET_UNKNOWN = "target-unknown"


class EntityDigest(BaseModel):
    """One row, reduced to its stable identity and its canonical digest."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: `<entity_type>:<uuid>` — the same string the snapshot carries.
    identity: str
    digest: str

    @model_validator(mode="after")
    def _check(self) -> EntityDigest:
        if len(self.digest) != _SHA256_HEX_WIDTH:
            raise ValueError("an entity digest must be a sha256 hex digest")
        if ":" not in self.identity:
            raise ValueError(
                "an identity must be '<entity_type>:<uuid>'; a bare uuid cannot "
                "be correlated across entity types"
            )
        return self


class EntityTypeDigest(BaseModel):
    """Every digested row of one entity type, plus what was not read."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    entity_type: CohortEntityType
    count: int
    entries: tuple[EntityDigest, ...]
    completeness: Completeness
    #: Where a partial drain would resume. Present only when partial, so a
    #: reader cannot mistake a finished type for one with more to come.
    resume_from: ExportCursor | None
    aggregate: str

    @model_validator(mode="after")
    def _check(self) -> EntityTypeDigest:
        identities = [entry.identity for entry in self.entries]
        if identities != sorted(identities):
            raise ValueError(
                "entity digests must be sorted by identity; an unsorted list "
                "would make the aggregate depend on row order"
            )
        if len(set(identities)) != len(identities):
            raise ValueError("an entity type digest repeats an identity")
        if self.count != len(self.entries):
            raise ValueError(
                f"{self.entity_type} claims {self.count} rows and carries "
                f"{len(self.entries)}"
            )
        prefix = f"{self.entity_type.value}:"
        foreign = sorted(
            identity for identity in identities if not identity.startswith(prefix)
        )
        if foreign:
            raise ValueError(
                f"{self.entity_type} carries identities of another type: "
                + ", ".join(foreign)
            )
        if (self.completeness is Completeness.PARTIAL) != (
            self.resume_from is not None
        ):
            raise ValueError(
                "a partial entity type must say where to resume, and a "
                "complete one must not offer a continuation"
            )
        if len(self.aggregate) != _SHA256_HEX_WIDTH:
            raise ValueError("an aggregate must be a sha256 hex digest")
        return self


class CohortDigest(BaseModel):
    """The comparison artifact for one cohort at one source revision."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    cohort_code: str = COHORT_ID
    contract_version: ContractVersion
    schema_version: int = SCHEMA_VERSION
    tenant: TenantScope
    source_revision: SourceRevision
    entity_types: tuple[EntityTypeDigest, ...]
    total_count: int
    aggregate: str
    completeness: Completeness
    #: Evidence, not comparison input. See this module's docstring for why it
    #: has to sit outside the aggregate.
    generated_at: datetime

    @model_validator(mode="after")
    def _check(self) -> CohortDigest:
        declared = [item.entity_type for item in self.entity_types]
        if declared != sorted(declared, key=lambda value: value.value):
            raise ValueError("entity types must be sorted for a stable aggregate")
        if len(set(declared)) != len(declared):
            raise ValueError("a cohort digest repeats an entity type")
        if self.total_count != sum(item.count for item in self.entity_types):
            raise ValueError("the cohort total disagrees with its per-type counts")
        expected = (
            Completeness.COMPLETE
            if all(
                item.completeness is Completeness.COMPLETE for item in self.entity_types
            )
            else Completeness.PARTIAL
        )
        if self.completeness is not expected:
            raise ValueError(
                "cohort completeness must follow from its entity types; a "
                "cohort cannot be complete while one of its types is partial"
            )
        if len(self.aggregate) != _SHA256_HEX_WIDTH:
            raise ValueError("an aggregate must be a sha256 hex digest")
        return self

    def entity_type_digest(
        self, entity_type: CohortEntityType
    ) -> EntityTypeDigest | None:
        """Return one entity type's digest, or `None` if it was not exported."""

        for item in self.entity_types:
            if item.entity_type is entity_type:
                return item
        return None


def digest_page(page: SnapshotPage) -> tuple[EntityDigest, ...]:
    """Digest one snapshot page's records, sorted by identity."""

    return tuple(
        sorted(
            (
                EntityDigest(identity=record.identity.value, digest=record.digest())
                for record in page.records
            ),
            key=lambda entry: entry.identity,
        )
    )


def entity_type_aggregate(
    *,
    entity_type: CohortEntityType,
    entries: tuple[EntityDigest, ...],
    contract_version: ContractVersion,
    schema_version: int,
) -> str:
    """Aggregate one entity type's row digests into one comparable value."""

    fields: dict[str, CanonicalField] = {
        "contract_version": contract_version.value,
        "schema_version": schema_version,
        "entity_type": entity_type.value,
        "count": len(entries),
        "entries": tuple(
            f"{entry.identity}={entry.digest}"
            for entry in sorted(entries, key=lambda item: item.identity)
        ),
    }
    return canonical_digest(fields)


def build_entity_type_digest(
    *,
    entity_type: CohortEntityType,
    entries: tuple[EntityDigest, ...],
    completeness: Completeness,
    resume_from: ExportCursor | None,
    contract_version: ContractVersion,
    schema_version: int = SCHEMA_VERSION,
) -> EntityTypeDigest:
    """Assemble one entity type's digest with its aggregate already computed."""

    ordered = tuple(sorted(entries, key=lambda entry: entry.identity))
    return EntityTypeDigest(
        entity_type=entity_type,
        count=len(ordered),
        entries=ordered,
        completeness=completeness,
        resume_from=resume_from,
        aggregate=entity_type_aggregate(
            entity_type=entity_type,
            entries=ordered,
            contract_version=contract_version,
            schema_version=schema_version,
        ),
    )


def build_cohort_digest(
    *,
    tenant: TenantScope,
    source_revision: SourceRevision,
    entity_types: tuple[EntityTypeDigest, ...],
    contract_version: ContractVersion,
    generated_at: datetime,
    schema_version: int = SCHEMA_VERSION,
) -> CohortDigest:
    """Assemble the cohort digest, computing the aggregate over the sorted parts."""

    ordered = tuple(sorted(entity_types, key=lambda item: item.entity_type.value))
    fields: dict[str, CanonicalField] = {
        "cohort_code": COHORT_ID,
        "contract_version": contract_version.value,
        "schema_version": schema_version,
        "tenant_id": str(tenant.tenant_id),
        "entity_types": tuple(
            f"{item.entity_type.value}={item.count}={item.aggregate}"
            f"={item.completeness.value}"
            for item in ordered
        ),
    }
    fields.update(source_revision.canonical_fields())
    total = sum(item.count for item in ordered)
    completeness = (
        Completeness.COMPLETE
        if all(item.completeness is Completeness.COMPLETE for item in ordered)
        else Completeness.PARTIAL
    )
    return CohortDigest(
        contract_version=contract_version,
        schema_version=schema_version,
        tenant=tenant,
        source_revision=source_revision,
        entity_types=ordered,
        total_count=total,
        aggregate=canonical_digest(fields),
        completeness=completeness,
        generated_at=generated_at,
    )


class Mismatch(BaseModel):
    """One bounded difference between a source digest and a target digest."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    category: MismatchCategory
    entity_type: CohortEntityType
    identity: str | None
    #: Enough for a human to act on, and never a field value: an adjudicator
    #: reading a mismatch list must not need Sub credentials, and must not be
    #: handed customer data by a comparison report.
    detail: str

    @model_validator(mode="after")
    def _check(self) -> Mismatch:
        if not self.detail.strip():
            raise ValueError("a mismatch must say what it observed")
        needs_identity = {
            MismatchCategory.MISSING_FROM_TARGET,
            MismatchCategory.UNEXPECTED_IN_TARGET,
            MismatchCategory.DIVERGENT,
        }
        if self.category in needs_identity and not self.identity:
            raise ValueError(f"{self.category} must name the row it is about")
        return self


def compare(source: CohortDigest, target: CohortDigest) -> tuple[Mismatch, ...]:
    """Compare two cohort digests. Pure: no database, no network, no state.

    This runs wherever the comparison is being run — most likely in the
    destination, against a digest Sub published. It reaches no conclusion
    beyond the six bounded categories, and in particular it never decides that
    a difference is acceptable.
    """

    if (
        source.contract_version is not target.contract_version
        or source.schema_version != target.schema_version
    ):
        return tuple(
            Mismatch(
                category=MismatchCategory.UNSUPPORTED_VERSION,
                entity_type=entity_type,
                identity=None,
                detail=(
                    f"source is contract {source.contract_version.value} schema "
                    f"{source.schema_version}; target is contract "
                    f"{target.contract_version.value} schema "
                    f"{target.schema_version}. Digests across versions are not "
                    "comparable and a difference would not mean drift."
                ),
            )
            for entity_type in sorted(CohortEntityType, key=lambda value: value.value)
        )

    mismatches: list[Mismatch] = []
    for entity_type in sorted(CohortEntityType, key=lambda value: value.value):
        source_side = source.entity_type_digest(entity_type)
        target_side = target.entity_type_digest(entity_type)

        if source_side is None or source_side.completeness is Completeness.PARTIAL:
            mismatches.append(
                Mismatch(
                    category=MismatchCategory.SOURCE_UNKNOWN,
                    entity_type=entity_type,
                    identity=None,
                    detail=(
                        "the source digest does not carry a complete drain of "
                        "this entity type, so no conclusion about the target "
                        "follows from it"
                    ),
                )
            )
            continue
        if target_side is None or target_side.completeness is Completeness.PARTIAL:
            mismatches.append(
                Mismatch(
                    category=MismatchCategory.TARGET_UNKNOWN,
                    entity_type=entity_type,
                    identity=None,
                    detail=(
                        "the target digest does not carry a complete drain of "
                        "this entity type; treating it as complete would report "
                        "unread rows as missing"
                    ),
                )
            )
            continue

        source_rows = {entry.identity: entry.digest for entry in source_side.entries}
        target_rows = {entry.identity: entry.digest for entry in target_side.entries}

        for identity in sorted(source_rows.keys() - target_rows.keys()):
            mismatches.append(
                Mismatch(
                    category=MismatchCategory.MISSING_FROM_TARGET,
                    entity_type=entity_type,
                    identity=identity,
                    detail="present in the source digest, absent from the target",
                )
            )
        for identity in sorted(target_rows.keys() - source_rows.keys()):
            mismatches.append(
                Mismatch(
                    category=MismatchCategory.UNEXPECTED_IN_TARGET,
                    entity_type=entity_type,
                    identity=identity,
                    detail="present in the target digest, absent from the source",
                )
            )
        for identity in sorted(source_rows.keys() & target_rows.keys()):
            if source_rows[identity] != target_rows[identity]:
                mismatches.append(
                    Mismatch(
                        category=MismatchCategory.DIVERGENT,
                        entity_type=entity_type,
                        identity=identity,
                        detail=(
                            "canonical digests differ; the row exists on both "
                            "sides and its content does not agree"
                        ),
                    )
                )
    return tuple(mismatches)


__all__ = [
    "CohortDigest",
    "EntityDigest",
    "EntityTypeDigest",
    "Mismatch",
    "MismatchCategory",
    "build_cohort_digest",
    "build_entity_type_digest",
    "compare",
    "digest_page",
    "entity_type_aggregate",
]
