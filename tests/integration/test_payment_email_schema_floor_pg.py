"""Hosted-only catalog canaries against real 638/690 migrations and direct login."""

from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.services.payment_email_cutover import (
    PaymentEmailSchemaState,
    classify_payment_email_schema_floor,
    legacy_image_rollback_allowed,
    read_legacy_image_rollback_floor,
)
from tests.integration.test_payments_intent_correlation_pg import _actual_runtime_login


@dataclass(frozen=True)
class EmailFloorDatabase:
    admin: Engine
    runtime: Engine


@pytest.fixture
def email_floor_database(cloned_database) -> Iterator[EmailFloorDatabase]:
    # 690 includes actual released 638 DDL but predates the failed 691 case.
    admin = create_engine(cloned_database("690_backdated_invoice_permissions"))
    try:
        with _actual_runtime_login(admin) as runtime:
            yield EmailFloorDatabase(admin, runtime)
    finally:
        admin.dispose()


def test_real_638_catalog_is_installed_without_reading_activation_rows(
    email_floor_database: EmailFloorDatabase,
) -> None:
    with Session(email_floor_database.runtime) as db:
        observation = read_legacy_image_rollback_floor(db)
        assert (
            classify_payment_email_schema_floor(observation)
            is PaymentEmailSchemaState.INSTALLED
        )
        assert not legacy_image_rollback_allowed(observation)


@pytest.mark.parametrize(
    "mutation",
    [
        "DO $$ DECLARE constraint_name text; BEGIN SELECT conname INTO STRICT constraint_name FROM pg_catalog.pg_constraint WHERE conrelid='public.payment_email_cutovers'::regclass AND contype='p'; EXECUTE format('ALTER TABLE public.payment_email_cutovers DROP CONSTRAINT %I', constraint_name); END $$",
        "ALTER TABLE public.payment_email_parts ADD COLUMN unexpected_column boolean",
        "ALTER TABLE public.payment_email_parts ALTER COLUMN body DROP NOT NULL",
        "ALTER TABLE public.payment_email_parts ALTER COLUMN content_version SET DEFAULT 1",
        "ALTER TABLE public.payment_email_parts DROP CONSTRAINT ck_payment_email_part_body",
        "ALTER TABLE public.payment_email_parts DROP CONSTRAINT uq_payment_email_part_kind",
        "ALTER TABLE public.payment_email_parts DROP CONSTRAINT fk_payment_email_part_episode_scope",
        "ALTER TABLE public.payment_email_parts DISABLE TRIGGER ALL",
        "ALTER TABLE public.payment_email_episodes DISABLE ROW LEVEL SECURITY",
        "ALTER TABLE public.payment_email_episodes NO FORCE ROW LEVEL SECURITY",
        "ALTER POLICY payment_email_parts_tenant_isolation ON public.payment_email_parts USING (true)",
        "ALTER TABLE public.notification_templates ALTER COLUMN studio_content_sealed DROP NOT NULL",
        "ALTER TABLE public.notification_templates ALTER COLUMN studio_content_sealed SET DEFAULT true",
        "ALTER TABLE public.notification_templates DISABLE TRIGGER guard_sealed_payment_email_content",
        "ALTER FUNCTION public.prevent_sealed_payment_email_content() SECURITY DEFINER",
        "ALTER FUNCTION public.prevent_sealed_payment_email_content() SET search_path = pg_catalog",
        "CREATE OR REPLACE FUNCTION public.prevent_sealed_payment_email_content() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END $$",
        "CREATE FUNCTION public.prevent_sealed_payment_email_content(integer) RETURNS integer LANGUAGE sql AS $$ SELECT $1 $$",
        "CREATE TRIGGER extra_email_guard BEFORE UPDATE ON public.notification_templates FOR EACH ROW EXECUTE FUNCTION public.prevent_sealed_payment_email_content()",
        "DROP TABLE public.payment_email_cutovers",
    ],
)
def test_single_installed_artifact_drift_is_unknown_and_never_legacy(
    email_floor_database: EmailFloorDatabase,
    mutation: str,
) -> None:
    with email_floor_database.admin.begin() as connection:
        connection.execute(text(mutation))
    with Session(email_floor_database.runtime) as db:
        observation = read_legacy_image_rollback_floor(db)
        assert (
            classify_payment_email_schema_floor(observation)
            is PaymentEmailSchemaState.UNKNOWN
        )
        assert not legacy_image_rollback_allowed(observation)


def test_foreign_key_action_mutation_is_unknown(
    email_floor_database: EmailFloorDatabase,
) -> None:
    with email_floor_database.admin.begin() as connection:
        connection.execute(
            text(
                "ALTER TABLE public.payment_email_parts DROP CONSTRAINT fk_payment_email_part_episode_scope"
            )
        )
        connection.execute(
            text(
                "ALTER TABLE public.payment_email_parts ADD CONSTRAINT fk_payment_email_part_episode_scope FOREIGN KEY (tenant_id,episode_id) REFERENCES public.payment_email_episodes(tenant_id,id) ON DELETE RESTRICT"
            )
        )
    with Session(email_floor_database.runtime) as db:
        observation = read_legacy_image_rollback_floor(db)
        assert (
            classify_payment_email_schema_floor(observation)
            is PaymentEmailSchemaState.UNKNOWN
        )


@pytest.mark.parametrize(
    "definition",
    [
        "BEFORE INSERT OR UPDATE OR DELETE ON public.notification_templates FOR EACH ROW WHEN (true)",
        "AFTER INSERT OR UPDATE OR DELETE ON public.notification_templates FOR EACH ROW",
        "BEFORE INSERT OR UPDATE OF body OR DELETE ON public.notification_templates FOR EACH ROW",
        "BEFORE INSERT OR UPDATE OR DELETE ON public.notification_templates FOR EACH ROW",
    ],
)
def test_replaced_trigger_predicate_timing_or_arguments_refuses(
    email_floor_database: EmailFloorDatabase,
    definition: str,
) -> None:
    arguments = (
        "'unexpected'"
        if "WHEN" not in definition
        and "UPDATE OF" not in definition
        and definition.startswith("BEFORE")
        else ""
    )
    with email_floor_database.admin.begin() as connection:
        connection.execute(
            text(
                "DROP TRIGGER guard_sealed_payment_email_content ON public.notification_templates"
            )
        )
        connection.execute(
            text(
                "CREATE TRIGGER guard_sealed_payment_email_content "
                + definition
                + " EXECUTE FUNCTION public.prevent_sealed_payment_email_content("
                + arguments
                + ")"
            )
        )
    with Session(email_floor_database.runtime) as db:
        assert (
            classify_payment_email_schema_floor(read_legacy_image_rollback_floor(db))
            is PaymentEmailSchemaState.UNKNOWN
        )


def test_installed_email_tables_with_inheritance_are_unknown(
    email_floor_database: EmailFloorDatabase,
) -> None:
    with email_floor_database.admin.begin() as connection:
        connection.execute(text("CREATE TABLE public.email_floor_parent (id uuid)"))
        connection.execute(
            text(
                "ALTER TABLE public.payment_email_parts INHERIT public.email_floor_parent"
            )
        )
    with Session(email_floor_database.runtime) as db:
        observation = read_legacy_image_rollback_floor(db)
        assert (
            classify_payment_email_schema_floor(observation)
            is PaymentEmailSchemaState.UNKNOWN
        )
        assert not legacy_image_rollback_allowed(observation)
