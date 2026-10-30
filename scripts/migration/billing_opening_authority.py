#!/usr/bin/env python3
"""Read-only Gap B authority CLI: draft unsigned statements, verify signed ones.

Never signs, never holds a private key and writes no file (redirect stdout).
``verify`` runs every construction check the verifier runs and prints PASS or
a refusal code only; exit 0 pass, 1 refusal, 2 unavailable, usage or I/O error.
"""

from __future__ import annotations

import argparse
import re
import sys
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

from dotmac_billing.opening_contracts import OpeningVerificationUnavailable

from app.services.billing import opening_verifier as ov


def _instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("timestamps need an explicit offset")
    return parsed


def _pairs(values: Sequence[str] | None) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in values or ():
        role, sep, value = item.partition("=")
        if not sep or not value or role in result:
            raise argparse.ArgumentTypeError("use ROLE=VALUE once per role")
        result[role] = value
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    draft, verify = sub.add_parser("draft"), sub.add_parser("verify")
    for p in (draft, verify):
        for name in ("census", "assembly", "adapter"):
            p.add_argument(f"--{name}", type=Path, required=True)
        p.add_argument("--originals", type=Path)
        p.add_argument(
            "--environment", choices=("rehearsal", "production"), required=True
        )
    draft.add_argument(
        "--purpose", choices=(ov.ATTESTATION, ov.APPROVAL, ov.SEAL), required=True
    )
    draft.add_argument("--audience", required=True)
    for name in ("issued-at", "not-before", "not-after"):
        draft.add_argument(f"--{name}", type=_instant, required=True)
    draft.add_argument("--occurred-at", type=_instant)
    draft.add_argument("--statement-id")
    for name in ("attestation", "approval"):
        draft.add_argument(f"--{name}", type=Path, help="signed statement file")
    for name in (
        "window-id epoch principal-name provision-authority-ref command-key authority-ref"
    ).split():
        draft.add_argument(f"--{name}")
    for flag in ("attestation", "approval", "seal-approval"):
        required = flag != "seal-approval"
        verify.add_argument(f"--{flag}", type=Path, required=required)
        verify.add_argument(f"--{flag}-sig", type=Path, required=required)
    verify.add_argument(
        "--anchor", action="append", help="rehearsal ROLE=ed25519:<b64>"
    )
    verify.add_argument(
        "--anchor-ref", action="append", help="production ROLE=bao://m/p#f"
    )
    verify.add_argument(
        "--anchor-fingerprint", action="append", help="ROLE=sha256:<hex>"
    )
    return parser


def _draft(args: argparse.Namespace) -> bytes:
    evidence = ov.load_evidence(
        ov.OpeningEvidencePaths(
            args.census, args.assembly, args.adapter, args.originals
        )
    )
    meta = ov.StatementMeta(
        args.environment,
        args.audience,
        args.statement_id or str(uuid.uuid4()),
        args.issued_at,
        args.not_before,
        args.not_after,
    )
    params: dict[str, object] = {}
    if args.purpose in (ov.APPROVAL, ov.SEAL):
        params["source_attestation_sha256"] = ov.statement_sha256(
            args.attestation.read_bytes()
        )
    if args.purpose == ov.APPROVAL:
        params["window"] = {
            "window_id": args.window_id,
            "epoch": args.epoch,
            "principal_name": args.principal_name,
            "provision_authority_ref": args.provision_authority_ref,
        }
    if args.purpose == ov.SEAL:
        raw = args.approval.read_bytes()
        window = ov._json(raw, "statement_malformed")["body"]["window"]
        params["window_import_approval_sha256"] = ov.statement_sha256(raw)
        params["seal"] = {
            "window_id": window["window_id"],
            "epoch": window["epoch"],
            "manifest_fingerprint": evidence.fingerprint,
            "coupled_watermark": "sub-census-content:"
            + str(evidence.census["content_fingerprint"]),
            "command_key": args.command_key,
            "authority_ref": args.authority_ref,
            "occurred_at": ov.utc_text(args.occurred_at),
        }
    return ov.draft_statement(args.purpose, evidence, meta, **params)


def _verify(
    args: argparse.Namespace,
    observer: ov.SourceObservationPort | None,
    now: Callable[[], datetime],
) -> None:
    profile = ov.OpeningTrustProfile(
        args.environment,
        inline_anchors=_pairs(args.anchor),
        anchor_refs=_pairs(args.anchor_ref),
        anchor_fingerprints=_pairs(args.anchor_fingerprint),
    )
    statements = ov.OpeningStatementPaths(
        args.attestation,
        args.attestation_sig,
        args.approval,
        args.approval_sig,
        args.seal_approval,
        args.seal_approval_sig,
    )
    paths = ov.OpeningEvidencePaths(
        args.census, args.assembly, args.adapter, args.originals
    )
    ov.build_opening_verifiers(
        profile, paths, statements, observer or ov.SqlSourceObservationPort(), now
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    observer: ov.SourceObservationPort | None = None,
    now: Callable[[], datetime] | None = None,
) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        return 2 if exc.code else 0
    try:
        if args.command == "draft":
            sys.stdout.buffer.write(_draft(args))
        else:
            _verify(args, observer, now or (lambda: datetime.now(UTC)))
            print("PASS")
    except ov.OpeningVerifierConstructionError as exc:
        print(f"REFUSED:{exc.code}")
        return 1
    except OpeningVerificationUnavailable as exc:
        # Never print the cause: it may carry connection detail.
        code = str(exc.args[0]) if exc.args else ""
        print(
            f"UNAVAILABLE:{code if re.fullmatch(r'[a-z_]+', code) else 'unavailable'}"
        )
        return 2
    except (OSError, KeyError, TypeError, ValueError, argparse.ArgumentTypeError):
        print("ERROR:usage_or_io")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
