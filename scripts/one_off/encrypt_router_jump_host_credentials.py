"""Compatibility entrypoint for aggregate credential remediation."""

from __future__ import annotations

from scripts.one_off.remediate_credential_encryption import main

if __name__ == "__main__":
    raise SystemExit(main())
