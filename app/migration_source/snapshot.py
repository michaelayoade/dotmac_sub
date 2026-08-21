"""The versioned, read-only export contract for cohort-isp-01 source facts.

A snapshot is Sub saying *what it holds*. It is deliberately incapable of
saying what the destination should conclude: there is no target status
vocabulary here, no disposition field, no "should_migrate" flag. A source that
ships decisions has already made the destination's resolver redundant, and the
first time the two disagree nobody can tell which one was authoritative.

## What a page guarantees

- **One tenant, always.** `TenantScope.tenant_id` is required and non-optional.
  Sub is a single-operator deployment, so the value is the operator tenant and
  is resolved from `tenancy.operator_tenant` — never from a caller argument.
- **A stable opaque identity per row.** `SourceIdentity` is
  `"<entity_type>:<uuid>"`. It appears in both the snapshot and the comparison
  digest, so a destination can correlate the two without holding Sub rows.
- **Deterministic order.** Records sort by primary key, and pagination is
  keyset — `after_source_id` — not offset. An offset page silently reshuffles
  when a row is inserted mid-drain; a keyset page cannot.
- **A source revision.** Schema revision, application version, capture instant
  and, on PostgreSQL, the transaction id that fixed the read. That is the
  watermark a later delta capture starts from.
- **A bounded page.** `MAX_PAGE_SIZE` is enforced at construction, so a caller
  cannot ask for the whole table and call it a page.

## Minimisation

The contract carries the facts a destination needs to reconstruct a party, a
customer account and a brand, and nothing beyond them. Four categories are
excluded by construction rather than by habit:

- **Credentials and secrets** — not present in any cohort table. A test
  asserts no exported field name matches the credential vocabulary, so the
  absence stays a property rather than a coincidence.
- **Free-text operator notes** (`subscribers.notes`, `organizations.notes`,
  `subscriber_contacts.notes`) — unbounded prose, no declared shape, and
  routinely holding things nobody meant to migrate.
- **Regulatory and sensitive personal values** — `subscribers.nin` and
  `date_of_birth` cross as presence flags only. Neither is needed for service
  continuity, and a national identity number is not something a readiness
  export should be able to leak.
- **Unclassified JSON blobs** — every `metadata` column, `access_scope` and
  `legal_address`. Seven different modules write `subscribers.metadata` and no
  owner declares its shape, so it crosses as `OpaqueBlob`: a sorted key
  inventory and a digest. Reading structure into that column would invent a
  contract nobody owns and carry seven features' private conventions into the
  destination as though they were data.

## Derived fields are labelled, not filtered

`Subscriber.status`, its lifecycle-override columns and `mrr_total` are
projections of decisions owned elsewhere. They are exported — a reconciliation
that cannot see them cannot explain a difference — but each record type
declares `DERIVED_FIELDS`, so a destination is told mechanically which values
are downstream of something else and must be recomputed rather than trusted.
Labelling provenance is not deciding: it is the last honest thing a source can
say about a number it did not own.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, ClassVar, Final, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.migration_source.canonical import (
    CanonicalField,
    canonical_coordinate,
    canonical_datetime,
    canonical_decimal,
    canonical_digest,
    canonical_string,
    canonical_strings,
    canonical_uuid,
    digest_of_text,
)
from app.migration_source.cohort import CohortEntityType
from app.migration_source.programme import (
    ACCEPTED_REVISION,
    COHORT_ID,
    SOURCE_ASSEMBLY_ID,
)


class ContractVersion(StrEnum):
    """Versions of the export contract this build can produce."""

    V1 = "1"


#: Fail closed: a request naming anything else is refused, never answered with
#: a best-effort page. A destination that receives v1 bytes labelled v2 has no
#: way to know its parser is wrong.
SUPPORTED_CONTRACT_VERSIONS: Final[frozenset[ContractVersion]] = frozenset(
    {ContractVersion.V1}
)

#: The per-record schema version, separate from the contract version. The
#: contract governs paging, identity and envelope; the schema governs record
#: fields. They move independently and a digest carries both.
SCHEMA_VERSION: Final[int] = 1

#: A page is bounded so "give me everything" is not expressible.
MAX_PAGE_SIZE: Final[int] = 500
DEFAULT_PAGE_SIZE: Final[int] = 200


class UnsupportedContractVersionError(ValueError):
    """A caller asked for a contract version this build cannot produce."""


def require_contract_version(value: str) -> ContractVersion:
    """Resolve a requested contract version, or refuse.

    Refusal rather than negotiation: silently answering a v2 request with v1
    bytes puts the mismatch inside the destination's parser, where it surfaces
    as corrupt data rather than as a version error.
    """

    try:
        version = ContractVersion(value)
    except ValueError as exc:
        raise UnsupportedContractVersionError(
            f"contract version {value!r} is not supported; this build produces "
            + ", ".join(sorted(member.value for member in SUPPORTED_CONTRACT_VERSIONS))
        ) from exc
    if version not in SUPPORTED_CONTRACT_VERSIONS:  # pragma: no cover - defensive
        raise UnsupportedContractVersionError(
            f"contract version {version.value!r} is known but not enabled"
        )
    return version


class Completeness(StrEnum):
    """Whether a page is the end of its entity type at this revision."""

    #: No further rows exist for this entity type at this source revision.
    COMPLETE = "complete"
    #: More rows remain; `next_cursor` continues from here.
    PARTIAL = "partial"


class TenantScope(BaseModel):
    """The one tenant a snapshot describes. Never optional, never a wildcard."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: UUID


