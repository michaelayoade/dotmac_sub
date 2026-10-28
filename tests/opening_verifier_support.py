"""Synthetic census/assembly/originals and signing helpers for Gap B tests.

Everything here is fabricated: documentation-range style ids, ephemeral
Ed25519 keys generated per test, no estate values, no secrets.
"""

from __future__ import annotations

import base64
import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from dotmac_billing.contracts import (
    AppliedTaxSnapshotV1,
    PartyDocumentSnapshotV1,
    PartyTaxIdentitySnapshotV1,
    PaymentInstructionsSnapshotV1,
    PostalAddressSnapshotV1,
    PresentationAssetReferenceV1,
)
from dotmac_billing.opening_receivable_contracts import (
    OpeningHistoricalDischargeV1,
    OpeningOriginalDocumentV1,
    OpeningReplayBindingV1,
)
from dotmac_billing.service import _wire_value
from dotmac_kernel.money import Currency, Money

from app.services.billing import opening_verifier as ov

ADAPTER = (
    Path(__file__).parent
    / "fixtures"
    / "opening_verifier"
    / "billing_opening_rehearsal_adapter.a8.pysrc"
)
A8_ADAPTER_SHA = "aad4c84608fa74ab71d285ceeb1b5a4713563e0e9f48b46f686527695e7cb59b"

U = uuid.UUID
TENANT = "00000000-0000-0000-0000-000000000064"
INSTALLATION = "00000000-0000-0000-0000-000000000032"
SRC1, SRC2, SRC3 = (str(U(int=0xA0 + i)) for i in range(3))
BIL1, BIL2 = (str(U(int=0x3E0 + i)) for i in range(2))
INV1, INV2, INV3, INV4 = (str(U(int=0x100 + i)) for i in range(4))
SETTLE1 = str(U(int=0x1001))
PAY1 = str(U(int=0x2001))
WINDOW_ID = str(U(int=0x7001))
EPOCH = str(U(int=0x7002))
RESTORE_ID = "rehearsal-restore-test"
CONTENT_FP = "synthetic-content-fingerprint-1"
NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
ISSUED = datetime(2026, 10, 1, tzinfo=UTC)


def invoice(
    inv: str, acct: str, number: str, total: str, discharge: str, residual: str
):
    return {
        "invoice_id": inv,
        "account_id": acct,
        "currency": "NGN",
        "precision": 2,
        "invoice_number": number,
        "issued_at": ISSUED.isoformat(),
        "total": total,
        "canonical_discharge": discharge,
        "residual": residual,
        "settlement_ids": [SETTLE1] if inv == INV2 else [],
    }


def control(
    acct: str,
    count: int,
    eligible: int,
    excluded: int,
    total: str,
    dis: str,
    res: str,
    elig: str,
):
    return {
        "account_id": acct,
        "currency": "NGN",
        "invoice_count": count,
        "eligible_count": eligible,
        "excluded_count": excluded,
        "exception_count": 0,
        "original_total": total,
        "canonical_discharge": dis,
        "residual": res,
        "eligible_residual": elig,
        "source_count": 0,
    }


def lane(acct: str, collectible: str, **extra: str) -> dict[str, Any]:
    row = {
        "account_id": acct,
        "currency": "NGN",
        "fingerprint": f"lane-fp-{acct[-3:]}",
        "authority": "authoritative",
        "authority_cutover_id": str(U(int=0x1F4)),
        "posting_group_count": 0,
        "collectible_receivable": collectible,
        "unapplied_customer_credit": "0.00",
        "prepaid_funding_reserved": "0.00",
        "prepaid_funding_consumed": "0.00",
        "written_off_total": "0.00",
        "refunded_total": "0.00",
        "adjustment_total": "0.00",
    }
    row.update(extra)
    return row


