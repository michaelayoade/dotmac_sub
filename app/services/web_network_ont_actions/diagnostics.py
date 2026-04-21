"""Diagnostic operations for ONT web actions."""

from __future__ import annotations

import json

from sqlalchemy.orm import Session
from starlette.requests import Request

from app.services.network.ont_actions import ActionResult, OntActions
from app.services.web_network_ont_actions._common import _log_action_audit

IPHOST_CONFIG_TTL_SECONDS = 120


def run_ping_diagnostic(
    db: Session,
    ont_id: str,
    host: str,
    count: int = 4,
    *,
    request: Request | None = None,
) -> ActionResult:
    """Run ping diagnostic from ONT via TR-069."""
    result = OntActions.run_ping_diagnostic(db, ont_id, host, count)
    _log_action_audit(
        db,
        request=request,
        action="ping_diagnostic",
        ont_id=ont_id,
        metadata={
            "result": "success" if result.success else "error",
            "host": host,
            "count": count,
        },
        status_code=200 if result.success else 500,
        is_success=result.success,
    )
    return result


def run_traceroute_diagnostic(
    db: Session, ont_id: str, host: str, *, request: Request | None = None
) -> ActionResult:
    """Run traceroute diagnostic from ONT via TR-069."""
    result = OntActions.run_traceroute_diagnostic(db, ont_id, host)
    _log_action_audit(
        db,
        request=request,
        action="traceroute_diagnostic",
        ont_id=ont_id,
        metadata={"result": "success" if result.success else "error", "host": host},
        status_code=200 if result.success else 500,
        is_success=result.success,
    )
    return result


def fetch_running_config(db: Session, ont_id: str) -> ActionResult:
    """Fetch running config and return structured result."""
    return OntActions.get_running_config(db, ont_id)


def fetch_iphost_config(db: Session, ont_id: str) -> tuple[bool, str, dict[str, str]]:
    """Fetch ONT IPHOST config from OLT."""
    result = fetch_iphost_config_with_meta(db, ont_id)
    return result.ok, result.message, dict(result.data or {})


def fetch_iphost_config_with_meta(db: Session, ont_id: str):
    """Fetch ONT IPHOST config from OLT, falling back to last-known-good DB data."""
    from app.services.network.olt_ssh_ont import get_ont_iphost_config
    from app.services.olt_observed_state_adapter import (
        ObservedReadResult,
        get_cached_iphost_config,
        persist_iphost_config,
    )
    from app.services.web_network_service_ports import _resolve_ont_olt_context

    ont, olt, fsp, olt_ont_id = _resolve_ont_olt_context(db, ont_id)
    if not olt or not fsp or olt_ont_id is None:
        cached = get_cached_iphost_config(ont) if ont else None
        if cached:
            return cached
        return ObservedReadResult(
            ok=False,
            message="Cannot resolve OLT context for this ONT",
            data={},
            source="none",
        )

    cached = _read_iphost_cache(str(ont.id))
    if cached is not None:
        return ObservedReadResult(
            ok=True,
            message="Using recently fetched IPHOST configuration.",
            data=cached["config"],
            source="cache",
            fetched_at=cached["fetched_at"],
            stale=False,
        )

    ok, message, config = get_ont_iphost_config(olt, fsp, olt_ont_id)
    if ok:
        persist_iphost_config(db, ont, config)
        _write_iphost_cache(
            str(ont.id),
            config,
            fetched_at=getattr(ont, "olt_observed_snapshot_at", None),
        )
        return ObservedReadResult(
            ok=True,
            message=message,
            data=config,
            source="live",
            fetched_at=getattr(ont, "olt_observed_snapshot_at", None),
            stale=False,
        )
    cached = get_cached_iphost_config(ont)
    if cached:
        return ObservedReadResult(
            ok=True,
            message=f"Live IPHOST read unavailable: {message}",
            data=cached.data,
            source=cached.source,
            fetched_at=cached.fetched_at,
            stale=True,
        )
    return ObservedReadResult(
        ok=False,
        message=message,
        data={},
        source="live",
    )


def _iphost_cache_key(ont_id: str) -> str:
    return f"ont:{ont_id}:iphost_config"


def _read_iphost_cache(ont_id: str) -> dict[str, object] | None:
    from app.services.olt_observed_state_adapter import _parse_datetime
    from app.services.redis_client import safe_get

    raw = safe_get(_iphost_cache_key(ont_id))
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("config"), dict):
        return None
    return {
        "config": {str(key): str(value) for key, value in payload["config"].items()},
        "fetched_at": _parse_datetime(payload.get("fetched_at")),
    }


def _write_iphost_cache(
    ont_id: str, config: dict[str, str], *, fetched_at: object
) -> None:
    from app.services.redis_client import safe_set

    payload = {
        "config": dict(config),
        "fetched_at": (
            fetched_at.isoformat() if hasattr(fetched_at, "isoformat") else None
        ),
    }
    safe_set(
        _iphost_cache_key(ont_id),
        json.dumps(payload),
        ttl=IPHOST_CONFIG_TTL_SECONDS,
    )


def running_config_context(db: Session, ont_id: str) -> dict[str, object]:
    """Build display context for an ONT ACS running-config read."""
    result = fetch_running_config(db, ont_id)
    labels = {
        "device_info": "Device Info",
        "wan": "WAN / IP",
        "optical": "Optical",
        "wifi": "WiFi",
    }
    sections: list[dict[str, object]] = []
    for key, label in labels.items():
        values = (result.data or {}).get(key) if result.success else None
        if not isinstance(values, dict):
            continue
        rows = [
            {"key": row_key, "value": row_value}
            for row_key, row_value in values.items()
            if row_value is not None and str(row_value).strip() != ""
        ]
        if rows:
            sections.append({"key": key, "label": label, "rows": rows})
    return {
        "ont_id": ont_id,
        "config_result": result,
        "config_sections": sections,
    }