class SourceIdentity(BaseModel):
    """A stable opaque identity for one exported row."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    entity_type: CohortEntityType
    source_id: UUID

    @property
    def value(self) -> str:
        """`<entity_type>:<uuid>` — what both artifacts correlate on."""

        return f"{self.entity_type.value}:{self.source_id}"

    def __str__(self) -> str:
        return self.value


class SourceRevision(BaseModel):
    """What the snapshot was taken from, precisely enough to retake it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The Governance assembly this export speaks for.
    source_assembly_id: str = SOURCE_ASSEMBLY_ID
    #: The accepted programme revision the cohort definition comes from.
    governance_revision: str = ACCEPTED_REVISION
    #: Alembic head at capture. A destination replaying an old snapshot
    #: against a newer schema needs to know which one it holds.
    schema_revision: str
    application_version: str
    #: PostgreSQL transaction id fixing the read, when the bind provides one.
    #: `None` on binds without an equivalent, rather than a fabricated value.
    snapshot_transaction_id: str | None
    captured_at: datetime

    def canonical_fields(self) -> dict[str, CanonicalField]:
        """The revision's contribution to a digest, minus wall-clock time."""

        return {
            "source_assembly_id": canonical_string(self.source_assembly_id),
            "governance_revision": canonical_string(self.governance_revision),
            "schema_revision": canonical_string(self.schema_revision),
            "application_version": canonical_string(self.application_version),
            "snapshot_transaction_id": canonical_string(self.snapshot_transaction_id),
        }


class ExportCursor(BaseModel):
    """Where a drain resumes. Keyset, never offset.

    Named a cursor rather than a checkpoint on purpose. In this repository a
    *checkpoint* already means a durable position in an **external** feed —
    eleven modules hold one, and the fleet's connector inventory counts the
    word that way. This is an ordinary pagination cursor over Sub's own
    tables: it survives nothing, coordinates with nobody, and means only
    "resume after this primary key". Borrowing the other word would have
    collided with an established meaning in the same codebase.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    entity_type: CohortEntityType
    #: Exclusive lower bound on the primary key. `None` starts at the
    #: beginning, which is a different statement from "the first page".
    after_source_id: UUID | None
    page_size: int = Field(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE)

    def advanced_to(self, source_id: UUID) -> ExportCursor:
        """Return the cursor continuing after `source_id`."""

        return ExportCursor(
            entity_type=self.entity_type,
            after_source_id=source_id,
            page_size=self.page_size,
        )


class OpaqueBlob(BaseModel):
    """An unclassified JSON column, reduced to what can honestly be compared.

    The keys are exported because a destination has to be able to *see* what
    conventions exist before anyone can decide who owns them. The values are
    not, because no owner declares their shape and a typed export would be
    inventing one.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    keys: tuple[str, ...]
    digest: str

    @model_validator(mode="after")
    def _check(self) -> OpaqueBlob:
        if list(self.keys) != sorted(self.keys):
            raise ValueError("opaque blob keys must be sorted")
        if len(self.digest) != 64:
            raise ValueError("opaque blob digest must be a sha256 hex digest")
        return self