def census(kind: str = "credit_note") -> dict[str, Any]:
    """Two accounts: one open invoice; one part-paid invoice plus an excluded one."""
    tables = (
        "payments",
        "payment_settlements",
        "payment_provider_events",
        "integration_inbox",
        "payment_refunds",
        "payment_reversals",
        "payment_proofs",
        "payment_proof_corrections",
        "topup_intents",
    )
    return {
        "capture": {
            "declared_source": {
                "source_repository_sha": "0123456789abcdef0123456789abcdef01234567",
                "installation_id": INSTALLATION,
                "economic_cutoff": "2026-10-13T00:00:00+00:00",
                "currency_precisions": [["NGN", 2]],
                "population_version": "sub-billing-opening-census-v3",
            },
            "schema_heads": ["synthetic-schema:1"],
            "snapshot_id": "synthetic-snapshot:1",
            "captured_at": "2026-10-13T00:00:00+00:00",
            "isolation": "repeatable read",
            "population_complete": True,
            "mutable_history_reconstructed": False,
        },
        "subscriber_ids": [SRC1, SRC2],
        "invoices": [
            invoice(INV1, SRC1, "SYN-INV-1", "100.00", "0.00", "100.00"),
            invoice(INV2, SRC2, "SYN-INV-2", "100.00", "40.00", "60.00"),
            invoice(INV3, SRC2, "SYN-INV-3", "0.00", "0.00", "0.00"),
        ],
        "lines": [],
        "settlements": [
            {
                "kind": kind,
                "id": SETTLE1,
                "invoice_id": INV2,
                "source_id": PAY1,
                "amount": "40.00",
                "active": True,
            }
        ],
        "financial_sources": [],
        "lanes": [lane(SRC1, "100.00"), lane(SRC2, "60.00")],
        "effects": [],
        "eligible_invoice_ids": [INV1, INV2],
        "exclusions": [{"source_id": INV3, "reason": "synthetic-excluded"}],
        "blocking_exceptions": [],
        "controls": [
            control(SRC1, 1, 1, 0, "100.00", "0.00", "100.00", "100.00"),
            control(SRC2, 2, 1, 1, "100.00", "40.00", "60.00", "60.00"),
        ],
        "content_fingerprint": CONTENT_FP,
        "cash_evidence": {
            **{
                field_name: []
                for field_name in (
                    "payments",
                    "payment_settlements",
                    "provider_events",
                    "inbox_receipts",
                    "refunds",
                    "reversals",
                    "proofs",
                    "proof_corrections",
                    "topups",
                )
            },
            "source_counts": [{"table": t, "count": 0} for t in tables],
            "blocking_exceptions": [],
            "qualified_payment_ids": [],
        },
        "documentary_observations": {
            "current_invoices": [],
            "current_lines": [],
            "unavailable_original_evidence": [
                {
                    "invoice_id": inv,
                    "requirements": ["documentary_digest", "seller"],
                    "reasons": ["no_authenticated_frozen_document"],
                }
                for inv in (INV1, INV2)
            ],
        },
    }


def assembly(kind: str = "credit_note") -> dict[str, Any]:
    return {
        "schema": "billing.opening.rehearsal.assembly.v1",
        "scope": {"kind": "tenant", "tenant_id": TENANT},
        "source_system": "sub",
        "recorded_cutoff": "2026-10-13T00:00:00+00:00",
        "source_authority_ref": "rehearsal:source-authority",
        "account_mapping_version": "synthetic:account-mapping:1",
        "cash_mapping_version": "synthetic:cash-mapping:1",
        "receivable_mapping_version": "synthetic:receivable-mapping:1",
        "tax_mapping_version": "synthetic:tax-mapping:1",
        "source_resolver_version": "synthetic:source-resolver:1",
        "collection_authority": "synthetic:collection-authority",
        "collection_mapping_version": "synthetic:collection-mapping:1",
        "migration_authority_ref": "rehearsal:migration-authority",
        "occurred_at": "2026-10-13T00:00:00+00:00",
        "starter_head_sha": "7a133895d611d9043eff10dc1b8271bdda493ff2",
        "sub_exit_code": 1,
        "binding": {
            "target_installation_id": "00000000-0000-0000-0000-0000000000c8",
            "artifact_identity": "synthetic:artifact",
            "policy_identity": "synthetic:policy",
        },
        "partitions": [
            {
                "source_account_id": src,
                "source_account_key": f"source-account:{n}",
                "account_id": bil,
                "currency": "NGN",
                "precision": 2,
                "idempotency_key": f"opening-rehearsal:synthetic:{n}",
                "expected_position_fingerprint": f"pristine-position:{n}",
                "posting_watermark": f"posting:{n}",
            }
            for n, (src, bil) in enumerate(((SRC1, BIL1), (SRC2, BIL2)))
        ],
        "invoice_mappings": [
            {
                "source_invoice_id": inv,
                "source_identity": {"kind": "invoice", "key": f"invoice:{n}"},
                "source_invoice_key": f"invoice:{n}",
            }
            for n, inv in ((1, INV1), (2, INV2))
        ],
        "discharge_mappings": [
            {
                "settlement_kind": kind,
                "settlement_id": SETTLE1,
                "historical_reference": "application:one",
                "source_id": PAY1,
            }
        ],
        "accepted_manual_holds": [],
    }


def _money(minor: int) -> Money:
    return Money(Decimal(minor).scaleb(-2), Currency("NGN", 2))


