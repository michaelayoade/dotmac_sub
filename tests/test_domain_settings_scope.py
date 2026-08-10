"""Every settings row Sub writes belongs to the operator tenant.

ADR-0009: Sub provisions exactly one tenant, the ISP operator, and its settings
belong to it. `platform` is the kernel's deployment-wide level BENEATH tenant,
not a synonym for "this deployment" — so a row landing there is not a harmless
labelling difference, it is a row at the wrong level of the resolution chain.

Migration 509 moved every row to the operator tenant and the model's DEFAULT
stayed at `platform`, so settings written after that migration arrived
somewhere else from settings written before it. Resolution hides the split
(a platform row is exactly what a tenant read falls back to), which is why the
guard has to be here rather than in a resolution test.
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError, StatementError

from app.models.domain_settings import (
    PLATFORM_SCOPE,
    TENANT_SCOPE,
    DomainSetting,
    SettingDomain,
)
from app.models.subscription_engine import SettingValueType
from app.schemas.settings import DomainSettingCreate
from app.services import domain_settings as domain_settings_service
from app.services.operator_tenant import OPERATOR_TENANT_ID


def test_a_created_setting_belongs_to_the_operator_tenant(db_session):
    settings = domain_settings_service.DomainSettings(domain=SettingDomain.gis)
    created = settings.create(
        db_session,
        DomainSettingCreate(
            domain=SettingDomain.gis,
            key="scope_probe_created",
            value_type=SettingValueType.integer,
            value_text="60",
        ),
    )

    assert created.scope_kind == TENANT_SCOPE
    assert created.tenant_id == OPERATOR_TENANT_ID


def test_an_upserted_setting_belongs_to_the_operator_tenant(db_session):
    """The seed's path, and the one that writes most rows.

    `ensure_by_key` reaches `create` for a key with no row, which is how a
    newly declared setting first appears — and therefore the path by which the
    estate would have kept accumulating platform rows.
    """

    settings = domain_settings_service.DomainSettings(domain=SettingDomain.gis)
    row = settings.ensure_by_key(
        db_session,
        key="scope_probe_ensured",
        value_type=SettingValueType.string,
        value_text="anything",
    )

    assert row.scope_kind == TENANT_SCOPE
    assert row.tenant_id == OPERATOR_TENANT_ID


def test_the_kernel_resolves_a_row_written_at_that_scope(db_session):
    """The two halves must agree.

    The cutover resolves through `dotmac_kernel.settings_resolver` asking for
    the operator tenant's rows. A row written at a scope that read never looks
    at would resolve to its default instead — the setting would exist, be
    visible in the admin list, and have no effect.
    """

    from app.services.settings_spec import resolve_value

    settings = domain_settings_service.DomainSettings(domain=SettingDomain.gis)
    settings.ensure_by_key(
        db_session,
        key="sync_interval_minutes",
        value_type=SettingValueType.integer,
        value_text="47",
    )

    assert resolve_value(db_session, SettingDomain.gis, "sync_interval_minutes") == 47


def test_a_tenant_scoped_row_without_a_tenant_is_refused(db_session):
    """The invariant the CHECK carries, and why it is not just a default.

    A `tenant` row with a NULL tenant is invisible to the resolver, which
    filters on BOTH columns — a setting that exists and can never be read.
    Fixing only the default would have left a raw INSERT free to produce one.
    """

    db_session.add(
        DomainSetting(
            domain=SettingDomain.gis,
            key="scope_probe_headless",
            value_type=SettingValueType.string,
            value_text="x",
            scope_kind=TENANT_SCOPE,
            tenant_id=None,
        )
    )
    with pytest.raises((IntegrityError, StatementError)):
        db_session.flush()
    db_session.rollback()


def test_a_platform_row_carrying_a_tenant_is_refused(db_session):
    """The other direction of the same invariant: `platform` has no tenant."""

    db_session.add(
        DomainSetting(
            domain=SettingDomain.gis,
            key="scope_probe_confused",
            value_type=SettingValueType.string,
            value_text="x",
            scope_kind=PLATFORM_SCOPE,
            tenant_id=OPERATOR_TENANT_ID,
        )
    )
    with pytest.raises((IntegrityError, StatementError)):
        db_session.flush()
    db_session.rollback()


def test_one_key_may_hold_a_row_at_each_scope(db_session):
    """Uniqueness is per SCOPE, which is what migration 507 replaced.

    Pinned because the model kept declaring `UniqueConstraint(domain, key)`
    long after 507 dropped it, so the metadata-built lanes were enforcing a
    rule Postgres no longer had — and a test written against those lanes would
    have concluded, wrongly, that the old constraint was still the contract.
    """

    db_session.add(
        DomainSetting(
            domain=SettingDomain.gis,
            key="scope_probe_two_levels",
            value_type=SettingValueType.string,
            value_text="platform-level",
            scope_kind=PLATFORM_SCOPE,
            tenant_id=None,
        )
    )
    db_session.add(
        DomainSetting(
            domain=SettingDomain.gis,
            key="scope_probe_two_levels",
            value_type=SettingValueType.string,
            value_text="tenant-level",
            scope_kind=TENANT_SCOPE,
            tenant_id=OPERATOR_TENANT_ID,
        )
    )

    db_session.flush()  # must not raise
