"""Immutable reviewed ADR 0030 preservation evidence; admission stays in the gate.

Hashes preserve the reviewed v16/v17/v22 source manifests and exact published
main evidence. New convergence bytes are bound explicitly after source review. This module
never refreshes hashes from the working tree. External module lineages are not
part of this host-only declaration (ADR 0011).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SourceArtifact:
    name: str
    manifest_sha256: str


@dataclass(frozen=True)
class PreservedRevision:
    revision: str
    parents: tuple[str, ...]
    path: str
    sha256: str
    original_path: str


@dataclass(frozen=True)
class PreservedBridge:
    revision: str
    parents: tuple[str, str]


@dataclass(frozen=True)
class PreservationDeclaration:
    adr_path: str
    base_commit: str
    amendment_heading: str
    published_revisions: tuple[str, str]
    bridges: tuple[PreservedBridge, ...]
    sources: tuple[SourceArtifact, ...]
    revisions: tuple[PreservedRevision, ...]


FIELD_LIFECYCLE_PRESERVATION = PreservationDeclaration(
    adr_path="docs/adr/0030-preserved-field-migration-convergence.md",
    base_commit="b69f1b4a6d088522ce5834aa57ccd94a3530606d",
    amendment_heading="## Amendment: published router-health convergence — 2026-10-12",
    published_revisions=(
        "671_customer_connection_type_definitions",
        "672_router_health_reconciler",
    ),
    bridges=(
        PreservedBridge(
            "683_field_lifecycle_connection_types_merge",
            (
                "682_accepted_project_field_completion",
                "671_customer_connection_type_definitions",
            ),
        ),
        PreservedBridge(
            "686_field_lifecycle_router_health_merge",
            ("684_customer_connection_type_defaults", "672_router_health_reconciler"),
        ),
    ),
    sources=(
        SourceArtifact(
            "dotmac-main-a287-migration-hashes.json",
            "b78ae85af24ec9af49f726d0bd4b71d2801867d4ba87f68f85134d265d8b54a8",
        ),
        SourceArtifact(
            "dotmac-final-v22-hashes.json",
            "e063fff314cb62414805ef3e3fc8aea5f56aec1cdab7694923f91d699ea8b8e8",
        ),
        SourceArtifact(
            "dotmac-final-v16-hashes.json",
            "50f8a5d06eefe9cf68e3ecd243a70cb62b029ef6ae17c8e6a899d517aee1548e",
        ),
        SourceArtifact(
            "dotmac-final-v17-hashes.json",
            "2a343a2e5009d4c24dd8c292c431327ce01809543b90954802d9deff3f8546d4",
        ),
    ),
    revisions=(
        PreservedRevision(
            "672_router_health_reconciler",
            ("671_customer_connection_type_definitions",),
            "alembic/versions/672_router_health_reconciler.py",
            "8f7eb71e1a026329ad9865b3afabe02eb6a0b4d5786b5f13c2995fab3ac7f615",
            "alembic/versions/672_router_health_reconciler.py",
        ),
        PreservedRevision(
            "686_field_lifecycle_router_health_merge",
            ("684_customer_connection_type_defaults", "672_router_health_reconciler"),
            "alembic/versions/686_field_lifecycle_router_health_merge.py",
            "cca8f15251009bb8d5f16dd95e2be020abf47b92705f0fea0143be62dbab086b",
            "alembic/versions/686_field_lifecycle_router_health_merge.py",
        ),
        PreservedRevision(
            "671_customer_connection_type_definitions",
            ("670_user_credential_version",),
            "alembic/versions/671_customer_connection_type_definitions.py",
            "f38139e3b87ad288007d6c7665e7a32e78cc722838dc49b30dc3d18c4617012c",
            "alembic/versions/671_customer_connection_type_definitions.py",
        ),
        PreservedRevision(
            "672_field_identity_review",
            ("671_assignment_generation",),
            "alembic/versions/687_field_identity_review.py",
            "ef65af7442b0859a8ca1ccc6af368a78e9295b1e7083a435122639211e7c8e19",
            "alembic/versions/672_field_identity_review.py",
        ),
        PreservedRevision(
            "673_explicit_work_order_classification",
            ("672_field_identity_review",),
            "alembic/versions/673_explicit_work_order_classification.py",
            "7bd8af57c8958950975391bc5bd9e72da32c16e747c7c6926e0460fddaddedb8",
            "alembic/versions/673_explicit_work_order_classification.py",
        ),
        PreservedRevision(
            "674_selected_vendor_attending_person",
            ("673_explicit_work_order_classification",),
            "alembic/versions/674_selected_vendor_attending_person.py",
            "8484d9b72d0f87afc209bb4b8a1c57ad1d6a561ff5d9d629e9928d97c44ddbf7",
            "alembic/versions/674_selected_vendor_attending_person.py",
        ),
        PreservedRevision(
            "675_project_engineering_plans",
            ("674_selected_vendor_attending_person",),
            "alembic/versions/675_project_engineering_plans.py",
            "4773c79207fb512e0651e6e298e161344b4423eee63562979c4789cc4dda91fc",
            "alembic/versions/675_project_engineering_plans.py",
        ),
        PreservedRevision(
            "676_project_work_order_issuance",
            ("675_project_engineering_plans",),
            "alembic/versions/676_project_work_order_issuance.py",
            "a79cb32cfe5077f8a877edeff859500f0fd4c6a9f16c415868ddc535ab38269e",
            "alembic/versions/676_project_work_order_issuance.py",
        ),
        PreservedRevision(
            "677_field_completion_review",
            ("676_project_work_order_issuance",),
            "alembic/versions/677_field_completion_review.py",
            "94ec1407a8059b33abfc225e4be90b16024748714fd85a2ed0ee2dd630ab009c",
            "alembic/versions/677_field_completion_review.py",
        ),
        PreservedRevision(
            "678_support_resolution_completion_basis",
            ("677_field_completion_review",),
            "alembic/versions/678_support_resolution_completion_basis.py",
            "f0236669146a5efef8d43cdd2caa7de7dc27441e2a2cdfbb0c2db07b3e2b5077",
            "alembic/versions/678_support_resolution_completion_basis.py",
        ),
        PreservedRevision(
            "679_reviewed_failed_scope_replacement",
            ("678_support_resolution_completion_basis",),
            "alembic/versions/679_reviewed_failed_scope_replacement.py",
            "2ada5458eccc026c311a8513b45a5a2643bec39cc66a34cdd2799e104bda8232",
            "alembic/versions/679_reviewed_failed_scope_replacement.py",
        ),
        PreservedRevision(
            "680_field_visit_followup",
            ("679_reviewed_failed_scope_replacement",),
            "alembic/versions/680_field_visit_followup.py",
            "1f1c9e50410fca096326f77c190c88f2939e4482d552bb20d8be24060ccbd448",
            "alembic/versions/680_field_visit_followup.py",
        ),
        PreservedRevision(
            "681_shared_outage_completion",
            ("680_field_visit_followup",),
            "alembic/versions/681_shared_outage_completion.py",
            "7aeaaf680c3215a5823536926173d08d57e4e8dabf8a018e4cf96061a1ce6cc9",
            "alembic/versions/681_shared_outage_completion.py",
        ),
        PreservedRevision(
            "682_accepted_project_field_completion",
            ("681_shared_outage_completion",),
            "alembic/versions/682_accepted_project_field_completion.py",
            "17462fa5ff811a02963984f1f7a6fc797d0d2de62b64bfa81e64ddaab6e7c5be",
            "alembic/versions/682_accepted_project_field_completion.py",
        ),
        PreservedRevision(
            "683_field_lifecycle_connection_types_merge",
            (
                "682_accepted_project_field_completion",
                "671_customer_connection_type_definitions",
            ),
            "alembic/versions/683_field_lifecycle_connection_types_merge.py",
            "86273a08e0cfe7e5a24abfc665aa3673e3fee4d16b853bf91b6a15357dca88ae",
            "alembic/versions/683_field_lifecycle_connection_types_merge.py",
        ),
        PreservedRevision(
            "684_customer_connection_type_defaults",
            ("683_field_lifecycle_connection_types_merge",),
            "alembic/versions/684_customer_connection_type_defaults.py",
            "ec7dafd15f9a7ed2fff7f74911bf252008e74b5c8594bf409b903bf382418681",
            "alembic/versions/684_customer_connection_type_defaults.py",
        ),
        PreservedRevision(
            "671_assignment_generation",
            ("670_user_credential_version",),
            "alembic/versions/685_assignment_generation.py",
            "00f3de074016f91b7e5eb63086f2fcc3de3e0d87ec87fd3204fac5d66b9fa465",
            "alembic/versions/671_assignment_generation.py",
        ),
    ),
)
