"""Portable, closed, typed increment-2 contracts; vendored byte-for-byte."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import struct
import types
from collections.abc import Callable, Collection
from datetime import datetime, timedelta
from typing import (
    Annotated,
    Literal,
    NotRequired,
    Protocol,
    TypeAlias,
    TypedDict,
    Union,
    cast,
    get_args,
    get_origin,
    get_type_hints,
    is_typeddict,
)
from uuid import UUID

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

# The portable host contract also runs on Python 3.11; retain evaluated aliases.
Json: TypeAlias = dict[str, object]  # noqa: UP040
BoundedText: TypeAlias = Annotated[str, r"(?s).{1,2048}"]  # noqa: UP040
Digest: TypeAlias = Annotated[str, r"sha256:[0-9a-f]{64}"]  # noqa: UP040
Revision: TypeAlias = Annotated[str, r"[0-9a-f]{40}"]  # noqa: UP040
Identifier: TypeAlias = Annotated[str, r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}"]  # noqa: UP040
ConfigPath: TypeAlias = Literal[  # noqa: UP040
    "docker-compose.yml", "docker-compose.override.yml"
]
Disposition: TypeAlias = Literal[  # noqa: UP040
    "applied", "already_applied", "refused", "failed", "rolled_back"
]


class MigrationPolicy(TypedDict):
    mode: Literal["upgrade_heads"]
    expected_heads: NotRequired[list[Identifier]]


class Approval(TypedDict):
    environment: Literal["staging"]
    actor: BoundedText
    run_id: int


class DesiredSummary(TypedDict):
    nonce: str
    plan_digest: Digest
    image_digest: Digest
    source_revision: Revision
    config_digest: Digest


class HostIdentity(TypedDict):
    product: Literal["dotmac_sub"]
    environment: Literal["staging"]
    host: Identifier
    image_reference: Literal["ghcr.io/michaelayoade/dotmac_sub"]


class Plan(HostIdentity):
    image_digest: Digest
    from_image_digest: Digest
    source_revision: Revision
    source_tree: Revision
    product_manifest_digest: Digest
    build_run_id: int
    evidence_run_id: int
    config_digest: Digest
    config_files: dict[ConfigPath, Digest]
    migration_policy: MigrationPolicy


class DesiredState(Plan):
    schema: Literal["dotmac.desired_state/1"]
    deployment_ref: str
    plan_digest: Digest
    approvals: list[Approval]
    nonce: str
    issued_at: str
    expires_at: str
    signer_key_id: Identifier


class Observed(TypedDict):
    running_image: BoundedText | None
    running_revision_label: BoundedText | None
    pinned_image: BoundedText | None
    pinned_git_sha: BoundedText | None
    config_files: dict[ConfigPath, Digest]
    migration_heads: list[Identifier]


class HttpHealth(TypedDict):
    url: BoundedText
    status: int | None
    checked_at: str


class ReadyHealth(HttpHealth):
    body_status: BoundedText | None


class BackgroundHealth(TypedDict):
    celery_inspect_ok: bool
    containers: dict[Identifier, BoundedText]
    restart_counts_zero: bool


class Health(TypedDict):
    live: HttpHealth
    ready: ReadyHealth
    background: BackgroundHealth


class DeployReceipt(TypedDict):
    exit_code: int | None
    receipt_line: BoundedText | None


class AgentIdentity(TypedDict):
    version: BoundedText
    artifact_digest: Digest


class AppliedState(TypedDict):
    schema: Literal["dotmac.deploy_applied_state/1"]
    report_id: str
    deployment_ref: BoundedText
    host_key_id: Identifier
    signer_keyring_generation: int
    desired: DesiredSummary
    disposition: Disposition
    reason: str | None
    observed: Observed
    health: Health
    deploy_receipt: DeployReceipt
    observed_at: str
    agent: AgentIdentity


class ChallengeFacts(TypedDict):
    key_id: Identifier
    deployment_ref: BoundedText
    nonce: str
    expires_at: str


class Challenge(ChallengeFacts):
    schema: Literal["dotmac.deploy_host_challenge/1"]


class Possession(ChallengeFacts):
    schema: Literal["dotmac.deploy_host_possession/1"]
    observed_image_digest: Digest


class Envelope(TypedDict):
    schema: str
    key_id: Identifier
    payload_b64: str
    signature_b64: str


class Signer(Protocol):
    key_id: str

    def sign(self, payload: bytes) -> bytes: ...


class HostSigner(Signer, Protocol):
    deployment_ref: str


DESIRED = "dotmac.desired_state/1"
APPLIED = "dotmac.deploy_applied_state/1"
CHALLENGE = "dotmac.deploy_host_challenge/1"
POSSESSION = "dotmac.deploy_host_possession/1"
DOMAINS = {
    DESIRED: ("dotmac-desired-state-envelope/1", b"dotmac-desired-state/1\0"),
    APPLIED: ("dotmac-applied-state-envelope/1", b"dotmac-deploy-applied-state/1\0"),
    CHALLENGE: ("dotmac-deploy-challenge-envelope/1", b"dotmac-deploy-challenge/1\0"),
    POSSESSION: (
        "dotmac-deploy-possession-envelope/1",
        b"dotmac-deploy-possession/1\0",
    ),
}
CONFIG_PATHS = set(get_args(ConfigPath))
DESIRED_FIELDS = set(DesiredState.__annotations__)
APPLIED_FIELDS = set(AppliedState.__annotations__)
PLAN_FIELDS = (
    "product environment host image_reference image_digest from_image_digest "
    "source_revision source_tree product_manifest_digest build_run_id "
    "evidence_run_id config_digest config_files migration_policy"
).split()
REASONS = set(
    (
        "bad_signature unknown_signer_key schema_violation from_image_mismatch "
        "host_mismatch expired nonce_replay plan_digest_mismatch cosign_verify_failed "
        "attestation_identity_mismatch signature_missing attestation_missing "
        "config_drift checkout_drift rollback_digest health_gate_failed interrupted "
        "observed_mismatch executor_failed executor_timeout"
    ).split()
)


class ContractError(ValueError):
    def __init__(self, code: str = "schema_violation") -> None:
        self.code = code
        super().__init__(code)


def require(condition: bool, code: str = "schema_violation") -> None:
    if not condition:
        raise ContractError(code)


def exact(value: object, fields: Collection[str]) -> None:
    require(isinstance(value, dict) and set(value) == set(fields))


def _shape(value: object, annotation: object) -> None:
    origin, args = get_origin(annotation), get_args(annotation)
    if origin is Annotated:
        _shape(value, args[0])
        require(isinstance(value, str) and re.fullmatch(args[1], value) is not None)
    elif origin is Literal:
        require(value in args and type(value) is type(args[0]))
    elif origin in (Union, types.UnionType):
        for option in args:
            try:
                _shape(value, option)
                return
            except ContractError:
                pass
        raise ContractError()
    elif is_typeddict(annotation):
        hints = get_type_hints(annotation, include_extras=True)
        optional = {
            key for key, hint in hints.items() if get_origin(hint) is NotRequired
        }
        require(
            isinstance(value, dict)
            and set(hints) - optional <= set(value) <= set(hints)
        )
        for key, item in cast(Json, value).items():
            hint = hints[key]
            _shape(item, get_args(hint)[0] if key in optional else hint)
    elif origin in (dict, list):
        require(isinstance(value, origin))
        for entry_key, item in (
            cast(dict[object, object], value).items()
            if origin is dict
            else enumerate(cast(list[object], value))
        ):
            if origin is dict:
                _shape(entry_key, args[0])
            _shape(item, args[-1])
    else:
        require(type(value) is annotation)


def canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    except (ValueError, TypeError, UnicodeError):
        raise ContractError() from None


def _pairs(pairs: list[tuple[str, object]]) -> Json:
    result: Json = {}
    for key, value in pairs:
        require(key not in result)
        result[key] = value
    return result


def read_json(raw: bytes) -> Json:
    try:
        value: object = json.loads(
            raw, object_pairs_hook=_pairs, parse_constant=lambda _: require(False)
        )
        require(isinstance(value, dict))
        return cast(Json, value)
    except (ValueError, TypeError, UnicodeError):
        raise ContractError() from None


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def unb64(value: str) -> bytes:
    require(
        isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]+", value) is not None
    )
    try:
        raw = base64.b64decode(
            value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
        )
    except ValueError:
        raise ContractError() from None
    require(b64(raw) == value)
    return raw


def digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value)).hexdigest()


def identifier(value: object) -> None:
    _shape(value, Identifier)


def sha(value: object, prefix: bool = True) -> None:
    _shape(value, Digest if prefix else Revision)


def timestamp(value: str) -> datetime:
    pattern = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z"
    require(isinstance(value, str) and re.fullmatch(pattern, value) is not None)
    try:
        return datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise ContractError() from None


def plan_document(desired: DesiredState | Plan) -> Plan:
    return cast(Plan, {key: cast(Json, desired)[key] for key in PLAN_FIELDS})


def plan_digest(desired: DesiredState | Plan) -> str:
    return digest(plan_document(desired))


def validate_desired(
    doc: object,
    *,
    now: datetime | None = None,
    identity: HostIdentity | None = None,
    approved_plan_digest: str | None = None,
) -> DesiredState:
    _shape(doc, DesiredState)
    value = cast(DesiredState, doc)
    require(
        value["deployment_ref"]
        == "/".join((value["product"], value["environment"], value["host"]))
    )
    require(value["build_run_id"] > 0 and value["evidence_run_id"] > 0)
    require(
        set(value["config_files"]) == CONFIG_PATHS
        and value["config_digest"] == digest(value["config_files"])
    )
    require(
        bool(value["approvals"])
        and all(item["run_id"] > 0 for item in value["approvals"])
    )
    heads = value["migration_policy"].get("expected_heads", [])
    require(len(heads) == len(set(heads)))
    require(len(unb64(value["nonce"])) >= 16)
    issued, expires = timestamp(value["issued_at"]), timestamp(value["expires_at"])
    require(timedelta(0) < expires - issued <= timedelta(hours=24))
    if now is not None:
        require(issued <= now + timedelta(seconds=300) and now < expires, "expired")
    if identity is not None:
        require(
            all(
                cast(Json, value)[key] == cast(Json, identity)[key]
                for key in ("product", "environment", "host", "image_reference")
            ),
            "host_mismatch",
        )
    require(value["plan_digest"] == plan_digest(value), "plan_digest_mismatch")
    if approved_plan_digest is not None:
        require(value["plan_digest"] == approved_plan_digest, "plan_digest_mismatch")
    return value


def validate_applied(doc: object) -> AppliedState:
    _shape(doc, AppliedState)
    value = cast(AppliedState, doc)
    components = value["deployment_ref"].split("/")
    require(len(components) == 3)
    for component in components:
        identifier(component)
    try:
        require(str(UUID(value["report_id"])) == value["report_id"])
    except ValueError:
        raise ContractError() from None
    require(
        value["signer_keyring_generation"] > 0
        and len(unb64(value["desired"]["nonce"])) >= 16
    )
    require(
        value["reason"] in REASONS
        if value["disposition"] in {"refused", "failed"}
        else value["reason"] is None
    )
    for health in (value["health"]["live"], value["health"]["ready"]):
        timestamp(health["checked_at"])
        require(health["status"] is None or 100 <= health["status"] <= 599)
    timestamp(value["observed_at"])
    return value


def validate_challenge(doc: object, *, now: datetime | None = None) -> Challenge:
    _shape(doc, Challenge)
    value = cast(Challenge, doc)
    require(len(unb64(value["nonce"])) >= 16)
    expires = timestamp(value["expires_at"])
    if now is not None:
        require(now < expires, "expired")
    return value


def validate_possession(doc: object, *, now: datetime | None = None) -> Possession:
    _shape(doc, Possession)
    value = cast(Possession, doc)
    challenge = {
        key: item for key, item in value.items() if key != "observed_image_digest"
    }
    validate_challenge({**challenge, "schema": CHALLENGE}, now=now)
    return value


def answer_challenge(
    challenge: Challenge,
    signer: HostSigner,
    observed_image_digest: str,
    *,
    now: datetime | None = None,
) -> Envelope:
    validate_challenge(challenge, now=now)
    require(
        challenge["key_id"] == signer.key_id
        and challenge["deployment_ref"] == signer.deployment_ref,
        "host_mismatch",
    )
    return seal(
        POSSESSION,
        {
            **challenge,
            "schema": POSSESSION,
            "observed_image_digest": observed_image_digest,
        },
        signer,
    )


def signing_input(kind: str, key_id: str, payload: bytes) -> bytes:
    identifier(key_id)
    key = key_id.encode()
    return (
        DOMAINS[kind][1]
        + struct.pack(">I", len(key))
        + key
        + struct.pack(">I", len(payload))
        + payload
    )


def seal(kind: str, doc: object, signer: Signer) -> Envelope:
    value = cast(Json, VALIDATORS[kind](doc))
    require(value[KEY_FIELDS[kind]] == signer.key_id, "host_mismatch")
    if kind != DESIRED:
        require(
            value["deployment_ref"] == cast(HostSigner, signer).deployment_ref,
            "host_mismatch",
        )
    raw = canonical_json(doc)
    return {
        "schema": DOMAINS[kind][0],
        "key_id": signer.key_id,
        "payload_b64": b64(raw),
        "signature_b64": b64(signer.sign(signing_input(kind, signer.key_id, raw))),
    }


def verify(kind: str, envelope: Envelope, public_key: bytes | str) -> Json:
    _shape(envelope, Envelope)
    require(envelope["schema"] == DOMAINS[kind][0])
    raw = unb64(envelope["payload_b64"])
    try:
        key = Ed25519PublicKey.from_public_bytes(
            unb64(public_key) if isinstance(public_key, str) else public_key
        )
        key.verify(
            unb64(envelope["signature_b64"]),
            signing_input(kind, envelope["key_id"], raw),
        )
    except (InvalidSignature, ValueError):
        raise ContractError("bad_signature") from None
    doc = read_json(raw)
    VALIDATORS[kind](doc)
    require(doc[KEY_FIELDS[kind]] == envelope["key_id"], "bad_signature")
    return doc


VALIDATORS: dict[str, Callable[[object], object]] = {
    DESIRED: validate_desired,
    APPLIED: validate_applied,
    CHALLENGE: validate_challenge,
    POSSESSION: validate_possession,
}
KEY_FIELDS = {
    DESIRED: "signer_key_id",
    APPLIED: "host_key_id",
    CHALLENGE: "key_id",
    POSSESSION: "key_id",
}
