"""Typed and PII-minimized contract for production-derived lineage evidence."""

from __future__ import annotations

import json
import stat

import pytest
from pydantic import ValidationError

from scripts.migration.kernel_lineage_rehearsal_evidence import (
    AuditActorKind,
    AuditCohort,
    KernelLineageRehearsalEvidence,
    ProjectionState,
    RoleCohort,
    TableContract,
    write_private_bundle,
)


def _evidence(
    *,
    source_revisions: tuple[str, ...] = ("528_roles_kernel_r1_additive",),
) -> KernelLineageRehearsalEvidence:
    empty_digest = "0" * 64
    return KernelLineageRehearsalEvidence(
        source_revisions=source_revisions,
        tables=tuple(
            TableContract(
                table_name=table_name,
                row_count=0,
                columns_sha256=empty_digest,
                constraints_sha256=empty_digest,
                indexes_sha256=empty_digest,
                rls_enabled=False,
                rls_forced=False,
            )
            for table_name in (
                "tenants",
                "tenant_domains",
                "roles",
                "user_credentials",
                "audit_events",
                "party_roles",
            )
        ),
        roles=(
            RoleCohort(
                projection_state=ProjectionState.PROJECTED,
                is_active=True,
                count=7,
                maximum_name_length=48,
            ),
        ),
        credentials=(),
        audit_events=(
            AuditCohort(
                actor_type=AuditActorKind.USER,
                has_actor_id=True,
                has_actor_party_id=True,
                has_details=True,
                has_created_at=True,
                is_active=True,
                count=11,
            ),
        ),
        party_roles=(),
    )


def test_bundle_round_trips_without_customer_values() -> None:
    private_values = (
        "private.person@example.test",
        "Private Customer Name",
        "not-a-real-password-hash",
    )

    serialized = _evidence().canonical_json()
    restored = KernelLineageRehearsalEvidence.model_validate_json(serialized)

    assert restored == _evidence()
    assert all(value not in serialized for value in private_values)
    assert "actor_id" not in json.loads(serialized)["audit_events"][0]


def test_bundle_rejects_unknown_fields_that_could_smuggle_row_data() -> None:
    payload = _evidence().model_dump(mode="json")
    payload["customer_email"] = "private.person@example.test"

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        KernelLineageRehearsalEvidence.model_validate(payload)


def test_bundle_accepts_host_and_composed_module_revision_ids() -> None:
    revisions = (
        "559_upcoming_charges_indexes",
        "567_inbox_agent_analytics_indexes",
        "558_receivable_projection",
        "557_outbox_relay_prereq",
        "bi_0001_billing",
        "cl_0001_collections",
        "pm_0001_payment_intents",
        "so_0001_service_delivery_orders",
        "su_0003_billing_treatments",
    )

    assert _evidence(source_revisions=revisions).source_revisions == revisions


@pytest.mark.parametrize(
    "revision",
    (
        "billing_0001_billing",
        "bi_001_billing",
        "BI_0001_billing",
        "bi_0001_Billing",
    ),
)
def test_bundle_rejects_revision_ids_outside_composed_contract(revision: str) -> None:
    with pytest.raises(ValidationError, match="String should match pattern"):
        _evidence(source_revisions=(revision,))


def test_private_writer_refuses_overwrite_and_uses_owner_only_mode(tmp_path) -> None:
    target = tmp_path / "lineage-evidence.json"

    write_private_bundle(target, _evidence())

    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):
        write_private_bundle(target, _evidence())
