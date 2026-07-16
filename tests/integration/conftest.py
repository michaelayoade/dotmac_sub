"""Integration flow tests: whole-module journeys on real PostgreSQL.

These reuse the root conftest's ``engine``/``db_session`` fixtures, which run
on PostgreSQL whenever ``TEST_DATABASE_URL`` points at one (CI's
Integration Tests job; on seabone export it against a scratch database in the
``dotmac_sub_db`` container). Under SQLite the whole package skips — flow
tests exist to exercise the PG-only surface (JSONB operators, FK cascades,
row locks) the unit suite shims away.

Each test drives a migrated module's NATIVE path end-to-end with its Phase 3
flag ON via ``enable_flags`` — the flag-off write-throughs stay covered by
the unit suite.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.models.domain_settings import DomainSetting, SettingDomain, SettingValueType
from app.services import control_registry


@pytest.fixture(autouse=True)
def _require_postgres(engine):
    if engine.dialect.name != "postgresql":
        pytest.skip("integration flows run on PostgreSQL only")


@pytest.fixture
def enable_flags(db_session: Session):
    """Flip Phase 3 controls for one test (rows roll back with the session).

    Controls resolve exclusively from their canonical ``modules.<feature>``
    row (``control_registry._resolve_own_flag`` — retired legacy aliases are
    deliberately ignored), so that is what we write. Accepts either the
    control key ("quotes.native_write") or its legacy setting name
    ("quotes_native_write_enabled") for readability at call sites.
    """

    def _enable(*keys: str) -> None:
        for key in keys:
            control = control_registry._CONTROLS.get(key)
            if control is None:
                # legacy-name convenience: find the control by alias
                dotted = key.removesuffix("_enabled").replace("_native_", ".native_")
                control = control_registry._CONTROLS.get(dotted)
            assert control is not None, f"unknown control for {key!r}"
            db_session.add(
                DomainSetting(
                    domain=SettingDomain.modules,
                    key=control_registry.canonical_setting_key(control),
                    value_type=SettingValueType.boolean,
                    value_text="true",
                    is_active=True,
                )
            )
        db_session.flush()

    return _enable
