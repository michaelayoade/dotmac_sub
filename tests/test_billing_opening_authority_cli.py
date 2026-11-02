"""The authority CLI drafts unsigned statements and verifies signed ones; it never signs."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
from dotmac_billing.opening_contracts import OpeningVerificationUnavailable

from app.services.billing import opening_verifier as ov
from scripts.migration import billing_opening_authority as cli
from tests import opening_verifier_support as s


def _common(w: s.World, env: str = "rehearsal") -> list[str]:
    args = [
        "--census",
        str(w.paths["census"]),
        "--assembly",
        str(w.paths["assembly"]),
        "--adapter",
        str(w.paths["adapter"]),
        "--environment",
        env,
    ]
    if "originals" in w.paths:
        args += ["--originals", str(w.paths["originals"])]
    return args


def _verify_args(w: s.World, **drop: bool) -> list[str]:
    args = ["verify", *_common(w)]
    for flag, key in (
        ("--attestation", "attestation"),
        ("--attestation-sig", "attestation_sig"),
        ("--approval", "approval"),
        ("--approval-sig", "approval_sig"),
        ("--seal-approval", "seal_approval"),
        ("--seal-approval-sig", "seal_approval_sig"),
    ):
        args += [flag, str(w.files[key])]
    args += ["--anchor", f"finance_attestor={s.pub(w.finance)}"]
    args += ["--anchor", f"migration_approver={s.pub(w.approver)}"]
    return args


def test_verify_passes_for_signed_statements(tmp_path, capsys):
    w = s.World(tmp_path)
    assert cli.main(_verify_args(w), observer=w.observer, now=w.clock) == 0
    assert capsys.readouterr().out.strip() == "PASS"


def test_verify_refuses_tampered_signature_with_exit_1(tmp_path, capsys):
    w = s.World(tmp_path)
    w.files["approval_sig"].write_bytes(
        s.sign(w.finance, "rehearsal", ov.APPROVAL, w.app)
    )
    assert cli.main(_verify_args(w), observer=w.observer, now=w.clock) == 1
    assert capsys.readouterr().out.strip() == "REFUSED:signature_invalid"


def test_verify_io_error_is_exit_2(tmp_path, capsys):
    w = s.World(tmp_path)
    args = _verify_args(w)
    args[args.index("--census") + 1] = str(tmp_path / "missing.json")
    code = cli.main(args, observer=w.observer, now=w.clock)
    assert code in (1, 2)


def test_usage_error_is_exit_2():
    assert cli.main(["draft"]) == 2


def test_draft_emits_exactly_the_statement_the_verifier_expects(tmp_path, capfd):
    w = s.World(tmp_path)
    meta = w.meta()
    args = [
        "draft",
        *_common(w),
        "--purpose",
        ov.ATTESTATION,
        "--audience",
        meta.audience,
        "--statement-id",
        meta.statement_id,
        "--issued-at",
        ov.utc_text(meta.issued_at),
        "--not-before",
        ov.utc_text(meta.not_before),
        "--not-after",
        ov.utc_text(meta.not_after),
    ]
    assert cli.main(args) == 0
    out = capfd.readouterr().out
    doc = json.loads(out)
    assert doc["purpose"] == ov.ATTESTATION and doc["signer_role"] == "finance_attestor"
    assert (
        doc["body"]
        == json.loads(ov.draft_statement(ov.ATTESTATION, w.ev, meta))["body"]
    )


def test_draft_approval_and_seal_chain(tmp_path, capfd):
    w = s.World(tmp_path)
    meta = w.meta()
    times = [
        "--audience",
        meta.audience,
        "--issued-at",
        ov.utc_text(meta.issued_at),
        "--not-before",
        ov.utc_text(meta.not_before),
        "--not-after",
        ov.utc_text(meta.not_after),
    ]
    assert (
        cli.main(
            [
                "draft",
                *_common(w),
                "--purpose",
                ov.APPROVAL,
                "--attestation",
                str(w.files["attestation"]),
                "--window-id",
                w.window["window_id"],
                "--epoch",
                w.window["epoch"],
                "--principal-name",
                w.window["principal_name"],
                "--provision-authority-ref",
                w.window["provision_authority_ref"],
                *times,
            ]
        )
        == 0
    )
    assert json.loads(capfd.readouterr().out)["body"]["window"] == w.window
    assert (
        cli.main(
            [
                "draft",
                *_common(w),
                "--purpose",
                ov.SEAL,
                "--attestation",
                str(w.files["attestation"]),
                "--approval",
                str(w.files["approval"]),
                "--command-key",
                w.seal_body["command_key"],
                "--authority-ref",
                w.seal_body["authority_ref"],
                "--occurred-at",
                w.seal_body["occurred_at"],
                *times,
            ]
        )
        == 0
    )
    assert json.loads(capfd.readouterr().out)["body"]["seal"] == w.seal_body


def test_cli_holds_no_private_key_signing_or_file_writes():
    tree = ast.parse(Path(cli.__file__).read_text())
    text = Path(cli.__file__).read_text()
    assert "Ed25519PrivateKey" not in text and ".sign(" not in text
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            assert node.attr not in {"write_bytes", "write_text"}
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "open":
            pytest.fail("CLI must not open files for writing")


def test_unavailable_prints_code_only_and_exits_2(tmp_path, capsys):
    w = s.World(tmp_path)

    class Down:
        def marker(self):
            raise OpeningVerificationUnavailable("postgresql://user:secret@db/x")

    code = cli.main(_verify_args(w), observer=Down(), now=w.clock)
    out = capsys.readouterr().out
    assert code == 2
    assert out.strip() == "UNAVAILABLE:unavailable"
    assert "secret" not in out
