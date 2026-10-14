"""Parse the optional JSON inputs of the report (layers, exceptions, assignments).

Pure and strict: anything unexpected raises ``InputError`` and the CLI refuses.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any

from app.services.composition.exceptions import ExceptionKind, ExceptionSpec
from app.services.composition.layers import Layer, LayerRevision

from .plugin import RouterBinding


class InputError(ValueError):
    """An input file is malformed."""


def _need(data: Mapping[str, Any], key: str, where: str) -> Any:
    if key not in data:
        raise InputError(f"{where}: missing {key!r}")
    return data[key]


def _str_tuple(value: Any, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)) or not all(
        isinstance(v, str) for v in value
    ):
        raise InputError(f"{where}: expected a list of strings")
    return tuple(value)


def _date(value: Any, where: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise InputError(f"{where}: not an ISO date") from exc


def parse_layers(data: Any) -> list[LayerRevision]:
    if not isinstance(data, Mapping) or not isinstance(data.get("layers"), list):
        raise InputError("layers file: expected {'layers': [...]}")
    revisions: list[LayerRevision] = []
    for index, raw in enumerate(data["layers"]):
        where = f"layers[{index}]"
        if not isinstance(raw, Mapping):
            raise InputError(f"{where}: expected an object")
        criteria_raw = raw.get("criteria", {})
        if not isinstance(criteria_raw, Mapping):
            raise InputError(f"{where}: criteria must be an object")
        layer = Layer(
            id=str(_need(raw, "id", where)),
            tier=str(_need(raw, "tier", where)),
            name=str(_need(raw, "name", where)),
            weight=int(_need(raw, "weight", where)),
            criteria={
                str(k): _str_tuple(v, f"{where}.criteria.{k}")
                for k, v in criteria_raw.items()
            },
            active=bool(raw.get("active", True)),
            kind=raw.get("kind"),
        )
        revisions.append(
            LayerRevision(
                layer,
                int(raw.get("revision", 1)),
                _need(raw, "document", where),
                raw.get("activated_at"),
            )
        )
    return revisions


def parse_exceptions(data: Any, router_ids: Mapping[str, str]) -> list[ExceptionSpec]:
    """``router_ids`` maps router name or id to the subject key (the router id)."""
    if not isinstance(data, Mapping) or not isinstance(data.get("exceptions"), list):
        raise InputError("exceptions file: expected {'exceptions': [...]}")
    out: list[ExceptionSpec] = []
    for index, raw in enumerate(data["exceptions"]):
        where = f"exceptions[{index}]"
        if not isinstance(raw, Mapping):
            raise InputError(f"{where}: expected an object")
        try:
            kind = ExceptionKind(str(_need(raw, "kind", where)))
        except ValueError as exc:
            raise InputError(f"{where}: unknown kind") from exc
        subject_key = None
        if raw.get("router") is not None:
            name = str(raw["router"])
            if name not in router_ids:
                raise InputError(f"{where}: unknown router")
            subject_key = router_ids[name]
        criteria_raw = raw.get("criteria", {})
        if not isinstance(criteria_raw, Mapping):
            raise InputError(f"{where}: criteria must be an object")
        until = raw.get("until")
        out.append(
            ExceptionSpec(
                id=str(_need(raw, "id", where)),
                module_key=str(_need(raw, "module", where)),
                kind=kind,
                reason=str(raw.get("reason", "")),
                created_on=_date(
                    _need(raw, "created_on", where), f"{where}.created_on"
                ),
                until=_date(until, f"{where}.until") if until else None,
                subject_key=subject_key,
                criteria={
                    str(k): _str_tuple(v, f"{where}.criteria.{k}")
                    for k, v in criteria_raw.items()
                },
                path=str(raw.get("path", "")),
                patch=raw.get("patch"),
                scope=raw.get("scope"),
                mode=raw.get("mode"),
                approved=bool(raw.get("approved", False)),
                revoked=bool(raw.get("revoked", False)),
                permanent=bool(raw.get("permanent", False)),
            )
        )
    return out


def parse_assignment(
    name: str, router_id: str, raw: Mapping[str, Any]
) -> RouterBinding:
    where = f"assignments[{name}]"
    if not isinstance(raw, Mapping):
        raise InputError(f"{where}: expected an object")
    return RouterBinding(
        router_id=router_id,
        name=name,
        site=raw.get("site"),
        profiles=_str_tuple(raw.get("profiles"), f"{where}.profiles"),
        tags=_str_tuple(raw.get("tags"), f"{where}.tags"),
        hardware_class=raw.get("hardware_class"),
        lifecycle=str(raw.get("lifecycle", "managed")),
        loopback_v4=raw.get("loopback_v4"),
        loopback_v6=raw.get("loopback_v6"),
    )


def parse_assignments(data: Any) -> dict[str, Mapping[str, Any]]:
    if not isinstance(data, Mapping) or not isinstance(data.get("routers"), Mapping):
        raise InputError("assignments file: expected {'routers': {name: {...}}}")
    return {str(k): v for k, v in data["routers"].items()}


def parse_sites(data: Any) -> dict[str, str]:
    if not isinstance(data, Mapping) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in data.items()
    ):
        raise InputError("sites file: expected {router name: site}")
    return dict(data)