class ExternalCorrelation(BaseModel):
    """An identifier belonging to another application, carried opaquely.

    Sub holds Splynx and CRM identifiers on its own rows. They cross as
    correlation references — a system name and an untyped string — and never
    as something the destination should resolve or trust. Nothing here implies
    the foreign system is reachable, or that its identifier still means
    anything.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    system: str
    reference: str

    def __str__(self) -> str:
        return f"{self.system}:{self.reference}"


def _stable_json(value: object) -> str:
    """Deterministically render arbitrary JSON for digesting only.

    Recursion is bounded by the stored document. This never produces exported
    data — only a digest — but the destination must be able to reproduce it
    from the same document, so the rendering is spelled out rather than
    delegated to a library default that can change between releases.
    """

    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return canonical_decimal(Decimal(repr(value))) or "0"
    if isinstance(value, str):
        normalised = canonical_string(value) or ""
        return '"' + normalised.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if isinstance(value, list):
        return "[" + ",".join(_stable_json(item) for item in value) + "]"
    if isinstance(value, dict):
        items = sorted((str(key), item) for key, item in value.items())
        return (
            "{"
            + ",".join(
                f"{_stable_json(key)}:{_stable_json(item)}" for key, item in items
            )
            + "}"
        )
    raise TypeError(
        f"{type(value).__name__} is not JSON this contract can digest; a "
        "cohort blob column holds only JSON"
    )


def opaque_blob(value: object) -> OpaqueBlob | None:
    """Reduce a stored JSON column to a key inventory and a digest."""

    if value is None:
        return None
    keys: tuple[str, ...] = ()
    if isinstance(value, dict):
        keys = canonical_strings(tuple(str(key) for key in value))
    return OpaqueBlob(keys=keys, digest=digest_of_text(_stable_json(value)))


def _correlations(values: tuple[ExternalCorrelation, ...]) -> tuple[str, ...]:
    return canonical_strings(tuple(str(value) for value in values))


def _blob(value: OpaqueBlob | None) -> tuple[CanonicalField, CanonicalField]:
    return (None, None) if value is None else (value.keys, value.digest)


class _CohortRecord(BaseModel):
    """Shared envelope for every exported cohort row."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Fields whose value is a projection of a decision owned elsewhere. A
    #: destination recomputes these; it does not adopt them.
    DERIVED_FIELDS: ClassVar[frozenset[str]] = frozenset()

    #: Declared here so the envelope can use it, narrowed to a single
    #: `Literal` by every subclass. That narrowing is what makes the union
    #: below discriminated, and what stops a record from being constructed
    #: with somebody else's entity type.
    entity_type: CohortEntityType
    source_id: UUID
    #: Nullable because several legacy cohort tables permit a null timestamp.
    #: Stated rather than defaulted: inventing a creation instant would make
    #: an unknown look like a fact.
    created_at: datetime | None
    updated_at: datetime | None

    @property
    def identity(self) -> SourceIdentity:
        return SourceIdentity(
            entity_type=CohortEntityType(self.entity_type), source_id=self.source_id
        )

    def _entity_fields(self) -> dict[str, CanonicalField]:  # pragma: no cover
        raise NotImplementedError

    def canonical_fields(self) -> dict[str, CanonicalField]:
        """The full canonical payload, envelope first."""

        fields: dict[str, CanonicalField] = {
            "contract_version": ContractVersion.V1.value,
            "schema_version": SCHEMA_VERSION,
            "entity_type": CohortEntityType(self.entity_type).value,
            "source_id": canonical_uuid(self.source_id),
            "created_at": canonical_datetime(self.created_at),
            "updated_at": canonical_datetime(self.updated_at),
        }
        overlap = set(fields) & set(self._entity_fields())
        if overlap:  # pragma: no cover - guarded by the field-name test
            raise ValueError(
                f"{type(self).__name__} shadows envelope fields "
                + ", ".join(sorted(overlap))
            )
        fields.update(self._entity_fields())
        return fields

    def digest(self) -> str:
        """SHA-256 over this record's canonical form."""

        return canonical_digest(self.canonical_fields())


class PartyRecord(_CohortRecord):
    """One native identity for one real-world person or organization."""

    entity_type: Literal[CohortEntityType.PARTY] = CohortEntityType.PARTY

    party_type: str
    display_name: str
    status: str
    data_classification: str
    merged_into_party_id: UUID | None
    merge_reason: str | None
    metadata_blob: OpaqueBlob | None

    def _entity_fields(self) -> dict[str, CanonicalField]:
        keys, digest = _blob(self.metadata_blob)
        return {
            "party_type": canonical_string(self.party_type),
            "display_name": canonical_string(self.display_name),
            "status": canonical_string(self.status),
            "data_classification": canonical_string(self.data_classification),
            "merged_into_party_id": canonical_uuid(self.merged_into_party_id),
            "merge_reason": canonical_string(self.merge_reason),
            "metadata_keys": keys,
            "metadata_digest": digest,
        }


class PartyRoleRecord(_CohortRecord):
    """One independently managed business role held by a party."""

    entity_type: Literal[CohortEntityType.PARTY_ROLE] = CohortEntityType.PARTY_ROLE

    party_id: UUID
    role_type: str
    role_key: str
    status: str
    valid_from: datetime | None
    valid_until: datetime | None
    source: str | None
    metadata_blob: OpaqueBlob | None

    def _entity_fields(self) -> dict[str, CanonicalField]:
        keys, digest = _blob(self.metadata_blob)
        return {
            "party_id": canonical_uuid(self.party_id),
            "role_type": canonical_string(self.role_type),
            "role_key": canonical_string(self.role_key),
            "status": canonical_string(self.status),
            "valid_from": canonical_datetime(self.valid_from),
            "valid_until": canonical_datetime(self.valid_until),
            "source": canonical_string(self.source),
            "metadata_keys": keys,
            "metadata_digest": digest,
        }


class PartyRelationshipRecord(_CohortRecord):
    """A directional relationship between parties; never an authorization grant."""

    entity_type: Literal[CohortEntityType.PARTY_RELATIONSHIP] = (
        CohortEntityType.PARTY_RELATIONSHIP
    )

    subject_party_id: UUID
    object_party_id: UUID
    relationship_type: str
    relationship_key: str
    status: str
    valid_from: datetime | None
    valid_until: datetime | None
    source: str | None
    metadata_blob: OpaqueBlob | None

    def _entity_fields(self) -> dict[str, CanonicalField]:
        keys, digest = _blob(self.metadata_blob)
        return {
            "subject_party_id": canonical_uuid(self.subject_party_id),
            "object_party_id": canonical_uuid(self.object_party_id),
            "relationship_type": canonical_string(self.relationship_type),
            "relationship_key": canonical_string(self.relationship_key),
            "status": canonical_string(self.status),
            "valid_from": canonical_datetime(self.valid_from),
            "valid_until": canonical_datetime(self.valid_until),
            "source": canonical_string(self.source),
            "metadata_keys": keys,
            "metadata_digest": digest,
        }


