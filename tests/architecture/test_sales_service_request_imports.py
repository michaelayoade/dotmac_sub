"""Import boundaries for Sales-owned customer service-request vocabulary."""

from __future__ import annotations

import subprocess
import sys


def test_portal_service_request_schema_imports_in_a_clean_interpreter() -> None:
    """The portal contract must not trigger the Sales self-service implementation.

    ``ServiceRequestOption`` is a typed Sales vocabulary used by the portal
    schema.  Importing that vocabulary must not eagerly load ``selfserve``,
    which in turn consumes portal response types.
    """
    result = subprocess.run(  # noqa: S603 -- fixed argv, our interpreter
        [
            sys.executable,
            "-c",
            "from app.schemas.portal import QuoteRequestCreate",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, result.stderr
