"""Observed OLT-side state cache and persistence helpers."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy.orm import Session

if TYPE_CHECKING:
    from app.services.network.olt_ssh_profiles import Tr069ServerProfile

from app.models.network import OLTDevice
from app.services.adapters import adapter_registry

logger = logging.getLogger(__name__)

TR069_PROFILE_TTL_SECONDS = 120


@dataclass(frozen=True)
class ObservedReadResult:
    ok: bool
    message: str
    data: Any
    source: str
    fetched_at: datetime | None = None
    stale: bool = False

    @property
    def freshness(self) -> dict[str, object]:
        return {
            "source": self.source,
            "fetched_at": self.fetched_at,
            "stale": self.stale,
            "message": self.message,
        }


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _parse_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def profile_to_dict(profile: object) -> dict[str, object]:
    """Convert a profile object to a dictionary for JSON serialization.

    Used to serialize profiles for the OLT's tr069_profiles_snapshot field.
    Public API - used by olt_tr069_admin for profile snapshot refresh.
    """
    if hasattr(profile, "__dataclass_fields__"):
        return dict(asdict(cast(Any, profile)))
    return {
        "profile_id": getattr(profile, "profile_id", None),
        "name": getattr(profile, "name", ""),
        "acs_url": getattr(profile, "acs_url", ""),
        "acs_username": getattr(profile, "acs_username", ""),
        "inform_interval": getattr(profile, "inform_interval", 0),
        "binding_count": getattr(profile, "binding_count", 0),
    }


def _profiles_from_payload(payload: object) -> list[Tr069ServerProfile]:
    from app.services.network.olt_ssh_profiles import Tr069ServerProfile

    profiles = payload if isinstance(payload, list) else []
    result: list[Tr069ServerProfile] = []
    for item in profiles:
        if not isinstance(item, dict):
            continue
        result.append(
            Tr069ServerProfile(
                profile_id=int(item.get("profile_id") or 0),
                name=str(item.get("name") or ""),
                acs_url=str(item.get("acs_url") or ""),
                acs_username=str(item.get("acs_username") or ""),
                inform_interval=int(item.get("inform_interval") or 0),
                binding_count=int(item.get("binding_count") or 0),
            )
        )
    return result


def _profile_cache_key(olt_id: object) -> str:
    return f"olt:{olt_id}:tr069_profiles"


def _read_redis_json(key: str) -> dict[str, object] | None:
    from app.services.redis_client import safe_get

    raw = safe_get(key)
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        logger.debug("Invalid JSON in observed-state cache key %s", key)
        return None
    return value if isinstance(value, dict) else None


def _write_redis_json(key: str, value: dict[str, object], ttl: int) -> None:
    from app.services.redis_client import safe_set

    try:
        safe_set(key, json.dumps(value, default=str), ttl=ttl)
    except TypeError:
        logger.debug("Observed-state cache encode failed for %s", key, exc_info=True)


class OltObservedStateAdapter:
    """Read and persist observed OLT-side state behind one adapter boundary."""

    name = "olt.observed_state"

    def get_tr069_profiles_for_olt(
        self,
        db: Session,
        olt: OLTDevice,
        *,
        ttl_seconds: int = TR069_PROFILE_TTL_SECONDS,
        force_live: bool = False,
    ) -> ObservedReadResult:
        """Return OLT TR-069 profiles using Redis TTL, SSH, then DB fallback."""
        cache_key = _profile_cache_key(olt.id)
        if not force_live:
            cached = _read_redis_json(cache_key)
            if cached:
                profiles = _profiles_from_payload(cached.get("profiles"))
                fetched_at = _parse_datetime(cached.get("fetched_at"))
                return ObservedReadResult(
                    ok=True,
                    message="Using recently fetched TR-069 profile list.",
                    data=profiles,
                    source="cache",
                    fetched_at=fetched_at,
                    stale=False,
                )

        from app.services.network.olt_ssh_profiles import get_tr069_server_profiles

        ok, msg, profiles = get_tr069_server_profiles(olt)
        if ok:
            fetched_at = _utc_now()
            profile_payload = [profile_to_dict(profile) for profile in profiles]
            payload: dict[str, object] = {
                "profiles": profile_payload,
                "fetched_at": fetched_at.isoformat(),
            }
            _write_redis_json(cache_key, payload, ttl_seconds)
            olt.tr069_profiles_snapshot = payload
            olt.tr069_profiles_snapshot_at = fetched_at
            db.add(olt)
            db.flush()
            return ObservedReadResult(
                ok=True,
                message=msg,
                data=profiles,
                source="live",
                fetched_at=fetched_at,
                stale=False,
            )

        snapshot = (
            olt.tr069_profiles_snapshot
            if isinstance(olt.tr069_profiles_snapshot, dict)
            else {}
        )
        profiles = _profiles_from_payload(snapshot.get("profiles"))
        fetched_at = (
            _parse_datetime(snapshot.get("fetched_at"))
            or olt.tr069_profiles_snapshot_at
        )
        if profiles:
            return ObservedReadResult(
                ok=True,
                message=f"Live profile read unavailable: {msg}",
                data=profiles,
                source="db",
                fetched_at=fetched_at,
                stale=True,
            )
        return ObservedReadResult(
            ok=False,
            message=msg,
            data=[],
            source="live",
            fetched_at=None,
            stale=False,
        )

    def get_cached_tr069_profiles_for_olt(self, olt: OLTDevice) -> ObservedReadResult:
        """Return DB-cached TR-069 profiles without Redis or SSH reads."""
        snapshot = (
            olt.tr069_profiles_snapshot
            if isinstance(olt.tr069_profiles_snapshot, dict)
            else {}
        )
        profiles = _profiles_from_payload(snapshot.get("profiles"))
        fetched_at = (
            _parse_datetime(snapshot.get("fetched_at"))
            or olt.tr069_profiles_snapshot_at
        )
        if profiles:
            return ObservedReadResult(
                ok=True,
                message="Using DB-cached TR-069 profile list.",
                data=profiles,
                source="db",
                fetched_at=fetched_at,
                stale=True,
            )
        return ObservedReadResult(
            ok=True,
            message="No cached TR-069 profile list.",
            data=[],
            source="db",
            fetched_at=fetched_at,
            stale=True,
        )

    def get_cached_iphost_config(self, ont: object) -> ObservedReadResult:
        """Return cached IPHOST config without Redis or OLT reads.

        IPHOST live reads are intentionally limited to the dedicated OLT action
        views. The unified config overview derives desired state from durable
        ONT/assignment/config-pack data.
        """
        return ObservedReadResult(
            ok=True,
            message="No cached IPHOST configuration.",
            data={},
            source="db",
            fetched_at=None,
            stale=True,
        )



olt_observed_state_adapter = OltObservedStateAdapter()
adapter_registry.register(olt_observed_state_adapter)


def get_tr069_profiles_for_olt(
    db: Session,
    olt: OLTDevice,
    *,
    ttl_seconds: int = TR069_PROFILE_TTL_SECONDS,
    force_live: bool = False,
) -> ObservedReadResult:
    return olt_observed_state_adapter.get_tr069_profiles_for_olt(
        db,
        olt,
        ttl_seconds=ttl_seconds,
        force_live=force_live,
    )


def get_cached_tr069_profiles_for_olt(olt: OLTDevice) -> ObservedReadResult:
    return olt_observed_state_adapter.get_cached_tr069_profiles_for_olt(olt)


def get_cached_iphost_config(ont: object) -> ObservedReadResult:
    return olt_observed_state_adapter.get_cached_iphost_config(ont)