class PartyMembershipRecord(_CohortRecord):
    """A person's organization context and bounded authority scope."""

    entity_type: Literal[CohortEntityType.PARTY_MEMBERSHIP] = (
        CohortEntityType.PARTY_MEMBERSHIP
    )

    person_party_id: UUID
    organization_party_id: UUID
    membership_type: str
    membership_key: str
    status: str
    #: Opaque: an authority scope is a security-relevant document with no
    #: declared schema. The destination re-derives scope from its own roles.
    access_scope: OpaqueBlob | None
    valid_from: datetime | None
    valid_until: datetime | None
    source: str | None
    metadata_blob: OpaqueBlob | None

    def _entity_fields(self) -> dict[str, CanonicalField]:
        keys, digest = _blob(self.metadata_blob)
        scope_keys, scope_digest = _blob(self.access_scope)
        return {
            "person_party_id": canonical_uuid(self.person_party_id),
            "organization_party_id": canonical_uuid(self.organization_party_id),
            "membership_type": canonical_string(self.membership_type),
            "membership_key": canonical_string(self.membership_key),
            "status": canonical_string(self.status),
            "access_scope_keys": scope_keys,
            "access_scope_digest": scope_digest,
            "valid_from": canonical_datetime(self.valid_from),
            "valid_until": canonical_datetime(self.valid_until),
            "source": canonical_string(self.source),
            "metadata_keys": keys,
            "metadata_digest": digest,
        }


class PartyContactPointRecord(_CohortRecord):
    """Reachability evidence scoped to one party and provider context."""

    entity_type: Literal[CohortEntityType.PARTY_CONTACT_POINT] = (
        CohortEntityType.PARTY_CONTACT_POINT
    )

    party_id: UUID
    channel_type: str
    #: The normalized value is the comparable one and the uniqueness key.
    #: `display_value` is the same reachability re-spelled for humans and is
    #: excluded as duplicate personal data.
    normalized_value: str
    scope_key: str
    provider: str | None
    provider_account_id: str | None
    external_subject_id: str | None
    is_primary: bool
    is_active: bool
    verification_status: str
    verified_at: datetime | None
    verification_source: str | None
    consent_status: str
    consent_captured_at: datetime | None
    metadata_blob: OpaqueBlob | None

    def _entity_fields(self) -> dict[str, CanonicalField]:
        keys, digest = _blob(self.metadata_blob)
        return {
            "party_id": canonical_uuid(self.party_id),
            "channel_type": canonical_string(self.channel_type),
            "normalized_value": canonical_string(self.normalized_value),
            "scope_key": canonical_string(self.scope_key),
            "provider": canonical_string(self.provider),
            "provider_account_id": canonical_string(self.provider_account_id),
            "external_subject_id": canonical_string(self.external_subject_id),
            "is_primary": self.is_primary,
            "is_active": self.is_active,
            "verification_status": canonical_string(self.verification_status),
            "verified_at": canonical_datetime(self.verified_at),
            "verification_source": canonical_string(self.verification_source),
            "consent_status": canonical_string(self.consent_status),
            "consent_captured_at": canonical_datetime(self.consent_captured_at),
            "metadata_keys": keys,
            "metadata_digest": digest,
        }


class PartyExternalReferenceRecord(_CohortRecord):
    """A non-authoritative external identifier retained for import provenance."""

    entity_type: Literal[CohortEntityType.PARTY_EXTERNAL_REFERENCE] = (
        CohortEntityType.PARTY_EXTERNAL_REFERENCE
    )

    party_id: UUID
    source_system: str
    referenced_entity_type: str
    external_id: str
    is_active: bool
    metadata_blob: OpaqueBlob | None

    def _entity_fields(self) -> dict[str, CanonicalField]:
        keys, digest = _blob(self.metadata_blob)
        return {
            "party_id": canonical_uuid(self.party_id),
            "source_system": canonical_string(self.source_system),
            "referenced_entity_type": canonical_string(self.referenced_entity_type),
            "external_id": canonical_string(self.external_id),
            "is_active": self.is_active,
            "metadata_keys": keys,
            "metadata_digest": digest,
        }