def original(number: str, minor: int) -> OpeningOriginalDocumentV1:
    return OpeningOriginalDocumentV1(
        number,
        ISSUED,
        f"archive:{number}",
        "a" * 64,
        _money(minor),
        _money(0),
        _money(minor),
        PartyDocumentSnapshotV1(
            "Original Seller", PostalAddressSnapshotV1("Seller street", "NGA")
        ),
        PartyDocumentSnapshotV1(
            "Original Customer", PostalAddressSnapshotV1("Customer street", "NGA")
        ),
        (
            PartyTaxIdentitySnapshotV1(
                "seller", "tax_id", "historic-tax-id", "NGA", "party", "7"
            ),
        ),
        (
            AppliedTaxSnapshotV1(
                "vat",
                "NGA",
                "historic-policy",
                "7",
                Decimal("0"),
                _money(minor),
                _money(0),
            ),
        ),
        None,
        f"tax-document:{number}",
        "fx:not-applicable:NGN",
        "invoice.standard",
        "1",
        PaymentInstructionsSnapshotV1(
            method_code="bank_transfer",
            bank_name="Original bank",
            account_name="Original Seller",
            account_reference="documentary-account",
        ),
        PresentationAssetReferenceV1.none(),
        "en-NG",
        "Africa/Lagos",
    )


def originals_wire(
    census_sha: str, assembly_digest: str, kind: str = "credit_note"
) -> dict[str, Any]:
    bindings = (
        (
            OpeningReplayBindingV1(
                "billing.credit_note.issue",
                "dotmac-billing",
                "credit-note:one",
                "b" * 64,
            ),
        )
        if kind == "credit_note"
        else ()
    )
    effect = OpeningHistoricalDischargeV1(
        "application:one",
        kind,
        4000,
        ISSUED,
        ISSUED,
        "qualified-payment-owner",
        "opaque-parent-key",
        "billing.settlement.accept",
        "opaque-live-identity",
        "independent-mapping:1",
        "ledger:one",
        "b" * 64,
        "tax:not-applicable",
        (),
        None,
        replay_bindings=bindings,
    )
    return {
        "schema": "billing.opening.rehearsal.original-evidence.v1",
        "census_sha256": census_sha,
        "assembly_digest": assembly_digest,
        "invoices": [
            {
                "source_invoice_id": INV1,
                "original": _wire_value(original("SYN-INV-1", 10000)),
                "historical_discharges": [],
            },
            {
                "source_invoice_id": INV2,
                "original": _wire_value(original("SYN-INV-2", 10000)),
                "historical_discharges": [_wire_value(effect)],
            },
        ],
    }


def pub(key: Ed25519PrivateKey) -> str:
    raw = key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return "ed25519:" + base64.b64encode(raw).decode()


def fingerprint(key: Ed25519PrivateKey) -> str:
    return "sha256:" + ov._sha(base64.b64decode(pub(key)[8:]))


def sign(key: Ed25519PrivateKey, environment: str, purpose: str, raw: bytes) -> bytes:
    sig = key.sign(ov.signing_message(environment, purpose, raw))
    return base64.b64encode(sig) + b"\n"


@dataclass
class FakeObserver:
    """Read-only observation port double; records every call."""

    marker_value: str | None = RESTORE_ID
    roles: dict[str, ov.RoleObservation] = field(default_factory=dict)
    wire: bytes = b""
    calls: list[str] = field(default_factory=list)
    census_calls: list[tuple[str, datetime]] = field(default_factory=list)

    def marker(self) -> str | None:
        self.calls.append("marker")
        return self.marker_value

    def role(self, name: str) -> ov.RoleObservation | None:
        self.calls.append("role")
        return self.roles.get(name)

    def census_wire(self, query: Any, snapshot_id: str, captured_at: datetime) -> bytes:
        self.calls.append("census")
        self.census_calls.append((snapshot_id, captured_at))
        return self.wire


class Clock:
    def __init__(self, value: datetime = NOW) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


