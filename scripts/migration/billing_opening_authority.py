#!/usr/bin/env python3
"""Read-only Gap B authority CLI: draft unsigned statements, verify signed ones.

Never signs, never holds a private key and writes no file (redirect stdout).
``verify`` runs every construction check the verifier runs and prints PASS or
the refusal code; exit 0 pass, 1 refusal, 2 usage or I/O error.
"""

from __future__ import annotations

import argparse
import sys
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

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
    for name in ("draft", "verify"):
        p = sub.add_parser(name)
        p.add_argument("--census", type=Path, required=True)
        p.add_argument("--assembly", type=Path, required=True)
        p.add_argument("--adapter", type=Path, required=True)
        p.add_argument("--originals", type=Path)
        p.add_argument(
            "--environment", choices=("rehearsal", "production"), required=True
        )
    draft = sub.choices["draft"]
    draft.add_argument(
        "--purpose", choices=(ov.ATTESTATION, ov.APPROVAL, ov.SEAL), required=True
    )
    draft.add_argument("--audience", required=True)
    draft.add_argument("--issued-at", type=_instant, required=True)
    draft.add_argument("--not-before", type=_instant, required=True)
    draft.add_argument("--not-after", type=_instant, required=True)
    draft.add_argument("--statement-id", default=None)
    draft.add_argument("--attestation", type=Path, help="signed attestation statement")
    draft.add_argument(
        "--approval", type=Path, help="signed window approval (seal only)"
    )
    draft.add_argument("--window-id")
    draft.add_argument("--epoch")
    draft.add_argument("--principal-name")
    draft.add_argument("--provision-authority-ref")
    draft.add_argument("--command-key")
    draft.add_argument("--authority-ref")
    draft.add_argument("--occurred-at", type=_instant)
    verify = sub.choices["verify"]
    for flag in ("attestation", "approval", "seal-approval"):
        verify.add_argument(f"--{flag}", type=Path, required=flag != "seal-approval")
        verify.add_argument(
            f"--{flag}-sig", type=Path, required=flag != "seal-approval"
        )
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


def _body_of(path: Path) -> tuple[str, dict[str, object]]:
    import json

    raw = path.read_bytes()
    return ov.statement_sha256(raw), json.loads(raw)["body"]


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
        params["source_attestation_sha256"] = _body_of(args.attestation)[0]
    if args.purpose == ov.APPROVAL:
        params["window"] = {
            "window_id": args.window_id,
            "epoch": args.epoch,
            "principal_name": args.principal_name,
            "provision_authority_ref": args.provision_authority_ref,
        }
    if args.purpose == ov.SEAL:
        approval_sha, approval = _body_of(args.approval)
        window = approval["window"]
        params["window_import_approval_sha256"] = approval_sha
        params["seal"] = {
            "window_id": window["window_id"],  # type: ignore[index]
            "epoch": window["epoch"],  # type: ignore[index]
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
    if args.environment == "rehearsal":
        profile = ov.OpeningTrustProfile(
            "rehearsal", inline_anchors=_pairs(args.anchor)
        )
    else:
        profile = ov.OpeningTrustProfile(
            "production",
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
    except (OSError, KeyError, TypeError, ValueError, argparse.ArgumentTypeError):
        print("ERROR:usage_or_io")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
