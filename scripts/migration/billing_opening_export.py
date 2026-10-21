#!/usr/bin/env python3
"""Print a read-only current-snapshot billing census; never import or apply it."""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Sequence
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import TypeAlias
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError

from app.db import read_only_snapshot_session
from app.services.billing.opening_export import (
    BillingOpeningExportError,
    preview_billing_opening_export,
)
from app.services.billing.opening_export_contracts import (
    BillingOpeningExportPreviewV2,
    BillingOpeningExportPreviewV3,
    PreviewBillingOpeningExportQuery,
)

JSONValue: TypeAlias = (
    None | bool | int | str | list["JSONValue"] | dict[str, "JSONValue"]
)


def _json_value(value: object) -> JSONValue:
    """Encode evidence losslessly; unsupported scalars never become strings."""
    if isinstance(value, Enum):
        return _json_value(value.value)
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("Non-finite evidence cannot be serialized")
        return str(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Evidence timestamps must have an explicit offset")
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("Evidence JSON keys must be strings")
        return {key: _json_value(item) for key, item in value.items()}
    raise TypeError(f"Unsupported evidence value: {type(value).__name__}")


def _source_sha(value: str) -> str:
    if re.fullmatch(r"[0-9a-f]{40}", value) is None:
        raise argparse.ArgumentTypeError("use an exact lowercase 40-character Git SHA")
    return value


def _cutoff(value: str) -> datetime:
    try:
        cutoff = datetime.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("use an ISO timestamp with an offset") from exc
    if cutoff.tzinfo is None or cutoff.utcoffset() is None:
        raise argparse.ArgumentTypeError("economic cutoff requires an explicit offset")
    return cutoff


def _currency_precision(value: str) -> tuple[str, int]:
    if re.fullmatch(r"[A-Z]{3}:2", value) is None:
        raise argparse.ArgumentTypeError("use CURRENCY:2, for example NGN:2")
    return value[:3], 2


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-repository-sha", type=_source_sha, required=True)
    parser.add_argument("--installation-id", type=UUID, required=True)
    parser.add_argument("--economic-cutoff", type=_cutoff, required=True)
    parser.add_argument(
        "--currency-precision", type=_currency_precision, action="append", required=True
    )
    parser.add_argument(
        "--population-version",
        choices=(
            "sub-billing-opening-census-v1",
            "sub-billing-opening-census-v2",
            "sub-billing-opening-census-v3",
        ),
        required=True,
    )
    args = parser.parse_args(argv)
    precisions = tuple(args.currency_precision)
    if len(dict(precisions)) != len(precisions):
        parser.error("declare each currency precision exactly once")
    query = PreviewBillingOpeningExportQuery(
        source_repository_sha=args.source_repository_sha,
        installation_id=args.installation_id,
        economic_cutoff=args.economic_cutoff,
        currency_precisions=precisions,
        population_version=args.population_version,
    )
    try:
        with read_only_snapshot_session() as db:
            report = preview_billing_opening_export(db, query=query)
        output = _json_value(asdict(report))
    except BillingOpeningExportError as exc:
        print(json.dumps({"complete": False, "error": exc.code}, sort_keys=True))
        return 2
    except SQLAlchemyError:
        print(
            json.dumps(
                {"complete": False, "error": "snapshot_unavailable"}, sort_keys=True
            )
        )
        return 2
    except (TypeError, ValueError):
        print(
            json.dumps(
                {"complete": False, "error": "evidence_serialization_failed"},
                sort_keys=True,
            )
        )
        return 2
    print(json.dumps(output, sort_keys=True, allow_nan=False))
    return (
        1
        if report.blocking_exceptions
        or (
            isinstance(report, BillingOpeningExportPreviewV2)
            and report.cash_evidence.blocking_exceptions
        )
        or (
            isinstance(report, BillingOpeningExportPreviewV3)
            and report.documentary_observations.unavailable_original_evidence
        )
        else 0
    )


if __name__ == "__main__":
    raise SystemExit(main())
