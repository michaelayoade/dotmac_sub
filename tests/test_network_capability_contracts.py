"""Sub-owned network capability contracts for the suite-v1 RADIUS cutover.

Golden digests here are cross-checked against Starter's
``CapabilityContract.contract_digest`` computation; a mismatch means the
Integrator would refuse Sub's published descriptor, so the goldens are the
contract, not a snapshot habit.
"""

from __future__ import annotations

from app.services.integrations.network_capability_contracts import (
    ACCESS_PROJECTION_CONTRACT,
    ATTRIBUTE_KEYS,
    NETWORK_CAPABILITY_CONTRACTS,
    PROJECTION_COMMAND_SCHEMA,
    canonical_digest,
    contract_digest,
    projection_fingerprint,
)
from app.services.integrations.product_port_descriptor import (
    capability_contract_document,
)

GOLDEN_DIGESTS = {
    "network.access_projection.v1": (
        "4240e12e512486a9a20202addc779e8d2dbac041fcd53e06903472eb239195b2"
    ),
    "network.access_observation.v1": (
        "4b1ef5ffc129d4b6f8317996786299bf011815595b2529a672848a1b4a3d69c3"
    ),
    "network.session_disconnect.v1": (
        "e683ccdc57de2a0a3df80c27216815c2a4cfa678f5af1e119213ea599c63c6ed"
    ),
}

GOLDEN_FINGERPRINT = "5521e725637c66a227fde182f2c0e6be84f73561b6a1fbd40a60280b73c0adb5"


def test_contract_digests_are_golden() -> None:
    for capability_id, _owner, _summary, document in NETWORK_CAPABILITY_CONTRACTS:
        assert document.contract_digest == GOLDEN_DIGESTS[capability_id]
        assert document.schema_grace is None
        assert document.command_schema is not None
        assert document.result_schema is not None
        assert document.observation_schema is not None


def test_digest_rule_matches_the_canonical_form() -> None:
    assert (
        contract_digest(
            capability_id="network.access_projection.v1",
            command_schema=ACCESS_PROJECTION_CONTRACT.command_schema or {},
            result_schema=ACCESS_PROJECTION_CONTRACT.result_schema or {},
            observation_schema=ACCESS_PROJECTION_CONTRACT.observation_schema or {},
        )
        == GOLDEN_DIGESTS["network.access_projection.v1"]
    )
    # Key order is not identity.
    assert canonical_digest({"b": 1, "a": 2}) == canonical_digest({"a": 2, "b": 1})


def test_descriptor_document_carries_the_digest() -> None:
    document = capability_contract_document(ACCESS_PROJECTION_CONTRACT)
    assert document["contract_digest"] == GOLDEN_DIGESTS["network.access_projection.v1"]
    assert document["schema_grace"] is None


def test_projection_schema_cannot_express_a_secret() -> None:
    properties = PROJECTION_COMMAND_SCHEMA["properties"]
    assert isinstance(properties, dict)
    # Material names are absent; identifier-shaped names (secret_version)
    # name the secret without carrying it and are required by ADR-0076.
    for name in properties:
        assert "password" not in name
        assert "ciphertext" not in name
        assert "cleartext" not in name
        assert not name.endswith("_secret")
        assert name != "secret"
    # The fetch identity is present; the secret is not.
    assert {"credential_id", "secret_version", "projection_request_ref"} <= set(
        properties
    )
    assert PROJECTION_COMMAND_SCHEMA["additionalProperties"] is False


def test_attribute_vocabulary_is_closed() -> None:
    attributes = PROJECTION_COMMAND_SCHEMA["properties"]["attributes"]  # type: ignore[index]
    key_schema = attributes["items"]["properties"]["key"]  # type: ignore[index]
    assert key_schema["enum"] == list(ATTRIBUTE_KEYS)


def test_projection_fingerprint_golden_and_sensitivity() -> None:
    base = projection_fingerprint(
        username="user@dotmac",
        desired_state="enabled",
        credential_id="cred-1",
        secret_version=3,
        attributes=[("framed_ipv4_address", "10.0.0.5"), ("rate_limit", "50M/50M")],
    )
    assert base == GOLDEN_FINGERPRINT
    reordered = projection_fingerprint(
        username="user@dotmac",
        desired_state="enabled",
        credential_id="cred-1",
        secret_version=3,
        attributes=[("rate_limit", "50M/50M"), ("framed_ipv4_address", "10.0.0.5")],
    )
    assert reordered == base
    rotated = projection_fingerprint(
        username="user@dotmac",
        desired_state="enabled",
        credential_id="cred-1",
        secret_version=4,
        attributes=[("framed_ipv4_address", "10.0.0.5"), ("rate_limit", "50M/50M")],
    )
    assert rotated != base
    withdrawn = projection_fingerprint(
        username="user@dotmac",
        desired_state="disabled",
        credential_id=None,
        secret_version=None,
        attributes=[],
    )
    assert withdrawn != base


def test_every_contract_is_closed() -> None:
    for _capability_id, _owner, _summary, document in NETWORK_CAPABILITY_CONTRACTS:
        for schema in (
            document.command_schema,
            document.result_schema,
            document.observation_schema,
        ):
            assert schema is not None
            assert schema.get("additionalProperties") is False


def test_capability_registry_document_is_exact_and_digest_pinned() -> None:
    from app.services.integrations.network_capability_contracts import (
        capability_registry_document,
    )

    document = capability_registry_document("b" * 64)
    assert set(document) == {
        "schema_version",
        "application",
        "source_revision",
        "contracts",
        "descriptor_digest",
    }
    ids = [row["capability_id"] for row in document["contracts"]]
    assert ids == sorted(ids) and len(ids) == 3
    for row in document["contracts"]:
        assert set(row) == {
            "owner_module",
            "capability_id",
            "capability_summary",
            "contract_version",
            "capability_contract",
        }
    material = {k: v for k, v in document.items() if k != "descriptor_digest"}
    assert document["descriptor_digest"] == canonical_digest(material)
