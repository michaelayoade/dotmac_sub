"""Frozen 681 empty-baseline adoption shapes; never current-model introspection."""

import re

from sqlalchemy import Connection, inspect, text

from scripts.migrations.replacement_schema_contract import ReplacementTableShape

SHARED_OUTAGE_TABLES = (
    ReplacementTableShape(
        *(
            "outage_complaint_correlation_receipts",
            (
                ("id", "uuid", True),
                ("incident_id", "uuid", True),
                ("intent_id", "uuid", True),
                ("fingerprint", "character varying(64)", True),
                ("link_id", "uuid", True),
                ("subscription_id", "uuid", True),
                ("original_revision_id", "uuid", True),
                ("actor_user_id", "uuid", True),
                ("reason", "text", True),
                ("proof_origin", "character varying(24)", True),
                ("ref_id", "character varying(120)", True),
                ("created_at", "timestamp with time zone", True),
            ),
            ("id",),
            (("incident_id", "intent_id"),),
            (
                (("actor_user_id",), ("system_users.id",), "RESTRICT"),
                (("incident_id",), ("outage_incidents.id",), "RESTRICT"),
                (("link_id",), ("outage_incident_ticket_links.id",), "RESTRICT"),
                (("original_revision_id",), ("outage_scope_revisions.id",), "RESTRICT"),
                (("subscription_id",), ("subscriptions.id",), "RESTRICT"),
            ),
            (
                "length(fingerprint)=64 AND length(trim(reason))>0 AND length(ref_id)=113 AND proof_origin IN ('new_link','reviewed_adoption')",
            ),
        )
    ),
    ReplacementTableShape(
        *(
            "support_resolution_shared_incidents",
            (
                ("request_id", "uuid", True),
                ("complaint_link_id", "uuid", True),
                ("incident_id", "uuid", True),
                ("subscription_id", "uuid", True),
                ("original_receipt_id", "uuid", True),
                ("original_receipt_ref", "character varying(120)", True),
                ("original_revision_id", "uuid", True),
                ("original_revision_sequence", "integer", True),
                ("infrastructure_ticket_id", "uuid", True),
                ("reviewed_revision_id", "uuid", True),
                ("reviewed_revision_sequence", "integer", True),
                ("membership_token", "character varying(64)", True),
                ("resolved_at", "timestamp with time zone", True),
                ("empty_scope_review", "character varying(48)", False),
            ),
            ("request_id", "complaint_link_id"),
            (),
            (
                (
                    ("complaint_link_id",),
                    ("outage_incident_ticket_links.id",),
                    "RESTRICT",
                ),
                (("incident_id",), ("outage_incidents.id",), "RESTRICT"),
                (("infrastructure_ticket_id",), ("support_tickets.id",), "RESTRICT"),
                (
                    ("original_receipt_id",),
                    ("outage_complaint_correlation_receipts.id",),
                    "RESTRICT",
                ),
                (("original_revision_id",), ("outage_scope_revisions.id",), "RESTRICT"),
                (("request_id",), ("support_resolution_requests.id",), "RESTRICT"),
                (("reviewed_revision_id",), ("outage_scope_revisions.id",), "RESTRICT"),
                (("subscription_id",), ("subscriptions.id",), "RESTRICT"),
            ),
            (
                "empty_scope_review IS NULL OR empty_scope_review='restored_without_issued_field_scope'",
                "original_revision_sequence>0 AND reviewed_revision_sequence>0 AND length(membership_token)=64",
            ),
        )
    ),
    ReplacementTableShape(
        *(
            "support_resolution_shared_work_scopes",
            (
                ("request_id", "uuid", True),
                ("complaint_link_id", "uuid", True),
                ("issuance_link_id", "uuid", True),
                ("work_order_id", "uuid", True),
            ),
            ("request_id", "complaint_link_id", "issuance_link_id"),
            (),
            (
                (
                    ("issuance_link_id",),
                    ("outage_incident_work_order_links.id",),
                    "RESTRICT",
                ),
                (
                    ("request_id", "complaint_link_id"),
                    (
                        "support_resolution_shared_incidents.request_id",
                        "support_resolution_shared_incidents.complaint_link_id",
                    ),
                    "RESTRICT",
                ),
                (
                    ("request_id", "work_order_id"),
                    (
                        "support_resolution_bases.request_id",
                        "support_resolution_bases.work_order_id",
                    ),
                    "RESTRICT",
                ),
                (("work_order_id",), ("work_order.id",), "RESTRICT"),
            ),
            (),
        )
    ),
    ReplacementTableShape(
        *(
            "completion_replacement_outage_proofs",
            (
                ("decision_id", "uuid", True),
                ("source_issuance_link_id", "uuid", True),
                ("replacement_issuance_link_id", "uuid", True),
                ("incident_id", "uuid", True),
                ("infrastructure_ticket_id", "uuid", True),
                ("revision_id", "uuid", True),
                ("revision_sequence", "integer", True),
                ("membership_token", "character varying(64)", True),
                ("replacement_revision_id", "uuid", True),
                ("replacement_revision_sequence", "integer", True),
                ("replacement_membership_token", "character varying(64)", True),
                ("target_type", "character varying(20)", True),
                ("target_id", "uuid", False),
                ("project_id", "uuid", False),
                ("task_id", "uuid", False),
                ("purpose", "character varying(40)", False),
                ("access_technology", "character varying(40)", False),
            ),
            ("decision_id",),
            (),
            (
                (
                    ("decision_id",),
                    ("completion_replacement_decisions.id",),
                    "RESTRICT",
                ),
                (("incident_id",), ("outage_incidents.id",), "RESTRICT"),
                (("infrastructure_ticket_id",), ("support_tickets.id",), "RESTRICT"),
                (("project_id",), ("projects.id",), "RESTRICT"),
                (
                    ("replacement_issuance_link_id",),
                    ("outage_incident_work_order_links.id",),
                    "RESTRICT",
                ),
                (
                    ("replacement_revision_id",),
                    ("outage_scope_revisions.id",),
                    "RESTRICT",
                ),
                (("revision_id",), ("outage_scope_revisions.id",), "RESTRICT"),
                (
                    ("source_issuance_link_id",),
                    ("outage_incident_work_order_links.id",),
                    "RESTRICT",
                ),
                (("task_id",), ("project_tasks.id",), "RESTRICT"),
            ),
            (
                "source_issuance_link_id<>replacement_issuance_link_id AND revision_sequence>0 AND length(membership_token)=64",
            ),
        )
    ),
)


