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


# --- Combined IPv4/IPv6 union pagination window ---


def _window(offset, limit, total_ipv4):
    from app.services.web_network_ip import combined_address_window

    return combined_address_window(offset, limit, total_ipv4)


def test_window_page_spanning_both_families():
    # 3 IPv4 + N IPv6, page size 4, page 1 (offset 0): 3 IPv4 + 1 IPv6.
    assert _window(0, 4, 3) == (0, 3, 0, 1)


def test_window_page_wholly_in_ipv6():
    # page 2 (offset 4) with 3 IPv4 total: no IPv4, IPv6 from its offset 1.
    assert _window(4, 4, 3) == (3, 0, 1, 4)


def test_window_page_wholly_in_ipv4():
    # page 1 (offset 0), 10 IPv4 available, size 4: all from IPv4, none IPv6.
    assert _window(0, 4, 10) == (0, 4, 0, 0)


def test_window_exact_boundary():
    # offset lands exactly at the IPv4/IPv6 boundary: page is all IPv6 from 0.
    assert _window(3, 4, 3) == (3, 0, 0, 4)


def test_window_no_ipv4():
    # no IPv4 at all: the whole page comes from IPv6 at the raw offset.
    assert _window(6, 4, 0) == (0, 0, 6, 4)
