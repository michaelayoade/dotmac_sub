"""Translate the incumbent per-login work item into a contract step input.

Pure translation, no I/O, no decisions: the incumbent planner remains the
only decider until the suite cutover. This module inverts the incumbent's
rendered RADIUS attributes back to the neutral vocabulary of
``network.access_projection.v1`` so the same desired state can be expressed
to the Integrator connector — first for shadow comparison (connector rows
must equal incumbent rows), later as the cutover composition's plan author.

The work item's `cleartext_password` is NEVER read here. The step names the
credential by id and secret version; the secret crosses only through the
owner's fetch (ADR-0076 section 7). A test in this module's suite asserts
the password value appears nowhere in the output.

Anything the neutral vocabulary cannot express raises
`UntranslatableWorkItem` naming the login — those logins are the enumerated
shadow differences, never silently dropped attributes. Test-access grants
are the known v1 member of that set.
"""

from __future__ import annotations

import hashlib
import json
from ipaddress import ip_network

from sqlalchemy.orm import Session

from app.models.catalog import AccessCredential
from app.services.integrations.network_capability_contracts import (
    projection_fingerprint,
)
from app.services.integrations.projection_secret_fetch import (
    open_projection_request,
)
from app.services.radius_population import RadiusProjectionWorkItem

STATIC_REPLY_ATTRIBUTES = ("Service-Type", "Framed-Protocol", "Acct-Interim-Interval")

_REPLY_KEY_BY_ATTRIBUTE = {
    "Framed-IP-Address": "framed_ipv4_address",
    "Framed-IPv6-Prefix": "framed_ipv6_prefix",
    "Delegated-IPv6-Prefix": "delegated_ipv6_prefix",
    "Mikrotik-Rate-Limit": "rate_limit",
    "Mikrotik-Address-List": "address_list",
    "Idle-Timeout": "idle_timeout_seconds",
}

_MODE_TO_STATE_POLICY = {
    "active": ("enabled", "active"),
    "captive": ("enabled", "captive"),
    "reject": ("enabled", "reject"),
    "none": ("disabled", "active"),
}


class UntranslatableWorkItem(ValueError):
    """This login's desired state has no v1 neutral expression (named)."""


