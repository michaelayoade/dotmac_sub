"""Gap B opening verifier provider (Step 5 Phase B / Step 6).

Transitional composition. Implements Billing's opening verifier ports (import
V2, window provision, window seal) by RECOMPUTING the expected manifest and
commands from pinned evidence bytes and accepting a request only when it equals
that recomputation. Request-supplied digests are compared, never used as
inputs. Authority is Ed25519-signed statements; anchors are typed at ship time
(rehearsal: inline public keys; production: OpenBao pointers with typed
fingerprints). The source is read through a read-only port; nothing is written.
Contract: ``None`` is an authoritative rejection, ``OpeningVerificationUnavailable``
means evidence or custody is unavailable, and a partially trusted verifier is
never built.

Separation caveat: rehearsal output is unusable in production because of the
signing domain, the anchor source and the DB marker, but Billing itself accepts
any non-empty proof refs, so the runtime separator at the Billing target is the
source marker plus these controls (a planted marker would need prod DDL, the
shredded rehearsal keys and code execution: the accepted fake-verifier
residual). The Step 6 runner must therefore hard-code ``production`` (no
environment flag) and a post-import audit must assert every receipt ref starts
``gapb1:production:``. Production anchors are re-resolved on every call, so
revocation by deleting the KV entry lags by the OpenBao cache TTL (default 60 s,
up to 3600 s via ``OPENBAO_CACHE_TTL_SECONDS``).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import sys
import types
import uuid
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from dotmac_billing.opening_contracts import (
    OpeningEvidenceError,
    OpeningPartitionV1,
    OpeningVerificationUnavailable,
)
from dotmac_billing.opening_receivable_contracts import (
    OpeningImportVerificationRequestV2,
    OpeningImportVerificationV2,
)
from dotmac_billing.opening_receivable_validation import (
    opening_account_command_digest_v2,
    opening_manifest_fingerprint_v2,
)
from dotmac_billing.opening_rehearsal import (
    opening_rehearsal_assembly_digest,
    opening_rehearsal_assembly_wire,
    parse_opening_rehearsal_assembly,
    parse_opening_rehearsal_json,
    parse_opening_rehearsal_original_evidence,
)
from dotmac_billing.opening_window_contracts import (
    OpeningWindowProvisionVerificationRequestV1,
    OpeningWindowProvisionVerificationV1,
    OpeningWindowSealVerificationRequestV1,
    OpeningWindowSealVerificationV1,
    opening_admission_population_digest,
)
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

SCHEMA = "dotmac-sub.billing.opening.authority-statement"
DOMAIN = "dotmac-sub-opening-authority-v1"
ATTESTATION, APPROVAL, SEAL = (
    "source_attestation",
    "window_import_approval",
    "seal_approval",
)
_ROLE = {
    ATTESTATION: "finance_attestor",
    APPROVAL: "migration_approver",
    SEAL: "migration_approver",
}
# Reviewed adapter bytes (Starter dotmac-billing a8). Changing this is a reviewed diff.
_PINNED_ADAPTER_SHA256 = frozenset(
    {"aad4c84608fa74ab71d285ceeb1b5a4713563e0e9f48b46f686527695e7cb59b"}
)
_MAX_VALIDITY = {"rehearsal": timedelta(days=14), "production": timedelta(hours=72)}
_ISOLATION = frozenset({"repeatable read", "serializable"})


def _keys(names: str) -> frozenset[str]:
    return frozenset(names.split())


_ENVELOPE = _keys(
    "schema purpose environment audience statement_id issued_at not_before "
    "not_after signer_role body"
)
_BODY_KEYS = {
    ATTESTATION: _keys(
        "scope installation_id source_system census_sha256 "
        "census_content_fingerprint census_snapshot_id census_captured_at "
        "economic_cutoff population_version source_repository_sha schema_heads "
        "assembly_digest original_evidence_sha256 cash_lot_evidence_sha256 "
        "adapter_sha256 manifest_fingerprint source_authority_ref"
    ),
    APPROVAL: _keys(
        "source_attestation_sha256 window migration_authority_ref import_commands"
    ),
    SEAL: _keys("source_attestation_sha256 window_import_approval_sha256 seal"),
}
_WINDOW_KEYS = _keys("window_id epoch principal_name provision_authority_ref")
_SEAL_KEYS = _keys(
    "window_id epoch manifest_fingerprint coupled_watermark command_key "
    "authority_ref occurred_at"
)
_LANE_FIELDS = tuple(
    (name, f"{name}_minor")
    for name in (
        "unapplied_customer_credit prepaid_funding_reserved "
        "prepaid_funding_consumed written_off_total refunded_total adjustment_total"
    ).split()
)


class OpeningVerifierConstructionError(Exception):
    """Pinning, cross-check or statement failure; no verifier is built."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _AnchorMismatch(Exception):
    pass


def _refuse(code: str) -> OpeningVerifierConstructionError:
    return OpeningVerifierConstructionError(code)


def _require(condition: object, code: str) -> None:
    if not condition:
        raise _refuse(code)


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def canonical_json(value: object) -> bytes:
    text_ = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return (text_ + "\n").encode("utf-8")