class CustomerAccountRecord(_CohortRecord):
    """One service and billing account, with its legacy identity columns."""

    entity_type: Literal[CohortEntityType.CUSTOMER_ACCOUNT] = (
        CohortEntityType.CUSTOMER_ACCOUNT
    )

    DERIVED_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "status",
            "is_active",
            "lifecycle_override_status",
            "lifecycle_override_reason",
            "lifecycle_override_source",
            "lifecycle_override_at",
            "legacy_party_status",
            "mrr_total",
        }
    )

    # canonical identity binding
    party_id: UUID | None
    party_bound_at: datetime | None
    party_binding_source: str | None
    party_binding_reason: str | None

    # legacy identity columns
    first_name: str
    last_name: str
    display_name: str | None
    company_name: str | None
    legal_name: str | None
    tax_id: str | None
    domain: str | None
    website: str | None
    email: str
    email_verified: bool
    phone: str | None
    #: Presence only. A national identity number is regulated evidence and is
    #: not needed for service continuity.
    nin_present: bool
    #: Presence only, for the same reason.
    date_of_birth_present: bool
    gender: str | None
    preferred_contact_method: str | None
    locale: str | None
    timezone: str | None

    # contact address
    address_line1: str | None
    address_line2: str | None
    city: str | None
    region: str | None
    lga: str | None
    postal_code: str | None
    country_code: str | None
    pop_site_id: UUID | None

    # account
    subscriber_number: str | None
    account_number: str | None
    account_start_date: datetime | None
    status: str | None
    #: A legacy duplicate of the account's party status, retained because
    #: dropping a column at migration time is how a fact disappears quietly.
    #: The destination reconciles it against `party_id`; it does not adopt it.
    legacy_party_status: str | None
    lifecycle_override_status: str | None
    lifecycle_override_reason: str | None
    lifecycle_override_source: str | None
    lifecycle_override_at: datetime | None
    user_type: str | None
    is_active: bool
    marketing_opt_in: bool
    reseller_id: UUID | None
    tax_rate_id: UUID | None
    policy_set_id: UUID | None
    organization_id: UUID | None
    sales_order_id: UUID | None

    # billing preferences
    billing_enabled: bool
    captive_redirect_enabled: bool
    billing_name: str | None
    billing_address_line1: str | None
    billing_address_line2: str | None
    billing_city: str | None
    billing_region: str | None
    billing_postal_code: str | None
    billing_country_code: str | None
    payment_method: str | None
    deposit: Decimal | None
    billing_mode: str | None
    billing_day: int | None
    payment_due_days: int | None
    grace_period_days: int | None
    min_balance: Decimal | None
    prepaid_low_balance_at: datetime | None
    prepaid_deactivation_at: datetime | None
    mrr_total: Decimal | None

    external_correlations: tuple[ExternalCorrelation, ...]
    metadata_blob: OpaqueBlob | None

    def _entity_fields(self) -> dict[str, CanonicalField]:
        keys, digest = _blob(self.metadata_blob)
        return {
            "party_id": canonical_uuid(self.party_id),
            "party_bound_at": canonical_datetime(self.party_bound_at),
            "party_binding_source": canonical_string(self.party_binding_source),
            "party_binding_reason": canonical_string(self.party_binding_reason),
            "first_name": canonical_string(self.first_name),
            "last_name": canonical_string(self.last_name),
            "display_name": canonical_string(self.display_name),
            "company_name": canonical_string(self.company_name),
            "legal_name": canonical_string(self.legal_name),
            "tax_id": canonical_string(self.tax_id),
            "domain": canonical_string(self.domain),
            "website": canonical_string(self.website),
            "email": canonical_string(self.email),
            "email_verified": self.email_verified,
            "phone": canonical_string(self.phone),
            "nin_present": self.nin_present,
            "date_of_birth_present": self.date_of_birth_present,
            "gender": canonical_string(self.gender),
            "preferred_contact_method": canonical_string(self.preferred_contact_method),
            "locale": canonical_string(self.locale),
            "timezone": canonical_string(self.timezone),
            "address_line1": canonical_string(self.address_line1),
            "address_line2": canonical_string(self.address_line2),
            "city": canonical_string(self.city),
            "region": canonical_string(self.region),
            "lga": canonical_string(self.lga),
            "postal_code": canonical_string(self.postal_code),
            "country_code": canonical_string(self.country_code),
            "pop_site_id": canonical_uuid(self.pop_site_id),
            "subscriber_number": canonical_string(self.subscriber_number),
            "account_number": canonical_string(self.account_number),
            "account_start_date": canonical_datetime(self.account_start_date),
            "status": canonical_string(self.status),
            "legacy_party_status": canonical_string(self.legacy_party_status),
            "lifecycle_override_status": canonical_string(
                self.lifecycle_override_status
            ),
            "lifecycle_override_reason": canonical_string(
                self.lifecycle_override_reason
            ),
            "lifecycle_override_source": canonical_string(
                self.lifecycle_override_source
            ),
            "lifecycle_override_at": canonical_datetime(self.lifecycle_override_at),
            "user_type": canonical_string(self.user_type),
            "is_active": self.is_active,
            "marketing_opt_in": self.marketing_opt_in,
            "reseller_id": canonical_uuid(self.reseller_id),
            "tax_rate_id": canonical_uuid(self.tax_rate_id),
            "policy_set_id": canonical_uuid(self.policy_set_id),
            "organization_id": canonical_uuid(self.organization_id),
            "sales_order_id": canonical_uuid(self.sales_order_id),
            "billing_enabled": self.billing_enabled,
            "captive_redirect_enabled": self.captive_redirect_enabled,
            "billing_name": canonical_string(self.billing_name),
            "billing_address_line1": canonical_string(self.billing_address_line1),
            "billing_address_line2": canonical_string(self.billing_address_line2),
            "billing_city": canonical_string(self.billing_city),
            "billing_region": canonical_string(self.billing_region),
            "billing_postal_code": canonical_string(self.billing_postal_code),
            "billing_country_code": canonical_string(self.billing_country_code),
            "payment_method": canonical_string(self.payment_method),
            "deposit": canonical_decimal(self.deposit),
            "billing_mode": canonical_string(self.billing_mode),
            "billing_day": self.billing_day,
            "payment_due_days": self.payment_due_days,
            "grace_period_days": self.grace_period_days,
            "min_balance": canonical_decimal(self.min_balance),
            "prepaid_low_balance_at": canonical_datetime(self.prepaid_low_balance_at),
            "prepaid_deactivation_at": canonical_datetime(self.prepaid_deactivation_at),
            "mrr_total": canonical_decimal(self.mrr_total),
            "external_correlations": _correlations(self.external_correlations),
            "metadata_keys": keys,
            "metadata_digest": digest,
        }


