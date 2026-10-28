"""Gap B opening verifier: positives, the packet's negative list, and a
rehearsal-shaped flow over a synthetic census (no database)."""

from __future__ import annotations

import json
import uuid
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from dotmac_billing.opening_contracts import OpeningVerificationUnavailable
from dotmac_billing.opening_receivable_contracts import (
    ImportOpeningAccountV2,
    OpeningImportReversalVerificationRequestV2,
    OpeningImportVerificationRequestV2,
)
from dotmac_billing.opening_receivable_validation import (
    opening_account_command_digest_v2,
    opening_manifest_fingerprint_v2,
    preview_opening_account_import_v2,
    verify_opening_account_import_v2,
)
from dotmac_billing.opening_rehearsal import report_opening_rehearsal
from dotmac_billing.opening_window_contracts import (
    OpeningWindowAdmissionV1,
    OpeningWindowProvisionVerificationRequestV1,
    OpeningWindowSealVerificationRequestV1,
    OpeningWindowV1,
    ProvisionOpeningWindowV1,
    SealOpeningWindowV1,
    opening_admission_population_digest,
)
from dotmac_kernel.cache import TenantScope

from app.services.billing import opening_verifier as ov
from tests import opening_verifier_support as s

SCOPE = TenantScope(uuid.UUID(s.TENANT))
GOOD_ROLE = ov.RoleObservation(4242, False)


@pytest.fixture()
def world(tmp_path: Path) -> s.World:
    w = s.World(tmp_path)
    w.observer.roles["opening_window_role"] = GOOD_ROLE
    return w


def refused(world: s.World, code: str | None = None, **kwargs) -> str:
    with pytest.raises(ov.OpeningVerifierConstructionError) as info:
        world.build(**kwargs)
    if code is not None:
        assert info.value.code == code
    return info.value.code


def commands(world: s.World) -> list[ImportOpeningAccountV2]:
    return [
        world.ev.expected[p]
        for p in sorted(world.ev.expected, key=lambda p: p.source_account_key)
    ]


def import_request(command, **over):
    values = {
        "command": command,
        "command_digest": opening_account_command_digest_v2(command),
        "manifest_fingerprint": opening_manifest_fingerprint_v2(command.manifest),
    }
    values.update(over)
    return OpeningImportVerificationRequestV2(**values)


def window_command(world: s.World, **over) -> ProvisionOpeningWindowV1:
    w = world.window
    values = {
        "scope": SCOPE,
        "window_id": uuid.UUID(w["window_id"]),
        "epoch": uuid.UUID(w["epoch"]),
        "manifest_version": 2,
        "manifest_fingerprint": world.ev.fingerprint,
        "manifest": world.ev.manifest,
        "principal_name": w["principal_name"],
        "principal_oid": GOOD_ROLE.oid,
        "provision_authority_ref": w["provision_authority_ref"],
        "recorded_at": s.NOW,
    }
    values.update(over)
    return ProvisionOpeningWindowV1(OpeningWindowV1(**values))


def provision_request(world, **over) -> OpeningWindowProvisionVerificationRequestV1:
    return OpeningWindowProvisionVerificationRequestV1(
        window_command(world, **over), "f" * 64
    )


def admissions(world: s.World, **over):
    rows = []
    for command in commands(world):
        values = {
            "scope": SCOPE,
            "window_id": uuid.UUID(s.WINDOW_ID),
            "receipt_id": uuid.uuid4(),
            "billing_account_id": command.partition.account_id,
            "request_identity": command.idempotency_key,
            "request_digest": world.ev.digests[command.partition],
            "manifest_fingerprint": world.ev.fingerprint,
        }
        values.update(over)
        rows.append(OpeningWindowAdmissionV1(**values))
    return tuple(rows)


