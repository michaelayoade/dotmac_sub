#!/usr/bin/env python3
"""Single source of truth for the PR-label to version-bump-kind mapping.

Used by the bump job (writes step outputs) and by validate_bump_patch.py (to
recompute the expected version independently of the untrusted bump job).
Stdlib only.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

IMPACTS = ("version:major", "version:minor", "version:patch", "version:none")


def resolve(labels_json: str) -> tuple[str, str]:
    """Return (impact, bump) such as ("version:patch", "patch")."""
    labels = {
        label["name"]
        for label in json.loads(labels_json)
        if isinstance(label, dict) and isinstance(label.get("name"), str)
    }
    impacts = sorted(labels & set(IMPACTS))
    if len(impacts) > 1:
        raise ValueError(
            f"Expected one version impact label, found: {', '.join(impacts)}"
        )
    impact = impacts[0] if impacts else "version:none"
    return impact, impact.split(":", 1)[1]


def main() -> int:
    try:
        impact, bump = resolve(os.environ["LABELS_JSON"])
    except (KeyError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
        output.write(f"impact={impact}\n")
        output.write(f"bump={bump}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
