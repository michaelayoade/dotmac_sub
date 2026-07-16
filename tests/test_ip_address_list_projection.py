"""Tests for the IP-address list-projection contract.

See app/services/web_network_ip.py (ui.ip_address_list_projection). Contract-only
migration: it owns request-state validation, ordering and page sizing. The
IPv4/IPv6 union pagination correctness is a documented separate follow-up.
"""

from __future__ import annotations

import pytest

from app.services.web_network_ip import (
    IP_ADDRESS_LIST_DEFINITION,
    build_ip_address_list_query,
)


def test_ip_address_definition_capabilities():
    d = IP_ADDRESS_LIST_DEFINITION
    assert d.filterable_keys == ("pool_filter",)
    assert d.sortable_keys == ("address",)
    assert d.default_sort == "address"
    assert d.default_sort_dir == "asc"
    assert d.default_per_page == 50


def test_build_ip_address_list_query_normalizes_and_rejects():
    q = build_ip_address_list_query(search="10.0", pool_filter=" ", page=2)
    assert q.search == "10.0"
    assert q.filter_value("pool_filter") is None  # blank dropped
    assert q.sort_by == "address"
    assert q.sort_dir == "asc"
    assert q.page == 2
    assert q.per_page == 50

    assert build_ip_address_list_query(sort_dir="desc").sort_dir == "desc"

    with pytest.raises(ValueError):
        build_ip_address_list_query(sort_by="pool_filter")  # not sortable
    with pytest.raises(ValueError):
        build_ip_address_list_query(per_page=30)  # not an allowed size
