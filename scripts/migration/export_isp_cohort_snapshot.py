#!/usr/bin/env python3
"""Operator entry point for the cohort-isp-01 read-only export.

Thin by construction: it parses arguments, opens the one read-only snapshot
seam, calls `migration.cohort_export`, and serialises what comes back. Every
decision — tenant resolution, contract-version admission, field minimisation,
canonicalisation — belongs to the owning service, so a second adapter cannot
reach a different answer.

Two modes, and the default is the safe one.

`--digest` (default) prints the privacy-safe comparison artifact: identities
and hashes, no field values. It is meant to be read on a terminal, attached to
a control record and handed to whoever runs the shadow comparison.

`--snapshot` writes the full typed export, which carries customer identity
data. It refuses to print to a terminal and requires `--out`, and the file is
created 0600. That asymmetry is deliberate: the artifact people will run most
often should be the one that cannot leak, and the one that carries personal
data should take a deliberate extra step.

This script reads. It writes no database row, completes no transaction, and
contacts no destination.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.db import read_only_snapshot_session  # noqa: E402
from app.migration_source.cohort import CohortEntityType  # noqa: E402
from app.migration_source.snapshot import (  # noqa: E402
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    ContractVersion,
)
from app.services.migration_source_export import (  # noqa: E402
    CohortExportCommand,
    CohortExportError,
    export_cohort_digest,
    export_page,
)
from app.services.operator_tenant import operator_tenant_id  # noqa: E402


def _dump(payload: Any) -> str:
    return json.dumps(payload, indent=2, sort_keys=True, default=str)


def _write_private(path: Path, body: str) -> None:
    """Write a file only its owner can read, without a world-readable moment."""

    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(body)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--digest",
        action="store_true",
        help="print the privacy-safe comparison digest (default)",
    )
    mode.add_argument(
        "--snapshot",
        action="store_true",
        help="write the full typed export; requires --out",
    )
    parser.add_argument(
        "--entity-type",
        choices=sorted(member.value for member in CohortEntityType),
        help="with --snapshot, the single entity type to page",
    )
    parser.add_argument("--after", help="resume a snapshot after this source id")
    parser.add_argument(
        "--page-size", type=int, default=DEFAULT_PAGE_SIZE, help="rows per page"
    )
    parser.add_argument(
        "--contract-version",
        default=ContractVersion.V1.value,
        help="export contract version to produce",
    )
    parser.add_argument("--out", type=Path, help="destination file, created 0600")
    arguments = parser.parse_args(argv)

    if arguments.page_size < 1 or arguments.page_size > MAX_PAGE_SIZE:
        parser.error(f"--page-size must be between 1 and {MAX_PAGE_SIZE}")
    if arguments.snapshot:
        if arguments.entity_type is None:
            parser.error("--snapshot needs --entity-type")
        if arguments.out is None:
            parser.error(
                "--snapshot carries customer identity data and will not print "
                "to a terminal; give it --out"
            )

    try:
        with read_only_snapshot_session() as db:
            tenant_id = operator_tenant_id()
            if arguments.snapshot:
                page = export_page(
                    db,
                    CohortExportCommand(
                        contract_version=arguments.contract_version,
                        entity_type=CohortEntityType(arguments.entity_type),
                        after_source_id=arguments.after,
                        page_size=arguments.page_size,
                        tenant_id=tenant_id,
                    ),
                )
                body = _dump(page.model_dump(mode="json"))
            else:
                digest = export_cohort_digest(
                    db,
                    contract_version=arguments.contract_version,
                    tenant_id=tenant_id,
                    page_size=arguments.page_size,
                )
                body = _dump(digest.model_dump(mode="json"))
    except CohortExportError as error:
        print(f"export refused: {error}", file=sys.stderr)
        return 2
    except ValueError as error:
        print(f"export refused: {error}", file=sys.stderr)
        return 2

    if arguments.out is not None:
        _write_private(arguments.out, body + "\n")
        print(f"wrote {arguments.out} ({len(body)} bytes, mode 0600)")
    else:
        print(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
