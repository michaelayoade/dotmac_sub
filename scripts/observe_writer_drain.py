"""Count-only drain CLI. Broker-only evidence never claims worker coverage."""

from __future__ import annotations

import argparse
import json
import os

from app.services.deployment_writer_drain import (
    DrainBudgets,
    DrainMode,
    DrainObservation,
    DrainObservationError,
    DrainOutcome,
    DrainRefusal,
    DrainRequest,
    DrainResult,
    WorkerNodeId,
    observe_writer_drain,
    wait_for_stable_writer_drain,
)
from app.services.deployment_writer_drain_transport import (
    RedisCeleryDrainSource,
    configured_redis_drain,
)


class _SafeParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        # argparse's usual diagnostics echo arbitrary input into stderr.
        raise ValueError("invalid CLI input")


def observation_json(observation: DrainObservation | None) -> dict[str, object] | None:
    if observation is None:
        return None
    return {
        "mode": observation.mode.value,
        "started_at": observation.started_at.isoformat(),
        "completed_at": observation.completed_at.isoformat(),
        "workers": [
            {
                "node": w.node.value,
                "active": w.active,
                "reserved": w.reserved,
                "scheduled": w.scheduled,
            }
            for w in observation.workers
        ],
        "queues": [
            {"queue": q.queue.value, "ready": q.ready} for q in observation.queues
        ],
        "unacknowledged": {
            "hash_entries": observation.unacknowledged.hash_entries,
            "index_entries": observation.unacknowledged.index_entries,
        },
    }


def result_json(result: DrainResult) -> dict[str, object]:
    return {
        "schema_version": 1,
        "mode": result.mode.value,
        "outcome": result.outcome.value,
        "refusal": result.refusal.value if result.refusal else None,
        "observation": observation_json(result.observation),
        "previous_zero": observation_json(result.previous_zero),
    }


def main(argv: list[str] | None = None) -> int:
    parser = _SafeParser(description=__doc__)
    parser.add_argument("operation", choices=("inspect", "wait-for-stable-empty"))
    parser.add_argument(
        "--mode", required=True, choices=tuple(m.value for m in DrainMode)
    )
    parser.add_argument("--expected-node", action="append", default=[])
    parser.add_argument("--inspection-timeout", type=float, default=5.0)
    parser.add_argument("--drain-budget", type=float, default=600.0)
    parser.add_argument("--stability-interval", type=float, default=5.0)
    mode: DrainMode | None = None
    try:
        args = parser.parse_args(argv)
        mode = DrainMode(args.mode)
        request = DrainRequest(
            mode=mode,
            expected_nodes=tuple(WorkerNodeId(node) for node in args.expected_node),
            budgets=DrainBudgets(
                args.inspection_timeout, args.drain_budget, args.stability_interval
            ),
        )
        source = RedisCeleryDrainSource(
            configuration=configured_redis_drain(environment=os.environ)
        )
        result = (
            observe_writer_drain(request=request, source=source)
            if args.operation == "inspect"
            else wait_for_stable_writer_drain(request=request, source=source)
        )
        evidence = result_json(result)
        evidence["request"] = {
            "expected_nodes": [node.value for node in request.expected_nodes],
            "declared_queues": [queue.value for queue in request.declared_queues],
            "inspection_timeout": request.budgets.inspection_timeout,
            "drain_budget": request.budgets.drain_budget,
            "stability_interval": request.budgets.stability_interval,
        }
        exit_code = (
            2
            if result.outcome is DrainOutcome.REFUSED
            else 1
            if result.outcome
            in (DrainOutcome.OBSERVED_BUSY, DrainOutcome.DEADLINE_EXCEEDED)
            else 0
        )
    except (ValueError, DrainObservationError) as exc:
        refusal = (
            exc.code
            if isinstance(exc, DrainObservationError)
            else DrainRefusal.INVALID_INPUT
        )
        evidence = {
            "schema_version": 1,
            "mode": mode.value if mode else None,
            "outcome": DrainOutcome.REFUSED.value,
            "refusal": refusal.value,
            "observation": None,
            "previous_zero": None,
        }
        exit_code = 2
    print(json.dumps(evidence, separators=(",", ":"), allow_nan=False))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