def _check(value: str) -> str:
    value = value.lower().replace("both from", "")
    value = re.sub(r"::(?:character varying|text)(?:\[\])?", "", value)
    value = re.sub(r"([a-z_]+) in \(([^)]+)\)", r"\1=anyarray[\2]", value)
    return re.sub(r'[\s()"]', "", value)


def verify_shared_outage_adoption(bind: Connection) -> None:
    inspector = inspect(bind)
    for expected in SHARED_OUTAGE_TABLES:
        if not inspector.has_table(expected.name):
            continue
        if bind.scalar(
            text(f'SELECT EXISTS (SELECT 1 FROM "{expected.name}" LIMIT 1)')
        ):
            raise RuntimeError(
                f"Nonempty pre-migration shared outage table: {expected.name}"
            )
        columns = tuple(
            (str(row[0]), str(row[1]), bool(row[2]))
            for row in bind.execute(
                text(
                    "SELECT attname,format_type(atttypid,atttypmod),attnotnull FROM pg_attribute "
                    "WHERE attrelid=to_regclass(:name) AND attnum>0 AND NOT attisdropped ORDER BY attnum"
                ),
                {"name": expected.name},
            )
        )
        primary = tuple(
            inspector.get_pk_constraint(expected.name)["constrained_columns"]
        )
        unique = tuple(
            sorted(
                tuple(item["column_names"])
                for item in inspector.get_unique_constraints(expected.name)
            )
        )
        foreign = tuple(
            sorted(
                (
                    tuple(item["constrained_columns"]),
                    tuple(
                        f"{item['referred_table']}.{column}"
                        for column in item["referred_columns"]
                    ),
                    item["options"].get("ondelete"),
                )
                for item in inspector.get_foreign_keys(expected.name)
            )
        )
        checks = tuple(
            sorted(
                _check(str(item["sqltext"]))
                for item in inspector.get_check_constraints(expected.name)
            )
        )
        indexes = tuple(
            sorted(
                (tuple(item["column_names"]), bool(item["unique"]))
                for item in inspector.get_indexes(expected.name)
            )
        )
        expected_indexes = tuple(sorted((item, True) for item in expected.unique))
        if (
            columns != expected.columns
            or primary != expected.primary_key
            or unique != expected.unique
            or foreign != expected.foreign_keys
            or checks != tuple(sorted(_check(item) for item in expected.checks))
            or indexes != expected_indexes
        ):
            raise RuntimeError(f"Shared outage schema mismatch: {expected.name}")