def translate_work_item(
    item: RadiusProjectionWorkItem,
    *,
    subject_ref: str,
    credential_id: str | None,
    secret_version: int | None,
    projection_request_ref: str | None,
    policy_version: str = "v1",
    previous_username: str | None = None,
) -> dict[str, object]:
    """One `network.access_projection.v1` step input for one login."""
    if item.test_access is not None or item.test_check_attrs or item.test_reply_attrs:
        raise UntranslatableWorkItem(
            f"{item.username}: test-access grants have no v1 expression"
        )
    mode = _MODE_TO_STATE_POLICY.get(item.mode)
    if mode is None:
        raise UntranslatableWorkItem(f"{item.username}: unknown mode {item.mode!r}")
    desired_state, policy_code = mode

    attributes: list[dict[str, str]] = []
    for attribute, op, value in item.reply_attrs:
        if attribute in STATIC_REPLY_ATTRIBUTES:
            continue
        if attribute == "Simultaneous-Use":
            attributes.append({"key": "simultaneous_use", "value": str(value)})
            continue
        if attribute == "Framed-Route":
            tokens = str(value).split()
            if op != "+=" or len(tokens) != 3 or tokens[1:] != ["0.0.0.0", "1"]:  # noqa: S104 # nosec B104 -- RADIUS gateway, not a socket bind
                raise UntranslatableWorkItem(
                    f"{item.username}: Framed-Route has no v1 expression"
                )
            cidr = tokens[0]
            try:
                if "/" not in cidr:
                    raise ValueError("missing prefix")
                ip_network(cidr, strict=False)
            except ValueError:
                raise UntranslatableWorkItem(
                    f"{item.username}: Framed-Route has no v1 expression"
                ) from None
            attributes.append({"key": "framed_route", "value": cidr})
            continue
        key = _REPLY_KEY_BY_ATTRIBUTE.get(attribute)
        if key is None:
            raise UntranslatableWorkItem(
                f"{item.username}: reply attribute {attribute!r} has no "
                "neutral vocabulary entry"
            )
        attributes.append({"key": key, "value": str(value)})

    for attribute, _op, value in item.check_attrs:
        if attribute in ("Cleartext-Password", "Crypt-Password", "Auth-Type"):
            # Auth-Type Reject is expressed by policy_code, the password by
            # credential identity. Their values never cross.
            continue
        if attribute == "Simultaneous-Use":
            attributes.append({"key": "simultaneous_use", "value": str(value)})
            continue
        raise UntranslatableWorkItem(
            f"{item.username}: check attribute {attribute!r} has no "
            "neutral vocabulary entry"
        )

    if item.profile_group and item.mode == "active":
        attributes.append({"key": "profile_group", "value": item.profile_group})

    # Only an authenticating login names a credential. Reject renders one
    # Auth-Type row and no password; naming a credential would burn the
    # single-use fetch on a secret nobody writes.
    authenticates = desired_state == "enabled" and policy_code != "reject"
    fingerprint = projection_fingerprint(
        username=item.username,
        desired_state=desired_state,
        credential_id=credential_id if authenticates else None,
        secret_version=secret_version if authenticates else None,
        attributes=[(entry["key"], entry["value"]) for entry in attributes],
    )
    return {
        "subject_ref": subject_ref,
        "username": item.username,
        "previous_username": previous_username,
        "desired_state": desired_state,
        "policy_code": policy_code,
        "policy_version": policy_version,
        "attributes": attributes,
        "credential_id": credential_id if authenticates else None,
        "secret_version": secret_version if authenticates else None,
        "projection_request_ref": projection_request_ref if authenticates else None,
        "expected_fingerprint": fingerprint,
        "valid_until": None,
    }


def open_and_translate(
    db: Session,
    item: RadiusProjectionWorkItem,
    *,
    subject_ref: str,
    credential: AccessCredential | None,
    policy_version: str = "v1",
    previous_username: str | None = None,
) -> dict[str, object]:
    """Translate, opening the single-use fetch authorization when needed.

    Authenticating logins (active/captive) get a fresh
    `ProjectionSecretRequest`; its ref and the credential's current secret
    version ride in the step input. Reject/disabled plans open nothing.
    Unsupported plans open no authorization. A supported plan's request
    joins the caller's transaction and rolls back with aborted plan work.
    """
    authenticates = item.mode in {"active", "captive"}
    if authenticates and credential is None:
        raise UntranslatableWorkItem(
            f"{item.username}: authenticating login without a credential"
        )
    credential_id = str(credential.id) if authenticates and credential else None
    # Validate every attribute before authoring any authorization. A caller
    # may catch an unsupported login and still commit its other plan work.
    step = translate_work_item(
        item,
        subject_ref=subject_ref,
        credential_id=credential_id,
        secret_version=None,
        projection_request_ref=None,
        policy_version=policy_version,
        previous_username=previous_username,
    )
    if authenticates and credential is not None:
        request = open_projection_request(db, credential=credential)
        # Build the final fingerprint from the version captured by the actual
        # authorization, keeping its binding and the submitted step identical.
        step = translate_work_item(
            item,
            subject_ref=subject_ref,
            credential_id=credential_id,
            secret_version=request.secret_version,
            projection_request_ref=request.request_ref,
            policy_version=policy_version,
            previous_username=previous_username,
        )
    return step


def command_id_for(step_input: dict[str, object]) -> str:
    """Replay the exact same plan; any changed step material gets a new id."""
    canonical = json.dumps(
        step_input, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"radius-projection:{step_input['username']}:{digest}"


__all__ = [
    "UntranslatableWorkItem",
    "command_id_for",
    "open_and_translate",
    "translate_work_item",
]
