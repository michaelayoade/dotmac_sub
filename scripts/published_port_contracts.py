"""Strict wire contracts for published-port plans and their run receipts."""

from __future__ import annotations

import hashlib
import hmac
import json
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
PLAN_WORKFLOW = ".github/workflows/infrastructure-reconcile-plan.yml"
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
    service: ServiceName
    container: ContainerName
    container_id: ContainerId


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