class CustomerContactRecord(_CohortRecord):
    """A legacy contact row attached to one account."""

    entity_type: Literal[CohortEntityType.CUSTOMER_CONTACT] = (
        CohortEntityType.CUSTOMER_CONTACT
    )

    subscriber_id: UUID
    person_party_id: UUID | None
    party_bound_at: datetime | None
    party_binding_source: str | None
    party_binding_reason: str | None
    full_name: str | None
    phone: str | None
    email: str | None
    whatsapp: str | None
    facebook: str | None
    instagram: str | None
    x_handle: str | None
    telegram: str | None
    linkedin: str | None
    #: Presence only: free-text overflow for channels with no column.
    other_social_present: bool
    contact_relationship: str | None
    #: Nullable: the column carries a Python-side default rather than a NOT
    #: NULL constraint, so a legacy row can hold no type at all. Exported as
    #: absent rather than defaulted — a contact whose type nobody recorded is
    #: not a "general" contact, it is one nobody classified.
    contact_type: str | None
    is_billing_contact: bool
    is_authorized: bool
    receives_notifications: bool

    def _entity_fields(self) -> dict[str, CanonicalField]:
        return {
            "subscriber_id": canonical_uuid(self.subscriber_id),
            "person_party_id": canonical_uuid(self.person_party_id),
            "party_bound_at": canonical_datetime(self.party_bound_at),
            "party_binding_source": canonical_string(self.party_binding_source),
            "party_binding_reason": canonical_string(self.party_binding_reason),
            "full_name": canonical_string(self.full_name),
            "phone": canonical_string(self.phone),
            "email": canonical_string(self.email),
            "whatsapp": canonical_string(self.whatsapp),
            "facebook": canonical_string(self.facebook),
            "instagram": canonical_string(self.instagram),
            "x_handle": canonical_string(self.x_handle),
            "telegram": canonical_string(self.telegram),
            "linkedin": canonical_string(self.linkedin),
            "other_social_present": self.other_social_present,
            "contact_relationship": canonical_string(self.contact_relationship),
            "contact_type": canonical_string(self.contact_type),
            "is_billing_contact": self.is_billing_contact,
            "is_authorized": self.is_authorized,
            "receives_notifications": self.receives_notifications,
        }


class CustomerAddressRecord(_CohortRecord):
    """A service or billing address attached to one account."""

    entity_type: Literal[CohortEntityType.CUSTOMER_ADDRESS] = (
        CohortEntityType.CUSTOMER_ADDRESS
    )

    subscriber_id: UUID
    tax_rate_id: UUID | None
    address_type: str | None
    label: str | None
    address_line1: str
    address_line2: str | None
    city: str | None
    region: str | None
    lga: str | None
    postal_code: str | None
    country_code: str | None
    #: `geom` is excluded: it is a PostGIS projection of these two columns and
    #: exporting both would give a destination two sources for one fact.
    latitude: float | None
    longitude: float | None
    is_primary: bool

    def _entity_fields(self) -> dict[str, CanonicalField]:
        return {
            "subscriber_id": canonical_uuid(self.subscriber_id),
            "tax_rate_id": canonical_uuid(self.tax_rate_id),
            "address_type": canonical_string(self.address_type),
            "label": canonical_string(self.label),
            "address_line1": canonical_string(self.address_line1),
            "address_line2": canonical_string(self.address_line2),
            "city": canonical_string(self.city),
            "region": canonical_string(self.region),
            "lga": canonical_string(self.lga),
            "postal_code": canonical_string(self.postal_code),
            "country_code": canonical_string(self.country_code),
            "latitude": canonical_coordinate(self.latitude),
            "longitude": canonical_coordinate(self.longitude),
            "is_primary": self.is_primary,
        }


