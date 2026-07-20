#!/usr/bin/env python3
"""Emit the settings/event relationship audit; exit 2 on unsafe settings."""

from __future__ import annotations

import json

from app.db import SessionLocal
from app.services.control_relationships import (
    audit_setting_relationships,
    event_policies,
    event_topology,
    relationship_manifest,
)


def main() -> int:
    with SessionLocal() as db:
        findings = audit_setting_relationships(db)
        print(
            json.dumps(
                {
                    "relationships": relationship_manifest(),
                    "event_topology": event_topology(),
                    "event_policies": event_policies(),
                    "settings_findings": [item.to_dict() for item in findings],
                },
                indent=2,
                sort_keys=True,
            )
        )
    return 2 if any(item.severity == "error" for item in findings) else 0


if __name__ == "__main__":
    raise SystemExit(main())
