"""Exact historical operator Tenant participant; runtime provisioning unchanged."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.services.operator_tenant import (
    OPERATOR_TENANT_ID,
    OperatorTenantScopeError,
    apply_operator_tenant_transaction_scope,
)


class HistoricalOperatorTenantImportRefused(OperatorTenantScopeError):
    """Exact historical Tenant import differs from its fixture contract."""

    code = "tenancy.operator_tenant_history.refused"


def stage_historical_operator_tenant(
    connection: Connection, *, tenant_id: UUID
) -> UUID:
    """Copy the one escrow Tenant exactly; rollback-only historical participant.

    Called only by party.kernel_import after canonical OID validation. No boot
    provisioning, inferred defaults, transaction completion or domain activation.
    """
    if (
        tenant_id != OPERATOR_TENANT_ID
        or not connection.in_transaction()
        or connection.in_nested_transaction()
    ):
        raise HistoricalOperatorTenantImportRefused(
            "Historical operator tenant scope differs"
        )
    ids = tuple(
        connection.scalars(
            text("SELECT id FROM sub_kernel_entry_fixture_escrow.tenants ORDER BY id")
        )
    )
    if ids != (tenant_id,) or connection.scalar(
        text("SELECT count(*) FROM public.tenants")
    ):
        raise HistoricalOperatorTenantImportRefused(
            "Historical import requires exactly one source and empty destination"
        )
    connection.execute(
        text(
            "INSERT INTO public.tenants "
            "(id,slug,name,is_active,suspended_at,deleted_at,created_at,updated_at) "
            "SELECT id,slug,name,is_active,suspended_at,deleted_at,created_at,updated_at "
            "FROM sub_kernel_entry_fixture_escrow.tenants WHERE id=:tenant"
        ),
        {"tenant": tenant_id},
    )
    apply_operator_tenant_transaction_scope(connection)
    return tenant_id


def verify_historical_operator_tenant(
    connection: Connection, *, tenant_id: UUID
) -> UUID:
    """Verify the complete Tenant destination keyset and every copied field."""
    if tenant_id != OPERATOR_TENANT_ID or connection.scalar(
        text("""
        SELECT EXISTS (
            SELECT 1 FROM sub_kernel_entry_fixture_escrow.tenants s
            FULL JOIN public.tenants t ON t.id=s.id
            WHERE s.id IS NULL OR t.id IS NULL OR s.id<>:tenant
               OR (t.slug,t.name,t.is_active,t.suspended_at,t.deleted_at,t.created_at,t.updated_at)
                  IS DISTINCT FROM
                  (s.slug,s.name,s.is_active,s.suspended_at,s.deleted_at,s.created_at,s.updated_at)
        ) OR (SELECT count(*) FROM public.tenants)<>1
    """),
        {"tenant": tenant_id},
    ):
        raise HistoricalOperatorTenantImportRefused(
            "Historical Tenant destination verification failed"
        )
    return tenant_id
