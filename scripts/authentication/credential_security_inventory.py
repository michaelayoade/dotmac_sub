"""Emit a bounded credential census. Production execution requires Michael."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import asdict
from datetime import UTC, datetime

from sqlalchemy.exc import SQLAlchemyError

from app.db import read_only_snapshot_session
from app.services.credential_security_inventory import (
    CredentialInventoryQuery,
    CredentialInventoryResult,
    read_credential_inventory,
)
from app.services.domain_errors import DomainError


def render_inventory(*, result: CredentialInventoryResult) -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "observed_at": result.observed_at.isoformat(),
            "total_count": result.total_count,
            "reversible_count": result.reversible_count,
            "dormant_legacy_count": result.dormant_legacy_count,
            "cohorts": [asdict(row) for row in result.cohorts],
        },
        sort_keys=True,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("read-only",), required=True)
    # Reject arguments before session creation, including every write/apply mode.
    parser.parse_args(argv)
    query = CredentialInventoryQuery(observed_at=datetime.now(UTC))
    try:
        with read_only_snapshot_session() as db:
            result = read_credential_inventory(db=db, query=query)
        print(render_inventory(result=result))
    except (DomainError, SQLAlchemyError):
        # DB exceptions can contain SQL parameters/connection strings; never
        # serialize them or attach traceback/exception messages to this output.
        print("Credential inventory unavailable.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
