"""Sub-owned Integrator capability contracts for network access (suite-v1).

These are the payload contracts Sub publishes for the three capabilities the
FreeRADIUS connector will claim in the network-suite-v1 cutover (ADR-0038 in
Starter, ADR-0076 sections 7-8 for the secret-fetch and convergence rules,
ADR-0074 for fleet targets). Sub authors them; the Integrator transports
them and verifies that a connector's claimed digest covers exactly these
schemas. The digest replication below must stay byte-identical to Starter's
``dotmac_integration.spi.canonical_digest`` over
``{capability_id, command_schema, result_schema, observation_schema}`` —
Sub deliberately does not depend on the Integrator package, so the rule is
reimplemented here and pinned by golden tests on both sides.

Shape rules every schema here obeys:

* **No secret can be expressed.** A projection step carries the credential's
  id, its secret version and the open projection-request reference — never
  the secret, its ciphertext or any hash of it. The secret itself crosses
  only through the credential owner's single-use fetch (ADR-0076 section 7).
* **Closed objects.** ``additionalProperties: false`` everywhere; a new field
  is a new contract version (a new capability id), never a silent widening.
* **Provider-neutral vocabulary.** Attributes use the network-access module's
  neutral keys. Rendering them to ``radcheck``/``radreply`` rows (Mikrotik
  names, ops, static defaults) is connector knowledge, proved by golden-row
  conformance on the connector side.

Test-access grants (the incumbent ``TEST_RADIUS_*`` rows) are deliberately
absent from v1: they are a Sub-local walled-garden mechanism with no
network-access concept, and carrying them requires either a contract v2 or
retiring the mechanism before cutover. The shadow comparison must therefore
treat test-access logins as a named, expected difference until that decision
is taken.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping

from app.services.integrations.product_port_descriptor import (
    CapabilityContractDocument,
    capability_contract_document,
)

APPLICATION = "sub"
OWNER_MODULE = "network.access_projection"
OBSERVATION_OWNER_MODULE = "network.access_observation"
DISCONNECT_OWNER_MODULE = "network.session_disconnect"

ACCESS_PROJECTION_CAPABILITY = "network.access_projection.v1"
ACCESS_OBSERVATION_CAPABILITY = "network.access_observation.v1"
SESSION_DISCONNECT_CAPABILITY = "network.session_disconnect.v1"

ACCESS_PROJECTION_SUMMARY = (
    "Desired RADIUS login projection (apply and withdraw) for one service"
)
ACCESS_OBSERVATION_SUMMARY = (
    "Provider authentication and accounting observations for network access"
)
SESSION_DISCONNECT_SUMMARY = (
    "CoA/Disconnect of one live access session through the NAS fleet"
)

PROJECTION_ENDPOINT = "radius.projection.upsert"
DISCONNECT_ENDPOINT = "radius.session.disconnect"

#: Neutral attribute keys a projection may carry (network-access vocabulary).
#: Values are always strings; typing richer forms is the renderer's job.
ATTRIBUTE_KEYS = (
    "framed_ipv4_address",
    "framed_ipv6_prefix",
    "delegated_ipv6_prefix",
    "rate_limit",
    "address_list",
    "framed_route",
    "profile_group",
    "idle_timeout_seconds",
    "simultaneous_use",
)

_REF = {"type": "string", "minLength": 1, "maxLength": 240}
_FINGERPRINT = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
_OBSERVED_AT = {"type": "string", "format": "date-time"}

ATTRIBUTE_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["key", "value"],
    "properties": {
        "key": {"type": "string", "enum": list(ATTRIBUTE_KEYS)},
        "value": {"type": "string", "maxLength": 253},
    },
}

PROJECTION_COMMAND_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "subject_ref",
        "username",
        "desired_state",
        "policy_code",
        "policy_version",
        "attributes",
        "expected_fingerprint",
    ],
    "properties": {
        "subject_ref": _REF,
        "username": {"type": "string", "minLength": 1, "maxLength": 64},
        "previous_username": {
            "type": ["string", "null"],
            "maxLength": 64,
            "default": None,
        },
        "desired_state": {
            "type": "string",
            "enum": ["enabled", "disabled", "suspended"],
        },
        "policy_code": _REF,
        "policy_version": _REF,
        "attributes": {"type": "array", "items": ATTRIBUTE_SCHEMA, "maxItems": 64},
        "credential_id": {"type": ["string", "null"], "maxLength": 240},
        "secret_version": {"type": ["integer", "null"], "minimum": 1},
        "projection_request_ref": {"type": ["string", "null"], "maxLength": 240},
        "expected_fingerprint": _FINGERPRINT,
        "valid_until": {"type": ["string", "null"], "format": "date-time"},
    },
    "allOf": [
        {
            # An enabled, non-reject login authenticates: it must name the
            # credential to fetch.
            "if": {
                "properties": {
                    "desired_state": {"const": "enabled"},
                    "policy_code": {"not": {"const": "reject"}},
                },
                "required": ["desired_state"],
            },
            "then": {
                "required": [
                    "credential_id",
                    "secret_version",
                    "projection_request_ref",
                ],
                "properties": {
                    "credential_id": {"type": "string", "minLength": 1},
                    "secret_version": {"type": "integer", "minimum": 1},
                    "projection_request_ref": {"type": "string", "minLength": 1},
                },
            },
        },
        {
            # Withdrawal, suspension and explicit reject render no password
            # row, so they must not name a credential (naming one would burn
            # a single-use fetch for a secret nobody writes).
            "if": {
                "anyOf": [
                    {
                        "properties": {
                            "desired_state": {"enum": ["disabled", "suspended"]}
                        },
                        "required": ["desired_state"],
                    },
                    {
                        "properties": {"policy_code": {"const": "reject"}},
                        "required": ["policy_code"],
                    },
                ]
            },
            "then": {
                "properties": {
                    "credential_id": {"type": "null"},
                    "secret_version": {"type": "null"},
                    "projection_request_ref": {"type": "null"},
                }
            },
        },
    ],
}

PROJECTION_RESULT_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "applied_state",
        "observed_fingerprint",
        "rows_written",
        "target_ref",
    ],
    "properties": {
        "applied_state": {
            "type": "string",
            "enum": ["enabled", "disabled", "suspended", "absent"],
        },
        "credential_id": {"type": ["string", "null"], "maxLength": 240},
        "secret_version": {"type": ["integer", "null"], "minimum": 1},
        "observed_fingerprint": _FINGERPRINT,
        "rows_written": {"type": "integer", "minimum": 0},
        "target_ref": _REF,
    },
}

PROJECTION_OBSERVATION_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "kind",
        "subject_ref",
        "username",
        "observed_state",
        "observed_fingerprint",
        "source_ref",
        "observed_at",
    ],
    "properties": {
        "kind": {
            "type": "string",
            "enum": ["projection_converged", "projection_drifted"],
        },
        "subject_ref": _REF,
        "username": {"type": "string", "minLength": 1, "maxLength": 64},
        "observed_state": {
            "type": "string",
            "enum": ["enabled", "disabled", "suspended", "absent"],
        },
        "observed_fingerprint": _FINGERPRINT,
        "credential_id": {"type": ["string", "null"], "maxLength": 240},
        "secret_version": {"type": ["integer", "null"], "minimum": 1},
        "reason_code": {"type": ["string", "null"], "maxLength": 120},
        "source_ref": _REF,
        "observed_at": _OBSERVED_AT,
    },
}

# POLL has no product-issued command: the engine drives the handler. The
# closed empty object says exactly that, rather than leaving the slot null.
OBSERVATION_COMMAND_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {},
}

OBSERVATION_RESULT_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["events_observed", "cursor_advanced"],
    "properties": {
        "events_observed": {"type": "integer", "minimum": 0},
        "cursor_advanced": {"type": "boolean"},
    },
}

OBSERVATION_OBSERVATION_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "kind",
        "subject_ref",
        "nas_ref",
        "source_ref",
        "observed_at",
        "fingerprint",
    ],
    "properties": {
        "kind": {"type": "string", "enum": ["authentication", "accounting"]},
        "subject_ref": _REF,
        "nas_ref": _REF,
        "session_ref": {"type": ["string", "null"], "maxLength": 240},
        "outcome": {
            "type": ["string", "null"],
            "enum": ["accepted", "rejected", "error", None],
        },
        "event_kind": {"type": ["string", "null"], "maxLength": 40},
        "reason_code": {"type": ["string", "null"], "maxLength": 120},
        "input_octets": {"type": ["integer", "null"], "minimum": 0},
        "output_octets": {"type": ["integer", "null"], "minimum": 0},
        "session_seconds": {"type": ["integer", "null"], "minimum": 0},
        "source_ref": _REF,
        "observed_at": _OBSERVED_AT,
        "fingerprint": _FINGERPRINT,
    },
    "allOf": [
        {
            "if": {
                "properties": {"kind": {"const": "authentication"}},
                "required": ["kind"],
            },
            "then": {
                "required": ["outcome"],
                "properties": {
                    "outcome": {
                        "type": "string",
                        "enum": ["accepted", "rejected", "error"],
                    }
                },
            },
        },
        {
            "if": {
                "properties": {"kind": {"const": "accounting"}},
                "required": ["kind"],
            },
            "then": {
                "required": [
                    "session_ref",
                    "event_kind",
                    "input_octets",
                    "output_octets",
                    "session_seconds",
                ],
                "properties": {
                    "session_ref": {"type": "string", "minLength": 1},
                    "event_kind": {"type": "string", "minLength": 1},
                    "input_octets": {"type": "integer", "minimum": 0},
                    "output_octets": {"type": "integer", "minimum": 0},
                    "session_seconds": {"type": "integer", "minimum": 0},
                },
            },
        },
    ],
}

DISCONNECT_COMMAND_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "subject_ref",
        "username",
        "nas_ref",
        "nas_address",
        "coa_port",
        "reason_code",
    ],
    "properties": {
        "subject_ref": _REF,
        "username": {"type": "string", "minLength": 1, "maxLength": 64},
        "nas_ref": _REF,
        # The fleet endpoint the connector must hold a bound SPI 1.7 network
        # target for; naming it in the contract lets the engine refuse an
        # out-of-declaration target before any packet exists.
        "nas_address": {"type": "string", "maxLength": 253},
        "coa_port": {"type": "integer", "minimum": 1, "maximum": 65535},
        "session_ref": {"type": ["string", "null"], "maxLength": 240},
        "framed_ipv4_address": {"type": ["string", "null"], "maxLength": 45},
        "reason_code": {"type": "string", "minLength": 1, "maxLength": 120},
    },
}

DISCONNECT_RESULT_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["disconnect_outcome", "target_ref"],
    "properties": {
        "disconnect_outcome": {
            "type": "string",
            "enum": ["acknowledged", "refused", "unanswered", "unsupported"],
        },
        "error_cause": {"type": ["string", "null"], "maxLength": 120},
        "target_ref": _REF,
    },
}

DISCONNECT_OBSERVATION_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "kind",
        "subject_ref",
        "username",
        "nas_ref",
        "disconnect_outcome",
        "source_ref",
        "observed_at",
    ],
    "properties": {
        "kind": {"type": "string", "const": "session_disconnect_observed"},
        "subject_ref": _REF,
        "username": {"type": "string", "minLength": 1, "maxLength": 64},
        "nas_ref": _REF,
        "session_ref": {"type": ["string", "null"], "maxLength": 240},
        "disconnect_outcome": {
            "type": "string",
            "enum": ["acknowledged", "refused", "unanswered", "unsupported"],
        },
        "reason_code": {"type": ["string", "null"], "maxLength": 120},
        "source_ref": _REF,
        "observed_at": _OBSERVED_AT,
    },
}


def canonical_digest(value: object) -> str:
    """Starter's one canonical-JSON digest rule, replicated byte-for-byte.

    SHA-256 over ``json.dumps(value, sort_keys=True, separators=(",", ":"),
    default=str)``. Two implementations of an identity rule is the thing
    Starter's own docstring warns against; the alternative here is Sub taking
    a dependency on the Integrator package to hash three schemas, which is
    worse. Golden tests pin this against the Starter computation.
    """
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def contract_digest(
    *,
    capability_id: str,
    command_schema: Mapping[str, object],
    result_schema: Mapping[str, object],
    observation_schema: Mapping[str, object],
) -> str:
    """The digest a connector claims: the three schemas and the id, nothing else."""
    return canonical_digest(
        {
            "capability_id": capability_id,
            "command_schema": command_schema,
            "result_schema": result_schema,
            "observation_schema": observation_schema,
        }
    )


def projection_fingerprint(
    *,
    username: str,
    desired_state: str,
    credential_id: str | None,
    secret_version: int | None,
    attributes: Iterable[tuple[str, str]],
) -> str:
    """The shared expected/observed fingerprint canonicalization.

    Material is canonical JSON over the login, desired state, credential
    identity (id and version — never the secret, its ciphertext or any hash
    of it) and the sorted neutral attributes. Sub computes the expected
    value when it authors the plan; the connector computes the observed
    value from the exact step input whose rendering it read back from the
    target. Equality therefore attests "this neutral projection is what the
    target holds", while row-level render parity is proved separately by
    the connector's golden-row conformance and, during cutover, by shadow
    comparison against the incumbent writer. Unkeyed SHA-256 is safe here
    precisely because no secret is in the material; ADR-0076's keyed-HMAC
    rule governs fingerprints OF secrets, which this never is.
    """
    return canonical_digest(
        {
            "attributes": sorted(
                [key.strip(), value.strip()] for key, value in attributes
            ),
            "credential_id": (credential_id or "").strip(),
            "desired_state": desired_state.strip(),
            "secret_version": secret_version or 0,
            "username": username.strip(),
        }
    )


def _document(
    capability_id: str,
    command_schema: dict[str, object],
    result_schema: dict[str, object],
    observation_schema: dict[str, object],
) -> CapabilityContractDocument:
    return CapabilityContractDocument(
        command_schema=command_schema,
        result_schema=result_schema,
        observation_schema=observation_schema,
        deprecation=None,
        schema_grace=None,
        contract_digest=contract_digest(
            capability_id=capability_id,
            command_schema=command_schema,
            result_schema=result_schema,
            observation_schema=observation_schema,
        ),
    )


ACCESS_PROJECTION_CONTRACT = _document(
    ACCESS_PROJECTION_CAPABILITY,
    PROJECTION_COMMAND_SCHEMA,
    PROJECTION_RESULT_SCHEMA,
    PROJECTION_OBSERVATION_SCHEMA,
)
ACCESS_OBSERVATION_CONTRACT = _document(
    ACCESS_OBSERVATION_CAPABILITY,
    OBSERVATION_COMMAND_SCHEMA,
    OBSERVATION_RESULT_SCHEMA,
    OBSERVATION_OBSERVATION_SCHEMA,
)
SESSION_DISCONNECT_CONTRACT = _document(
    SESSION_DISCONNECT_CAPABILITY,
    DISCONNECT_COMMAND_SCHEMA,
    DISCONNECT_RESULT_SCHEMA,
    DISCONNECT_OBSERVATION_SCHEMA,
)

CAPABILITY_REGISTRY_SCHEMA_VERSION = (
    "dotmac.io/integration-query-capability-registry/v1"
)


def published_source_revision() -> str:
    """The 64-hex revision of this contract set.

    Derived, not configured: the digest of the three contract digests, so
    the revision changes exactly when a published contract changes and can
    never drift from what is actually served.
    """
    digests = [
        document.contract_digest
        for _cid, _owner, _summary, document in NETWORK_CAPABILITY_CONTRACTS
    ]
    present = [digest for digest in digests if digest is not None]
    if len(present) != len(digests):
        raise ValueError("a network capability contract has no digest")
    return canonical_digest(sorted(present))


def capability_registry_document(
    source_revision: str | None = None,
) -> dict[str, object]:
    """The authenticated registry document the Integrator reconciles against.

    Mirrors the product capability-registry shape the Integrator already
    verifies for ERP's query contracts (exact field sets, sorted unique
    capability ids, digest covering every field except itself), so Sub's
    command contracts install through the same machinery. The Integrator
    pins `descriptor_digest` in deployment settings; any drift between the
    served document and the pin refuses startup rather than executing
    against unreviewed contracts.

    `source_revision` is a 64-hex content hash of the publishing build, not
    a free-form git string — the Integrator refuses anything else.
    """
    if source_revision is None:
        source_revision = published_source_revision()
    if len(source_revision) != 64 or any(
        ch not in "0123456789abcdef" for ch in source_revision
    ):
        raise ValueError(
            "source_revision must be a 64-character lowercase hex content hash"
        )
    contracts = [
        {
            "owner_module": owner,
            "capability_id": capability_id,
            "capability_summary": summary,
            "contract_version": int(capability_id.rsplit(".v", 1)[1]),
            "capability_contract": capability_contract_document(document),
        }
        for capability_id, owner, summary, document in sorted(
            NETWORK_CAPABILITY_CONTRACTS, key=lambda entry: entry[0]
        )
    ]
    document: dict[str, object] = {
        "schema_version": CAPABILITY_REGISTRY_SCHEMA_VERSION,
        "application": APPLICATION,
        "source_revision": source_revision,
        "contracts": contracts,
    }
    document["descriptor_digest"] = canonical_digest(document)
    return document


#: Everything descriptor publication (slice: Integrator wiring) will carry.
NETWORK_CAPABILITY_CONTRACTS: tuple[
    tuple[str, str, str, CapabilityContractDocument], ...
] = (
    (
        ACCESS_PROJECTION_CAPABILITY,
        OWNER_MODULE,
        ACCESS_PROJECTION_SUMMARY,
        ACCESS_PROJECTION_CONTRACT,
    ),
    (
        ACCESS_OBSERVATION_CAPABILITY,
        OBSERVATION_OWNER_MODULE,
        ACCESS_OBSERVATION_SUMMARY,
        ACCESS_OBSERVATION_CONTRACT,
    ),
    (
        SESSION_DISCONNECT_CAPABILITY,
        DISCONNECT_OWNER_MODULE,
        SESSION_DISCONNECT_SUMMARY,
        SESSION_DISCONNECT_CONTRACT,
    ),
)

__all__ = [
    "ACCESS_OBSERVATION_CAPABILITY",
    "ACCESS_OBSERVATION_CONTRACT",
    "ACCESS_OBSERVATION_SUMMARY",
    "ACCESS_PROJECTION_CAPABILITY",
    "ACCESS_PROJECTION_CONTRACT",
    "ACCESS_PROJECTION_SUMMARY",
    "APPLICATION",
    "ATTRIBUTE_KEYS",
    "DISCONNECT_COMMAND_SCHEMA",
    "DISCONNECT_ENDPOINT",
    "DISCONNECT_OBSERVATION_SCHEMA",
    "DISCONNECT_OWNER_MODULE",
    "DISCONNECT_RESULT_SCHEMA",
    "NETWORK_CAPABILITY_CONTRACTS",
    "OBSERVATION_COMMAND_SCHEMA",
    "OBSERVATION_OBSERVATION_SCHEMA",
    "OBSERVATION_OWNER_MODULE",
    "OBSERVATION_RESULT_SCHEMA",
    "OWNER_MODULE",
    "PROJECTION_COMMAND_SCHEMA",
    "PROJECTION_ENDPOINT",
    "PROJECTION_OBSERVATION_SCHEMA",
    "PROJECTION_RESULT_SCHEMA",
    "SESSION_DISCONNECT_CAPABILITY",
    "SESSION_DISCONNECT_CONTRACT",
    "SESSION_DISCONNECT_SUMMARY",
    "canonical_digest",
    "capability_registry_document",
    "published_source_revision",
    "contract_digest",
    "projection_fingerprint",
]