class World:
    """Everything a verifier build needs, written under tmp_path."""

    def __init__(
        self,
        tmp: Path,
        *,
        environment: str = "rehearsal",
        with_originals: bool = True,
        mutate_census: Any = None,
        mutate_assembly: Any = None,
        seal: bool = True,
        kind: str = "credit_note",
    ) -> None:
        self.tmp = tmp
        self.env = environment
        rehearsal = environment == "rehearsal"
        self.finance, self.approver = (
            Ed25519PrivateKey.generate(),
            Ed25519PrivateKey.generate(),
        )
        census_doc, assembly_doc = census(kind), assembly(kind)
        if not rehearsal:
            assembly_doc["source_authority_ref"] = "finance:source-authority"
            assembly_doc["migration_authority_ref"] = "michael:migration-authority"
        if mutate_census:
            mutate_census(census_doc)
        if mutate_assembly:
            mutate_assembly(assembly_doc)
        self.census_bytes = ov.canonical_json(census_doc)
        self.assembly_bytes = ov.canonical_json(assembly_doc)
        self.paths = {
            "census": self._put("census.json", self.census_bytes),
            "assembly": self._put("assembly.json", self.assembly_bytes),
            "adapter": self._put("adapter.py", ADAPTER.read_bytes()),
        }
        evidence = ov.OpeningEvidencePaths(**self.paths)
        if with_originals:
            from dotmac_billing.opening_rehearsal import (
                opening_rehearsal_assembly_digest,
                parse_opening_rehearsal_assembly,
            )

            digest = opening_rehearsal_assembly_digest(
                parse_opening_rehearsal_assembly(assembly_doc)
            )
            wire = originals_wire(ov._sha(self.census_bytes), digest, kind)
            self.paths["originals"] = self._put(
                "originals.json", json.dumps(wire).encode()
            )
            evidence = ov.OpeningEvidencePaths(**self.paths)
        self.evidence_paths = evidence
        self.ev = ov.load_evidence(evidence)
        self.observer = FakeObserver(
            marker_value=RESTORE_ID if rehearsal else None, wire=self.census_bytes
        )
        self.clock = Clock()
        self.audience = RESTORE_ID if rehearsal else f"production:{INSTALLATION}"
        self.window = {
            "window_id": WINDOW_ID,
            "epoch": EPOCH,
            "principal_name": "opening_window_role",
            "provision_authority_ref": (
                "rehearsal:provision-authority" if rehearsal else "michael:provision"
            ),
        }
        self.seal_body = {
            "window_id": WINDOW_ID,
            "epoch": EPOCH,
            "manifest_fingerprint": self.ev.fingerprint,
            "coupled_watermark": "sub-census-content:" + CONTENT_FP,
            "command_key": "seal-command-1",
            "authority_ref": (
                "rehearsal:seal-authority" if rehearsal else "michael:seal"
            ),
            "occurred_at": utc(NOW),
        }
        self.sign_all(seal=seal)

    def meta(self, **over: Any) -> ov.StatementMeta:
        values = {
            "environment": self.env,
            "audience": self.audience,
            "statement_id": str(uuid.uuid4()),
            "issued_at": NOW - timedelta(hours=2),
            "not_before": NOW - timedelta(hours=1),
            "not_after": NOW + timedelta(hours=24),
        }
        values.update(over)
        return ov.StatementMeta(**values)

    def _put(self, name: str, raw: bytes) -> Path:
        path = self.tmp / name
        path.write_bytes(raw)
        return path

    def sign_all(
        self, *, seal: bool = True, meta: dict[str, Any] | None = None
    ) -> None:
        meta = meta or {}
        att = ov.draft_statement(ov.ATTESTATION, self.ev, self.meta(**meta))
        self.att = att
        app = ov.draft_statement(
            ov.APPROVAL,
            self.ev,
            self.meta(**meta),
            source_attestation_sha256=ov._sha(att),
            window=self.window,
        )
        self.app = app
        self.files: dict[str, Path] = {
            "attestation": self._put("att.json", att),
            "attestation_sig": self._put(
                "att.sig", sign(self.finance, self.env, ov.ATTESTATION, att)
            ),
            "approval": self._put("app.json", app),
            "approval_sig": self._put(
                "app.sig", sign(self.approver, self.env, ov.APPROVAL, app)
            ),
        }
        if seal:
            raw = ov.draft_statement(
                ov.SEAL,
                self.ev,
                self.meta(**meta),
                source_attestation_sha256=ov._sha(att),
                window_import_approval_sha256=ov._sha(app),
                seal=self.seal_body,
            )
            self.seal_raw = raw
            self.files["seal_approval"] = self._put("seal.json", raw)
            self.files["seal_approval_sig"] = self._put(
                "seal.sig", sign(self.approver, self.env, ov.SEAL, raw)
            )

    def profile(self) -> ov.OpeningTrustProfile:
        if self.env == "rehearsal":
            return ov.OpeningTrustProfile(
                "rehearsal",
                inline_anchors={
                    "finance_attestor": pub(self.finance),
                    "migration_approver": pub(self.approver),
                },
            )
        return self.production_profile()

    def production_profile(self, resolver: Any = None) -> ov.OpeningTrustProfile:
        keys = {"finance_attestor": self.finance, "migration_approver": self.approver}
        self.resolved = {f"bao://kv/anchors/{r}#key": pub(k) for r, k in keys.items()}
        return ov.OpeningTrustProfile(
            "production",
            anchor_refs={r: f"bao://kv/anchors/{r}#key" for r in keys},
            anchor_fingerprints={r: fingerprint(k) for r, k in keys.items()},
            resolver=resolver or self.resolved.__getitem__,
        )

    def build(
        self, profile: ov.OpeningTrustProfile | None = None
    ) -> ov.OpeningVerifiers:
        return ov.build_opening_verifiers(
            profile or self.profile(),
            self.evidence_paths,
            ov.OpeningStatementPaths(**self.files),
            self.observer,
            self.clock,
        )


def utc(value: datetime) -> str:
    return ov.utc_text(value)