def seal_request(
    world: s.World, rows=None, **over
) -> OpeningWindowSealVerificationRequestV1:
    rows = admissions(world) if rows is None else rows
    values = {
        "scope": SCOPE,
        "window_id": uuid.UUID(s.WINDOW_ID),
        "epoch": uuid.UUID(s.EPOCH),
        "manifest_fingerprint": world.ev.fingerprint,
        "admissions": rows,
        "coupled_watermark": world.seal_body["coupled_watermark"],
        "command_key": world.seal_body["command_key"],
        "authority_ref": world.seal_body["authority_ref"],
        "occurred_at": s.NOW,
    }
    values.update(over)
    return OpeningWindowSealVerificationRequestV1(
        SealOpeningWindowV1(**values),
        "e" * 64,
        opening_admission_population_digest(rows),
    )


# ----------------------------------------------------------------------------
# Positives
# ----------------------------------------------------------------------------


def test_construction_and_import_passes_billing_recheck(world):
    verifiers = world.build()
    assert verifiers.seal is not None
    for command in commands(world):
        result = verify_opening_account_import_v2(command, verifiers.import_v2)
        assert result is not None
        assert result.source_binding_proof_ref.startswith(
            "gapb1:rehearsal:source_binding:"
        )
        assert result.installation_id == command.manifest.capture.installation_id


def test_previews_admissible_and_report_blockers_on_a8(world):
    verifiers = world.build()
    adapter = ov._load_adapter(s.ADAPTER.read_bytes(), s.A8_ADAPTER_SHA)
    assert ov._sha(s.ADAPTER.read_bytes()) == s.A8_ADAPTER_SHA
    census = json.loads(world.census_bytes)
    obs, sidecar = adapter.load_sub_observation(census)
    assert obs.invoices
    previews = tuple(
        preview_opening_account_import_v2(c, verifier=verifiers.import_v2)
        for c in commands(world)
    )
    assert len(previews) == 2
    assert all(p.admissible and p.blockers == () for p in previews)
    assert [p.receivable_residual_minor for p in previews] == [10000, 6000]
    assert all(p.cash_residual_minor == 0 for p in previews)
    # Without a verifier the same commands are blocked (what the census artifact shows).
    assert not any(
        preview_opening_account_import_v2(c).admissible for c in commands(world)
    )


def test_rehearsal_shaped_flow_over_synthetic_census(world):
    """census -> originals -> signed statements -> preview -> report -> provision -> seal."""
    verifiers = world.build()
    adapter = ov._load_adapter(s.ADAPTER.read_bytes(), s.A8_ADAPTER_SHA)
    obs, sidecar = adapter.load_sub_observation(json.loads(world.census_bytes))
    from dotmac_billing.opening_rehearsal import parse_opening_rehearsal_assembly

    assembly = adapter.AssemblyInputs(
        parse_opening_rehearsal_assembly(json.loads(world.assembly_bytes))
    )
    originals = ov.parse_opening_rehearsal_original_evidence(
        json.loads(world.paths["originals"].read_bytes())
    )
    run = adapter.assemble_rehearsal_run(
        obs,
        assembly=assembly,
        sidecar=sidecar,
        original_documents=tuple(
            (r.source_invoice_id, r.original) for r in originals.invoices
        ),
        historical_discharges=tuple(
            (r.source_invoice_id, r.historical_discharges) for r in originals.invoices
        ),
    )
    previews = tuple(
        preview_opening_account_import_v2(c, verifier=verifiers.import_v2)
        for c in run.commands
    )
    assert all(p.admissible for p in previews)
    assert not any(
        b.code == "verifier_unavailable" for p in previews for b in p.blockers
    )
    report = report_opening_rehearsal(replace(run, previews=previews))
    # a8 reporter: the v3 unavailable-original list still blocks reconciliation.
    assert {b.code for b in report.blockers} == {
        "original_documentary_evidence_unavailable"
    }
    assert verifiers.import_v2 is not None
    provision = verifiers.provision.verify_provision(provision_request(world))
    assert provision is not None
    assert provision.authority_ref == world.window["provision_authority_ref"]
    assert provision.complete_population_proof_ref.startswith(
        "gapb1:rehearsal:complete_population:"
    )
    seal = verifiers.seal.verify_seal(seal_request(world))
    assert seal is not None and seal.population_count == 2
    assert seal.coupled_watermark_proof_ref.startswith(
        "gapb1:rehearsal:coupled_watermark:"
    )


