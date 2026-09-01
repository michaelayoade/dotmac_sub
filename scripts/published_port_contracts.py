"""Strict wire contracts for published-port plans and their run receipts."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    IPvAnyAddress,
    IPvAnyNetwork,
    StringConstraints,
    model_validator,
)

from scripts.published_ports import DeclaredPublishedPortPlan, normalise_bind

INTENT_SCHEMA = "PublishedPortIntentV1"
PLAN_SCHEMA = "PublishedPortPlanV1"
RECEIPT_SCHEMA = "PublishedPortPlanReceiptV1"
EXECUTION_PLAN_SCHEMA = "PublishedPortExecutionPlanV2"
HOST_SNAPSHOT_SCHEMA = "PublishedPortHostSnapshotV2"
ARTIFACT_RECEIPT_SCHEMA = "PublishedPortPlanArtifactReceiptV2"
RUN_OBSERVATION_SCHEMA = "PublishedPortPlanRunObservationV2"
APPLY_ADMISSION_SCHEMA = "PublishedPortApplyAdmissionV2"
FIREWALL_PROOF_SCHEMA = "PublishedPortFirewallProofV2"
CLIENT_REACH_PROOF_SCHEMA = "PublishedPortClientReachProofV2"
APPLY_OUTCOME_SCHEMA = "PublishedPortApplyOutcomeV2"
POSTCONDITION_VERDICT_SCHEMA = "PublishedPortPostconditionVerdictV2"
DEADMAN_STATE_SCHEMA = "PublishedPortDeadmanStateV2"
PLAN_WORKFLOW = ".github/workflows/infrastructure-reconcile-plan.yml"
APPLY_WORKFLOW = ".github/workflows/infrastructure-reconcile-apply.yml"
PROTECTED_REF = "refs/heads/main"

GitSha = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$")]
ContainerId = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Sha256Digest = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]
NonEmpty = Annotated[str, StringConstraints(min_length=1)]
EnvironmentKey = Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]*$")]
ServiceName = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")]
TargetName = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")]
ContainerName = Annotated[
    str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
]
ImageReference = Annotated[
    str,
    StringConstraints(pattern=r"^[^\s@]+@sha256:[0-9a-f]{64}$"),
]
OperationId = Annotated[
    str, StringConstraints(pattern=r"^port-[a-z0-9-]+-[1-9][0-9]*$")
]


class CanonicalContractError(ValueError):
    """A serialized evidence object is invalid or non-canonical."""


class StrictContract(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, populate_by_name=True
    )

    def canonical_bytes(self) -> bytes:
        return (
            json.dumps(
                self.model_dump(mode="json", by_alias=True),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            )
            + "\n"
        ).encode("utf-8")

    def canonical_digest(self) -> str:
        return f"sha256:{hashlib.sha256(self.canonical_bytes()).hexdigest()}"

    @classmethod
    def from_canonical_bytes(cls, raw: bytes) -> Self:
        try:
            parsed = cls.model_validate_json(raw, strict=True)
        except (ValueError, TypeError) as error:
            raise CanonicalContractError(f"invalid {cls.__name__}: {error}") from error
        if raw != parsed.canonical_bytes():
            raise CanonicalContractError(f"non-canonical {cls.__name__} bytes")
        return parsed


class EvidenceConclusion(StrEnum):
    SUCCESS = "success"


class ProofVerdict(StrEnum):
    ADMITTED = "admitted"
    REACHABLE = "reachable"


def _require_utc(value: datetime, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{field} must be expressed in UTC")


class PublishedPortTargetV1(StrictContract):
    key: NonEmpty
    host_port: int = Field(ge=1, le=65535)
    container_port: int = Field(ge=1, le=65535)
    protocol: Literal["tcp", "udp"]
    bind_env: EnvironmentKey
    bind: IPvAnyAddress
    expected_listeners: tuple[IPvAnyAddress, ...] = Field(min_length=1)
    required_clients: tuple[IPvAnyNetwork, ...]

    @model_validator(mode="after")
    def canonical_collections(self) -> Self:
        if tuple(map(str, self.expected_listeners)) != tuple(
            sorted(set(map(str, self.expected_listeners)))
        ):
            raise ValueError("expected_listeners must be unique and sorted")
        if tuple(map(str, self.required_clients)) != tuple(
            sorted(set(map(str, self.required_clients)))
        ):
            raise ValueError("required_clients must be unique and sorted")
        return self


class PublishedPortIntentV1(StrictContract):
    """Declared intent emitted by the existing CLI; not an apply plan."""

    schema_id: Literal["PublishedPortIntentV1"] = Field(
        default=INTENT_SCHEMA, alias="schema"
    )
    service: ServiceName
    environment: NonEmpty
    assignments: dict[EnvironmentKey, NonEmpty] = Field(min_length=1)
    targets: tuple[PublishedPortTargetV1, ...] = Field(min_length=1)
    recreated_by_deploy: bool

    @model_validator(mode="after")
    def canonical_targets(self) -> Self:
        keys = tuple(target.key for target in self.targets)
        if keys != tuple(sorted(set(keys))):
            raise ValueError("targets must be unique and sorted by key")
        expected_keys = tuple(
            f"{self.service}:{target.host_port}/{target.protocol}"
            for target in self.targets
        )
        if keys != expected_keys:
            raise ValueError("every target key must exactly name its service socket")
        bind_envs = {target.bind_env for target in self.targets}
        if set(self.assignments) != bind_envs:
            raise ValueError(
                "assignment keys must exactly match target bind_env values"
            )
        for target in self.targets:
            assigned = normalise_bind(self.assignments[target.bind_env])
            if assigned != str(target.bind):
                raise ValueError("assignment value and target bind must agree")
        return self

    @classmethod
    def from_declared(
        cls, declared: DeclaredPublishedPortPlan
    ) -> PublishedPortIntentV1:
        return cls(
            service=declared.service,
            environment=declared.environment,
            assignments={item.key: item.value for item in declared.assignments},
            targets=tuple(
                PublishedPortTargetV1(
                    key=item.key,
                    host_port=item.host_port,
                    container_port=item.container_port,
                    protocol=item.protocol,
                    bind_env=item.bind_env,
                    bind=normalise_bind(item.bind),
                    expected_listeners=item.expected_listeners,
                    required_clients=tuple(sorted(item.required_clients)),
                )
                for item in declared.targets
            ),
            recreated_by_deploy=declared.recreated_by_deploy,
        )


class PublishedPortObservedListenerV1(StrictContract):
    container_port: int = Field(ge=1, le=65535)
    host_ip: IPvAnyAddress
    host_port: int = Field(ge=1, le=65535)
    protocol: Literal["tcp", "udp"]


class PublishedPortProjectContainerV1(StrictContract):
    """Identity of one project container: what it is, not what it came from.

    This is the whole of the non-target observation contract.  A non-target is
    never recreated by this operation, so its provenance is not a property the
    operation can promise anything about; what must hold is that it is the
    SAME RUNNING CONTAINER before and after, and a container ID proves that
    strictly better than an image reference does.  There is deliberately no
    image field here, so a non-target's mutable tag is not merely tolerated --
    it is unrepresentable, and therefore cannot be borrowed as evidence.
    """

    service: ServiceName
    container: ContainerName
    container_id: ContainerId


class PublishedPortContainerObservationV2(StrictContract):
    """Secret-free normalized subset collected from the TARGET container.

    The target is the one container this operation destroys and recreates, so
    it is the only one whose image identity must be immutable: a tag could
    resolve to different bytes between the plan and the recreate.  Requiring
    the same of a non-target would fail PLAN for services this operation never
    touches -- see ``PublishedPortProjectContainerV1``.
    """

    compose_project: Literal["dotmac_sub"] = "dotmac_sub"
    service: ServiceName
    container: ContainerName
    container_id: ContainerId
    image_id: Sha256Digest
    image_reference: ImageReference
    listeners: tuple[PublishedPortObservedListenerV1, ...]

    @model_validator(mode="after")
    def listener_rows_are_canonical(self) -> Self:
        keys = tuple(
            (row.container_port, str(row.host_ip), row.host_port, row.protocol)
            for row in self.listeners
        )
        if keys != tuple(sorted(set(keys))):
            raise ValueError("container listeners must be unique and sorted")
        return self

    def identity(self) -> PublishedPortProjectContainerV1:
        return PublishedPortProjectContainerV1(
            service=self.service,
            container=self.container,
            container_id=self.container_id,
        )


class PublishedPortHostSnapshotV2(StrictContract):
    """Safe output of the root-owned, read-only Docker observer.

    The two observation contracts are split rather than uniform: ``target``
    carries immutable image identity and listeners, ``non_targets`` carry
    identity alone.
    """

    schema_id: Literal["PublishedPortHostSnapshotV2"] = Field(
        default=HOST_SNAPSHOT_SCHEMA, alias="schema"
    )
    target_server_name: Literal["dotmac-sub-prod"] = "dotmac-sub-prod"
    service: ServiceName
    observer_digest: Sha256Digest
    non_port_projection: Literal["DockerComposeServiceProjectionV1"] = (
        "DockerComposeServiceProjectionV1"
    )
    non_port_definition_digest: Sha256Digest
    effective_image_reference: ImageReference
    target: PublishedPortContainerObservationV2
    non_targets: tuple[PublishedPortProjectContainerV1, ...]

    @model_validator(mode="after")
    def snapshot_is_complete_and_canonical(self) -> Self:
        if self.target.service != self.service:
            raise ValueError("snapshot target does not name the observed service")
        keys = tuple(
            (item.service, item.container, item.container_id)
            for item in self.non_targets
        )
        if keys != tuple(sorted(set(keys))):
            raise ValueError("snapshot non-targets must be unique and sorted")
        if any(item.service == self.service for item in self.non_targets):
            raise ValueError("the target service may not appear among non-targets")
        if self.target.container_id in {item.container_id for item in self.non_targets}:
            raise ValueError("the target container may not appear among non-targets")
        return self

    def project_containers(self) -> tuple[PublishedPortProjectContainerV1, ...]:
        """The complete container-identity map, target included."""

        return tuple(
            sorted(
                (self.target.identity(), *self.non_targets),
                key=lambda item: (item.service, item.container, item.container_id),
            )
        )


class PublishedPortPrestateV1(StrictContract):
    """Typed host readback that makes a plan stale when the host changes."""

    target_container_id: ContainerId
    target_image_digest: Sha256Digest
    listeners: tuple[PublishedPortObservedListenerV1, ...] = Field(min_length=1)
    non_port_projection: Literal["DockerComposeServiceProjectionV1"] = (
        "DockerComposeServiceProjectionV1"
    )
    non_port_definition_digest: Sha256Digest
    project_containers: tuple[PublishedPortProjectContainerV1, ...] = Field(
        min_length=1
    )

    @model_validator(mode="after")
    def canonical_collections(self) -> Self:
        listener_keys = tuple(
            (item.container_port, str(item.host_ip), item.host_port, item.protocol)
            for item in self.listeners
        )
        if listener_keys != tuple(sorted(set(listener_keys))):
            raise ValueError("observed listeners must be unique and sorted")
        container_keys = tuple(
            (item.service, item.container, item.container_id)
            for item in self.project_containers
        )
        if container_keys != tuple(sorted(set(container_keys))):
            raise ValueError("project container map must be unique and sorted")
        if self.target_container_id not in {
            item.container_id for item in self.project_containers
        }:
            raise ValueError("target container id must appear in the project map")
        return self


class PublishedPortPlanV1(StrictContract):
    """Immutable protected-main decision plus the exact observed host prestate."""

    schema_id: Literal["PublishedPortPlanV1"] = Field(
        default=PLAN_SCHEMA, alias="schema"
    )
    repository: Literal["michaelayoade/dotmac_sub"] = "michaelayoade/dotmac_sub"
    workflow: Literal[".github/workflows/infrastructure-reconcile-plan.yml"] = (
        PLAN_WORKFLOW
    )
    protected_ref: Literal["refs/heads/main"] = PROTECTED_REF
    source_sha: GitSha
    target_server_name: TargetName
    change_reference: NonEmpty
    reason: NonEmpty
    declaration_digest: Sha256Digest
    compose_digest: Sha256Digest
    intent: PublishedPortIntentV1
    prestate: PublishedPortPrestateV1

    @model_validator(mode="after")
    def production_binding_is_coherent(self) -> Self:
        if self.target_server_name != "dotmac-sub-prod":
            raise ValueError("target_server_name must be dotmac-sub-prod")
        if self.intent.environment != "production":
            raise ValueError("the production target requires production intent")
        target_rows = tuple(
            item
            for item in self.prestate.project_containers
            if item.service == self.intent.service
        )
        if len(target_rows) != 1:
            raise ValueError(
                "prestate must contain exactly one target service container"
            )
        if target_rows[0].container_id != self.prestate.target_container_id:
            raise ValueError("target service row and target container id differ")
        return self

    def prestate_digest(self) -> str:
        return self.prestate.canonical_digest()


class PublishedPortPlanReceiptV1(StrictContract):
    """Run evidence for a plan; matching evidence is not apply authorization."""

    schema_id: Literal["PublishedPortPlanReceiptV1"] = Field(
        default=RECEIPT_SCHEMA, alias="schema"
    )
    repository: Literal["michaelayoade/dotmac_sub"] = "michaelayoade/dotmac_sub"
    workflow: Literal[".github/workflows/infrastructure-reconcile-plan.yml"] = (
        PLAN_WORKFLOW
    )
    protected_ref: Literal["refs/heads/main"] = PROTECTED_REF
    source_sha: GitSha
    run_id: int = Field(gt=0)
    run_attempt: Literal[1] = 1
    artifact_name: NonEmpty
    artifact_file: Literal["plan.json"] = "plan.json"
    plan_digest: Sha256Digest
    prestate_digest: Sha256Digest

    @classmethod
    def for_plan(
        cls,
        *,
        plan: PublishedPortPlanV1,
        run_id: int,
    ) -> PublishedPortPlanReceiptV1:
        return cls(
            source_sha=plan.source_sha,
            run_id=run_id,
            artifact_name=(
                f"published-port-plan-v1-{plan.intent.service}-{plan.source_sha}"
            ),
            plan_digest=plan.canonical_digest(),
            prestate_digest=plan.prestate_digest(),
        )


def verify_receipt_for_plan(
    receipt: PublishedPortPlanReceiptV1,
    plan: PublishedPortPlanV1,
) -> None:
    if not hmac.compare_digest(receipt.plan_digest, plan.canonical_digest()):
        raise CanonicalContractError("plan receipt does not bind the exact plan bytes")
    if not hmac.compare_digest(receipt.prestate_digest, plan.prestate_digest()):
        raise CanonicalContractError("plan receipt does not bind the exact prestate")
    if receipt.source_sha != plan.source_sha:
        raise CanonicalContractError("plan receipt source SHA differs")
    expected_name = f"published-port-plan-v1-{plan.intent.service}-{receipt.source_sha}"
    if receipt.artifact_name != expected_name:
        raise CanonicalContractError("plan receipt artifact identity differs")


class PublishedPortClientObligationV2(StrictContract):
    """One client/network path that must survive the target-only recreate."""

    target_key: NonEmpty
    client_network: IPvAnyNetwork


class PublishedPortExecutionPlanV2(StrictContract):
    """The v1 decision plus immutable execution and proof coordinates."""

    schema_id: Literal["PublishedPortExecutionPlanV2"] = Field(
        default=EXECUTION_PLAN_SCHEMA, alias="schema"
    )
    plan: PublishedPortPlanV1
    compose_project: Literal["dotmac_sub"] = "dotmac_sub"
    plan_observer_digest: Sha256Digest
    target_image_reference: ImageReference
    target_image_id: Sha256Digest
    client_obligations: tuple[PublishedPortClientObligationV2, ...] = Field(
        min_length=1
    )

    @model_validator(mode="after")
    def execution_coordinates_are_derived(self) -> Self:
        image_digest = self.target_image_reference.rsplit("@", 1)[1]
        if image_digest != self.plan.prestate.target_image_digest:
            raise ValueError("target image reference and prestate digest differ")
        expected = tuple(
            sorted(
                (
                    PublishedPortClientObligationV2(
                        target_key=target.key,
                        client_network=network,
                    )
                    for target in self.plan.intent.targets
                    for network in target.required_clients
                ),
                key=lambda item: (item.target_key, str(item.client_network)),
            )
        )
        if self.client_obligations != expected:
            raise ValueError(
                "client obligations must exactly derive from declared required clients"
            )
        return self


class PublishedPortPlanArtifactReceiptV2(StrictContract):
    """A plan-job artifact receipt; terminal run status is observed later."""

    schema_id: Literal["PublishedPortPlanArtifactReceiptV2"] = Field(
        default=ARTIFACT_RECEIPT_SCHEMA, alias="schema"
    )
    receipt: PublishedPortPlanReceiptV1
    execution_plan_digest: Sha256Digest
    planned_at: datetime
    expires_at: datetime

    @model_validator(mode="after")
    def bounded_freshness(self) -> Self:
        _require_utc(self.planned_at, "planned_at")
        _require_utc(self.expires_at, "expires_at")
        if self.expires_at <= self.planned_at:
            raise ValueError("plan artifact expiry must follow planning")
        if self.expires_at - self.planned_at > timedelta(hours=1):
            raise ValueError("plan artifact freshness may not exceed one hour")
        return self


class PublishedPortPlanRunObservationV2(StrictContract):
    """Reduced GitHub run observation made only after the plan run terminates."""

    schema_id: Literal["PublishedPortPlanRunObservationV2"] = Field(
        default=RUN_OBSERVATION_SCHEMA, alias="schema"
    )
    repository: Literal["michaelayoade/dotmac_sub"] = "michaelayoade/dotmac_sub"
    workflow: Literal[".github/workflows/infrastructure-reconcile-plan.yml"] = (
        PLAN_WORKFLOW
    )
    protected_ref: Literal["refs/heads/main"] = PROTECTED_REF
    source_sha: GitSha
    run_id: int = Field(gt=0)
    run_attempt: Literal[1] = 1
    event: Literal["workflow_dispatch"] = "workflow_dispatch"
    status: Literal["completed"] = "completed"
    conclusion: Literal[EvidenceConclusion.SUCCESS] = EvidenceConclusion.SUCCESS
    created_at: datetime
    completed_at: datetime

    @model_validator(mode="after")
    def terminal_times_are_coherent(self) -> Self:
        _require_utc(self.created_at, "created_at")
        _require_utc(self.completed_at, "completed_at")
        if self.completed_at < self.created_at:
            raise ValueError("completed_at precedes created_at")
        return self


class PublishedPortApplyAdmissionV2(StrictContract):
    """Exact evidence admitted by the hosted half of the apply workflow."""

    schema_id: Literal["PublishedPortApplyAdmissionV2"] = Field(
        default=APPLY_ADMISSION_SCHEMA, alias="schema"
    )
    repository: Literal["michaelayoade/dotmac_sub"] = "michaelayoade/dotmac_sub"
    workflow: Literal[".github/workflows/infrastructure-reconcile-apply.yml"] = (
        APPLY_WORKFLOW
    )
    protected_ref: Literal["refs/heads/main"] = PROTECTED_REF
    source_sha: GitSha
    apply_run_id: int = Field(gt=0)
    target_service: ServiceName
    operation_id: OperationId
    execution_plan_digest: Sha256Digest
    firewall_verifier_identity: NonEmpty
    client_collector_identity: NonEmpty
    artifact_receipt_digests: tuple[Sha256Digest, Sha256Digest]
    plan_runs: tuple[
        PublishedPortPlanRunObservationV2, PublishedPortPlanRunObservationV2
    ]
    admitted_at: datetime
    expires_at: datetime

    @model_validator(mode="after")
    def admission_is_coherent(self) -> Self:
        _require_utc(self.admitted_at, "admitted_at")
        _require_utc(self.expires_at, "expires_at")
        if self.expires_at <= self.admitted_at:
            raise ValueError("apply admission expiry must follow admission")
        if self.expires_at - self.admitted_at > timedelta(minutes=30):
            raise ValueError("apply admission may not remain live for over 30 minutes")
        run_ids = tuple(run.run_id for run in self.plan_runs)
        if len(set(run_ids)) != 2:
            raise ValueError("two distinct plan run IDs are required")
        if run_ids != tuple(sorted(run_ids)):
            raise ValueError("plan runs must be sorted by run ID")
        if len(set(self.artifact_receipt_digests)) != 2:
            raise ValueError("two distinct artifact receipts are required")
        for run in self.plan_runs:
            if run.source_sha != self.source_sha:
                raise ValueError("plan run source differs from apply source")
            if run.completed_at > self.admitted_at:
                raise ValueError("plan run was not terminal at admission")
        expected_operation = f"port-{self.target_service}-{self.apply_run_id}"
        if self.operation_id != expected_operation:
            raise ValueError("operation ID must bind the service and apply run ID")
        if self.firewall_verifier_identity == self.client_collector_identity:
            raise ValueError(
                "firewall policy and external reach must have independent identities"
            )
        return self

    def require_fresh(self, now: datetime | None = None) -> None:
        observed = now or datetime.now(UTC)
        _require_utc(observed, "now")
        if observed > self.expires_at:
            raise CanonicalContractError("apply admission expired")


class PublishedPortFirewallProofV2(StrictContract):
    """A sanitized proof that host policy admits one declared client path."""

    schema_id: Literal["PublishedPortFirewallProofV2"] = Field(
        default=FIREWALL_PROOF_SCHEMA, alias="schema"
    )
    operation_id: OperationId
    execution_plan_digest: Sha256Digest
    target_key: NonEmpty
    client_network: IPvAnyNetwork
    verifier_identity: NonEmpty
    ruleset_digest: Sha256Digest
    verdict: Literal[ProofVerdict.ADMITTED] = ProofVerdict.ADMITTED
    observed_at: datetime

    @model_validator(mode="after")
    def observed_in_utc(self) -> Self:
        _require_utc(self.observed_at, "observed_at")
        return self


class PublishedPortClientReachProofV2(StrictContract):
    """An external-vantage success receipt; the target host cannot mint it."""

    schema_id: Literal["PublishedPortClientReachProofV2"] = Field(
        default=CLIENT_REACH_PROOF_SCHEMA, alias="schema"
    )
    operation_id: OperationId
    execution_plan_digest: Sha256Digest
    target_key: NonEmpty
    client_network: IPvAnyNetwork
    collector_identity: NonEmpty
    collector_evidence_digest: Sha256Digest
    verdict: Literal[ProofVerdict.REACHABLE] = ProofVerdict.REACHABLE
    observed_at: datetime

    @model_validator(mode="after")
    def observed_in_utc(self) -> Self:
        _require_utc(self.observed_at, "observed_at")
        return self


class PublishedPortEnvPreimageV2(StrictContract):
    key: EnvironmentKey
    present: bool
    value: str

    @model_validator(mode="after")
    def absent_means_empty(self) -> Self:
        if not self.present and self.value:
            raise ValueError("an absent environment key has no prior value")
        return self


class PublishedPortDeadmanStateV2(StrictContract):
    """Root-local rollback state; it contains only non-secret bind values."""

    schema_id: Literal["PublishedPortDeadmanStateV2"] = Field(
        default=DEADMAN_STATE_SCHEMA, alias="schema"
    )
    operation_id: OperationId
    execution_plan_digest: Sha256Digest
    service: ServiceName
    deploy_dir: NonEmpty
    env_file: NonEmpty
    docker_bin: NonEmpty
    compose_files: tuple[NonEmpty, ...] = Field(min_length=1)
    image_reference: ImageReference
    before_image_id: Sha256Digest
    env_preimage: tuple[PublishedPortEnvPreimageV2, ...] = Field(min_length=1)
    before_container_id: ContainerId
    before_listeners: tuple[PublishedPortObservedListenerV1, ...]
    deadline: datetime
    state: Literal["armed", "rolled_back", "disarmed"] = "armed"
    state_reason: str | None = None
    updated_at: datetime

    @model_validator(mode="after")
    def state_is_safe_and_canonical(self) -> Self:
        _require_utc(self.deadline, "deadline")
        _require_utc(self.updated_at, "updated_at")
        if (
            not self.deploy_dir.startswith("/")
            or not self.env_file.startswith("/")
            or not self.docker_bin.startswith("/")
        ):
            raise ValueError("deadman paths must be absolute")
        if any(not path.startswith("/") for path in self.compose_files):
            raise ValueError("deadman compose paths must be absolute")
        keys = tuple(item.key for item in self.env_preimage)
        if keys != tuple(sorted(set(keys))):
            raise ValueError("deadman env preimage must be unique and sorted")
        listener_keys = tuple(
            (item.container_port, str(item.host_ip), item.host_port, item.protocol)
            for item in self.before_listeners
        )
        if listener_keys != tuple(sorted(set(listener_keys))):
            raise ValueError("deadman listeners must be unique and sorted")
        if self.state == "armed" and self.state_reason is not None:
            raise ValueError("an armed deadman has no terminal reason")
        if self.state != "armed" and not self.state_reason:
            raise ValueError("a terminal deadman state requires a reason")
        return self


class AddressFamilyProofV2(StrictContract):
    family: Literal["ipv4", "ipv6"]
    expected: tuple[IPvAnyAddress, ...]
    observed: tuple[IPvAnyAddress, ...]
    matched: Literal[True] = True

    @model_validator(mode="after")
    def exact_family_match(self) -> Self:
        if self.expected != self.observed:
            raise ValueError("address-family listener proof is not exact")
        return self


class PublishedPortPostconditionVerdictV2(StrictContract):
    """All success proofs required before the deadman may be disarmed."""

    schema_id: Literal["PublishedPortPostconditionVerdictV2"] = Field(
        default=POSTCONDITION_VERDICT_SCHEMA, alias="schema"
    )
    operation_id: OperationId
    source_sha: GitSha
    execution_plan_digest: Sha256Digest
    apply_run_id: int = Field(gt=0)
    target_service: ServiceName
    before_target_container_id: ContainerId
    after_target_container_id: ContainerId
    target_image_id: Sha256Digest
    unchanged_non_target_container_digest: Sha256Digest
    non_port_definition_digest: Sha256Digest
    address_families: tuple[AddressFamilyProofV2, AddressFamilyProofV2]
    firewall_proof_digests: tuple[Sha256Digest, ...] = Field(min_length=1)
    client_reach_proof_digests: tuple[Sha256Digest, ...] = Field(min_length=1)
    verified_at: datetime
    verdict: Literal["postconditions_proved"] = "postconditions_proved"

    @model_validator(mode="after")
    def successful_recreate_is_observed(self) -> Self:
        _require_utc(self.verified_at, "verified_at")
        if self.before_target_container_id == self.after_target_container_id:
            raise ValueError("target container ID did not change")
        families = tuple(proof.family for proof in self.address_families)
        if families != ("ipv4", "ipv6"):
            raise ValueError("address-family proofs must cover ipv4 then ipv6")
        return self


class PublishedPortApplyOutcomeV2(StrictContract):
    """Sanitized terminal receipt written only after the deadman is disarmed."""

    schema_id: Literal["PublishedPortApplyOutcomeV2"] = Field(
        default=APPLY_OUTCOME_SCHEMA, alias="schema"
    )
    postconditions: PublishedPortPostconditionVerdictV2
    deadman_state_digest: Sha256Digest
    deadman_disarmed_at: datetime
    outcome: Literal["applied"] = "applied"

    @model_validator(mode="after")
    def disarm_follows_proof(self) -> Self:
        _require_utc(self.deadman_disarmed_at, "deadman_disarmed_at")
        if self.deadman_disarmed_at < self.postconditions.verified_at:
            raise ValueError("deadman was disarmed before postconditions were proved")
        return self
