#!/usr/bin/env python3
"""Issue one Kernel machine credential; print its raw key once after commit.

The caller's real source application must be named in this deployment's
ACCEPTED_SOURCE_APPLICATIONS configuration. The dedicated HMAC material is
loaded through Sub's held OpenBao SecretSource before issuance. Existing
labels are never reused; issue a replacement, move its caller, then revoke
the old credential after observing that it is no longer used.
"""

from __future__ import annotations

import argparse
import sys

from dotmac_kernel.machine_auth import MACHINE_KEY_SECRET_NAME
from dotmac_kernel.machine_models import MachineCredential
from dotmac_kernel.machine_rotation import issue_credential
from dotmac_kernel.secret_sources import get_secret
from dotmac_kernel.source_applications import (
    SourceApplicationRegistry,
    install_source_applications,
)
from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal
from app.services.kernel_secret_source import install as install_secret_source
from app.services.operator_tenant import operator_tenant


def _install_issuance_context(source_application: str) -> None:
    codes = [
        code.strip()
        for code in settings.accepted_source_applications.split(",")
        if code.strip()
    ]
    registry = SourceApplicationRegistry(codes)
    registry.require(source_application)
    install_source_applications(registry)
    install_secret_source()
    if not get_secret(MACHINE_KEY_SECRET_NAME):
        raise SystemExit(f"refusing issuance: {MACHINE_KEY_SECRET_NAME} is not held")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", required=True)
    parser.add_argument("--source-application", required=True)
    parser.add_argument(
        "--scope",
        action="append",
        dest="scopes",
        required=True,
        help="repeatable; the credential's access is exactly these",
    )
    args = parser.parse_args(argv)

    scopes = sorted({s.strip() for s in args.scopes if s.strip()})
    if not scopes:
        raise SystemExit("refusing to mint a credential with no scopes")

    _install_issuance_context(args.source_application)
    with SessionLocal() as db:
        tenant_id = operator_tenant(db).id
        existing = db.scalar(
            select(MachineCredential).where(
                MachineCredential.tenant_id == tenant_id,
                MachineCredential.label == args.label,
            )
        )
        if existing is not None:
            raise SystemExit(
                f"label {args.label!r} already exists for this tenant "
                f"({existing.id}). Mint the replacement under a new label, move "
                "the caller, then revoke the old row."
            )
        credential, raw = issue_credential(
            db,
            tenant_id=tenant_id,
            label=args.label,
            source_application=args.source_application,
            scopes=scopes,
        )
        credential_id = credential.id
        db.commit()

    print(f"credential_id: {credential_id}", file=sys.stderr)
    print(f"label:         {args.label}", file=sys.stderr)
    print(f"source:        {args.source_application}", file=sys.stderr)
    print(f"scopes:        {' '.join(scopes)}", file=sys.stderr)
    print("raw key (shown once, not stored):", file=sys.stderr)
    print(raw)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