def test_provision_result_matches_request_fields(world):
    request = provision_request(world)
    result = world.build().provision.verify_provision(request)
    window = request.command.window
    assert (result.operation, result.scope, result.window_id, result.epoch) == (
        "billing.opening.window.provision.v1",
        window.scope,
        window.window_id,
        window.epoch,
    )
    assert (
        result.manifest_fingerprint,
        result.principal_name,
        result.principal_oid,
    ) == (
        window.manifest_fingerprint,
        window.principal_name,
        window.principal_oid,
    )
    assert result.command_digest == request.command_digest
    assert result.authority_ref == window.provision_authority_ref


# ----------------------------------------------------------------------------
# Negatives 1-20
# ----------------------------------------------------------------------------


def test_n01_flipped_signature_bit(world):
    path = world.files["attestation_sig"]
    raw = bytearray(__import__("base64").b64decode(path.read_bytes()))
    raw[0] ^= 1
    path.write_bytes(__import__("base64").b64encode(bytes(raw)))
    refused(world, "signature_invalid")


def test_n02_valid_signature_under_wrong_purpose(world):
    forged = s.sign(world.finance, "rehearsal", ov.APPROVAL, world.att)
    world.files["attestation_sig"].write_bytes(forged)
    refused(world, "signature_invalid")


def test_n02b_signature_by_wrong_role_key(world):
    world.files["approval_sig"].write_bytes(
        s.sign(world.finance, "rehearsal", ov.APPROVAL, world.app)
    )
    refused(world, "signature_invalid")


def test_n03a_rehearsal_statement_under_production_profile(tmp_path):
    w = s.World(tmp_path)
    refused(w, "statement_environment", profile=w.production_profile())


def test_n03b_production_statement_under_rehearsal_profile(tmp_path):
    w = s.World(tmp_path, environment="production")
    rehearsal = ov.OpeningTrustProfile(
        "rehearsal",
        inline_anchors={
            "finance_attestor": s.pub(w.finance),
            "migration_approver": s.pub(w.approver),
        },
    )
    refused(w, "statement_environment", profile=rehearsal)


def test_n04_marker_absent_under_rehearsal_and_present_under_production(tmp_path):
    (tmp_path / "r").mkdir()
    w = s.World(tmp_path / "r")
    w.observer.marker_value = None
    refused(w, "marker_required")
    (tmp_path / "p").mkdir()
    p = s.World(tmp_path / "p", environment="production")
    p.observer.marker_value = s.RESTORE_ID
    refused(p, "marker_forbidden")


def test_n04b_marker_changes_after_build_refuses_provision_and_seal(world):
    verifiers = world.build()
    world.observer.marker_value = None
    assert verifiers.provision.verify_provision(provision_request(world)) is None
    assert verifiers.seal.verify_seal(seal_request(world)) is None


def test_n05_one_byte_change_to_census(world):
    path = world.paths["census"]
    path.write_bytes(
        path.read_bytes().replace(b"synthetic-snapshot:1", b"synthetic-snapshot:2")
    )
    refused(world)


def test_n06_reobservation_differs_at_provision_and_seal(world):
    verifiers = world.build()
    assert verifiers.provision.verify_provision(provision_request(world)) is not None
    world.observer.wire = world.census_bytes.replace(b"100.00", b"101.00", 1)
    assert verifiers.provision.verify_provision(provision_request(world)) is None
    assert verifiers.seal.verify_seal(seal_request(world)) is None


