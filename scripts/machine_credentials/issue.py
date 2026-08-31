#!/usr/bin/env python3
"""Mint one kernel machine credential. Prints the raw key ONCE, to stdout.

The kernel ships `hash_machine_key` and no minting helper, deliberately: how a
product decides who gets a credential is not the kernel's business. So issuance
is here, as a script rather than a shell one-liner, because a credential minted
from someone's terminal history leaves nothing anyone can review afterwards.

    python -m scripts.machine_credentials.issue \
        --label erp-ar-sync \
        --scope billing:invoice:read --scope customer:read ...

## What it will not do

**It will not reuse a label.** `uq_machine_credentials_tenant_label` would
refuse anyway; refusing here says why. Reissuing means minting the replacement
under a new label, moving the caller, and revoking the old row — in that order,
so there is a moment when both work and the move is observable rather than a
leap.

**It will not accept an empty scope set.** `scopes` is NOT NULL with no default
precisely so a credential cannot exist without saying what it may do, and the
kernel's `has_scope` is exact membership: an empty list authorises nothing. A
credential that can do nothing is not a safe default, it is a silent outage —
so say what you mean.

**It will not log the raw key.** It is returned once, on stdout, and never
stored: what the row holds is `hmac-sha256:<digest>`, from which the key cannot
be recovered. Losing it means minting another.

## Ordering, when replacing a live credential

1. mint the replacement (this script)
2. update the caller
3. watch the OLD row stop being used before revoking it
4. revoke

Step 3 is the one worth insisting on. During the migration window both the
kernel table and the legacy `api_keys` table are read, so a caller that did not
actually move keeps working — and revoking on the assumption that it moved is
how a scheduled job breaks at 3am rather than while someone is watching.
"""

from __future__ import annotations

import argparse
import secrets
import sys
from uuid import UUID, uuid4

from dotmac_kernel.machine_auth import MachineCredential, hash_machine_key
from dotmac_kernel.models import Tenant
from sqlalchemy import select

from app.db import SessionLocal


def _resolve_tenant(db, slug: str) -> UUID:
    tenant = db.scalar(select(Tenant).where(Tenant.slug == slug))
    if tenant is None:
        raise SystemExit(f"no tenant with slug {slug!r}")
    return tenant.id


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", required=True)
    parser.add_argument(
        "--scope",
        action="append",
        dest="scopes",
        required=True,
        help="repeatable; the credential's access is exactly these",
    )
    parser.add_argument("--tenant-slug", default="operator")
    args = parser.parse_args(argv)

    scopes = sorted({s.strip() for s in args.scopes if s.strip()})
    if not scopes:
        raise SystemExit("refusing to mint a credential with no scopes")

    raw = secrets.token_urlsafe(32)
    with SessionLocal() as db:
        tenant_id = _resolve_tenant(db, args.tenant_slug)
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
        credential = MachineCredential(
            id=uuid4(),
            tenant_id=tenant_id,
            label=args.label,
            key_hash=hash_machine_key(raw),
            scopes=scopes,
            is_active=True,
        )
        db.add(credential)
        db.commit()
        credential_id = credential.id

    print(f"credential_id: {credential_id}", file=sys.stderr)
    print(f"label:         {args.label}", file=sys.stderr)
    print(f"scopes:        {' '.join(scopes)}", file=sys.stderr)
    print("raw key (shown once, not stored):", file=sys.stderr)
    print(raw)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
