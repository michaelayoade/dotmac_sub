"""Typed RouterOS REST writes with mandatory resource readback."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from app.models.router_management import Router
from app.services.router_management.connection import (
    RouterConnectionService,
    RouterTransportError,
)

_SUPPORTED_ACTIONS = frozenset({"add", "set", "remove", "enable", "disable"})
_SELECTOR_KEYS = (".id", "id", "numbers")
_SECRET_MARKERS = ("password", "secret", "private-key", "token")


class RouterWriteAdapterError(RuntimeError):
    def __init__(
        self, message: str, *, partial_result: RouterApplyResult | None = None
    ) -> None:
        super().__init__(message)
        self.partial_result = partial_result


class RouterWriteUnsupported(RouterWriteAdapterError):
    pass


class RouterPostWriteReadbackError(RouterWriteAdapterError):
    """A write may have applied, but RouterOS could not be read back."""


class RouterWriteRejected(RouterWriteAdapterError):
    """RouterOS explicitly rejected a write after earlier commands may have applied."""


@dataclass(frozen=True)
class RouterCommandPlan:
    command: str
    path: str
    resource_path: str
    action: str
    payload: dict[str, Any]
    selector: str | None = None

    def preview(self) -> dict[str, Any]:
        safe_payload = redact_router_data(self.payload)
        return {
            "command": f"{self.path} {json.dumps(safe_payload, sort_keys=True)}",
            "path": self.path,
            "resource_path": self.resource_path,
            "action": self.action,
            "payload": safe_payload,
            "verifiable": True,
        }


@dataclass
class RouterCommandResult:
    plan: RouterCommandPlan
    response: Any = None
    observed: Any = None
    verified: bool = False
    duration_ms: int = 0
    drift: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.plan.preview(),
            "response": redact_router_data(self.response),
            "observed": redact_router_data(self.observed),
            "verified": self.verified,
            "duration_ms": self.duration_ms,
            "drift": redact_router_data(self.drift),
        }


@dataclass
class RouterApplyResult:
    commands: list[RouterCommandResult]

    @property
    def verified(self) -> bool:
        return bool(self.commands) and all(row.verified for row in self.commands)

    def to_dict(self) -> dict[str, Any]:
        return {
            "write_accepted": True,
            "verified": self.verified,
            "commands": [row.to_dict() for row in self.commands],
        }


def redact_router_data(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: (
                "***REDACTED***"
                if any(marker in str(key).lower() for marker in _SECRET_MARKERS)
                else redact_router_data(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_router_data(item) for item in value]
    return value


def _contains_secret_key(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    return any(
        any(marker in str(key).lower() for marker in _SECRET_MARKERS)
        or _contains_secret_key(item)
        for key, item in value.items()
    )


def parse_routeros_rest_command(command: str) -> RouterCommandPlan:
    raw = command.strip()
    if not raw:
        raise RouterWriteUnsupported("RouterOS command cannot be blank")
    parts = raw.split(None, 1)
    path = parts[0].rstrip("/")
    if not path.startswith("/") or "/" not in path[1:]:
        raise RouterWriteUnsupported(f"Invalid RouterOS REST path: {path!r}")
    action = path.rsplit("/", 1)[1].lower()
    if action not in _SUPPORTED_ACTIONS:
        raise RouterWriteUnsupported(
            f"RouterOS action '{action}' is not supported by verified writes; "
            f"supported actions: {', '.join(sorted(_SUPPORTED_ACTIONS))}"
        )
    if len(parts) != 2:
        raise RouterWriteUnsupported(
            f"RouterOS {action} requires a JSON object payload"
        )
    try:
        payload = json.loads(parts[1])
    except json.JSONDecodeError as exc:
        raise RouterWriteUnsupported(
            f"RouterOS payload is invalid JSON at column {exc.colno}: {exc.msg}"
        ) from exc
    if not isinstance(payload, dict) or not payload:
        raise RouterWriteUnsupported("RouterOS payload must be a non-empty JSON object")
    if _contains_secret_key(payload):
        raise RouterWriteUnsupported(
            "Secret-bearing RouterOS writes are not accepted by generic config push"
        )

    selector = next(
        (str(payload[key]) for key in _SELECTOR_KEYS if payload.get(key) is not None),
        None,
    )
    if action in {"remove", "enable", "disable"} and not selector:
        raise RouterWriteUnsupported(
            f"RouterOS {action} requires one of: {', '.join(_SELECTOR_KEYS)}"
        )
    desired = {
        key: value for key, value in payload.items() if key not in _SELECTOR_KEYS
    }
    if action in {"add", "set"} and not desired:
        raise RouterWriteUnsupported(
            f"RouterOS {action} has no desired fields to verify"
        )
    return RouterCommandPlan(
        command=raw,
        path=path,
        resource_path=path.rsplit("/", 1)[0],
        action=action,
        payload=payload,
        selector=selector,
    )


def parse_routeros_rest_commands(commands: list[str]) -> list[RouterCommandPlan]:
    return [parse_routeros_rest_command(command) for command in commands]


def _normal(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value).strip().lower()


def _rows(response: Any) -> list[dict[str, Any]]:
    if isinstance(response, dict):
        return [response]
    if isinstance(response, list):
        return [row for row in response if isinstance(row, dict)]
    raise RouterPostWriteReadbackError(
        f"RouterOS readback returned {type(response).__name__}, expected JSON"
    )


def _matches(row: dict[str, Any], desired: dict[str, Any]) -> bool:
    return all(
        key in row and _normal(row[key]) == _normal(value)
        for key, value in desired.items()
    )


def _selected_rows(
    rows: list[dict[str, Any]], selector: str | None
) -> list[dict[str, Any]]:
    if selector is None:
        return rows
    return [
        row
        for row in rows
        if any(_normal(row.get(key, "")) == _normal(selector) for key in _SELECTOR_KEYS)
    ]


def verify_routeros_readback(
    plan: RouterCommandPlan, response: Any
) -> tuple[bool, Any, dict[str, Any]]:
    rows = _rows(response)
    selected = _selected_rows(rows, plan.selector)
    desired = {
        key: value for key, value in plan.payload.items() if key not in _SELECTOR_KEYS
    }
    if plan.action == "remove":
        verified = not selected
        return (
            verified,
            selected,
            {} if verified else {"expected": "absent", "observed": selected},
        )
    if plan.action in {"enable", "disable"}:
        expected = "false" if plan.action == "enable" else "true"
        verified = bool(selected) and all(
            _normal(row.get("disabled", "")) == expected for row in selected
        )
        return verified, selected, {} if verified else {"disabled": expected}
    matches = [row for row in selected if _matches(row, desired)]
    verified = bool(matches)
    return (
        verified,
        matches or selected,
        {} if verified else {"expected": redact_router_data(desired)},
    )


class RouterConfigurationWriteAdapter:
    def apply(
        self, router: Router, plans: list[RouterCommandPlan]
    ) -> RouterApplyResult:
        results: list[RouterCommandResult] = []
        for plan in plans:
            started = time.monotonic()
            try:
                response = RouterConnectionService.execute(
                    router,
                    "POST",
                    plan.path,
                    payload=plan.payload,
                    max_retries=1,
                )
            except RouterTransportError as exc:
                raise RouterPostWriteReadbackError(
                    f"RouterOS write outcome is unknown for {plan.path}: {exc}",
                    partial_result=RouterApplyResult(commands=results),
                ) from exc
            except Exception as exc:
                raise RouterWriteRejected(
                    f"RouterOS rejected {plan.path}: {exc}",
                    partial_result=RouterApplyResult(commands=results),
                ) from exc
            try:
                observed = RouterConnectionService.execute(
                    router, "GET", plan.resource_path
                )
            except Exception as exc:
                raise RouterPostWriteReadbackError(
                    f"RouterOS accepted {plan.path}, but readback failed: {exc}",
                    partial_result=RouterApplyResult(
                        commands=[
                            *results,
                            RouterCommandResult(plan=plan, response=response),
                        ]
                    ),
                ) from exc
            verified, evidence, drift = verify_routeros_readback(plan, observed)
            results.append(
                RouterCommandResult(
                    plan=plan,
                    response=response,
                    observed=evidence,
                    verified=verified,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    drift=drift,
                )
            )
            if not verified:
                break
        return RouterApplyResult(commands=results)

    def readback(
        self, router: Router, plans: list[RouterCommandPlan]
    ) -> RouterApplyResult:
        results: list[RouterCommandResult] = []
        for plan in plans:
            started = time.monotonic()
            try:
                observed = RouterConnectionService.execute(
                    router, "GET", plan.resource_path
                )
            except Exception as exc:
                raise RouterPostWriteReadbackError(
                    f"RouterOS readback failed for {plan.resource_path}: {exc}"
                ) from exc
            verified, evidence, drift = verify_routeros_readback(plan, observed)
            results.append(
                RouterCommandResult(
                    plan=plan,
                    observed=evidence,
                    verified=verified,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    drift=drift,
                )
            )
        return RouterApplyResult(commands=results)