def test_n07_assembly_substituted_with_other_mapping(world):
    path = world.paths["assembly"]
    other = json.loads(path.read_bytes())
    other["partitions"][0]["idempotency_key"] = "opening-rehearsal:other"
    path.write_bytes(ov.canonical_json(other))
    refused(world)


def test_n08_self_consistent_plus_one_minor_manifest(world):
    verifiers = world.build()
    command = commands(world)[0]
    manifest = command.manifest
    item = replace(
        manifest.receivables[0],
        residual_minor=manifest.receivables[0].residual_minor + 1,
    )
    ctl = replace(
        manifest.controls[0],
        collectible_receivable_minor=manifest.controls[0].collectible_receivable_minor
        + 1,
        open_ar_minor=manifest.controls[0].open_ar_minor + 1,
    )
    forged_manifest = replace(
        manifest,
        receivables=(item, *manifest.receivables[1:]),
        controls=(ctl, *manifest.controls[1:]),
    )
    forged = replace(command, manifest=forged_manifest)
    # Consistent digest and fingerprint, computed by the "caller".
    assert verifiers.import_v2.verify_import_v2(import_request(forged)) is None
    window = provision_request(
        world,
        manifest=forged_manifest,
        manifest_fingerprint=opening_manifest_fingerprint_v2(forged_manifest),
    )
    assert verifiers.provision.verify_provision(window) is None


def test_n09_command_carrying_other_partition_target_or_key(world):
    verifiers = world.build()
    first, second = commands(world)
    assert (
        verifiers.import_v2.verify_import_v2(
            import_request(replace(first, target=second.target))
        )
        is None
    )
    assert (
        verifiers.import_v2.verify_import_v2(
            import_request(replace(first, idempotency_key=second.idempotency_key))
        )
        is None
    )


def test_n10_changed_occurred_at_is_not_approved(world):
    verifiers = world.build()
    command = commands(world)[0]
    moved = replace(command, occurred_at=command.occurred_at + timedelta(seconds=1))
    assert verifiers.import_v2.verify_import_v2(import_request(moved)) is None


@pytest.mark.parametrize(
    "variant",
    [
        "window_id",
        "epoch",
        "principal",
        "oid_mismatch",
        "bypassrls",
        "superuser",
        "missing_role",
    ],
)
def test_n11_provision_binding_and_role(world, variant):
    verifiers = world.build()
    over = {}
    if variant == "window_id":
        over["window_id"] = uuid.uuid4()
    elif variant == "epoch":
        over["epoch"] = uuid.uuid4()
    elif variant == "principal":
        over["principal_name"] = "someone_else"
        world.observer.roles["someone_else"] = GOOD_ROLE
    elif variant == "oid_mismatch":
        over["principal_oid"] = 1
    elif variant == "bypassrls":
        world.observer.roles["opening_window_role"] = replace(
            GOOD_ROLE, privileged=True
        )
    elif variant == "superuser":
        world.observer.roles["opening_window_role"] = replace(
            GOOD_ROLE, privileged=True
        )
    else:
        world.observer.roles.clear()
    assert (
        verifiers.provision.verify_provision(provision_request(world, **over)) is None
    )


@pytest.mark.parametrize(
    "variant",
    [
        "missing",
        "extra",
        "duplicate",
        "request_digest",
        "population_digest",
        "watermark",
        "identity",
        "wrong_account",
    ],
)
def test_n12_seal_population_and_binding(world, variant):
    verifiers = world.build()
    rows = admissions(world)
    over = {}
    if variant == "missing":
        rows = rows[:1]
    elif variant == "extra":
        rows = (
            *rows,
            replace(rows[0], billing_account_id=uuid.uuid4(), receipt_id=uuid.uuid4()),
        )
    elif variant == "duplicate":
        rows = (rows[0], replace(rows[0], receipt_id=uuid.uuid4()))
    elif variant == "request_digest":
        rows = (replace(rows[0], request_digest="0" * 64), rows[1])
    elif variant == "identity":
        rows = (replace(rows[0], request_identity="other-key"), rows[1])
    elif variant == "wrong_account":
        rows = (
            replace(rows[0], billing_account_id=rows[1].billing_account_id),
            rows[1],
        )
    elif variant == "watermark":
        over["coupled_watermark"] = "sub-census-content:something-else"
    request = seal_request(world, rows, **over)
    if variant == "population_digest":
        request = replace(request, population_digest="9" * 64)
    assert verifiers.seal.verify_seal(request) is None


