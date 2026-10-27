"""Outbound provision client: disarmed by default, secret-free when armed."""

from __future__ import annotations

import pytest

from app.services.integrations import network_provision_client as client

STEP = {
    "subject_ref": "sub:subscription:9f2c",
    "username": "jane@radius",
    "previous_username": None,
    "desired_state": "enabled",
    "policy_code": "active",
    "policy_version": "v1",
    "attributes": [{"key": "rate_limit", "value": "10M/10M"}],
    "credential_id": "cred-4711",
    "secret_version": 3,
    "projection_request_ref": "projection-request:9f2c:3",
    "expected_fingerprint": "0" * 64,
    "valid_until": None,
}


class _Answer:
    def __init__(self, status_code: int = 202) -> None:
        self.status_code = status_code

    def json(self) -> dict[str, object]:
        return {
            "status": "accepted",
            "operation_id": "op-1",
            "operation_state": "pending",
        }


def test_submit_request_shape_matches_the_provision_port() -> None:
    document = client.build_submit_request(
        capability_binding_id="binding-7", step_input=STEP
    )
    assert document["schema_version"] == "dotmac.io/integration-provision-request/v1"
    assert document["command_id"] == f"radius-projection:jane@radius:{'0' * 64}"
    assert document["steps"] == [
        {
            "step_key": "projection",
            "endpoint_code": "radius.projection.upsert",
            "depends_on": [],
            "input": STEP,
        }
    ]


def test_disarmed_client_refuses_before_any_transport() -> None:
    def explode(*args: object, **kwargs: object) -> _Answer:
        raise AssertionError("transport called while disarmed")

    with pytest.raises(client.NetworkProvisionDisabled):
        client.submit_projection(
            capability_binding_id="binding-7", step_input=STEP, transport=explode
        )


class _armed:
    """Arm the client settings for one test, restoring them after."""

    def __enter__(self) -> None:
        import dataclasses

        self._prior = client.settings
        client.settings = dataclasses.replace(
            client.settings,
            network_provision_enabled=True,
            integrator_provision_base_url="https://integrator.test",
            integrator_provision_api_key="key-1",
        )

    def __exit__(self, *exc: object) -> None:
        client.settings = self._prior


def test_armed_client_never_sends_the_password() -> None:
    seen: dict[str, object] = {}

    def record(url: str, *, content: str, headers: dict[str, str]) -> _Answer:
        seen.update(url=url, content=content, headers=headers)
        return _Answer()

    with _armed():
        receipt = client.submit_projection(
            capability_binding_id="binding-7",
            step_input=STEP,
            forbidden_material="Sup3rSecretPw",
            transport=record,
        )
    assert receipt.status == "accepted" and receipt.operation_id == "op-1"
    assert seen["url"] == "https://integrator.test/product/v1/provisions"
    assert "Sup3rSecretPw" not in str(seen["content"])


def test_armed_client_refuses_a_payload_that_would_carry_material() -> None:
    poisoned = dict(STEP, username="Sup3rSecretPw@radius")
    with _armed(), pytest.raises(
        client.NetworkProvisionRefused, match="credential material"
    ):
        client.submit_projection(
            capability_binding_id="binding-7",
            step_input=poisoned,
            forbidden_material="Sup3rSecretPw",
            transport=lambda *a, **k: _Answer(),
        )
