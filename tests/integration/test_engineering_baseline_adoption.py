"""Migration-local baseline adoption refuses data and schema drift."""

import pytest
from sqlalchemy import Engine, text

from scripts.migrations.completion_schema_contract import COMPLETION_TABLES
from scripts.migrations.engineering_schema_contract import (
    ENGINEERING_TABLES,
    ISSUANCE_TABLES,
    verify_precreated_tables,
)
from scripts.migrations.outer_transaction_provenance import CreationParentTable
from scripts.migrations.support_resolution_schema_contract import (
    SUPPORT_RESOLUTION_TABLES,
)
from tests.integration.test_project_engineering_plan_acceptance import (
    NativeScope,
    prepare,
)
from tests.integration.test_project_engineering_plan_acceptance import (
    native_engine as migrated_engine_fixture,
)
from tests.integration.test_project_engineering_plan_acceptance import (
    scope as native_scope_fixture,
)

native_engine = migrated_engine_fixture
scope = native_scope_fixture


@pytest.mark.parametrize(
    "statement,table_name",
    [
        (
            "ALTER TABLE engineering_plan_receipts DROP CONSTRAINT fk_engineering_receipt_decision",
            "engineering_plan_receipts",
        ),
        (
            "ALTER TABLE engineering_plan_revisions ALTER COLUMN content DROP NOT NULL",
            "engineering_plan_revisions",
        ),
        (
            "DROP INDEX uq_engineering_task_plan; "
            "CREATE UNIQUE INDEX uq_engineering_task_plan ON project_engineering_plans "
            "(project_id, task_id, technology) WHERE task_id IS NULL",
            "project_engineering_plans",
        ),
        (
            "ALTER TABLE project_work_order_issuances DROP CONSTRAINT fk_project_issuance_approval",
            "project_work_order_issuances",
        ),
        (
            "ALTER TABLE completion_observations ALTER COLUMN observation DROP NOT NULL",
            "completion_observations",
        ),
        (
            "ALTER TABLE stored_file_retentions ALTER COLUMN source_owner DROP NOT NULL",
            "stored_file_retentions",
        ),
        (
            "CREATE UNIQUE INDEX support_resolution_requests_ticket_extra "
            "ON support_resolution_requests(ticket_id)",
            "support_resolution_requests",
        ),
    ],
)
def test_precreated_schema_is_verified_before_adoption(
    native_engine: Engine, statement: str, table_name: str
) -> None:
    expected = (
        ENGINEERING_TABLES
        + ISSUANCE_TABLES
        + COMPLETION_TABLES
        + SUPPORT_RESOLUTION_TABLES
    )
    outer_transaction_extensions = (
        CreationParentTable.criteria_revision,
        CreationParentTable.submission,
        CreationParentTable.review_decision,
        CreationParentTable.support_request,
    )
    with native_engine.begin() as db:
        verify_precreated_tables(
            db,
            expected=expected,
            outer_transaction_extensions=outer_transaction_extensions,
        )
        for sql in statement.split("; "):
            db.execute(text(sql))
        with pytest.raises(RuntimeError, match=f"schema drift: {table_name}"):
            verify_precreated_tables(
                db,
                expected=expected,
                outer_transaction_extensions=outer_transaction_extensions,
            )


def test_precreated_engineering_evidence_is_never_adopted_as_empty_baseline(
    native_engine: Engine, scope: NativeScope
) -> None:
    prepare(native_engine, scope)
    with native_engine.connect() as db:
        with pytest.raises(
            RuntimeError, match="Nonempty pre-migration engineering table"
        ):
            verify_precreated_tables(db, expected=ENGINEERING_TABLES)