def test_n13_expired_and_not_yet_valid(world):
    verifiers = world.build()
    world.clock.value = s.NOW + timedelta(days=3)
    assert (
        verifiers.import_v2.verify_import_v2(import_request(commands(world)[0])) is None
    )
    assert verifiers.provision.verify_provision(provision_request(world)) is None
    assert verifiers.seal.verify_seal(seal_request(world)) is None
    world.clock.value = s.NOW - timedelta(days=3)
    assert (
        verifiers.import_v2.verify_import_v2(import_request(commands(world)[0])) is None
    )
    refused(world, "statement_not_current")


def test_n13b_validity_window_too_long(world):
    world.sign_all(meta={"not_after": s.NOW + timedelta(days=15)})
    refused(world, "statement_validity")


def test_n14_same_key_for_both_roles(world):
    key = world.finance
    profile = ov.OpeningTrustProfile(
        "rehearsal",
        inline_anchors={
            "finance_attestor": s.pub(key),
            "migration_approver": s.pub(key),
        },
    )
    refused(world, "anchors_not_distinct", profile=profile)


def test_n15a_production_fingerprint_mismatch(tmp_path):
    w = s.World(tmp_path, environment="production")
    profile = w.production_profile()
    bad = dict(profile.anchor_fingerprints)
    bad["finance_attestor"] = "sha256:" + "0" * 64
    refused(
        w,
        "anchor_fingerprint_mismatch",
        profile=replace(profile, anchor_fingerprints=bad),
    )


def test_n15b_openbao_unavailable_is_unavailable_not_rejection(tmp_path):
    w = s.World(tmp_path, environment="production")
    outage = {"on": False}

    def resolver(ref):
        if outage["on"]:
            raise RuntimeError("openbao down")
        return w.resolved[ref]

    verifiers = w.build(w.production_profile(resolver))
    command = commands(w)[0]
    assert verifiers.import_v2.verify_import_v2(import_request(command)) is not None
    outage["on"] = True
    with pytest.raises(OpeningVerificationUnavailable):
        verifiers.import_v2.verify_import_v2(import_request(command))
    preview = preview_opening_account_import_v2(command, verifier=verifiers.import_v2)
    assert not preview.admissible
    assert {b.code for b in preview.blockers} >= {"verification_unavailable"}


def test_n15c_revoked_anchor_key_rejects(tmp_path):
    w = s.World(tmp_path, environment="production")
    rotated = {"on": False}

    def resolver(ref):
        if rotated["on"]:
            return s.pub(s.Ed25519PrivateKey.generate())
        return w.resolved[ref]

    verifiers = w.build(w.production_profile(resolver))
    rotated["on"] = True
    assert verifiers.import_v2.verify_import_v2(import_request(commands(w)[0])) is None


@pytest.mark.parametrize(
    "ref", ["env://SOME_NAME", "plain-text-secret", "vault://kv/x#y", "file:///x"]
)
def test_n15d_non_bao_anchor_refs_refused(tmp_path, ref):
    w = s.World(tmp_path, environment="production")
    profile = w.production_profile()
    refs = dict(profile.anchor_refs)
    refs["finance_attestor"] = ref
    with pytest.raises(ov.OpeningVerifierConstructionError) as info:
        replace(profile, anchor_refs=refs)
    assert info.value.code == "anchor_ref_scheme"


