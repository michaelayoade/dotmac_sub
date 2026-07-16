"""Tests for the NAS dashboard list-projection contract + hybrid pagination.

See app/services/nas/web_builders.py (ui.nas_list_projection). The read owner
paginates purely in SQL when no post-query filter is active (fixing the prior
unconditional 1000-row load-then-slice) and pages over a bounded in-memory scan
when partner_org_id / olt_status filters are active.
"""

from __future__ import annotations

import pytest

from app.models.catalog import NasDevice
from app.services.nas.web_builders import (
    NAS_LIST_DEFINITION,
    build_nas_dashboard_data,
    build_nas_list_query,
)


def _nas(db, name, *, tags=None):
    device = NasDevice(name=name, tags=tags)
    db.add(device)
    db.commit()
    db.refresh(device)
    return device


def _dashboard(db, **overrides):
    params = {
        "vendor": None,
        "nas_type": None,
        "status": None,
        "pop_site_id": None,
        "partner_org_id": None,
        "olt_status": None,
        "search": None,
        "refresh": None,
        "page": 1,
        "limit": 2,
    }
    params.update(overrides)
    return build_nas_dashboard_data(db, **params)


# --- Contract ---


def test_nas_list_definition_declares_expected_capabilities():
    definition = NAS_LIST_DEFINITION
    assert definition.filterable_keys == (
        "vendor",
        "nas_type",
        "status",
        "pop_site_id",
        "partner_org_id",
        "olt_status",
    )
    assert definition.sortable_keys == ("name",)
    assert definition.default_sort == "name"
    assert definition.default_sort_dir == "asc"
    assert definition.default_per_page == 25


def test_build_nas_list_query_normalizes_and_defaults():
    query = build_nas_list_query(vendor=" ", status="active", partner_org_id="acme")
    assert query.filter_value("vendor") is None  # blank dropped
    assert query.filter_value("status") == "active"
    assert query.filter_value("partner_org_id") == "acme"
    assert query.sort_by == "name"
    assert query.sort_dir == "asc"
    assert query.per_page == 25


def test_build_nas_list_query_rejects_out_of_contract_params():
    with pytest.raises(ValueError):
        build_nas_list_query(sort_by="status")
    with pytest.raises(ValueError):
        build_nas_list_query(per_page=30)


# --- Hybrid pagination ---


def test_sql_path_paginates_and_counts_in_db(db_session):
    for name in ("nas-a", "nas-b", "nas-c"):
        _nas(db_session, name)

    first = _dashboard(db_session, page=1, limit=2)
    assert first["total"] == 3
    assert first["total_pages"] == 2
    assert [d.name for d in first["devices"]] == ["nas-a", "nas-b"]

    second = _dashboard(db_session, page=2, limit=2)
    assert [d.name for d in second["devices"]] == ["nas-c"]


def test_sql_path_sort_dir_desc(db_session):
    for name in ("nas-a", "nas-b", "nas-c"):
        _nas(db_session, name)

    result = _dashboard(db_session, page=1, limit=10, sort_dir="desc")
    assert [d.name for d in result["devices"]] == ["nas-c", "nas-b", "nas-a"]


def test_post_query_partner_filter_scopes_and_counts(db_session):
    _nas(db_session, "nas-untagged")
    _nas(db_session, "nas-acme", tags=["partner_org:acme"])
    _nas(db_session, "nas-other", tags=["partner_org:other"])

    result = _dashboard(db_session, page=1, limit=10, partner_org_id="acme")
    assert result["total"] == 1
    assert [d.name for d in result["devices"]] == ["nas-acme"]
