"""Read-only declared secret requirements in the operator-tenant snapshot.

This reports the deployment obligations of live attributed machine credentials
and enabled pinned connector installations. Operation-only material is reported
without inferring enablement from historical prepaid or Fiber records. It is
not a fleet census, provider-health check, or proof that every feature is ready.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from dotmac_kernel.machine_models import MachineCredential
from sqlalchemy import literal, or_, select
from sqlalchemy.orm import Session

from app.models.integration_platform import (
    IntegrationConfigRevision,
    IntegrationInstallation,
    IntegrationInstallationState,
)
from app.services.deployment_secret_schema import (
    DeploymentSecretSchemaResult,
    SchemaCapabilityState,
)
from app.services.domain_errors import DomainError
from app.services.integrations.registry import require_pinned_connector_definition
from app.services.kernel_secret_source import OPTIONAL_SECRET_REFS
from app.services.operator_tenant import operator_tenant_id
from app.services.secrets import (
    is_secret_ref,
    resolve_openbao_ref_optional,
    resolve_secret,
)


class SecretRequirement(StrEnum):
    DEPLOYMENT_REQUIRED = "deployment_required"
    DORMANT = "dormant"
    OPERATION_ONLY = "operation_only"
    EVALUATION_UNAVAILABLE = "evaluation_unavailable"


class MaterialStatus(StrEnum):
    READY = "ready"
    ABSENT = "absent"
    INVALID = "invalid"
    UNAVAILABLE = "unavailable"


class RequirementName(StrEnum):
    DEPLOYMENT_REQUIREMENTS = "deployment_requirements"
    MACHINE_HMAC = "machine_credential_hmac_key"
    PREPAID_ATTESTATION = "prepaid_attestation_public_key"
    CONVERSION_INGEST = "conversion_ingest_api_key"
    INTEGRATION_PIN = "integration_manifest_pin"
    INTEGRATION_REVISION = "integration_current_revision"
    INTEGRATION_BINDINGS = "integration_required_bindings"


class RequirementCoverage(StrEnum):
    OPERATOR_SNAPSHOT = "operator_tenant_snapshot"


class RequirementEvaluationPhase(StrEnum):
    PRE_MIGRATION = "pre_migration"
    POST_MIGRATION = "post_migration"


class DeploymentSecretRequirementsError(DomainError):
    """Invalid typed query input; contains no operator-supplied values."""


@dataclass(frozen=True, slots=True)
class DeploymentSecretRequirementsQuery:
    evaluated_at: datetime
    schema: DeploymentSecretSchemaResult
    phase: RequirementEvaluationPhase = RequirementEvaluationPhase.PRE_MIGRATION

    def __post_init__(self) -> None:
        if self.evaluated_at.tzinfo is None:
            raise DeploymentSecretRequirementsError(
                code="deployment_requirements.timezone_required",
                message="Readiness evaluation requires a timezone-aware timestamp",
                retryable=False,
            )


@dataclass(frozen=True, slots=True)
class SecretRequirementCheck:
    name: RequirementName
    requirement: SecretRequirement
    material: MaterialStatus
    count: int

    @property
    def ok(self) -> bool:
        if self.requirement is SecretRequirement.EVALUATION_UNAVAILABLE:
            return False
        if self.material in (MaterialStatus.INVALID, MaterialStatus.UNAVAILABLE):
            return False
        return (
            self.requirement is not SecretRequirement.DEPLOYMENT_REQUIRED
            or self.material is MaterialStatus.READY
        )


@dataclass(frozen=True, slots=True)
class DeploymentSecretRequirementsResult:
    checks: tuple[SecretRequirementCheck, ...]
    schema: DeploymentSecretSchemaResult
    enabled_installation_count: int = 0
    coverage: RequirementCoverage = RequirementCoverage.OPERATOR_SNAPSHOT

    @property
    def ok(self) -> bool:
        return self.schema.ok and all(check.ok for check in self.checks)


def unavailable_requirements(
    schema: DeploymentSecretSchemaResult | None = None,
) -> DeploymentSecretRequirementsResult:
    """Bounded evidence for snapshot/query failure, never an empty success."""
    return DeploymentSecretRequirementsResult(
        (
            SecretRequirementCheck(
                RequirementName.DEPLOYMENT_REQUIREMENTS,
                SecretRequirement.EVALUATION_UNAVAILABLE,
                MaterialStatus.UNAVAILABLE,
                1,
            ),
        ),
        schema
        or DeploymentSecretSchemaResult(
            integrations=SchemaCapabilityState.EVALUATION_UNAVAILABLE,
            machine_credentials=SchemaCapabilityState.EVALUATION_UNAVAILABLE,
            machine_attribution_available=False,
        ),
    )


@dataclass(frozen=True, slots=True)
class _BindingPlan:
    reference: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class _RequirementsPlan:
    machine_required: bool | None
    checks: tuple[SecretRequirementCheck, ...]
    bindings: tuple[_BindingPlan, ...] = field(repr=False)
    enabled_installation_count: int


def _derive_requirements(
    db: Session, query: DeploymentSecretRequirementsQuery
) -> _RequirementsPlan:
    # Only existence is read. Never fetch hashes, labels, scopes or raw tokens.
    # A known pre-introduction schema is explicit assessment evidence, never
    # interpreted as a query that happened to return no rows.
    machine_required: bool | None = None
    if query.schema.machine_credentials is SchemaCapabilityState.EVALUATED:
        machine_query = (
            select(literal(True))
            .select_from(MachineCredential)
            .where(
                MachineCredential.tenant_id == operator_tenant_id(),
                MachineCredential.is_active.is_(True),
                MachineCredential.revoked_at.is_(None),
                or_(
                    MachineCredential.expires_at.is_(None),
                    MachineCredential.expires_at > query.evaluated_at.astimezone(UTC),
                ),
            )
            .limit(1)
        )
        # Before the attribution migration, every live lifecycle row requires
        # HMAC conservatively. Never select a column not installed there.
        if query.schema.machine_attribution_available:
            machine_query = machine_query.where(
                MachineCredential.source_application.is_not(None)
            )
        machine_required = bool(db.scalar(machine_query))
    checks: list[SecretRequirementCheck] = []
    bindings: list[_BindingPlan] = []
    enabled_count = 0
    if query.schema.integrations is not SchemaCapabilityState.EVALUATED:
        return _RequirementsPlan(machine_required, (), (), 0)
    # These exact columns exist at introduction revision 376; loading the full
    # current ORM row would couple predecessor readiness to later unrelated
    # model additions.
    installations = db.execute(
        select(
            IntegrationInstallation.id,
            IntegrationInstallation.connector_key,
            IntegrationInstallation.connector_version,
            IntegrationInstallation.manifest_digest,
            IntegrationInstallation.current_config_revision_id,
        )
        .where(
            IntegrationInstallation.state == IntegrationInstallationState.enabled.value
        )
        .order_by(IntegrationInstallation.id)
    ).tuples()
    for installation_id, connector_key, version, digest, revision_id in installations:
        enabled_count += 1
        try:
            definition = require_pinned_connector_definition(
                connector_key,
                version=version,
                manifest_digest=digest,
            )
        except KeyError:
            checks.append(
                _required_check(RequirementName.INTEGRATION_PIN, MaterialStatus.INVALID)
            )
            continue
        checks.append(
            _required_check(RequirementName.INTEGRATION_PIN, MaterialStatus.READY)
        )
        revision = (
            db.execute(
                select(
                    IntegrationConfigRevision.installation_id,
                    IntegrationConfigRevision.secret_refs,
                ).where(IntegrationConfigRevision.id == revision_id)
            )
            .tuples()
            .one_or_none()
            if revision_id is not None
            else None
        )
        if revision is None or revision[0] != installation_id:
            checks.append(
                _required_check(
                    RequirementName.INTEGRATION_REVISION, MaterialStatus.INVALID
                )
            )
            continue
        checks.append(
            _required_check(RequirementName.INTEGRATION_REVISION, MaterialStatus.READY)
        )
        # JSON is untrusted at this persistence boundary. Never coerce arbitrary
        # objects to str: that could turn plaintext or malformed containers into
        # apparently valid references and leak values through object repr.
        raw_refs: object = revision[1]
        if not isinstance(raw_refs, dict) or any(
            not isinstance(name, str) or not isinstance(reference, str)
            for name, reference in raw_refs.items()
        ):
            checks.append(
                _required_check(
                    RequirementName.INTEGRATION_BINDINGS, MaterialStatus.INVALID
                )
            )
            continue
        declared = frozenset(binding.name for binding in definition.secrets)
        if set(raw_refs) - declared or any(
            not is_secret_ref(reference) for reference in raw_refs.values()
        ):
            checks.append(
                _required_check(
                    RequirementName.INTEGRATION_BINDINGS, MaterialStatus.INVALID
                )
            )
            continue
        for name in sorted(definition.required_secret_names):
            reference = raw_refs.get(name)
            if not isinstance(reference, str):
                checks.append(
                    _required_check(
                        RequirementName.INTEGRATION_BINDINGS, MaterialStatus.ABSENT
                    )
                )
                continue
            bindings.append(_BindingPlan(reference))
    return _RequirementsPlan(
        machine_required, tuple(checks), tuple(bindings), enabled_count
    )


def _required_check(
    name: RequirementName, material: MaterialStatus
) -> SecretRequirementCheck:
    return SecretRequirementCheck(
        name, SecretRequirement.DEPLOYMENT_REQUIRED, material, 1
    )


def _material_status(
    reference: str, resolver: Callable[[str], str | None]
) -> MaterialStatus:
    try:
        value = resolver(reference)
    except Exception:  # Client errors may embed a payload. Return status only.
        return MaterialStatus.UNAVAILABLE
    if value is None:
        return MaterialStatus.ABSENT
    return MaterialStatus.READY if value.strip() else MaterialStatus.INVALID


def _conversion_material() -> str | None:
    from dotmac_kernel.secret_sources import get_secret

    from app.config import settings

    # Same precedence as marketing_conversion_projection._subject_key; the
    # candidate probe does not install a held source, so it resolves the boot
    # source directly when neither existing source supplied the value.
    configured = settings.conversion_ingest_api_key
    if configured:
        return configured
    held = get_secret(RequirementName.CONVERSION_INGEST.value)
    if held:
        return held
    return resolve_openbao_ref_optional(
        OPTIONAL_SECRET_REFS[RequirementName.CONVERSION_INGEST.value]
    )


def evaluate_deployment_secret_requirements(
    db: Session,
    query: DeploymentSecretRequirementsQuery,
    *,
    optional_resolver: Callable[[str], str | None] = resolve_openbao_ref_optional,
    binding_resolver: Callable[[str], str | None] = resolve_secret,
    conversion_resolver: Callable[[], str | None] = _conversion_material,
) -> DeploymentSecretRequirementsResult:
    """Evaluate declared obligations in the caller's read-only snapshot.

    Modern lifecycle predicates mirror the public kernel model/auth contract:
    active, not revoked, not expired, and attributed. A known legacy schema
    without attribution conservatively requires every live lifecycle row.
    RLS remains authoritative; no
    cross-tenant visibility or credential validity beyond that is claimed.
    Derivation finishes before any secret resolution, and no SQL error is
    interpreted as a dormant feature. This query does not install held keys,
    execute connector operations, flush, commit, or rollback.
    """
    if not query.schema.ok:
        return unavailable_requirements(query.schema)
    if query.phase is RequirementEvaluationPhase.POST_MIGRATION and (
        query.schema.integrations is not SchemaCapabilityState.EVALUATED
        or query.schema.machine_credentials is not SchemaCapabilityState.EVALUATED
        or not query.schema.machine_attribution_available
    ):
        return unavailable_requirements(query.schema)
    try:
        with db.no_autoflush:
            plan = _derive_requirements(db, query)
    except Exception:
        return unavailable_requirements(query.schema)

    checks = list(plan.checks)
    if plan.machine_required is not None:
        checks.append(
            SecretRequirementCheck(
                RequirementName.MACHINE_HMAC,
                SecretRequirement.DEPLOYMENT_REQUIRED
                if plan.machine_required
                else SecretRequirement.DORMANT,
                _material_status(
                    OPTIONAL_SECRET_REFS[RequirementName.MACHINE_HMAC.value],
                    optional_resolver,
                ),
                1,
            )
        )
    checks.append(
        SecretRequirementCheck(
            RequirementName.PREPAID_ATTESTATION,
            SecretRequirement.OPERATION_ONLY,
            _material_status(
                OPTIONAL_SECRET_REFS[RequirementName.PREPAID_ATTESTATION.value],
                optional_resolver,
            ),
            1,
        )
    )
    try:
        conversion_value = conversion_resolver()
        conversion_status = (
            MaterialStatus.ABSENT
            if conversion_value is None
            else MaterialStatus.READY
            if conversion_value.strip()
            else MaterialStatus.INVALID
        )
    except Exception:
        conversion_status = MaterialStatus.UNAVAILABLE
    checks.append(
        SecretRequirementCheck(
            RequirementName.CONVERSION_INGEST,
            SecretRequirement.OPERATION_ONLY,
            conversion_status,
            1,
        )
    )
    checks.extend(
        _required_check(
            RequirementName.INTEGRATION_BINDINGS,
            _material_status(binding.reference, binding_resolver),
        )
        for binding in plan.bindings
    )
    # A fixed set of enum names/statuses bounds the output independently of
    # installation counts. Installation labels, IDs, pins and refs never leave.
    counts = Counter(
        (check.name, check.requirement, check.material) for check in checks
    )
    return DeploymentSecretRequirementsResult(
        tuple(
            SecretRequirementCheck(name, requirement, material, count)
            for (name, requirement, material), count in sorted(counts.items())
        ),
        query.schema,
        plan.enabled_installation_count,
    )
