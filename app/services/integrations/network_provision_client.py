"""Submit Sub-authored projection plans to the Integrator provision port.

Built, not activated: every entry point refuses while
`network_provision_enabled` is off (the default), so importing this module
into the composition cannot move a byte before the network-suite-v1
cutover arms it. The client is deliberately dumb — it submits the exact
plan the translator authored and reports the Integrator's verdict. All
policy was decided upstream (access policy -> planner -> translator).

The payload can never carry a secret: the step input names the credential
by id and secret version only (ADR-0076 section 7), and the submit builder
re-checks the serialized body for the work item's password before any
transport call.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from app.config import settings
from app.services.integrations.network_projection_plan import command_id_for

PROVISION_REQUEST_SCHEMA = "dotmac.io/integration-provision-request/v1"
PROJECTION_ENDPOINT_CODE = "radius.projection.upsert"


class NetworkProvisionDisabled(RuntimeError):
    """The cutover has not armed outbound provisioning."""


class NetworkProvisionRefused(RuntimeError):
    """The Integrator answered, and the answer was not acceptance."""


@dataclass(frozen=True)
class ProvisionReceipt:
    status: str
    operation_id: str | None
    operation_state: str | None


def build_submit_request(
    *,
    capability_binding_id: str,
    step_input: dict[str, object],
    correlation_id: str | None = None,
) -> dict[str, object]:
    """One exact provision-request document for one login's desired state."""
    return {
        "schema_version": PROVISION_REQUEST_SCHEMA,
        "capability_binding_id": capability_binding_id,
        "command_id": command_id_for(step_input),
        "steps": [
            {
                "step_key": "projection",
                "endpoint_code": PROJECTION_ENDPOINT_CODE,
                "depends_on": [],
                "input": step_input,
            }
        ],
        "correlation_id": correlation_id,
    }


def submit_projection(
    *,
    capability_binding_id: str,
    step_input: dict[str, object],
    forbidden_material: str | None = None,
    correlation_id: str | None = None,
    transport: Callable[..., httpx.Response] | None = None,
) -> ProvisionReceipt:
    """Submit one projection plan; refuse loudly while disarmed."""
    if not settings.network_provision_enabled:
        raise NetworkProvisionDisabled(
            "network provisioning is disarmed until the suite cutover"
        )
    if not settings.integrator_provision_base_url:
        raise NetworkProvisionRefused("integrator provision base URL is unset")
    document = build_submit_request(
        capability_binding_id=capability_binding_id,
        step_input=step_input,
        correlation_id=correlation_id,
    )
    body = json.dumps(document)
    if forbidden_material and forbidden_material in body:
        raise NetworkProvisionRefused(
            "provision payload would carry credential material"
        )
    url = f"{settings.integrator_provision_base_url.rstrip('/')}/product/v1/provisions"
    headers = {
        "X-Api-Key": settings.integrator_provision_api_key,
        "Content-Type": "application/json",
    }
    if transport is not None:
        answer = transport(url, content=body, headers=headers)
    else:
        answer = httpx.post(url, content=body, headers=headers, timeout=15.0)
    if answer.status_code not in (200, 201, 202):
        raise NetworkProvisionRefused(
            f"integrator provision port answered {answer.status_code}"
        )
    payload = answer.json()
    return ProvisionReceipt(
        status=str(payload.get("status") or ""),
        operation_id=payload.get("operation_id"),
        operation_state=payload.get("operation_state"),
    )


__all__ = [
    "NetworkProvisionDisabled",
    "NetworkProvisionRefused",
    "ProvisionReceipt",
    "build_submit_request",
    "submit_projection",
]
