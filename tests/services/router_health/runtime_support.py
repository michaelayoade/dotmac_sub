"""Builders for runtime-poll tests (documentation ranges and synthetic names only)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from app.services.router_health.contracts import (
    CheckId,
    RuntimeObservation,
    SignalState,
)
from app.services.router_health.policy import (
    OpsUserPolicy,
    RestUserPolicy,
    RouterHealthPolicy,
    built_in_defaults,
)
from app.services.router_health.runtime_checks import (
    RuntimeEvaluation,
    RuntimeRouter,
    evaluate_runtime,
)
from app.services.router_health.settings import RouterHealthSettings
from tests.services.router_health.support import effective

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
ROUTER_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
ROUTER = RuntimeRouter(router_id=ROUTER_ID, name="Test Access")
SETTINGS = RouterHealthSettings()


def policy_with_users(**updates: object) -> RouterHealthPolicy:
    base = built_in_defaults().model_copy(
        update={
            "ops_user": OpsUserPolicy(
                name="ops-snap", group="read", required_address="192.0.2.50"
            ),
            "rest_user": RestUserPolicy(name="rest-api", forbidden_groups=["full"]),
            **updates,
        }
    )
    return RouterHealthPolicy(defaults=base)


def run(
    obs: RuntimeObservation,
    *,
    prior: dict[tuple[str, str], SignalState] | None = None,
    policy: RouterHealthPolicy | None = None,
    settings: RouterHealthSettings = SETTINGS,
    now: datetime = NOW,
    optics_enabled: bool = False,
) -> RuntimeEvaluation:
    return evaluate_runtime(
        router=ROUTER,
        observation=obs,
        prior=prior or {},
        policy=effective(policy or policy_with_users()),
        settings=settings,
        now=now,
        optics_enabled=optics_enabled,
    )


def observation(**fields: object) -> RuntimeObservation:
    base: dict[str, object] = {
        "router_id": ROUTER_ID,
        "observed_at": NOW,
        "resource": {"uptime": "1d", "version": "7.16.2"},
    }
    base.update(fields)
    return RuntimeObservation(**base)  # type: ignore[arg-type]


def state(
    signal: str,
    scope: str = "router",
    *,
    status: str = "ok",
    value: float | None = None,
    text: str | None = None,
    samples: tuple[tuple[int, float], ...] = (),
    first_abnormal_at: datetime | None = None,
    last_normal_at: datetime | None = None,
    streak: int = 0,
    observed: datetime = NOW - timedelta(minutes=15),
) -> SignalState:
    return SignalState(
        signal=signal,
        scope=scope,
        status=status,
        last_value=value,
        last_text=text,
        samples=samples,
        first_abnormal_at=first_abnormal_at,
        last_normal_at=last_normal_at,
        last_observed_at=observed,
        consecutive_abnormal=streak,
    )


def keyed(*states: SignalState) -> dict[tuple[str, str], SignalState]:
    return {(s.signal, s.scope): s for s in states}


def checks(evaluation: RuntimeEvaluation) -> set[CheckId]:
    return {f.check for f in evaluation.findings}


def find(evaluation: RuntimeEvaluation, check: CheckId):
    return [f for f in evaluation.findings if f.check is check]
