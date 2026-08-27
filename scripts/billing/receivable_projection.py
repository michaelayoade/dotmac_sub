"""Operator CLI for the `receivable-shadow-01` projection and parity report.

A thin adapter: it parses arguments, opens the session, calls the registered
owner, and prints the typed result. It owns no business decision, moves no
authority, and creates no collections case.

## Dry run is the only default, and there is no way to spell it wrong

Every writing subcommand takes `--apply` and nothing else. There is deliberately
**no `--dry-run` flag**: a flag that must be *present* to be safe is one typo,
one shell-quoting accident, or one copied-and-edited runbook line away from a
write. Omitting an argument here can only mean dry run.

`cohort` and `parity` never write at all, with or without `--apply`; `parity
--apply` records the report as durable run evidence and still changes no
projected row.

Subcommands::

    poetry run python -m scripts.billing.receivable_projection cohort \
        --window-start 2026-07-01T00:00:00+00:00 \
        --window-end   2026-08-01T00:00:00+00:00 \
        --cutoff       2026-08-25T00:00:00+00:00

    poetry run python -m scripts.billing.receivable_projection backfill \
        --window-start ... --window-end ... --cutoff ... \
        --code-version <sha> --schema-version <alembic revision> \
        --idempotency-key <key> [--apply]

    poetry run python -m scripts.billing.receivable_projection reconcile   [same args]
    poetry run python -m scripts.billing.receivable_projection repair-drift [same args]
    poetry run python -m scripts.billing.receivable_projection parity      [same args]
    poetry run python -m scripts.billing.receivable_projection readiness   [same args]

`--strict` exits non-zero when a pass reports drift (missing, stale-skipped,
ambiguous-watermark or orphaned rows) or when parity reports a divergence, so
the same command is usable as a CI or runbook gate. A `not_expressible` count
is NOT a strict failure: it is a recorded, pinned limit, not a regression.

`readiness` is the stronger, always-read-only authority-review gate. It exits
non-zero unless the compared cohort is non-empty, fully classified, converged,
fully expressible, divergence-free, and carries no standing contract blocker.
Passing it still does not move authority or retire a writer.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from datetime import datetime
from uuid import uuid4

from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models.billing_receivable_projection import ReceivableProjectionRunKind
from app.services.billing.receivable_cohort import (
    COHORT_DEFINITION_VERSION,
    COHORT_NAME,
    PROJECTION_POLICY_VERSION,
    ReceivableCohortWindow,
    definition_payload,
    definition_seal,
)
from app.services.billing.receivable_parity import (
    assess_receivable_cutover_readiness,
    evaluate_receivable_parity,
)
from app.services.billing.receivable_projection import (
    ProjectionMode,
    ReceivableProjectionError,
    ReconcileReceivableProjectionCommand,
    reconcile_receivable_projection,
)
from app.services.owner_commands import CommandContext

_ACTOR = "operator:receivable_projection"

_RUN_KINDS = {
    "backfill": ReceivableProjectionRunKind.backfill,
    "reconcile": ReceivableProjectionRunKind.reconcile,
    "repair-drift": ReceivableProjectionRunKind.drift_repair,
    "parity": ReceivableProjectionRunKind.parity_report,
}


def _emit(payload: object) -> None:
    print(json.dumps(payload, indent=2, default=str))


def _instant(raw: str, *, label: str) -> datetime:
    """Parse an ISO-8601 instant, refusing a naive one.

    Refused rather than assumed-UTC: an operator who omitted the offset does
    not yet know which instant they meant, and guessing silently shifts a
    cohort boundary by the host's timezone.
    """
    try:
        value = datetime.fromisoformat(raw)
    except ValueError as exc:  # pragma: no cover - argparse surfaces the message
        raise SystemExit(f"{label} is not a valid ISO-8601 instant: {raw!r}") from exc
    if value.tzinfo is None or value.utcoffset() is None:
        raise SystemExit(
            f"{label} must carry a UTC offset (e.g. 2026-08-01T00:00:00+00:00); "
            "a naive instant cannot seal a reproducible cohort"
        )
    return value


def _window(args: argparse.Namespace) -> ReceivableCohortWindow:
    return ReceivableCohortWindow(
        cutoff_at=_instant(args.cutoff, label="--cutoff"),
        window_start=_instant(args.window_start, label="--window-start"),
        window_end=_instant(args.window_end, label="--window-end"),
    )


def _context(args: argparse.Namespace, *, scope: str) -> CommandContext:
    return CommandContext.system(
        actor=_ACTOR,
        scope=scope,
        reason=getattr(args, "reason", None) or f"{scope} over {COHORT_NAME}",
        idempotency_key=(
            getattr(args, "idempotency_key", None) or f"{scope}:{uuid4()}"
        ),
    )


def _mode(args: argparse.Namespace) -> ProjectionMode:
    return ProjectionMode.APPLY if args.apply else ProjectionMode.DRY_RUN


def _cmd_cohort(_db: Session, args: argparse.Namespace) -> int:
    """Print the sealed cohort definition. Touches no table."""
    window = _window(args)
    _emit(
        {
            "cohort_name": COHORT_NAME,
            "definition_version": COHORT_DEFINITION_VERSION,
            "projection_policy_version": PROJECTION_POLICY_VERSION,
            "definition_seal": definition_seal(window),
            "definition": definition_payload(window),
        }
    )
    return 0


def _drift_total(payload: Mapping[str, object]) -> int:
    total = 0
    for key in (
        "missing_count",
        "stale_skipped_count",
        "ambiguous_watermark_count",
        "orphaned_count",
    ):
        value = payload.get(key, 0)
        if not isinstance(value, int):
            raise TypeError(f"{key} must be an integer count")
        total += value
    return total


def _cmd_project(db: Session, args: argparse.Namespace, *, subcommand: str) -> int:
    command = ReconcileReceivableProjectionCommand(
        context=_context(args, scope=subcommand),
        window=_window(args),
        code_version=args.code_version,
        database_schema_version=args.schema_version,
        run_kind=_RUN_KINDS[subcommand],
        mode=_mode(args),
    )
    try:
        result = reconcile_receivable_projection(db, command)
    except ReceivableProjectionError as exc:
        _emit({"error": exc.code, "message": exc.message, "details": exc.details})
        return 2

    payload = {
        "subcommand": subcommand,
        "mode": result.mode.value,
        "run_id": result.run_id,
        "cohort_name": result.cohort_name,
        "cohort_definition_seal": result.cohort_definition_seal,
        "membership_digest": result.membership_digest,
        "cohort_count": result.cohort_count,
        "classification": result.classification_counts,
        "inserted_count": result.inserted_count,
        "updated_count": result.updated_count,
        "unchanged_count": result.unchanged_count,
        "stale_skipped_count": result.stale_skipped_count,
        "ambiguous_watermark_count": result.ambiguous_watermark_count,
        "orphaned_count": result.orphaned_count,
        "missing_count": result.missing_count,
        "currency_totals": result.currency_totals,
        "source_fingerprint": result.source_fingerprint,
        "result_fingerprint": result.result_fingerprint,
        "projection_version_low": result.projection_version_low,
        "projection_version_high": result.projection_version_high,
        "blockers": list(result.blockers),
    }
    _emit(payload)
    if args.strict and _drift_total(payload) > 0:
        return 1
    return 0


def _cmd_parity(db: Session, args: argparse.Namespace) -> int:
    window = _window(args)
    context = _context(args, scope="parity")
    report = evaluate_receivable_parity(
        db,
        window=window,
        context=context,
        code_version=args.code_version,
        database_schema_version=args.schema_version,
    )
    _emit(
        {
            "subcommand": "parity",
            "recorded": bool(args.apply),
            "cohort_definition_seal": report.cohort_definition_seal,
            "evaluated_count": report.evaluated_count,
            "unprojected_count": report.unprojected_count,
            "matched_count": report.matched_count,
            "diverged_count": report.diverged_count,
            "not_expressible_count": report.not_expressible_count,
            "by_dimension": report.by_dimension,
            "not_expressible_reasons": report.not_expressible_reasons,
            "blockers": list(report.blockers),
            "report_fingerprint": report.report_fingerprint,
        }
    )
    if args.apply:
        # Recording the report is a write, so it goes through the one owner of
        # the run row rather than being inserted here. The projection pass it
        # performs is idempotent, so recording evidence cannot change a row.
        reconcile_receivable_projection(
            db,
            ReconcileReceivableProjectionCommand(
                context=_context(args, scope="parity-record"),
                window=window,
                code_version=args.code_version,
                database_schema_version=args.schema_version,
                run_kind=ReceivableProjectionRunKind.parity_report,
                mode=ProjectionMode.APPLY,
                parity_evidence=report.as_run_evidence(),
            ),
        )
    if args.strict and report.diverged_count > 0:
        return 1
    return 0


def _cmd_readiness(db: Session, args: argparse.Namespace) -> int:
    """Emit the sealed read-only authority-review gate and never write."""
    report = evaluate_receivable_parity(
        db,
        window=_window(args),
        context=_context(args, scope="receivable-readiness"),
        code_version=args.code_version,
        database_schema_version=args.schema_version,
    )
    readiness = assess_receivable_cutover_readiness(report)
    _emit(
        {
            "subcommand": "readiness",
            "ready": readiness.ready,
            "cohort_definition_seal": readiness.cohort_definition_seal,
            "membership_digest": readiness.membership_digest,
            "report_fingerprint": readiness.report_fingerprint,
            "readiness_fingerprint": readiness.readiness_fingerprint,
            "blockers": [
                {
                    "code": blocker.code.value,
                    "count": blocker.count,
                    "detail": blocker.detail,
                }
                for blocker in readiness.blockers
            ],
        }
    )
    return 0 if readiness.ready else 1


def _add_window_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--window-start", required=True)
    parser.add_argument("--window-end", required=True)
    parser.add_argument("--cutoff", required=True)


def _add_run_arguments(parser: argparse.ArgumentParser) -> None:
    _add_window_arguments(parser)
    parser.add_argument("--code-version", required=True)
    parser.add_argument("--schema-version", required=True)
    parser.add_argument("--idempotency-key")
    parser.add_argument("--reason")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write. Omitted means dry run; there is no --dry-run flag.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit non-zero on drift or divergence, for use as a gate.",
    )


def _add_readiness_arguments(parser: argparse.ArgumentParser) -> None:
    _add_window_arguments(parser)
    parser.add_argument("--code-version", required=True)
    parser.add_argument("--schema-version", required=True)
    parser.add_argument("--reason")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="receivable_projection", description=__doc__)
    sub = parser.add_subparsers(dest="subcommand", required=True)

    cohort = sub.add_parser("cohort", help="print the sealed cohort definition")
    _add_window_arguments(cohort)

    for name in ("backfill", "reconcile", "repair-drift", "parity"):
        _add_run_arguments(sub.add_parser(name))
    _add_readiness_arguments(
        sub.add_parser(
            "readiness",
            help="read-only sealed authority-review gate",
        )
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    with SessionLocal() as db:
        if args.subcommand == "cohort":
            return _cmd_cohort(db, args)
        if args.subcommand == "parity":
            return _cmd_parity(db, args)
        if args.subcommand == "readiness":
            return _cmd_readiness(db, args)
        return _cmd_project(db, args, subcommand=args.subcommand)


if __name__ == "__main__":  # pragma: no cover - console entry point
    sys.exit(main())
