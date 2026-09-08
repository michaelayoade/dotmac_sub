"""Wire contracts for the structurally single-use legacy image-pin bootstrap.

WHY A SEPARATE FAMILY OF CONTRACTS EXISTS
=========================================

The steady-state published-port reconcile (v2) requires the service it is
about to recreate to already carry an immutable ``name@sha256:...`` reference.
That requirement is correct and is NOT relaxed here: a mutable tag can resolve
to different bytes between the moment a plan is made and the moment a container
is recreated, so a tag plus an image ID is not admissible PLAN evidence.

But ``postgres-local`` is currently tag-pinned, so v2 can never take its own
first step.  This module owns the one-time bootstrap that carries the service
from the legacy tag to the exact digest of the bytes ALREADY RUNNING, so that
ordinary v2 PLAN/APPLY becomes possible afterwards.

Two properties keep that from becoming a hole in the steady-state rule:

*   These are a DIFFERENT schema family.  A ``LegacyImagePinBootstrapSnapshotV1``
    cannot be handed to the v2 planner, which reads only
    ``PublishedPortHostSnapshotV2``; the strict contracts refuse each other's
    bytes.  The tag is admissible here and nowhere else.
*   The bootstrap is structurally single-use.  It admits only the exact legacy
    prestate, and a terminal root-owned receipt permanently refuses a second
    run.  See ``LegacyImagePinBootstrapReceiptV1``.

The digest is taken from the RUNNING image's own registry digest and is proved
to resolve locally to the running image ID.  It is never resolved by asking a
registry what the mutable tag means now -- that could name a NEWER image, and
adopting it would silently schedule an upgrade inside a containment change.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from typing import Annotated, Literal, Self

from pydantic import (
    Field,
    IPvAnyAddress,
    IPvAnyNetwork,
    StringConstraints,
    model_validator,
)

from scripts.published_port_contracts import (
    ContainerId,
    GitSha,
    NonEmpty,
    PublishedPortObservedListenerV1,
    PublishedPortProjectContainerV1,
    Sha256Digest,
    StrictContract,
    _require_utc,
)

BOOTSTRAP_SNAPSHOT_SCHEMA = "LegacyImagePinBootstrapSnapshotV1"
BOOTSTRAP_PLAN_SCHEMA = "LegacyImagePinBootstrapPlanV1"
BOOTSTRAP_ADMISSION_SCHEMA = "LegacyImagePinBootstrapAdmissionV1"
BOOTSTRAP_DEADMAN_SCHEMA = "LegacyImagePinBootstrapDeadmanStateV1"
BOOTSTRAP_RECEIPT_SCHEMA = "LegacyImagePinBootstrapReceiptV1"
BOOTSTRAP_OPERATION_SCHEMA = "LegacyImagePinBootstrapOperationV1"
BOOTSTRAP_RESOLUTION_SCHEMA = "LegacyImagePinLocalResolutionV1"

BOOTSTRAP_PLAN_WORKFLOW = ".github/workflows/legacy-image-pin-bootstrap-plan.yml"
BOOTSTRAP_APPLY_WORKFLOW = ".github/workflows/legacy-image-pin-bootstrap-apply.yml"
PROTECTED_REF = "refs/heads/main"

# The one physical production host this bootstrap may ever touch. Michael named
# it explicitly; binding it here means a plan built for any other machine is
# refused by the contract rather than by an operator's memory.
PRODUCTION_HOST = "94.72.107.76"
PRODUCTION_LOGIN = "root@94.72.107.76"
PRODUCTION_SERVER_NAME = "dotmac-sub-prod"

# The only service in scope. FreeRADIUS is deliberately excluded: it gets the
# same generic facility later, with its own digest, plans, proofs and window.
BOOTSTRAP_SERVICE = "postgres-local"

# The declared IPv4 wildcard. Binding every IPv4 address is DELIBERATE and is
# the corrected state, not the defect: the standby streams WAL from another
# host, so loopback is unavailable, and exposure is source-restricted to that
# one address by a host DOCKER-USER rule. The defect being corrected is the
# UNDECLARED second listener on [::], which no rule in that chain can reach.
DECLARED_IPV4_WILDCARD = "0.0.0.0"  # noqa: S104
DECLARED_HOST_PORT = 9001
DECLARED_CONTAINER_PORT = 5432

# A mutable tag reference: a name, a ':' tag, and explicitly NO '@sha256:'.
# This is the only contract family in the repository that admits one at all.
LegacyImageTag = Annotated[
    str,
    StringConstraints(
        pattern=r"^[a-z0-9]+(?:[._\-/][a-z0-9]+)*:[A-Za-z0-9_][A-Za-z0-9._\-]{0,127}$"
    ),
]
ImageDigestReference = Annotated[
    str, StringConstraints(pattern=r"^[^\s@]+@sha256:[0-9a-f]{64}$")
]
BootstrapOperationId = Annotated[
    str, StringConstraints(pattern=r"^imagepin-[a-z0-9-]+-[1-9][0-9]*$")
]


def _repository_of(reference: str) -> str:
    """The repository name, with the tag or digest removed."""

    if "@" in reference:
        return reference.rsplit("@", 1)[0]
    return reference.rsplit(":", 1)[0]


class LegacyImagePinLocalResolutionV1(StrictContract):
    """Proof that the desired digest names the bytes that are already running.

    This is the measurement Michael made load-bearing: ``docker image inspect
    <desired digest>`` must resolve, on the target host and with no pull, to
    the SAME image ID the running container reports.  If it does not, the
    bootstrap stops rather than adopting whatever digest the mutable tag
    currently points at.

    The resolved image ID is deliberately NOT required to equal the digest in
    the reference.  Whether those coincide is a property of the host's image
    store -- with containerd they are the same manifest digest, with the
    classic store they are the config digest and the manifest digest and they
    differ -- and encoding one store's accident would make the contract refuse
    a correct host.
    """

    schema_id: Literal["LegacyImagePinLocalResolutionV1"] = Field(
        default=BOOTSTRAP_RESOLUTION_SCHEMA, alias="schema"
    )
    reference: ImageDigestReference
    resolved_image_id: Sha256Digest
    running_image_id: Sha256Digest
    pulled: Literal[False] = False

    @model_validator(mode="after")
    def resolution_binds_the_running_bytes(self) -> Self:
        if self.resolved_image_id != self.running_image_id:
            raise ValueError(
                "the desired digest does not resolve to the running image ID"
            )
        return self


class LegacyImagePinDeployedFileV1(StrictContract):
    """One Compose file as it exists ON THE HOST, with the bytes' digest.

    This is a CURRENT-state fact. The host's deployed tree is the authority for
    what production actually is; an Actions checkout must never masquerade as
    observed production state.
    """

    path: NonEmpty
    digest: Sha256Digest

    @model_validator(mode="after")
    def deployed_paths_are_absolute(self) -> Self:
        if not self.path.startswith("/"):
            raise ValueError("a deployed Compose path must be absolute")
        return self


def overlay_document(image_reference: str) -> dict[str, object]:
    """The single Compose overlay this operation adds: the digest reference."""

    return {"services": {BOOTSTRAP_SERVICE: {"image": image_reference}}}


def overlay_digest(image_reference: str) -> str:
    raw = (
        json.dumps(
            overlay_document(image_reference),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        + "\n"
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


class LegacyImagePinBindKnobProofV1(StrictContract):
    """Proof that setting the bind variable actually moves the listener.

    WHY THIS EXISTS
    ===============

    Measured on dotmac-sub-prod on 2026-09-01, the DEPLOYED Compose file
    publishes ``- 9001:5432`` -- a bare publish with no ``${PG_LOCAL_BIND}``
    interpolation at all.  ``main`` carries the knob; the host is running an
    older release that does not.  Against that file, setting
    ``PG_LOCAL_BIND=0.0.0.0:`` changes NOTHING: the recreate would faithfully
    reproduce the same dual-family publish, the containment defect would
    survive the maintenance window, and the only thing that would notice is
    the deadman rolling the change back on the listener postcondition.

    A plan that assumes a knob is wired, when the file it will actually be
    applied to has no such knob, is a plan that cannot do what it says.  So
    the bootstrap proves the knob instead of assuming it: the observer renders
    the effective Compose projection three times -- once as-is, once with the
    variable set to the desired IPv4 wildcard, and once with it set to
    loopback -- and this contract refuses unless the two injections produce
    two DIFFERENT, exactly-predicted bindings.

    One injection would not be enough.  A file that hardcodes ``0.0.0.0:``
    would satisfy a single ``0.0.0.0`` probe while being just as unresponsive
    to the variable.  The loopback probe is the control that makes the first
    result mean something.
    """

    schema_id: Literal["LegacyImagePinBindKnobProofV1"] = Field(
        default="LegacyImagePinBindKnobProofV1", alias="schema"
    )
    env_key: Literal["PG_LOCAL_BIND"] = "PG_LOCAL_BIND"
    wildcard_injection: Literal["0.0.0.0:"] = "0.0.0.0:"
    wildcard_host_ip: IPvAnyAddress
    control_injection: Literal["127.0.0.1:"] = "127.0.0.1:"
    control_host_ip: IPvAnyAddress
    # Rendered with the host's REAL environment and no injection: what the
    # deployed file plus the deployed .env actually resolve to right now.
    current_host_ip: IPvAnyAddress
    host_port: Literal[9001] = DECLARED_HOST_PORT
    container_port: Literal[5432] = DECLARED_CONTAINER_PORT
    protocol: Literal["tcp"] = "tcp"

    @model_validator(mode="after")
    def the_knob_is_live_and_the_proof_is_falsifiable(self) -> Self:
        if str(self.wildcard_host_ip) != DECLARED_IPV4_WILDCARD:
            raise ValueError(
                "setting the bind variable to the IPv4 wildcard did not produce "
                "an IPv4 wildcard binding; the deployed Compose file does not "
                "interpolate this variable and the bootstrap cannot correct the "
                "listener through it"
            )
        if str(self.control_host_ip) != "127.0.0.1":
            raise ValueError(
                "the loopback control injection did not move the binding; the "
                "observed wildcard binding is hardcoded, not variable-driven"
            )
        # THE STAGING HAZARD, refused at plan time rather than discovered at
        # 03:00. The release publishes ${PG_LOCAL_BIND:-127.0.0.1:}9001:5432.
        # PG_LOCAL_BIND is absent from the production .env, so the moment the
        # release Compose is staged the file resolves to LOOPBACK -- and any
        # recreate after that, by this operation or by anything else, cuts the
        # replication standby off from a port it is streaming WAL through.
        # Staging must therefore set the variable, and a plan cannot be built
        # against a host where it has not been set.
        if str(self.current_host_ip) != DECLARED_IPV4_WILDCARD:
            raise ValueError(
                "the deployed Compose and .env currently resolve this publish to "
                f"{self.current_host_ip}, which does not admit the replication "
                "standby; set PG_LOCAL_BIND when staging the release, before any "
                "recreate can strand it"
            )
        return self


class LegacyImagePinBootstrapSnapshotV1(StrictContract):
    """Safe output of the root-owned, read-only bootstrap observer.

    Distinct from ``PublishedPortHostSnapshotV2`` so that it can never be fed
    to the steady-state planner: this is the only snapshot type in the tree
    whose target may carry a mutable tag.
    """

    schema_id: Literal["LegacyImagePinBootstrapSnapshotV1"] = Field(
        default=BOOTSTRAP_SNAPSHOT_SCHEMA, alias="schema"
    )
    target_server_name: Literal["dotmac-sub-prod"] = PRODUCTION_SERVER_NAME
    service: Literal["postgres-local"] = BOOTSTRAP_SERVICE
    observer_digest: Sha256Digest
    legacy_image_reference: LegacyImageTag
    desired_image_reference: ImageDigestReference
    resolution: LegacyImagePinLocalResolutionV1
    target_container_id: ContainerId
    target_image_id: Sha256Digest
    volume_identity_digest: Sha256Digest
    listeners: tuple[PublishedPortObservedListenerV1, ...] = Field(min_length=1)
    non_port_projection: Literal["DockerComposeServiceProjectionV1"] = (
        "DockerComposeServiceProjectionV1"
    )
    non_port_definition_digest: Sha256Digest
    image_free_definition_digest: Sha256Digest
    effective_image_reference: LegacyImageTag
    deployed_compose_files: tuple[LegacyImagePinDeployedFileV1, ...] = Field(
        min_length=1
    )
    bind_knob: LegacyImagePinBindKnobProofV1
    non_targets: tuple[PublishedPortProjectContainerV1, ...]

    @model_validator(mode="after")
    def prestate_is_the_exact_legacy_shape(self) -> Self:
        if _repository_of(self.legacy_image_reference) != _repository_of(
            self.desired_image_reference
        ):
            raise ValueError(
                "the desired digest must name the same repository as the legacy tag"
            )
        if self.effective_image_reference != self.legacy_image_reference:
            raise ValueError(
                "effective Compose image is not the exact observed legacy tag"
            )
        if self.resolution.reference != self.desired_image_reference:
            raise ValueError("the local resolution names another image reference")
        if self.resolution.running_image_id != self.target_image_id:
            raise ValueError("the local resolution names another running image")
        listener_keys = tuple(
            (row.container_port, str(row.host_ip), row.host_port, row.protocol)
            for row in self.listeners
        )
        if listener_keys != tuple(sorted(set(listener_keys))):
            raise ValueError("observed listeners must be unique and sorted")
        non_target_keys = tuple(
            (row.service, row.container, row.container_id) for row in self.non_targets
        )
        if non_target_keys != tuple(sorted(set(non_target_keys))):
            raise ValueError("snapshot non-targets must be unique and sorted")
        if any(row.service == self.service for row in self.non_targets):
            raise ValueError("the target service may not appear among non-targets")
        if self.target_container_id in {row.container_id for row in self.non_targets}:
            raise ValueError("the target container may not appear among non-targets")
        paths = tuple(row.path for row in self.deployed_compose_files)
        if paths != tuple(sorted(set(paths))):
            raise ValueError("deployed Compose files must be unique and sorted")
        return self


class LegacyImagePinBootstrapOperationV1(StrictContract):
    """Exactly what the bootstrap is permitted to mutate, and nothing else."""

    schema_id: Literal["LegacyImagePinBootstrapOperationV1"] = Field(
        default=BOOTSTRAP_OPERATION_SCHEMA, alias="schema"
    )
    service: Literal["postgres-local"] = BOOTSTRAP_SERVICE
    bind_env: Literal["PG_LOCAL_BIND"] = "PG_LOCAL_BIND"
    desired_bind: Literal["0.0.0.0:"] = "0.0.0.0:"
    host_port: Literal[9001] = 9001
    container_port: Literal[5432] = 5432
    protocol: Literal["tcp"] = "tcp"
    desired_image_reference: ImageDigestReference
    recreate_flags: tuple[str, ...] = (
        "--no-deps",
        "--no-build",
        "--pull",
        "never",
        "--force-recreate",
    )

    @model_validator(mode="after")
    def the_recreate_can_never_resolve_or_widen(self) -> Self:
        if self.recreate_flags != (
            "--no-deps",
            "--no-build",
            "--pull",
            "never",
            "--force-recreate",
        ):
            raise ValueError("the bootstrap recreate flags are fixed")
        return self


class LegacyImagePinBootstrapPlanV1(StrictContract):
    """The immutable protected-main decision for the one-time image pin.

    Every coordinate the eventual maintenance window needs is bound here, so
    that APPLY can compare a freshly re-observed prestate against the plan
    byte for byte rather than re-deriving anything under the lock.
    """

    schema_id: Literal["LegacyImagePinBootstrapPlanV1"] = Field(
        default=BOOTSTRAP_PLAN_SCHEMA, alias="schema"
    )
    repository: Literal["michaelayoade/dotmac_sub"] = "michaelayoade/dotmac_sub"
    workflow: Literal[".github/workflows/legacy-image-pin-bootstrap-plan.yml"] = (
        BOOTSTRAP_PLAN_WORKFLOW
    )
    protected_ref: Literal["refs/heads/main"] = PROTECTED_REF
    source_sha: GitSha
    production_host: Literal["94.72.107.76"] = PRODUCTION_HOST
    production_login: Literal["root@94.72.107.76"] = PRODUCTION_LOGIN
    target_server_name: Literal["dotmac-sub-prod"] = PRODUCTION_SERVER_NAME
    service: Literal["postgres-local"] = BOOTSTRAP_SERVICE

    change_reference: NonEmpty
    reason: NonEmpty
    declaration_digest: Sha256Digest
    observer_digest: Sha256Digest

    # ---- CURRENT: what is deployed and running now. Measured from the host,
    # and revalidated under the lock at apply. A plan whose current input has
    # moved describes a host that no longer exists and is refused, not warned
    # about -- staging the release that carries PG_LOCAL_BIND moves it, so no
    # plan taken before staging survives.
    deployed_compose_files: tuple[LegacyImagePinDeployedFileV1, ...] = Field(
        min_length=1
    )

    # ---- DESIRED: the immutable bytes APPLY will use, pinned by digest rather
    # than taken from whatever a checkout happens to contain at the time.
    desired_release_compose_digest: Sha256Digest
    desired_overlay_digest: Sha256Digest

    legacy_image_reference: LegacyImageTag
    observed_image_id: Sha256Digest
    desired_image_reference: ImageDigestReference
    resolution: LegacyImagePinLocalResolutionV1

    target_container_id: ContainerId
    volume_identity_digest: Sha256Digest
    current_listeners: tuple[PublishedPortObservedListenerV1, ...] = Field(min_length=1)
    desired_listeners: tuple[PublishedPortObservedListenerV1, ...] = Field(min_length=1)
    replication_client: IPvAnyNetwork
    non_port_definition_digest: Sha256Digest
    image_free_definition_digest: Sha256Digest
    bind_knob: LegacyImagePinBindKnobProofV1
    non_target_containers: tuple[PublishedPortProjectContainerV1, ...] = Field(
        min_length=1
    )
    operation: LegacyImagePinBootstrapOperationV1
    planned_at: datetime
    expires_at: datetime

    @model_validator(mode="after")
    def the_transition_is_exactly_the_declared_one(self) -> Self:
        _require_utc(self.planned_at, "planned_at")
        _require_utc(self.expires_at, "expires_at")
        if self.expires_at <= self.planned_at:
            raise ValueError("plan expiry must follow planning")
        if self.expires_at - self.planned_at > timedelta(hours=1):
            raise ValueError("plan freshness may not exceed one hour")
        if self.operation.desired_image_reference != self.desired_image_reference:
            raise ValueError("the operation names another image reference")
        if self.resolution.reference != self.desired_image_reference:
            raise ValueError("the resolution names another image reference")
        if self.resolution.running_image_id != self.observed_image_id:
            raise ValueError("the resolution names another running image")
        if _repository_of(self.legacy_image_reference) != _repository_of(
            self.desired_image_reference
        ):
            raise ValueError(
                "the desired digest must name the same repository as the legacy tag"
            )
        if str(self.replication_client) != "75.119.157.91/32":
            raise ValueError("the declared replication obligation differs")

        deployed_paths = tuple(row.path for row in self.deployed_compose_files)
        if deployed_paths != tuple(sorted(set(deployed_paths))):
            raise ValueError("deployed Compose files must be unique and sorted")
        # The release bytes must be AMONG the bytes actually deployed. This is
        # the PG_LOCAL_BIND precondition made structural: until the host has
        # been staged to this exact release, the plan cannot be built at all,
        # so "verify the deployed Compose digest" is a contract check rather
        # than a step someone remembers to perform.
        if self.desired_release_compose_digest not in {
            row.digest for row in self.deployed_compose_files
        }:
            raise ValueError(
                "the desired release Compose bytes are not among the bytes "
                "deployed on the host; stage the release first"
            )
        # The overlay is fully determined by the desired reference, so it
        # cannot be substituted between planning and apply.
        if self.desired_overlay_digest != overlay_digest(self.desired_image_reference):
            raise ValueError("the overlay digest does not match the desired image")

        # The prestate is the dual-family publish that this change exists to
        # correct: both a v4 and a v6 listener on the declared socket.
        current = tuple(
            (row.container_port, str(row.host_ip), row.host_port, row.protocol)
            for row in self.current_listeners
        )
        if current != tuple(sorted(set(current))):
            raise ValueError("current listeners must be unique and sorted")
        if current != (
            (
                DECLARED_CONTAINER_PORT,
                DECLARED_IPV4_WILDCARD,
                DECLARED_HOST_PORT,
                "tcp",
            ),
            (DECLARED_CONTAINER_PORT, "::", DECLARED_HOST_PORT, "tcp"),
        ):
            raise ValueError(
                "the bootstrap admits only the observed dual-family prestate"
            )
        desired = tuple(
            (row.container_port, str(row.host_ip), row.host_port, row.protocol)
            for row in self.desired_listeners
        )
        if desired != (
            (
                DECLARED_CONTAINER_PORT,
                DECLARED_IPV4_WILDCARD,
                DECLARED_HOST_PORT,
                "tcp",
            ),
        ):
            raise ValueError("the bootstrap desires exactly one IPv4 listener")

        keys = tuple(
            (row.service, row.container, row.container_id)
            for row in self.non_target_containers
        )
        if keys != tuple(sorted(set(keys))):
            raise ValueError("the non-target map must be unique and sorted")
        if any(row.service == self.service for row in self.non_target_containers):
            raise ValueError("the target service may not appear among non-targets")
        if self.target_container_id in {
            row.container_id for row in self.non_target_containers
        }:
            raise ValueError("the target container may not appear among non-targets")
        return self

    def operation_digest(self) -> str:
        return self.operation.canonical_digest()

    def prestate_key(self) -> str:
        """The exact legacy prestate this plan admits, as one digest.

        APPLY re-observes the host and recomputes this; a post-bootstrap host
        cannot produce it, which is one half of "structurally single-use".
        """

        return _prestate_key(
            legacy_image_reference=self.legacy_image_reference,
            observed_image_id=self.observed_image_id,
            target_container_id=self.target_container_id,
            non_port_definition_digest=self.non_port_definition_digest,
            deployed_compose_digests=tuple(
                f"{row.path}={row.digest}" for row in self.deployed_compose_files
            ),
        )

    def desired_ipv4_address(self) -> IPvAnyAddress:
        return self.desired_listeners[0].host_ip


def _prestate_key(
    *,
    legacy_image_reference: str,
    observed_image_id: str,
    target_container_id: str,
    non_port_definition_digest: str,
    deployed_compose_digests: tuple[str, ...],
) -> str:
    material = "\n".join(
        (
            "LegacyImagePinPrestateV1",
            legacy_image_reference,
            observed_image_id,
            target_container_id,
            non_port_definition_digest,
            *deployed_compose_digests,
        )
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(material).hexdigest()}"


class LegacyImagePinBootstrapAdmissionV1(StrictContract):
    """Authority produced only from two distinct byte-identical plan runs."""

    schema_id: Literal["LegacyImagePinBootstrapAdmissionV1"] = Field(
        default=BOOTSTRAP_ADMISSION_SCHEMA, alias="schema"
    )
    repository: Literal["michaelayoade/dotmac_sub"] = "michaelayoade/dotmac_sub"
    workflow: Literal[".github/workflows/legacy-image-pin-bootstrap-apply.yml"] = (
        BOOTSTRAP_APPLY_WORKFLOW
    )
    protected_ref: Literal["refs/heads/main"] = PROTECTED_REF
    source_sha: GitSha
    apply_run_id: int = Field(gt=0)
    operation_id: BootstrapOperationId
    plan_digest: Sha256Digest
    operation_digest: Sha256Digest
    prestate_key: Sha256Digest
    plan_run_ids: tuple[int, int]
    artifact_receipt_digests: tuple[Sha256Digest, Sha256Digest]
    firewall_verifier_identity: NonEmpty
    client_collector_identity: NonEmpty
    admitted_at: datetime
    expires_at: datetime

    @model_validator(mode="after")
    def admission_is_coherent(self) -> Self:
        _require_utc(self.admitted_at, "admitted_at")
        _require_utc(self.expires_at, "expires_at")
        if self.expires_at <= self.admitted_at:
            raise ValueError("admission expiry must follow admission")
        if self.expires_at - self.admitted_at > timedelta(minutes=30):
            raise ValueError("admission may not remain live for over 30 minutes")
        if len(set(self.plan_run_ids)) != 2:
            raise ValueError("two distinct plan run IDs are required")
        if self.plan_run_ids != tuple(sorted(self.plan_run_ids)):
            raise ValueError("plan runs must be sorted by run ID")
        if len(set(self.artifact_receipt_digests)) != 2:
            raise ValueError("two distinct artifact receipts are required")
        if self.operation_id != f"imagepin-{BOOTSTRAP_SERVICE}-{self.apply_run_id}":
            raise ValueError("operation ID must bind the service and apply run ID")
        if self.firewall_verifier_identity == self.client_collector_identity:
            raise ValueError(
                "firewall policy and external reach must have independent identities"
            )
        return self


class LegacyImagePinBootstrapDeadmanStateV1(StrictContract):
    """Root-local recovery state. Recovery goes FORWARD, never backwards.

    THE ASSERTION IS INVERTED, AND THIS IS THE POINT
    ================================================

    An earlier draft had the deadman restore the listener preimage it observed
    -- a publish on both ``0.0.0.0`` and ``[::]``. That was wrong, and wrong in
    the most dangerous direction: **the dual-family listener is the
    vulnerability this whole change exists to remove**, not a healthy state to
    return to. Its IPv6 half terminates on INPUT rather than traversing
    DOCKER-USER, so no host firewall rule reaches it.

    So a dual-family listener reappearing is a deadman FAILURE, not a deadman
    success. Automatic recovery recreates forward: the retained immutable pin,
    the IPv4-only bind, and an explicit refusal if any IPv6 listener is
    observed afterwards.

    There is deliberately no ``before_listeners`` field. A preimage that must
    never be restored should not be sitting in the state where someone can
    mistake it for a target.

    Returning to dual-family is break-glass: separately authorized, never
    automatic, and never retained on disk for convenience. The pre-staging
    Compose is not bundled here for exactly that reason.
    """

    schema_id: Literal["LegacyImagePinBootstrapDeadmanStateV1"] = Field(
        default=BOOTSTRAP_DEADMAN_SCHEMA, alias="schema"
    )
    operation_id: BootstrapOperationId
    plan_digest: Sha256Digest
    service: Literal["postgres-local"] = BOOTSTRAP_SERVICE
    deploy_dir: NonEmpty
    env_file: NonEmpty
    docker_bin: NonEmpty
    compose_files: tuple[NonEmpty, ...] = Field(min_length=1)
    retained_image_reference: ImageDigestReference
    before_image_id: Sha256Digest
    bind_env: Literal["PG_LOCAL_BIND"] = "PG_LOCAL_BIND"
    # The FORWARD target, not a preimage. Recovery drives the host here.
    forward_bind: Literal["0.0.0.0:"] = "0.0.0.0:"
    forward_listeners: tuple[PublishedPortObservedListenerV1, ...] = Field(min_length=1)
    # Data identity. A recreate that keeps the container discipline and the
    # image but silently re-binds a volume would pass every other check.
    volume_identity_digest: Sha256Digest
    before_container_id: ContainerId
    deadline: datetime
    state: Literal["armed", "recovered_forward", "disarmed"] = "armed"
    state_reason: str | None = None
    updated_at: datetime

    @model_validator(mode="after")
    def state_is_safe_and_canonical(self) -> Self:
        _require_utc(self.deadline, "deadline")
        _require_utc(self.updated_at, "updated_at")
        for field in (self.deploy_dir, self.env_file, self.docker_bin):
            if not field.startswith("/"):
                raise ValueError("deadman paths must be absolute")
        if any(not path.startswith("/") for path in self.compose_files):
            raise ValueError("deadman compose paths must be absolute")
        observed = tuple(
            (row.container_port, str(row.host_ip), row.host_port, row.protocol)
            for row in self.forward_listeners
        )
        if observed != tuple(sorted(set(observed))):
            raise ValueError("forward listeners must be unique and sorted")
        if observed != (
            (
                DECLARED_CONTAINER_PORT,
                DECLARED_IPV4_WILDCARD,
                DECLARED_HOST_PORT,
                "tcp",
            ),
        ):
            raise ValueError(
                "the forward target is exactly one IPv4 listener; a dual-family "
                "listener is the vulnerability, not a recovery state"
            )
        if any(row.host_ip.version == 6 for row in self.forward_listeners):
            raise ValueError("the forward target may not contain an IPv6 listener")
        if self.state == "armed" and self.state_reason is not None:
            raise ValueError("an armed deadman has no terminal reason")
        if self.state != "armed" and not self.state_reason:
            raise ValueError("a terminal deadman state requires a reason")
        return self


class LegacyImagePinBootstrapReceiptV1(StrictContract):
    """The terminal, root-owned record that this bootstrap has happened.

    This object IS the single-use mechanism.  It is written under the
    deployment lock to a fixed root-owned path, and its mere presence refuses
    a second bootstrap -- whether the first one succeeded or was rolled back.
    A rolled-back bootstrap has already achieved the durable half of its
    purpose (the immutable reference is retained), so repeating it would be
    both unnecessary and a second unreviewed recreate.
    """

    schema_id: Literal["LegacyImagePinBootstrapReceiptV1"] = Field(
        default=BOOTSTRAP_RECEIPT_SCHEMA, alias="schema"
    )
    outcome: Literal["applied", "recovered_forward"]
    operation_id: BootstrapOperationId
    source_sha: GitSha
    apply_run_id: int = Field(gt=0)
    target_server_name: Literal["dotmac-sub-prod"] = PRODUCTION_SERVER_NAME
    service: Literal["postgres-local"] = BOOTSTRAP_SERVICE
    plan_digest: Sha256Digest
    operation_digest: Sha256Digest
    prestate_key: Sha256Digest
    legacy_image_reference: LegacyImageTag
    retained_image_reference: ImageDigestReference
    before_container_id: ContainerId
    after_container_id: ContainerId
    image_id: Sha256Digest
    recorded_at: datetime

    @model_validator(mode="after")
    def a_receipt_records_a_real_recreate(self) -> Self:
        _require_utc(self.recorded_at, "recorded_at")
        if self.before_container_id == self.after_container_id:
            raise ValueError("a bootstrap receipt requires a recreated container")
        return self


class LegacyImagePinPostconditionVerdictV1(StrictContract):
    """Every success proof required before the bootstrap deadman may disarm."""

    schema_id: Literal["LegacyImagePinPostconditionVerdictV1"] = Field(
        default="LegacyImagePinPostconditionVerdictV1", alias="schema"
    )
    operation_id: BootstrapOperationId
    source_sha: GitSha
    plan_digest: Sha256Digest
    apply_run_id: int = Field(gt=0)
    before_target_container_id: ContainerId
    after_target_container_id: ContainerId
    image_id: Sha256Digest
    effective_image_reference: ImageDigestReference
    volume_identity_digest: Sha256Digest
    observed_listeners: tuple[PublishedPortObservedListenerV1, ...] = Field(
        min_length=1
    )
    image_free_definition_digest: Sha256Digest
    unchanged_non_target_container_digest: Sha256Digest
    replication_state: Literal["streaming"] = "streaming"
    firewall_proof_digest: Sha256Digest
    client_reach_proof_digest: Sha256Digest
    unauthorized_vantage_refused: Literal[True] = True
    positive_control_observed: Literal[True] = True
    verified_at: datetime
    verdict: Literal["bootstrap_postconditions_proved"] = (
        "bootstrap_postconditions_proved"
    )

    @model_validator(mode="after")
    def a_successful_recreate_is_observed(self) -> Self:
        _require_utc(self.verified_at, "verified_at")
        if self.before_target_container_id == self.after_target_container_id:
            raise ValueError("target container ID did not change")
        observed = tuple(
            (row.container_port, str(row.host_ip), row.host_port, row.protocol)
            for row in self.observed_listeners
        )
        if observed != (
            (
                DECLARED_CONTAINER_PORT,
                DECLARED_IPV4_WILDCARD,
                DECLARED_HOST_PORT,
                "tcp",
            ),
        ):
            raise ValueError(
                "the bootstrap requires exactly one IPv4 listener afterwards"
            )
        return self


class LegacyImagePinReplicationProbeV1(StrictContract):
    """A read-only observation that the standby is actually streaming.

    Taken before the mutation (a recreate must not be started while
    replication is already broken, or the change would be blamed for a fault
    it did not cause) and again after it.
    """

    schema_id: Literal["LegacyImagePinReplicationProbeV1"] = Field(
        default="LegacyImagePinReplicationProbeV1", alias="schema"
    )
    operation_id: BootstrapOperationId
    phase: Literal["prestate", "poststate"]
    state: Literal["streaming"]
    client_addr: IPvAnyAddress
    observed_at: datetime

    @model_validator(mode="after")
    def probe_names_the_declared_standby(self) -> Self:
        _require_utc(self.observed_at, "observed_at")
        if str(self.client_addr) != "75.119.157.91":
            raise ValueError("the replication probe names another standby")
        return self


class LegacyImagePinFirewallProofV1(StrictContract):
    """Sanitized proof that host policy admits exactly the declared path."""

    schema_id: Literal["LegacyImagePinFirewallProofV1"] = Field(
        default="LegacyImagePinFirewallProofV1", alias="schema"
    )
    operation_id: BootstrapOperationId
    plan_digest: Sha256Digest
    client_network: IPvAnyNetwork
    verifier_identity: NonEmpty
    ruleset_digest: Sha256Digest
    verdict: Literal["admitted"] = "admitted"
    ipv6_listener_absent: Literal[True] = True
    observed_at: datetime

    @model_validator(mode="after")
    def proof_names_the_declared_client(self) -> Self:
        _require_utc(self.observed_at, "observed_at")
        if str(self.client_network) != "75.119.157.91/32":
            raise ValueError("the firewall proof names another client path")
        return self


class LegacyImagePinReachProofV1(StrictContract):
    """External-vantage evidence; the target host cannot mint it.

    A refusal on its own proves nothing -- a collector with a broken route, a
    wrong port, or no network at all also "fails to connect". So the same
    unauthorized vantage must, in the same observation, successfully reach a
    control target. Without that, "refused" is indistinguishable from "the
    probe never left the building".
    """

    schema_id: Literal["LegacyImagePinReachProofV1"] = Field(
        default="LegacyImagePinReachProofV1", alias="schema"
    )
    operation_id: BootstrapOperationId
    plan_digest: Sha256Digest
    collector_identity: NonEmpty
    authorized_client: IPvAnyNetwork
    authorized_verdict: Literal["reachable"] = "reachable"
    unauthorized_vantage: NonEmpty
    unauthorized_verdict: Literal["refused"] = "refused"
    positive_control_target: NonEmpty
    positive_control_verdict: Literal["reachable"] = "reachable"
    collector_evidence_digest: Sha256Digest
    observed_at: datetime

    @model_validator(mode="after")
    def the_refusal_is_falsifiable(self) -> Self:
        _require_utc(self.observed_at, "observed_at")
        if str(self.authorized_client) != "75.119.157.91/32":
            raise ValueError("the reach proof names another authorized client")
        if self.unauthorized_vantage == self.positive_control_target:
            raise ValueError(
                "the positive control must name a target, not the vantage itself"
            )
        return self


class LegacyImagePinStagingJournalV1(StrictContract):
    """The staging operation's intent record, and its COMMIT POINT.

    Staging lands two things that are only safe together: the release Compose
    file, and ``PG_LOCAL_BIND=0.0.0.0:`` in the deployment environment. A host
    carrying the release Compose WITHOUT the variable resolves the publish to
    ``127.0.0.1:`` -- and the next recreate of ``postgres-local``, this
    operation's or anyone's, strands the replication standby on a port it is
    actively streaming WAL through. So neither may land alone, and a torn write
    must never leave that pairing half-applied.

    Two files cannot be renamed in one atomic step, so the pairing is made
    atomic by a journal instead. ``state`` names which regime the host is in:

    ``preparing``
        Nothing is committed. Both originals are preserved and recovery
        restores them, atomically, leaving the host as it was observed.

    ``committed``
        THE COMMIT POINT HAS PASSED. Recovery never goes backwards from here:
        it recreates forward with the retained immutable pin and the IPv4-only
        bind. Returning to the dual-family publish is break-glass -- separately
        authorized, never automatic, and deliberately not possible from
        anything left on disk, which is why the pre-staging Compose is not
        preserved past this point.
    """

    schema_id: Literal["LegacyImagePinStagingJournalV1"] = Field(
        default="LegacyImagePinStagingJournalV1", alias="schema"
    )
    target_server_name: Literal["dotmac-sub-prod"] = PRODUCTION_SERVER_NAME
    service: Literal["postgres-local"] = BOOTSTRAP_SERVICE
    source_sha: GitSha
    compose_path: NonEmpty
    env_path: NonEmpty
    observed_compose_digest: Sha256Digest
    observed_env_digest: Sha256Digest
    desired_compose_digest: Sha256Digest
    bind_env: Literal["PG_LOCAL_BIND"] = "PG_LOCAL_BIND"
    desired_bind: Literal["0.0.0.0:"] = "0.0.0.0:"
    container_ids_before: tuple[PublishedPortProjectContainerV1, ...] = Field(
        min_length=1
    )
    state: Literal["preparing", "committed"]
    committed_at: datetime | None = None
    updated_at: datetime

    @model_validator(mode="after")
    def the_commit_point_is_explicit(self) -> Self:
        _require_utc(self.updated_at, "updated_at")
        for path in (self.compose_path, self.env_path):
            if not path.startswith("/"):
                raise ValueError("staging paths must be absolute")
        if self.state == "preparing" and self.committed_at is not None:
            raise ValueError("an uncommitted staging operation has no commit point")
        if self.state == "committed":
            if self.committed_at is None:
                raise ValueError("a committed staging operation names its commit point")
            _require_utc(self.committed_at, "committed_at")
        if self.observed_compose_digest == self.desired_compose_digest:
            raise ValueError(
                "the host already carries the desired release Compose; there is "
                "nothing to stage"
            )
        keys = tuple(
            (row.service, row.container, row.container_id)
            for row in self.container_ids_before
        )
        if keys != tuple(sorted(set(keys))):
            raise ValueError("the pre-staging container map must be unique and sorted")
        return self