class OrganizationRecord(_CohortRecord):
    """A business account record. No counted writer; see the ownership map."""

    entity_type: Literal[CohortEntityType.ORGANIZATION] = CohortEntityType.ORGANIZATION

    party_id: UUID | None
    party_bound_at: datetime | None
    party_binding_source: str | None
    party_binding_reason: str | None
    name: str
    legal_name: str | None
    tax_id: str | None
    domain: str | None
    website: str | None
    phone: str | None
    email: str | None
    account_type: str
    account_status: str
    parent_id: UUID | None
    primary_contact_id: UUID | None
    owner_id: UUID | None
    industry: str | None
    employee_count: str | None
    annual_revenue: str | None
    source: str | None
    address_line1: str | None
    address_line2: str | None
    city: str | None
    region: str | None
    postal_code: str | None
    country_code: str | None
    tags: tuple[str, ...]
    commission_rate: Decimal | None
    is_active: bool
    external_correlations: tuple[ExternalCorrelation, ...]
    metadata_blob: OpaqueBlob | None

    def _entity_fields(self) -> dict[str, CanonicalField]:
        keys, digest = _blob(self.metadata_blob)
        return {
            "party_id": canonical_uuid(self.party_id),
            "party_bound_at": canonical_datetime(self.party_bound_at),
            "party_binding_source": canonical_string(self.party_binding_source),
            "party_binding_reason": canonical_string(self.party_binding_reason),
            "name": canonical_string(self.name),
            "legal_name": canonical_string(self.legal_name),
            "tax_id": canonical_string(self.tax_id),
            "domain": canonical_string(self.domain),
            "website": canonical_string(self.website),
            "phone": canonical_string(self.phone),
            "email": canonical_string(self.email),
            "account_type": canonical_string(self.account_type),
            "account_status": canonical_string(self.account_status),
            "parent_id": canonical_uuid(self.parent_id),
            "primary_contact_id": canonical_uuid(self.primary_contact_id),
            "owner_id": canonical_uuid(self.owner_id),
            "industry": canonical_string(self.industry),
            "employee_count": canonical_string(self.employee_count),
            "annual_revenue": canonical_string(self.annual_revenue),
            "source": canonical_string(self.source),
            "address_line1": canonical_string(self.address_line1),
            "address_line2": canonical_string(self.address_line2),
            "city": canonical_string(self.city),
            "region": canonical_string(self.region),
            "postal_code": canonical_string(self.postal_code),
            "country_code": canonical_string(self.country_code),
            "tags": canonical_strings(self.tags),
            "commission_rate": canonical_decimal(self.commission_rate),
            "is_active": self.is_active,
            "external_correlations": _correlations(self.external_correlations),
            "metadata_keys": keys,
            "metadata_digest": digest,
        }


class OrganizationMembershipRecord(_CohortRecord):
    """A person's membership of a business account."""

    entity_type: Literal[CohortEntityType.ORGANIZATION_MEMBERSHIP] = (
        CohortEntityType.ORGANIZATION_MEMBERSHIP
    )

    organization_id: UUID
    person_id: UUID
    party_membership_id: UUID | None
    party_bound_at: datetime | None
    party_binding_source: str | None
    party_binding_reason: str | None
    role: str
    is_active: bool

    def _entity_fields(self) -> dict[str, CanonicalField]:
        return {
            "organization_id": canonical_uuid(self.organization_id),
            "person_id": canonical_uuid(self.person_id),
            "party_membership_id": canonical_uuid(self.party_membership_id),
            "party_bound_at": canonical_datetime(self.party_bound_at),
            "party_binding_source": canonical_string(self.party_binding_source),
            "party_binding_reason": canonical_string(self.party_binding_reason),
            "role": canonical_string(self.role),
            "is_active": self.is_active,
        }


class BrandProfileRecord(_CohortRecord):
    """A customer-facing brand identity for one platform or tenant scope."""

    entity_type: Literal[CohortEntityType.BRAND_PROFILE] = (
        CohortEntityType.BRAND_PROFILE
    )

    scope_type: str
    scope_id: UUID | None
    brand_name: str | None
    product_name: str | None
    legal_name: str | None
    tagline: str | None
    primary_color: str | None
    secondary_color: str | None
    logo_url: str | None
    dark_logo_url: str | None
    favicon_url: str | None
    support_email: str | None
    support_phone: str | None
    from_email: str | None
    from_name: str | None
    app_url: str | None
    portal_domain: str | None
    legal_address: OpaqueBlob | None
    is_active: bool
    metadata_blob: OpaqueBlob | None

    def _entity_fields(self) -> dict[str, CanonicalField]:
        keys, digest = _blob(self.metadata_blob)
        address_keys, address_digest = _blob(self.legal_address)
        return {
            "scope_type": canonical_string(self.scope_type),
            "scope_id": canonical_uuid(self.scope_id),
            "brand_name": canonical_string(self.brand_name),
            "product_name": canonical_string(self.product_name),
            "legal_name": canonical_string(self.legal_name),
            "tagline": canonical_string(self.tagline),
            "primary_color": canonical_string(self.primary_color),
            "secondary_color": canonical_string(self.secondary_color),
            "logo_url": canonical_string(self.logo_url),
            "dark_logo_url": canonical_string(self.dark_logo_url),
            "favicon_url": canonical_string(self.favicon_url),
            "support_email": canonical_string(self.support_email),
            "support_phone": canonical_string(self.support_phone),
            "from_email": canonical_string(self.from_email),
            "from_name": canonical_string(self.from_name),
            "app_url": canonical_string(self.app_url),
            "portal_domain": canonical_string(self.portal_domain),
            "legal_address_keys": address_keys,
            "legal_address_digest": address_digest,
            "is_active": self.is_active,
            "metadata_keys": keys,
            "metadata_digest": digest,
        }