def statement_sha256(raw: bytes) -> str:
    return _sha(raw)


def signing_message(environment: str, purpose: str, statement_bytes: bytes) -> bytes:
    """The exact ASCII message an Ed25519 signer signs (domain separated)."""
    return f"{DOMAIN}\n{environment}\n{purpose}\n{_sha(statement_bytes)}".encode(
        "ascii"
    )


def _json(raw: bytes, code: str) -> Any:
    try:
        return parse_opening_rehearsal_json(raw.decode("utf-8"))
    except (UnicodeDecodeError, OpeningEvidenceError):
        raise _refuse(code) from None


def _instant(value: object, code: str = "bad_instant") -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        raise _refuse(code) from None
    _require(parsed.tzinfo is not None and parsed.utcoffset() is not None, code)
    return parsed


def _z(value: object) -> datetime:
    _require(isinstance(value, str) and value.endswith("Z"), "statement_time_not_utc")
    return _instant(str(value)[:-1] + "+00:00", "statement_time_not_utc")


def utc_text(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _uuid(value: object) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except ValueError:
        raise _refuse("bad_uuid") from None


def _minor(value: object, precision: int) -> int:
    try:
        scaled = Decimal(str(value)).scaleb(precision)
        exact = scaled == scaled.to_integral_value()
        number = int(scaled) if exact else 0
    except (InvalidOperation, ValueError, OverflowError):
        raise _refuse("minor_units_invalid") from None
    _require(exact, "minor_units_inexact")
    return number


@dataclass(frozen=True, slots=True)
class RoleObservation:
    oid: int
    privileged: bool  # any of super/bypassrls/createrole/createdb/replication


class SourceObservationPort(Protocol):
    def marker(self) -> str | None: ...

    def role(self, name: str) -> RoleObservation | None: ...

    def census_wire(
        self, query: Any, snapshot_id: str, captured_at: datetime
    ) -> bytes: ...


class SqlSourceObservationPort:
    """Default port: its own REPEATABLE READ, READ ONLY snapshot, never the caller's."""

    @staticmethod
    def _read(statements: Callable[[Any], Any], code: str) -> Any:
        from app.db import read_only_snapshot_session

        try:
            with read_only_snapshot_session() as db:
                return statements(db)
        except SQLAlchemyError as exc:
            raise OpeningVerificationUnavailable(code) from exc

    def marker(self) -> str | None:
        def read(db: Any) -> list[Any] | None:
            present = db.execute(
                text("SELECT to_regclass('public.rehearsal_marker') IS NOT NULL")
            ).scalar()
            if not present:
                return None
            return db.execute(
                text("SELECT restore_id FROM public.rehearsal_marker")
            ).all()

        rows = self._read(read, "marker_unavailable")
        if rows is None:
            return None
        if len(rows) != 1:
            raise OpeningVerificationUnavailable("marker_ambiguous")
        return str(rows[0][0])

    def role(self, name: str) -> RoleObservation | None:
        row = self._read(
            lambda db: db.execute(
                text(
                    "SELECT oid::bigint, (rolsuper OR rolbypassrls OR rolcreaterole "
                    "OR rolcreatedb OR rolreplication) FROM pg_roles WHERE rolname = :n"
                ),
                {"n": name},
            ).one_or_none(),
            "role_unavailable",
        )
        return None if row is None else RoleObservation(int(row[0]), bool(row[1]))

    def census_wire(self, query: Any, snapshot_id: str, captured_at: datetime) -> bytes:
        from app.services.billing.opening_census_wire import (
            OpeningCensusWireError,
            render_opening_census_wire,
        )
        from app.services.billing.opening_export import (
            BillingOpeningExportError,
            preview_billing_opening_export,
        )
        from app.services.billing.opening_export_contracts import (
            BillingOpeningExportPreviewV3,
        )

        try:
            preview = self._read(
                lambda db: preview_billing_opening_export(db, query=query),
                "census_unavailable",
            )
            _require(
                isinstance(preview, BillingOpeningExportPreviewV3), "census_not_v3"
            )
            capture = replace(
                preview.capture, snapshot_id=snapshot_id, captured_at=captured_at
            )
            return render_opening_census_wire(replace(preview, capture=capture)).encode(
                "utf-8"
            )
        except (
            BillingOpeningExportError,
            OpeningCensusWireError,
            OpeningVerifierConstructionError,
        ) as exc:
            raise OpeningVerificationUnavailable("census_unavailable") from exc


def _parse_key(value: str) -> bytes:
    _require(
        isinstance(value, str) and value.startswith("ed25519:"), "anchor_malformed"
    )
    try:
        raw = base64.b64decode(value[8:], validate=True)
    except (binascii.Error, ValueError):
        raise _refuse("anchor_malformed") from None
    _require(len(raw) == 32, "anchor_malformed")
    return raw


def _default_resolver(reference: str) -> str:
    from app.services.secrets import resolve_openbao_ref

    return resolve_openbao_ref(reference)


@dataclass(frozen=True, eq=False)
class OpeningTrustProfile:
    """Anchors and domain for one environment; built only from typed parameters.

    ``_resolver`` is a test seam: production callers must go through OpenBao.
    """

    environment: str
    inline_anchors: Mapping[str, str] = field(default_factory=dict)
    anchor_refs: Mapping[str, str] = field(default_factory=dict)
    anchor_fingerprints: Mapping[str, str] = field(default_factory=dict)
    _resolver: Callable[[str], str] | None = None

    def __post_init__(self) -> None:
        roles = {"finance_attestor", "migration_approver"}
        _require(self.environment in _MAX_VALIDITY, "profile_environment")
        if self.environment == "rehearsal":
            _require(set(self.inline_anchors) == roles, "profile_anchors")
            _require(
                not self.anchor_refs and not self.anchor_fingerprints, "profile_anchors"
            )
            return
        _require(not self.inline_anchors, "production_inline_anchor_refused")
        _require(
            set(self.anchor_refs) == roles == set(self.anchor_fingerprints),
            "profile_anchors",
        )
        for ref in self.anchor_refs.values():
            _require(
                isinstance(ref, str) and ref.startswith("bao://"), "anchor_ref_scheme"
            )
        for fp in self.anchor_fingerprints.values():
            _require(
                isinstance(fp, str) and fp.startswith("sha256:") and len(fp) == 71,
                "anchor_fingerprint_malformed",
            )

    def anchors(self) -> dict[str, bytes]:
        """Resolve public keys; production re-resolves on every call."""
        if self.environment == "rehearsal":
            keys = {role: _parse_key(v) for role, v in self.inline_anchors.items()}
        else:
            keys = {}
            for role, ref in self.anchor_refs.items():
                try:
                    value = (self._resolver or _default_resolver)(ref)
                except Exception as exc:  # noqa: BLE001 - any custody failure is unavailable
                    raise OpeningVerificationUnavailable("anchor_unavailable") from exc
                try:
                    keys[role] = _parse_key(value)
                except OpeningVerifierConstructionError:
                    raise _AnchorMismatch(role) from None
                if "sha256:" + _sha(keys[role]) != self.anchor_fingerprints[role]:
                    raise _AnchorMismatch(role)
        _require(
            keys["finance_attestor"] != keys["migration_approver"],
            "anchors_not_distinct",
        )
        return keys

    def ref_ok(self, ref: str) -> bool:
        return ref.startswith("rehearsal:") == (self.environment == "rehearsal")


@dataclass(frozen=True)
class OpeningEvidencePaths:
    census: Path
    assembly: Path
    adapter: Path
    originals: Path | None = None
    cash_lots: Path | None = None


@dataclass(frozen=True)
class OpeningStatementPaths:
    attestation: Path
    attestation_sig: Path
    approval: Path
    approval_sig: Path
    seal_approval: Path | None = None
    seal_approval_sig: Path | None = None


@dataclass(frozen=True)
class _Evidence:
    census_bytes: bytes
    census_sha: str
    census: Mapping[str, Any]
    declaration: Any
    assembly_digest: str
    originals_sha: str | None
    adapter_sha: str
    manifest: Any
    fingerprint: str
    expected: Mapping[OpeningPartitionV1, Any]
    digests: Mapping[OpeningPartitionV1, str]
    query: Any


def _read(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError:
        raise _refuse("evidence_unreadable") from None


def _load_adapter(raw: bytes, sha: str) -> types.ModuleType:
    """Execute ONLY reviewed bytes: the pin is checked before compile/exec."""
    _require(_sha(raw) == sha and sha in _PINNED_ADAPTER_SHA256, "adapter_not_pinned")
    name = f"_pinned_opening_adapter_{sha[:16]}"
    module = types.ModuleType(name)
    code = compile(raw, f"<pinned-adapter:{sha}>", "exec", dont_inherit=True)
    sys.modules[name] = module
    try:
        exec(code, module.__dict__)  # noqa: S102  # nosec B102
        return module
    finally:
        sys.modules.pop(name, None)


def load_evidence(
    paths: OpeningEvidencePaths, adapter_bytes: bytes | None = None
) -> _Evidence:
    """Read each input once, recompute M* and the commands, run X1-X7."""
    _require(paths.cash_lots is None, "cash_lot_evidence_unsupported")
    census_bytes, assembly_bytes = _read(paths.census), _read(paths.assembly)
    adapter_bytes = _read(paths.adapter) if adapter_bytes is None else adapter_bytes
    census = _json(census_bytes, "census_malformed")
    _require(
        isinstance(census, dict) and canonical_json(census) == census_bytes,
        "census_not_canonical",
    )
    try:
        declaration = parse_opening_rehearsal_assembly(
            _json(assembly_bytes, "assembly_malformed")
        )
    except OpeningEvidenceError:
        raise _refuse("assembly_malformed") from None
    assembly_digest = opening_rehearsal_assembly_digest(declaration)
    census_sha, adapter_sha = _sha(census_bytes), _sha(adapter_bytes)
    originals: tuple[Any, ...] = ()
    originals_sha = None
    if paths.originals is not None:
        raw = _read(paths.originals)
        originals_sha = _sha(raw)
        try:
            sidecar = parse_opening_rehearsal_original_evidence(
                _json(raw, "originals_malformed")
            )
        except OpeningEvidenceError:
            raise _refuse("originals_malformed") from None
        _require(
            sidecar.census_sha256 == census_sha
            and sidecar.assembly_digest == assembly_digest,
            "originals_misaligned",
        )
        originals = sidecar.invoices
    adapter = _load_adapter(adapter_bytes, adapter_sha)
    try:
        observation, side = adapter.load_sub_observation(census)
        assembly = adapter.load_assembly(opening_rehearsal_assembly_wire(declaration))
        _require(assembly.declaration == declaration, "assembly_mismatch")
        run = adapter.assemble_rehearsal_run(
            observation,
            assembly=assembly,
            sidecar=side,
            original_documents=tuple(
                (r.source_invoice_id, r.original) for r in originals
            ),
            historical_discharges=tuple(
                (r.source_invoice_id, r.historical_discharges) for r in originals
            ),
        )
    except (adapter.AdapterInputError, OpeningEvidenceError) as exc:
        raise _refuse(f"adapter_rejected:{getattr(exc, 'code', 'evidence')}") from None
    evidence = _Evidence(
        census_bytes=census_bytes,
        census_sha=census_sha,
        census=census,
        declaration=declaration,
        assembly_digest=assembly_digest,
        originals_sha=originals_sha,
        adapter_sha=adapter_sha,
        manifest=run.manifest,
        fingerprint=opening_manifest_fingerprint_v2(run.manifest),
        expected={c.partition: c for c in run.commands},
        digests={
            c.partition: opening_account_command_digest_v2(c) for c in run.commands
        },
        query=_query(census),
    )
    try:
        _cross_check(evidence, {r.source_invoice_id: r for r in originals}, run)
    except (KeyError, TypeError, ValueError, AttributeError, InvalidOperation):
        raise _refuse("census_malformed") from None
    return evidence


def _query(census: Mapping[str, Any]) -> Any:
    from app.services.billing.opening_export_contracts import (
        PreviewBillingOpeningExportQuery,
    )

    declared = census["capture"]["declared_source"]
    return PreviewBillingOpeningExportQuery(
        source_repository_sha=declared["source_repository_sha"],
        installation_id=_uuid(declared["installation_id"]),
        economic_cutoff=_instant(declared["economic_cutoff"]),
        currency_precisions=tuple(
            (str(c), int(p)) for c, p in declared["currency_precisions"]
        ),
        population_version=declared["population_version"],
    )


def _cross_check(ev: _Evidence, originals: Mapping[uuid.UUID, Any], run: Any) -> None:
    """X1-X7, computed from the raw census JSON without the adapter's types."""
    census, decl, manifest = ev.census, ev.declaration, ev.manifest
    capture = census["capture"]
    declared = capture["declared_source"]
    mc = manifest.capture
    _require(capture["population_complete"] is True, "x1_population_incomplete")
    _require(capture["isolation"] in _ISOLATION, "x1_isolation")
    _require(
        (mc.installation_id, mc.source_repository_sha, mc.population_version)
        == (
            str(_uuid(declared["installation_id"])),
            declared["source_repository_sha"],
            declared["population_version"],
        )
        and mc.economic_cutoff == _instant(declared["economic_cutoff"])
        and mc.schema_heads == tuple(capture["schema_heads"])
        and (mc.snapshot_id, mc.captured_at)
        == (capture["snapshot_id"], _instant(capture["captured_at"]))
        and mc.isolation == capture["isolation"].replace(" ", "_")
        and mc.source_authority_ref == decl.source_authority_ref
        and manifest.scope == decl.scope,
        "x1_capture",
    )
    keymap: dict[tuple[uuid.UUID, str], OpeningPartitionV1] = {}
    for row in decl.partitions:
        key = (row.source_account_id, row.currency)
        _require(key not in keymap, "partition_duplicate_source")
        keymap[key] = OpeningPartitionV1(
            row.source_account_key, row.account_id, row.currency, row.precision
        )
    partitions = tuple(keymap.values())
    _require(
        len({p.account_id for p in partitions}) == len(partitions),
        "partition_account_collision",
    )
    sources = {k[0] for k in keymap}
    invoices = {_uuid(i["invoice_id"]): i for i in census["invoices"]}
    _require(len(invoices) == len(census["invoices"]), "x2_duplicate_invoice")

    def key_of(row: Mapping[str, Any]) -> tuple[uuid.UUID, str]:
        return (_uuid(row["account_id"]), row["currency"])

    # X2: complete population. Closes the adapter's silent drop of unmapped rows.
    for row in (*census["invoices"], *census["lanes"]):
        _require(key_of(row) in keymap, "x2_unmapped_population")
    for row in census["controls"]:
        if row["account_id"] is not None:
            _require(
                row["currency"] is not None and key_of(row) in keymap,
                "x2_unmapped_population",
            )
    for row in census["blocking_exceptions"]:
        _require(row["account_id"] is not None, "x2_accountless_exception")
        _require(_uuid(row["account_id"]) in sources, "x2_unmapped_population")
    eligible = {_uuid(i) for i in census["eligible_invoice_ids"]}
    _require(eligible <= set(invoices), "x2_unknown_eligible")
    mapping = {m.source_invoice_id: m for m in decl.invoice_mappings}
    expected_ids = Counter(
        (mapping[i].source_identity.kind, mapping[i].source_identity.key)
        if i in eligible and i in mapping
        else ("invoice", str(i))
        for i in invoices
    )
    _require(
        len(manifest.dispositions) == len(invoices)
        and Counter((d.identity.kind, d.identity.key) for d in manifest.dispositions)
        == expected_ids,
        "x2_dispositions",
    )
    # X5
    _require(
        len(run.commands) == len(partitions) and set(ev.expected) == set(partitions),
        "x5_partitions",
    )
    kinds = Counter(d.disposition for d in manifest.dispositions)
    counts = manifest.counts
    _require(
        (counts.population, counts.eligible, counts.excluded, counts.blocking)
        == (
            len(manifest.dispositions),
            kinds["eligible"],
            kinds["excluded"],
            kinds["blocking"],
        ),
        "x5_counts",
    )
    _require(manifest.cash_lots == (), "x4_cash_lots_unsupported")
    for key, p in keymap.items():
        # X3
        residual = sum(
            _minor(i["residual"], p.precision)
            for i in census["invoices"]
            if key_of(i) == key and _uuid(i["invoice_id"]) in eligible
        )
        receivables = sum(
            r.residual_minor for r in manifest.receivables if r.partition == p
        )
        controls = [
            c for c in census["controls"] if c["account_id"] and key_of(c) == key
        ]
        mcs = [c for c in manifest.controls if c.partition == p]
        _require(len(controls) == 1 and len(mcs) == 1, "x3_control_missing")
        _require(
            residual
            == receivables
            == mcs[0].collectible_receivable_minor
            == mcs[0].open_ar_minor
            == _minor(controls[0]["eligible_residual"], p.precision),
            "x3_residual",
        )
        # X4
        lanes = [la for la in census["lanes"] if key_of(la) == key]
        _require(len(lanes) == 1, "x4_lane_missing")
        _require(
            all(
                _minor(lanes[0][wire], p.precision) == getattr(mcs[0], attr)
                for wire, attr in _LANE_FIELDS
            )
            and mcs[0].available_cash_credit_minor == 0
            and mcs[0].available_non_cash_credit_minor == 0,
            "x4_lanes",
        )
    by_key = {m.source_invoice_key: m for m in decl.invoice_mappings}
    for item in manifest.receivables:
        mapped = by_key[item.source_invoice_key]
        row = invoices[mapped.source_invoice_id]
        p = item.partition
        discharged = sum(d.amount_minor for d in item.historical_discharges)
        total = _minor(row["total"], p.precision)
        # X7 (M8-minimal): the discharge history must reproduce the canonical discharge.
        _require(
            discharged == _minor(row["canonical_discharge"], p.precision)
            and total - discharged
            == item.residual_minor
            == _minor(row["residual"], p.precision),
            "x7_discharge",
        )
        if item.original is not None:  # X6
            source = originals.get(mapped.source_invoice_id)
            grand = item.original.grand_total
            _require(
                source is not None and source.original == item.original, "x6_original"
            )
            _require(
                item.original.document_number == row["invoice_number"]
                and item.original.issued_at == _instant(row["issued_at"])
                and grand.amount == Decimal(row["total"])
                and grand.currency.code == row["currency"]
                and grand.currency.minor_units == p.precision,
                "x6_original",
            )


@dataclass(frozen=True)
class StatementMeta:
    environment: str
    audience: str
    statement_id: str
    issued_at: datetime
    not_before: datetime
    not_after: datetime


@dataclass(frozen=True)
class _Statement:
    raw: bytes
    sha: str
    body: Mapping[str, Any]
    audience: str
    not_before: datetime
    not_after: datetime


def _import_commands(ev: _Evidence) -> list[dict[str, str]]:
    return sorted(
        (
            {
                "source_account_key": p.source_account_key,
                "account_id": str(p.account_id),
                "command_digest": d,
            }
            for p, d in ev.digests.items()
        ),
        key=lambda r: (r["source_account_key"], r["account_id"], r["command_digest"]),
    )


def _attestation_body(ev: _Evidence) -> dict[str, Any]:
    census = ev.census
    declared = census["capture"]["declared_source"]
    return {
        "scope": opening_rehearsal_assembly_wire(ev.declaration)["scope"],
        "installation_id": declared["installation_id"],
        "source_system": ev.declaration.source_system,
        "census_sha256": ev.census_sha,
        "census_content_fingerprint": census["content_fingerprint"],
        "census_snapshot_id": census["capture"]["snapshot_id"],
        "census_captured_at": census["capture"]["captured_at"],
        "economic_cutoff": declared["economic_cutoff"],
        "population_version": declared["population_version"],
        "source_repository_sha": declared["source_repository_sha"],
        "schema_heads": list(census["capture"]["schema_heads"]),
        "assembly_digest": ev.assembly_digest,
        "original_evidence_sha256": ev.originals_sha,
        "cash_lot_evidence_sha256": None,
        "adapter_sha256": ev.adapter_sha,
        "manifest_fingerprint": ev.fingerprint,
        "source_authority_ref": ev.declaration.source_authority_ref,
    }


def draft_statement(
    purpose: str, ev: _Evidence, meta: StatementMeta, **params: Any
) -> bytes:
    """Canonical UNSIGNED statement bytes, computed by the same path build verifies."""
    if purpose == ATTESTATION:
        body = _attestation_body(ev)
    elif purpose == APPROVAL:
        body = {
            "source_attestation_sha256": params["source_attestation_sha256"],
            "window": params["window"],
            "migration_authority_ref": ev.declaration.migration_authority_ref,
            "import_commands": _import_commands(ev),
        }
    elif purpose == SEAL:
        body = {
            "source_attestation_sha256": params["source_attestation_sha256"],
            "window_import_approval_sha256": params["window_import_approval_sha256"],
            "seal": params["seal"],
        }
    else:
        raise _refuse("unknown_purpose")
    return canonical_json(
        {
            "schema": SCHEMA,
            "purpose": purpose,
            "environment": meta.environment,
            "audience": meta.audience,
            "statement_id": meta.statement_id,
            "issued_at": utc_text(meta.issued_at),
            "not_before": utc_text(meta.not_before),
            "not_after": utc_text(meta.not_after),
            "signer_role": _ROLE[purpose],
            "body": body,
        }
    )


def _load_statement(
    purpose: str,
    raw: bytes,
    sig_raw: bytes,
    profile: OpeningTrustProfile,
    anchors: Mapping[str, bytes],
) -> _Statement:
    doc = _json(raw, "statement_malformed")
    _require(isinstance(doc, dict) and set(doc) == _ENVELOPE, "statement_shape")
    _require(canonical_json(doc) == raw, "statement_not_canonical")
    _require(doc["schema"] == SCHEMA and doc["purpose"] == purpose, "statement_purpose")
    _require(doc["environment"] == profile.environment, "statement_environment")
    _require(doc["signer_role"] == _ROLE[purpose], "statement_role")
    _uuid(doc["statement_id"])
    _z(doc["issued_at"])
    start, end = _z(doc["not_before"]), _z(doc["not_after"])
    _require(
        start < end and end - start <= _MAX_VALIDITY[profile.environment],
        "statement_validity",
    )
    _require(isinstance(doc["audience"], str) and doc["audience"], "statement_audience")
    body = doc["body"]
    _require(
        isinstance(body, dict) and set(body) == _BODY_KEYS[purpose],
        "statement_body_shape",
    )
    try:
        signature = base64.b64decode(sig_raw.strip(), validate=True)
        _require(len(signature) == 64, "signature_invalid")
        Ed25519PublicKey.from_public_bytes(anchors[_ROLE[purpose]]).verify(
            signature, signing_message(profile.environment, purpose, raw)
        )
    except (binascii.Error, InvalidSignature, ValueError):
        raise _refuse("signature_invalid") from None
    return _Statement(raw, _sha(raw), body, doc["audience"], start, end)


def _proof(env: str, kind: str, value: str) -> str:
    return f"gapb1:{env}:{kind}:{value}"


class _Verifier:
    """Implements the import V2, provision and seal ports over one trusted state."""

    def __init__(
        self,
        profile: OpeningTrustProfile,
        ev: _Evidence,
        attestation: _Statement,
        approval: _Statement,
        seal_approval: _Statement | None,
        observer: SourceObservationPort,
        now: Callable[[], datetime],
    ) -> None:
        self._profile, self._ev = profile, ev
        self._att, self._app, self._seal = attestation, approval, seal_approval
        self._observer, self._now = observer, now
        self._window = dict(approval.body["window"])
        self._env = profile.environment

    def _live(self, *statements: _Statement) -> bool:
        try:
            self._profile.anchors()
        except _AnchorMismatch:
            return False
        moment = self._now()
        return all(s.not_before <= moment <= s.not_after for s in statements)

    def _marker_ok(self) -> bool:
        marker = self._observer.marker()
        if self._env == "rehearsal":
            return marker is not None and marker == self._att.audience
        return marker is None

    def _fenced(self) -> bool:
        """Re-render the census from the source; any content movement differs."""
        cap = self._ev.census["capture"]
        wire = self._observer.census_wire(
            self._ev.query, cap["snapshot_id"], _instant(cap["captured_at"])
        )
        return wire == self._ev.census_bytes

    def verify_import_v2(
        self, request: OpeningImportVerificationRequestV2
    ) -> OpeningImportVerificationV2 | None:
        ev = self._ev
        if type(request) is not OpeningImportVerificationRequestV2:
            return None
        if not self._live(self._att, self._app):
            return None
        command = request.command
        expected = ev.expected.get(command.partition)
        if expected is None or not (command is expected or command == expected):
            return None
        digest = ev.digests[command.partition]
        if (
            request.command_digest != digest
            or request.manifest_fingerprint != ev.fingerprint
        ):
            return None
        b, env = ev.census_sha, self._env
        mapping_ref = ev.originals_sha or f"none-{b}"
        att = self._att.body
        return OpeningImportVerificationV2(
            operation="billing.opening.import.v2",
            scope=expected.scope,
            command_digest=digest,
            manifest_fingerprint=ev.fingerprint,
            partition=expected.partition,
            target=expected.target,
            installation_id=str(att["installation_id"]),
            migration_authority_ref=str(self._app.body["migration_authority_ref"]),
            source_authority_ref=str(att["source_authority_ref"]),
            source_binding_proof_ref=_proof(env, "source_binding", self._att.sha),
            cash_lifecycle_proof_ref=_proof(env, "cash_lifecycle", f"empty-{b}"),
            receivable_lifecycle_proof_ref=_proof(env, "receivable_lifecycle", b),
            documentary_tax_mapping_proof_ref=_proof(
                env, "documentary_tax_mapping", mapping_ref
            ),
            historical_discharge_mapping_proof_ref=_proof(
                env, "historical_discharge_mapping", mapping_ref
            ),
            migration_authority_proof_ref=_proof(
                env, "migration_authority", self._app.sha
            ),
        )

    def verify_import_reversal_v2(self, request: object) -> None:
        return None  # v1 has no reversal purpose: an abort is a restore, not an unwind

    def verify_provision(
        self, request: OpeningWindowProvisionVerificationRequestV1
    ) -> OpeningWindowProvisionVerificationV1 | None:
        ev, approved = self._ev, self._window
        if type(request) is not OpeningWindowProvisionVerificationRequestV1:
            return None
        if not self._live(self._att, self._app):
            return None
        w = request.command.window
        if (
            w.scope != ev.declaration.scope
            or w.manifest_version != 2
            or w.manifest != ev.manifest
            or w.manifest_fingerprint != ev.fingerprint
            or (
                str(w.window_id),
                str(w.epoch),
                w.principal_name,
                w.provision_authority_ref,
            )
            != tuple(
                approved[k]
                for k in (
                    "window_id",
                    "epoch",
                    "principal_name",
                    "provision_authority_ref",
                )
            )
            or not (self._app.not_before <= w.recorded_at <= self._app.not_after)
        ):
            return None
        role = self._observer.role(w.principal_name)
        if role is None or role.oid != w.principal_oid or role.privileged:
            return None
        if not self._marker_ok() or not self._fenced():
            return None
        return OpeningWindowProvisionVerificationV1(
            operation="billing.opening.window.provision.v1",
            scope=w.scope,
            window_id=w.window_id,
            epoch=w.epoch,
            manifest_fingerprint=ev.fingerprint,
            principal_name=w.principal_name,
            principal_oid=w.principal_oid,
            command_digest=request.command_digest,
            authority_ref=approved["provision_authority_ref"],
            complete_population_proof_ref=_proof(
                self._env, "complete_population", f"census-reobserved-{ev.census_sha}"
            ),
            authority_proof_ref=_proof(self._env, "authority", self._app.sha),
        )

    def verify_seal(
        self, request: OpeningWindowSealVerificationRequestV1
    ) -> OpeningWindowSealVerificationV1 | None:
        ev, seal_approval = self._ev, self._seal
        if (
            type(request) is not OpeningWindowSealVerificationRequestV1
            or seal_approval is None
        ):
            return None
        if not self._live(self._att, self._app, seal_approval):
            return None
        cmd = request.command
        seal = seal_approval.body["seal"]
        # fingerprint and watermark were bound to the evidence when the verifier was built
        if (
            cmd.scope != ev.declaration.scope
            or (str(cmd.window_id), str(cmd.epoch), cmd.manifest_fingerprint)
            != (seal["window_id"], seal["epoch"], seal["manifest_fingerprint"])
            or cmd.coupled_watermark != seal["coupled_watermark"]
            or cmd.command_key != seal["command_key"]
            or cmd.authority_ref != seal["authority_ref"]
            or cmd.occurred_at != _instant(seal["occurred_at"])
        ):
            return None
        # Independent expected population: the rule Billing applies under the lock.
        by_account = {
            r.partition.account_id: r.partition
            for r in ev.manifest.receivables
            if r.residual_minor > 0
        }
        accounts = [a.billing_account_id for a in cmd.admissions]
        if len(accounts) != len(set(accounts)) or set(accounts) != set(by_account):
            return None
        for a in cmd.admissions:
            p = by_account[a.billing_account_id]
            if (
                a.scope != cmd.scope
                or a.window_id != cmd.window_id
                or a.request_identity != ev.expected[p].idempotency_key
                or a.request_digest != ev.digests[p]
                or a.manifest_fingerprint != ev.fingerprint
            ):
                return None
        if (
            opening_admission_population_digest(cmd.admissions)
            != request.population_digest
        ):
            return None
        if not self._marker_ok() or not self._fenced():
            return None
        return OpeningWindowSealVerificationV1(
            operation="billing.opening.window.seal.v1",
            scope=cmd.scope,
            window_id=cmd.window_id,
            epoch=cmd.epoch,
            manifest_fingerprint=ev.fingerprint,
            command_digest=request.command_digest,
            population_digest=request.population_digest,
            population_count=len(cmd.admissions),
            coupled_watermark=cmd.coupled_watermark,
            authority_ref=cmd.authority_ref,
            complete_population_proof_ref=_proof(
                self._env,
                "complete_population",
                f"admissions-{request.population_digest}",
            ),
            coupled_watermark_proof_ref=_proof(
                self._env, "coupled_watermark", f"census-reobserved-{ev.census_sha}"
            ),
            authority_proof_ref=_proof(self._env, "authority", seal_approval.sha),
        )


@dataclass(frozen=True)
class OpeningVerifiers:
    import_v2: _Verifier
    provision: _Verifier
    seal: _Verifier | None


def build_opening_verifiers(
    profile: OpeningTrustProfile,
    evidence: OpeningEvidencePaths,
    statements: OpeningStatementPaths,
    observer: SourceObservationPort,
    now: Callable[[], datetime],
) -> OpeningVerifiers:
    """The only API T8/Step 6 uses. Any failure raises; no partial verifier exists."""
    try:
        anchors = profile.anchors()
    except _AnchorMismatch:
        raise _refuse("anchor_fingerprint_mismatch") from None
    except OpeningVerificationUnavailable:
        raise _refuse("anchor_unavailable") from None
    # Authenticate the attested adapter hash BEFORE any evidence byte is executed.
    adapter_bytes = _read(evidence.adapter)
    att = _load_statement(
        ATTESTATION,
        _read(statements.attestation),
        _read(statements.attestation_sig),
        profile,
        anchors,
    )
    _require(att.body["adapter_sha256"] == _sha(adapter_bytes), "attestation_mismatch")
    ev = load_evidence(evidence, adapter_bytes)
    app = _load_statement(
        APPROVAL,
        _read(statements.approval),
        _read(statements.approval_sig),
        profile,
        anchors,
    )
    _require(att.body == _attestation_body(ev), "attestation_mismatch")
    body, window = app.body, app.body["window"]
    _require(
        body["source_attestation_sha256"] == att.sha, "approval_attestation_binding"
    )
    _require(
        isinstance(window, dict) and set(window) == _WINDOW_KEYS,
        "approval_window_shape",
    )
    _require(
        body["migration_authority_ref"] == ev.declaration.migration_authority_ref,
        "approval_authority",
    )
    _require(body["import_commands"] == _import_commands(ev), "approval_commands")
    for ref in (
        att.body["source_authority_ref"],
        body["migration_authority_ref"],
        window["provision_authority_ref"],
    ):
        _require(
            isinstance(ref, str) and profile.ref_ok(ref), "authority_ref_environment"
        )
    seal_approval = None
    if (statements.seal_approval is None) != (statements.seal_approval_sig is None):
        raise _refuse("seal_statement_incomplete")
    if (
        statements.seal_approval is not None
        and statements.seal_approval_sig is not None
    ):
        seal_approval = _load_statement(
            SEAL,
            _read(statements.seal_approval),
            _read(statements.seal_approval_sig),
            profile,
            anchors,
        )
        sb, seal = seal_approval.body, seal_approval.body["seal"]
        _require(sb["source_attestation_sha256"] == att.sha, "seal_attestation_binding")
        _require(
            sb["window_import_approval_sha256"] == app.sha, "seal_approval_binding"
        )
        _require(isinstance(seal, dict) and set(seal) == _SEAL_KEYS, "seal_shape")
        _require(
            seal["window_id"] == window["window_id"]
            and seal["epoch"] == window["epoch"]
            and seal["manifest_fingerprint"] == ev.fingerprint
            and seal["coupled_watermark"]
            == "sub-census-content:" + str(ev.census["content_fingerprint"]),
            "seal_binding",
        )
        _require(
            profile.ref_ok(str(seal["authority_ref"])), "authority_ref_environment"
        )
        _instant(seal["occurred_at"])
    signed = (att, app, *([seal_approval] if seal_approval else []))
    expected_audience = (
        f"production:{att.body['installation_id']}"
        if profile.environment == "production"
        else None
    )
    for statement in signed:
        _require(statement.audience == att.audience, "audience_mismatch")
        _require(expected_audience in (None, statement.audience), "audience_mismatch")
        _require(
            statement.not_before <= now() <= statement.not_after,
            "statement_not_current",
        )
    marker = observer.marker()
    if profile.environment == "rehearsal":
        _require(marker is not None and marker == att.audience, "marker_required")
    else:
        _require(marker is None, "marker_forbidden")
    verifier = _Verifier(profile, ev, att, app, seal_approval, observer, now)
    return OpeningVerifiers(verifier, verifier, verifier if seal_approval else None)
