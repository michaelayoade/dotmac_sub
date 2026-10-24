"""Environment-only gate for effects that leave this process.

Outside production an outbound effect (device control, messaging, payments,
webhooks) is blocked unless its name is listed in ``OUTBOUND_EFFECTS_ALLOW``.
This is deliberately not a setting: it never reads the database or the control
registry, because a non-production copy of production data would carry the
production switch values with it. Missing or unrecognised environment identity
means blocked.

Call ``require(Effect.X)`` immediately before the transport is opened. The log
event carries the effect name only, never a target.
"""

from __future__ import annotations

import logging
from enum import StrEnum

from app.config import settings

logger = logging.getLogger(__name__)

_PRODUCTION_ENVS = frozenset({"production", "prod"})


class Effect(StrEnum):
    radius_coa = "radius_coa"
    radius_auth = "radius_auth"
    router_api = "router_api"
    device_ssh = "device_ssh"
    acs_task = "acs_task"
    sms = "sms"
    email = "email"
    payment_gateway = "payment_gateway"
    webhook_out = "webhook_out"
    social_messaging = "social_messaging"


class OutboundEffectBlocked(RuntimeError):
    """Raised when an outbound effect is not permitted in this environment."""

    def __init__(self, effect: Effect) -> None:
        super().__init__(f"outbound effect blocked: {effect.value}")
        self.effect = effect


def is_production() -> bool:
    return (settings.app_env or "").strip().lower() in _PRODUCTION_ENVS


def allowed_effects() -> frozenset[Effect]:
    """Effects listed in the deploy allowlist; unknown names permit nothing."""
    allowed: set[Effect] = set()
    for token in (settings.outbound_effects_allow or "").split(","):
        name = token.strip().lower()
        if not name:
            continue
        try:
            allowed.add(Effect(name))
        except ValueError:
            logger.warning("outbound_effects ignoring unknown allowlist entry")
    return frozenset(allowed)


def permitted(effect: Effect) -> bool:
    return is_production() or effect in allowed_effects()


def require(effect: Effect) -> None:
    """Raise ``OutboundEffectBlocked`` unless ``effect`` is permitted here."""
    if permitted(effect):
        return
    logger.warning(
        "outbound_effect_blocked effect=%s env=%s",
        effect.value,
        (settings.app_env or "unknown").strip().lower() or "unknown",
    )
    raise OutboundEffectBlocked(effect)


def status() -> dict[str, object]:
    """Read-only summary for startup logs and the CLI."""
    return {
        "env": (settings.app_env or "unknown").strip().lower() or "unknown",
        "production": is_production(),
        "allowed": sorted(e.value for e in allowed_effects()),
    }


def log_startup_status() -> None:
    state = status()
    logger.info(
        "outbound_effects env=%s production=%s allowed=%s",
        state["env"],
        state["production"],
        state["allowed"],
    )


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - CLI entry
    import json
    import sys

    args = sys.argv[1:] if argv is None else argv
    if args != ["status"]:
        print("usage: python -m app.services.outbound_effects status", file=sys.stderr)
        return 2
    print(json.dumps(status(), sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())