def test_n15e_inline_anchor_refused_in_production(tmp_path):
    w = s.World(tmp_path, environment="production")
    with pytest.raises(ov.OpeningVerifierConstructionError) as info:
        replace(
            w.production_profile(),
            inline_anchors={"finance_attestor": s.pub(w.finance)},
        )
    assert info.value.code == "production_inline_anchor_refused"


def test_n16_authority_ref_prefix_rules(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    # rehearsal env with a non-rehearsal: authority ref
    w = s.World(
        tmp_path / "a",
        mutate_assembly=lambda a: a.update(source_authority_ref="finance:real"),
    )
    refused(w, "authority_ref_environment")
    # production env carrying a rehearsal: ref
    p = s.World(
        tmp_path / "b",
        environment="production",
        mutate_assembly=lambda a: a.update(
            migration_authority_ref="rehearsal:migration"
        ),
    )
    refused(p, "authority_ref_environment")


def test_n17_adapter_bytes_mismatch(world):
    path = world.paths["adapter"]
    path.write_bytes(path.read_bytes() + b"\n# tampered\n")
    refused(world, "attestation_mismatch")


def test_n18_unmapped_account_in_census_even_though_adapter_previews(tmp_path):
    def add_unmapped(doc):
        doc["invoices"].append(
            s.invoice(s.INV4, s.SRC3, "SYN-INV-4", "50.00", "0.00", "50.00")
        )
        doc["controls"].append(
            s.control(s.SRC3, 1, 0, 0, "50.00", "0.00", "50.00", "0.00")
        )

    # The adapter alone silently drops the unmapped invoice and still previews.
    (tmp_path / "ok").mkdir()
    clean = s.World(tmp_path / "ok")
    doc = json.loads(clean.census_bytes)
    add_unmapped(doc)
    adapter = ov._load_adapter(s.ADAPTER.read_bytes(), s.A8_ADAPTER_SHA)
    obs, side = adapter.load_sub_observation(doc)
    from dotmac_billing.opening_rehearsal import parse_opening_rehearsal_assembly

    run = adapter.assemble_rehearsal_run(
        obs,
        assembly=adapter.AssemblyInputs(
            parse_opening_rehearsal_assembly(json.loads(clean.assembly_bytes))
        ),
        sidecar=side,
    )
    assert len(run.manifest.dispositions) == 3 and len(doc["invoices"]) == 4
    assert len(run.commands) == 2
    (tmp_path / "bad").mkdir()
    with pytest.raises(ov.OpeningVerifierConstructionError) as info:
        s.World(tmp_path / "bad", mutate_census=add_unmapped)
    assert info.value.code == "x2_unmapped_population"


def test_n19_reversal_always_none(world):
    verifiers = world.build()
    assert verifiers.import_v2.verify_import_reversal_v2(object()) is None
    assert OpeningImportReversalVerificationRequestV2 is not None


def test_n20_seal_twice_equal_and_observation_is_read_only(world):
    verifiers = world.build()
    request = seal_request(world)
    first = verifiers.seal.verify_seal(request)
    second = verifiers.seal.verify_seal(request)
    assert first is not None and first == second
    assert world.observer.calls.count("census") == 2
    # Observation uses B's own snapshot label and capture time (content-only fence).
    cap = json.loads(world.census_bytes)["capture"]
    assert {c[0] for c in world.observer.census_calls} == {cap["snapshot_id"]}


def test_sql_port_uses_read_only_snapshot_and_selects_only(monkeypatch):
    from contextlib import contextmanager
    from unittest.mock import MagicMock

    executed: list[str] = []
    session = MagicMock()

    def execute(statement, *args, **kwargs):
        executed.append(str(statement))
        result = MagicMock()
        result.scalar.return_value = True
        result.all.return_value = [("restore-1",)]
        result.one_or_none.return_value = (7, False)
        return result

    session.execute.side_effect = execute
    entered: list[bool] = []

    @contextmanager
    def snapshot():
        entered.append(True)
        yield session

    monkeypatch.setattr("app.db.read_only_snapshot_session", snapshot)
    port = ov.SqlSourceObservationPort()
    assert port.marker() == "restore-1"
    assert port.role("r") == ov.RoleObservation(7, False)
    assert len(entered) == 2
    assert executed and all(
        sql.lstrip().upper().startswith("SELECT") for sql in executed
    )
    session.add.assert_not_called()
    session.commit.assert_not_called()


def test_cash_lot_evidence_and_missing_seal_pair_refused(world, tmp_path):
    with pytest.raises(ov.OpeningVerifierConstructionError) as info:
        ov.load_evidence(
            replace(world.evidence_paths, cash_lots=tmp_path / "cash.json")
        )
    assert info.value.code == "cash_lot_evidence_unsupported"
    world.files.pop("seal_approval_sig")
    refused(world, "seal_statement_incomplete")


def test_seal_unavailable_without_seal_approval(tmp_path):
    w = s.World(tmp_path, seal=False)
    w.observer.roles["opening_window_role"] = GOOD_ROLE
    verifiers = w.build()
    assert verifiers.seal is None
    assert verifiers.provision.verify_provision(provision_request(w)) is not None


def test_pinned_adapter_is_not_left_importable(world):
    import sys

    world.build()
    assert not [
        name for name in sys.modules if name.startswith("_pinned_opening_adapter_")
    ]


def test_adapter_vendored_hash_matches_a8():
    assert ov._sha(s.ADAPTER.read_bytes()) == s.A8_ADAPTER_SHA


def test_payment_discharge_verifies_but_billing_blocks_until_cash_lots_exist(tmp_path):
    """Finding for the architect: a payment discharge needs a cash lot (Gap C).

    The verifier's own checks (X6/X7, command equality) pass, but Billing's
    preview blocks `historical_cash_source_unclaimed` because a8 assembles
    `cash_lots=()` always. A credit-note discharge has no such dependency.
    """
    w = s.World(tmp_path, kind="payment")
    verifiers = w.build()
    previews = [
        preview_opening_account_import_v2(c, verifier=verifiers.import_v2)
        for c in commands(w)
    ]
    # The manifest is shared, so one payment-discharged invoice blocks every partition.
    assert [p.admissible for p in previews] == [False, False]
    for p in previews:
        assert {b.code for b in p.blockers} == {"historical_cash_source_unclaimed"}
    assert verify_opening_account_import_v2(commands(w)[1], verifiers.import_v2)


# ----------------------------------------------------------------------------
# Review fixes (REVIEW-GAPB): pin before exec, X2 completeness, error mapping
# ----------------------------------------------------------------------------

SENTINEL = b"\nimport builtins as _b\n_b._gapb_sentinel = True\n"


@pytest.fixture()
def sentinel():
    import builtins

    if hasattr(builtins, "_gapb_sentinel"):
        delattr(builtins, "_gapb_sentinel")
    yield lambda: hasattr(builtins, "_gapb_sentinel")
    if hasattr(builtins, "_gapb_sentinel"):
        delattr(builtins, "_gapb_sentinel")


def test_tampered_adapter_never_executes_under_build(world, sentinel):
    path = world.paths["adapter"]
    path.write_bytes(path.read_bytes() + SENTINEL)
    with pytest.raises(ov.OpeningVerifierConstructionError):
        world.build()
    assert not sentinel()


def test_tampered_adapter_never_executes_under_load_evidence_and_draft(
    world, sentinel, capsys
):
    from scripts.migration import billing_opening_authority as cli

    path = world.paths["adapter"]
    path.write_bytes(path.read_bytes() + SENTINEL)
    with pytest.raises(ov.OpeningVerifierConstructionError) as info:
        ov.load_evidence(world.evidence_paths)
    assert info.value.code == "adapter_not_pinned"
    meta = world.meta()
    code = cli.main(
        [
            "draft",
            "--census",
            str(world.paths["census"]),
            "--assembly",
            str(world.paths["assembly"]),
            "--adapter",
            str(path),
            "--environment",
            "rehearsal",
            "--purpose",
            ov.ATTESTATION,
            "--audience",
            meta.audience,
            "--issued-at",
            ov.utc_text(meta.issued_at),
            "--not-before",
            ov.utc_text(meta.not_before),
            "--not-after",
            ov.utc_text(meta.not_after),
        ]
    )
    assert code == 1
    assert "adapter_not_pinned" in capsys.readouterr().out
    assert not sentinel()


def test_x2_refuses_control_with_account_and_null_currency(tmp_path):
    def mutate(doc):
        row = s.control(s.SRC3, 1, 0, 0, "0.00", "0.00", "0.00", "0.00")
        row["currency"] = None
        doc["controls"].append(row)

    with pytest.raises(ov.OpeningVerifierConstructionError) as info:
        s.World(tmp_path, mutate_census=mutate)
    assert info.value.code == "x2_unmapped_population"


def test_x2_refuses_accountless_blocking_exception(tmp_path):
    def mutate(doc):
        doc["blocking_exceptions"].append(
            {
                "code": "orphan_line",
                "account_id": None,
                "currency": None,
                "source_id": s.INV4,
                "detail": "orphan",
            }
        )

    with pytest.raises(ov.OpeningVerifierConstructionError) as info:
        s.World(tmp_path, mutate_census=mutate)
    assert info.value.code == "x2_accountless_exception"


def test_malformed_openbao_key_at_verify_time_is_a_rejection_not_a_leak(tmp_path):
    w = s.World(tmp_path, environment="production")
    state = {"bad": False}

    def resolver(ref):
        return "garbage" if state["bad"] else w.resolved[ref]

    verifiers = w.build(w.production_profile(resolver))
    state["bad"] = True
    assert verifiers.import_v2.verify_import_v2(import_request(commands(w)[0])) is None


def test_census_render_error_maps_to_unavailable(monkeypatch):
    from contextlib import contextmanager
    from dataclasses import replace as rep
    from unittest.mock import MagicMock

    from tests.test_opening_census_wire import _preview

    bad = _preview()
    declared = rep(
        bad.capture.declared_source, population_version="sub-billing-opening-census-v1"
    )
    bad = rep(bad, capture=rep(bad.capture, declared_source=declared))

    @contextmanager
    def snapshot():
        yield MagicMock()

    monkeypatch.setattr("app.db.read_only_snapshot_session", snapshot)
    monkeypatch.setattr(
        "app.services.billing.opening_export.preview_billing_opening_export",
        lambda db, query: bad,
    )
    with pytest.raises(OpeningVerificationUnavailable):
        ov.SqlSourceObservationPort().census_wire(object(), "snap", s.NOW)


def test_production_resolver_is_not_a_public_parameter():
    import dataclasses

    names = {f.name for f in dataclasses.fields(ov.OpeningTrustProfile)}
    assert "resolver" not in names and "_resolver" in names


def test_x2_refuses_lane_for_account_outside_the_assembly(tmp_path):
    def mutate(doc):
        doc["lanes"].append(s.lane(s.SRC3, "0.00"))

    with pytest.raises(ov.OpeningVerifierConstructionError) as info:
        s.World(tmp_path, mutate_census=mutate)
    assert info.value.code == "x2_unmapped_population"


def test_x2_refuses_blocking_exception_for_account_outside_the_assembly(tmp_path):
    def mutate(doc):
        doc["blocking_exceptions"].append(
            {
                "code": "synthetic",
                "account_id": s.SRC3,
                "currency": "NGN",
                "source_id": s.INV4,
                "detail": "x",
            }
        )

    with pytest.raises(ov.OpeningVerifierConstructionError) as info:
        s.World(tmp_path, mutate_census=mutate)
    assert info.value.code == "x2_unmapped_population"
