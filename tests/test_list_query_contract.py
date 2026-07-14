from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pytest

from app.services.list_query import (
    ListDefinition,
    ListFieldDefinition,
    PageMeta,
)
from app.services.web_customer_lists import (
    build_customer_list_query,
    build_customer_list_query_from_legacy_params,
)
from app.services.web_subscriber_lists import (
    SUBSCRIBER_LIST_DEFINITION,
    build_subscriber_list_query,
    build_subscriber_list_query_from_legacy_params,
)


def _definition() -> ListDefinition:
    return ListDefinition(
        key="example",
        fields=(
            ListFieldDefinition("name", "Name", searchable=True, sortable=True),
            ListFieldDefinition("status", "Status", filterable=True),
            ListFieldDefinition("created_at", "Created", sortable=True),
        ),
        default_sort="created_at",
    )


def test_list_definition_owns_normalization_capabilities_and_url_round_trip():
    definition = _definition()

    query = definition.build_query(
        search="  Acme & Sons  ",
        filters={"status": " active "},
        sort_by="name",
        sort_dir="asc",
        page=3,
        per_page=50,
    )

    assert definition.searchable_keys == ("name",)
    assert definition.filterable_keys == ("status",)
    assert definition.sortable_keys == ("name", "created_at")
    assert query.search == "Acme & Sons"
    assert query.filter_value("status") == "active"
    assert query.offset == 100

    url = query.url("/admin/example")
    params = parse_qs(urlsplit(url).query)
    assert params == {
        "search": ["Acme & Sons"],
        "status": ["active"],
        "sort": ["name"],
        "dir": ["asc"],
        "page": ["3"],
        "per_page": ["50"],
    }


def test_sort_change_resets_page_and_preserves_search_filters_and_page_size():
    query = _definition().build_query(
        search="needle",
        filters={"status": "active"},
        page=4,
        per_page=50,
    )

    params = parse_qs(
        urlsplit(query.url("/admin/example", sort_by="name", sort_dir="asc")).query
    )

    assert params["page"] == ["1"]
    assert params["per_page"] == ["50"]
    assert params["search"] == ["needle"]
    assert params["status"] == ["active"]


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"filters": {"unknown": "x"}}, "Unsupported filters"),
        ({"sort_by": "unknown"}, "Unsupported sort field"),
        ({"sort_dir": "sideways"}, "sort_dir must be asc or desc"),
        ({"per_page": 20}, "per_page must be one of"),
    ],
)
def test_list_definition_rejects_undeclared_query_state(overrides, message):
    params = {
        "search": None,
        "filters": {},
        "sort_by": None,
        "sort_dir": None,
        "page": 1,
        "per_page": 25,
        **overrides,
    }

    with pytest.raises(ValueError, match=message):
        _definition().build_query(**params)


def test_page_meta_clamps_out_of_range_page_and_builds_compact_navigation():
    query = _definition().build_query(
        search=None,
        filters={},
        page=99,
        per_page=10,
    )

    meta = PageMeta.from_query(query, total_items=101)

    assert meta.page == 11
    assert meta.start_item == 101
    assert meta.end_item == 101
    assert meta.has_previous is True
    assert meta.has_next is False
    assert meta.navigation == (1, None, 10, 11)


def test_empty_page_meta_uses_zero_item_range():
    query = _definition().build_query(
        search=None,
        filters={},
        page=1,
        per_page=25,
    )

    meta = PageMeta.from_query(query, total_items=0)

    assert meta.page == 1
    assert meta.total_pages == 1
    assert meta.start_item == 0
    assert meta.end_item == 0
    assert meta.navigation == (1,)


def test_customer_query_canonicalizes_aliases_and_rejects_bad_filter_values():
    query = build_customer_list_query(
        search=None,
        status=" ACTIVE ",
        customer_type="individual",
        nas_id=None,
        pop_site_id=None,
    )

    assert query.filter_value("status") == "active"
    assert query.filter_value("customer_type") == "person"

    with pytest.raises(ValueError, match="Unsupported status filter"):
        build_customer_list_query(
            search=None,
            status="unknown",
            customer_type=None,
            nas_id=None,
            pop_site_id=None,
        )

    with pytest.raises(ValueError, match="nas_id must be a valid UUID"):
        build_customer_list_query(
            search=None,
            status=None,
            customer_type=None,
            nas_id="not-a-uuid",
            pop_site_id=None,
        )


def test_legacy_customer_table_query_maps_to_canonical_page_contract():
    query = build_customer_list_query_from_legacy_params(
        {
            "limit": "50",
            "offset": "100",
            "q": "  Acme  ",
            "activation_state": "inactive",
            "customer_type": "individual",
            "sort_by": "customer_name",
            "sort_dir": "asc",
            "_ts": "1771944545000",
        }
    )

    assert query.search == "Acme"
    assert query.filter_value("status") == "inactive"
    assert query.filter_value("customer_type") == "person"
    assert query.sort_by == "name"
    assert query.sort_dir == "asc"
    assert query.page == 3
    assert query.per_page == 50
    assert query.offset == 100


@pytest.mark.parametrize(
    ("params", "message"),
    [
        ({"limit": "20"}, "limit must be one of"),
        ({"limit": "25", "offset": "1"}, "offset must align"),
        ({"email": "customer@example.com"}, "Unsupported customer list parameters"),
        ({"sort_by": "email"}, "Unsupported sort field"),
        (
            {"status": "active", "activation_state": "inactive"},
            "filters conflict",
        ),
    ],
)
def test_legacy_customer_table_query_rejects_parallel_query_capabilities(
    params, message
):
    with pytest.raises(ValueError, match=message):
        build_customer_list_query_from_legacy_params(params)


def test_subscriber_query_declares_capabilities_and_normalizes_aliases():
    query = build_subscriber_list_query(
        search="  HWTC1234  ",
        status=" ACTIVE ",
        subscriber_type="individual",
        sort_by="subscriber_number",
        sort_dir="asc",
        page=2,
        per_page=25,
    )

    assert SUBSCRIBER_LIST_DEFINITION.filterable_keys == (
        "subscriber_type",
        "status",
    )
    assert query.search == "HWTC1234"
    assert query.filter_value("status") == "active"
    assert query.filter_value("subscriber_type") == "person"
    assert query.sort_by == "subscriber_number"
    assert query.offset == 25


def test_legacy_subscriber_table_query_maps_to_canonical_page_contract():
    query = build_subscriber_list_query_from_legacy_params(
        {
            "limit": "50",
            "offset": "100",
            "q": "  Acme  ",
            "activation_state": "inactive",
            "subscriber_type": "business",
            "sort_by": "subscriber_name",
            "sort_dir": "asc",
        }
    )

    assert query.search == "Acme"
    assert query.filter_value("status") == "inactive"
    assert query.filter_value("subscriber_type") == "business"
    assert query.sort_by == "name"
    assert query.page == 3
    assert query.offset == 100


@pytest.mark.parametrize(
    ("params", "message"),
    [
        ({"limit": "20"}, "limit must be one of"),
        ({"limit": "25", "offset": "1"}, "offset must align"),
        ({"approval_status": "approved"}, "Unsupported subscriber list parameters"),
        ({"sort_by": "email"}, "Unsupported sort field"),
        (
            {"status": "active", "activation_state": "inactive"},
            "filters conflict",
        ),
    ],
)
def test_legacy_subscriber_table_query_rejects_parallel_query_capabilities(
    params, message
):
    with pytest.raises(ValueError, match=message):
        build_subscriber_list_query_from_legacy_params(params)