#: A tagged union rather than a base-class annotation. Pydantic revalidates
#: against the declared type, so `tuple[_CohortRecord, ...]` would quietly
#: strip every subclass field on the way into a page.
CohortRecord = Annotated[
    PartyRecord
    | PartyRoleRecord
    | PartyRelationshipRecord
    | PartyMembershipRecord
    | PartyContactPointRecord
    | PartyExternalReferenceRecord
    | CustomerAccountRecord
    | CustomerContactRecord
    | CustomerAddressRecord
    | OrganizationRecord
    | OrganizationMembershipRecord
    | BrandProfileRecord,
    Field(discriminator="entity_type"),
]

#: Every record class, keyed by the entity type it exports. Built from the
#: union above so a new record type cannot be added to one and forgotten in
#: the other.
RECORD_TYPES: Final[dict[CohortEntityType, type[_CohortRecord]]] = {
    PartyRecord.model_fields["entity_type"].default: PartyRecord,
    PartyRoleRecord.model_fields["entity_type"].default: PartyRoleRecord,
    PartyRelationshipRecord.model_fields[
        "entity_type"
    ].default: PartyRelationshipRecord,
    PartyMembershipRecord.model_fields["entity_type"].default: PartyMembershipRecord,
    PartyContactPointRecord.model_fields[
        "entity_type"
    ].default: PartyContactPointRecord,
    PartyExternalReferenceRecord.model_fields[
        "entity_type"
    ].default: PartyExternalReferenceRecord,
    CustomerAccountRecord.model_fields["entity_type"].default: CustomerAccountRecord,
    CustomerContactRecord.model_fields["entity_type"].default: CustomerContactRecord,
    CustomerAddressRecord.model_fields["entity_type"].default: CustomerAddressRecord,
    OrganizationRecord.model_fields["entity_type"].default: OrganizationRecord,
    OrganizationMembershipRecord.model_fields[
        "entity_type"
    ].default: OrganizationMembershipRecord,
    BrandProfileRecord.model_fields["entity_type"].default: BrandProfileRecord,
}


class SnapshotPage(BaseModel):
    """One bounded, ordered page of one entity type at one source revision."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    contract_version: ContractVersion
    cohort_code: str = COHORT_ID
    schema_version: int = SCHEMA_VERSION
    tenant: TenantScope
    source_revision: SourceRevision
    entity_type: CohortEntityType
    records: tuple[CohortRecord, ...]
    next_cursor: ExportCursor | None
    completeness: Completeness

    @model_validator(mode="after")
    def _check(self) -> SnapshotPage:
        if self.cohort_code != COHORT_ID:
            raise ValueError(
                f"this build exports {COHORT_ID} only; got {self.cohort_code!r}"
            )
        mismatched = [
            record.identity.value
            for record in self.records
            if CohortEntityType(record.entity_type) is not self.entity_type
        ]
        if mismatched:
            raise ValueError(
                "a page carries one entity type; these records disagree with "
                "its declared type: " + ", ".join(sorted(mismatched))
            )
        identifiers = [record.source_id for record in self.records]
        if identifiers != sorted(identifiers, key=str):
            raise ValueError(
                "page records must be ordered by source id; unordered records "
                "make a keyset checkpoint meaningless"
            )
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("a page repeats a source id")
        if len(identifiers) > MAX_PAGE_SIZE:
            raise ValueError(
                f"a page holds at most {MAX_PAGE_SIZE} records; got {len(identifiers)}"
            )
        if self.completeness is Completeness.COMPLETE and (
            self.next_cursor is not None
        ):
            raise ValueError(
                "a complete page cannot also offer a continuation; a consumer "
                "reading either field alone would reach a different conclusion"
            )
        if self.completeness is Completeness.PARTIAL and self.next_cursor is None:
            raise ValueError("a partial page must say where to resume")
        if self.next_cursor is not None and (
            self.next_cursor.entity_type is not self.entity_type
        ):
            raise ValueError("a continuation must stay on the same entity type")
        return self

    @property
    def identities(self) -> tuple[SourceIdentity, ...]:
        """Each record's stable opaque identity, in page order."""

        return tuple(record.identity for record in self.records)


__all__ = [
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "RECORD_TYPES",
    "SCHEMA_VERSION",
    "SUPPORTED_CONTRACT_VERSIONS",
    "BrandProfileRecord",
    "ExportCursor",
    "CohortRecord",
    "Completeness",
    "ContractVersion",
    "CustomerAccountRecord",
    "CustomerAddressRecord",
    "CustomerContactRecord",
    "ExternalCorrelation",
    "OpaqueBlob",
    "OrganizationMembershipRecord",
    "OrganizationRecord",
    "PartyContactPointRecord",
    "PartyExternalReferenceRecord",
    "PartyMembershipRecord",
    "PartyRecord",
    "PartyRelationshipRecord",
    "PartyRoleRecord",
    "SnapshotPage",
    "SourceIdentity",
    "SourceRevision",
    "TenantScope",
    "UnsupportedContractVersionError",
    "opaque_blob",
    "require_contract_version",
]
