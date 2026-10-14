"""Equal-rank merge outcomes never depend on layer names (M8, M10).

Every case runs with the two layer-name orders: the same two patches are
written by layers ``a`` and ``b`` and then by ``b`` and ``a``.
"""

from __future__ import annotations

import pytest

from .demo_plugin import block, rev, run

BASE = block(
    "m",
    servers=["g.example"],
    sources=["192.0.2.0/24", "198.51.100.0/24"],
    denied=["telnet", "ftp"],
    routes=["R1"],
    btest={"enabled": False},
    peers=[{"remote": "192.0.2.1", "asn": 1}],
    x=0,
)


def outcomes(first: dict, second: dict, *, base: dict = BASE):
    """The module result with (a, b) = (first, second) and with (b, a)."""
    g = rev("global", "g", base)
    one = run(
        [
            g,
            rev("group", "a", block("m", **first), weight=100),
            rev("group", "b", block("m", **second), weight=100),
        ]
    ).modules["m"]
    two = run(
        [
            g,
            rev("group", "a", block("m", **second), weight=100),
            rev("group", "b", block("m", **first), weight=100),
        ]
    ).modules["m"]
    return one, two


def paths(result):
    return sorted(c.path for c in result.conflicts)


# ---------------------------------------------------- restrictive allow lists


@pytest.mark.parametrize(
    ("first", "second", "expected"),
    [
        # $replace with $add: only what the replace allows AND the add layer keeps
        (
            {"sources": {"$replace": ["198.51.100.0/24"]}},
            {"sources": {"$add": ["203.0.113.0/24"]}},
            ["198.51.100.0/24"],
        ),
        # $replace with $replace: the intersection
        (
            {"sources": {"$replace": ["192.0.2.0/24", "198.51.100.0/24"]}},
            {"sources": {"$replace": ["198.51.100.0/24", "203.0.113.0/24"]}},
            ["198.51.100.0/24"],
        ),
        # $replace with $remove: $remove still removes
        (
            {"sources": {"$replace": ["192.0.2.0/24", "198.51.100.0/24"]}},
            {"sources": {"$remove": ["192.0.2.0/24"]}},
            ["198.51.100.0/24"],
        ),
        # a plain list is {"$add": list}
        (
            {"sources": {"$replace": ["198.51.100.0/24"]}},
            {"sources": ["203.0.113.0/24"]},
            ["198.51.100.0/24"],
        ),
    ],
)
def test_allow_list_mixes_resolve_to_the_intersection_in_both_orders(
    first, second, expected
):
    one, two = outcomes(first, second)
    assert one.params["sources"] == two.params["sources"] == expected
    assert not one.conflicts and not two.conflicts


def test_allow_list_replace_never_widens_with_a_peer_add():
    """The reviewer's probe: neither order may end up wider than the $replace."""
    one, two = outcomes(
        {"sources": {"$replace": ["198.51.100.0/24"]}},
        {"sources": {"$add": ["203.0.113.0/24"]}},
    )
    for result in (one, two):
        assert "203.0.113.0/24" not in result.params["sources"]
        assert "192.0.2.0/24" not in result.params["sources"]


def test_allow_list_add_add_is_the_union_and_remove_beats_add():
    one, two = outcomes(
        {"sources": {"$add": ["203.0.113.0/24"]}},
        {"sources": {"$add": ["10.0.0.0/8"]}},
    )
    assert sorted(one.params["sources"]) == sorted(two.params["sources"])
    assert (
        "203.0.113.0/24" in one.params["sources"]
        and "10.0.0.0/8" in one.params["sources"]
    )
    one, two = outcomes(
        {"sources": {"$add": ["203.0.113.0/24"]}},
        {"sources": {"$remove": ["203.0.113.0/24"]}},
    )
    assert "203.0.113.0/24" not in one.params["sources"]
    assert "203.0.113.0/24" not in two.params["sources"]


def test_allow_list_three_layers_are_order_independent():
    g = rev("global", "g", BASE)
    patches = [
        {"sources": {"$replace": ["192.0.2.0/24", "198.51.100.0/24"]}},
        {"sources": {"$add": ["203.0.113.0/24"]}},
        {"sources": {"$replace": ["198.51.100.0/24", "203.0.113.0/24"]}},
    ]
    import itertools

    results = set()
    for perm in itertools.permutations(patches):
        layers = [
            rev("group", name, block("m", **p), weight=100)
            for name, p in zip("abc", perm, strict=True)
        ]
        results.add(tuple(run([g, *layers]).modules["m"].params["sources"]))
    assert results == {("198.51.100.0/24",)}


# ----------------------------------------------------- restrictive deny lists


@pytest.mark.parametrize(
    ("first", "second", "expected"),
    [
        (
            {"denied": {"$replace": ["http"]}},
            {"denied": {"$add": ["smtp"]}},
            ["telnet", "ftp", "http", "smtp"],
        ),
        (
            {"denied": {"$replace": ["http"]}},
            {"denied": {"$replace": ["smtp"]}},
            ["http", "smtp"],
        ),
        (
            {"denied": {"$replace": ["http"]}},
            {"denied": {"$remove": ["telnet"]}},
            ["ftp", "http"],
        ),
    ],
)
def test_deny_list_mixes_resolve_to_the_union_in_both_orders(first, second, expected):
    one, two = outcomes(first, second)
    assert (
        sorted(one.params["denied"]) == sorted(two.params["denied"]) == sorted(expected)
    )
    assert one.params["denied"] == two.params["denied"]  # same order, too
    assert not one.conflicts and not two.conflicts


# ----------------------------------------- non-restrictive: every mix conflicts


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ({"routes": {"$replace": ["A"]}}, {"routes": {"$add": ["B"]}}),
        ({"routes": {"$replace": ["A"]}}, {"routes": {"$remove": ["R1"]}}),
        ({"routes": {"$replace": ["A"]}}, {"routes": {"$replace": ["B"]}}),
        ({"routes": {"$add": ["B"]}}, {"routes": {"$remove": ["B"]}}),
        ({"servers": ["a.example"]}, {"servers": ["b.example"]}),
        ({"x": 1}, {"x": 2}),
        (
            {"peers": {"$replace": [{"remote": "192.0.2.9"}]}},
            {"peers": [{"remote": "192.0.2.8"}]},
        ),
        (
            {"peers": {"$replace": [{"remote": "192.0.2.9"}]}},
            {"peers": {"$replace": [{"remote": "192.0.2.8"}]}},
        ),
    ],
)
def test_other_equal_rank_mixes_conflict_in_both_orders(first, second):
    one, two = outcomes(first, second)
    assert paths(one) == paths(two) != []
    assert one.blocked and two.blocked


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ({"routes": {"$add": ["A"]}}, {"routes": {"$add": ["B"]}}),
        ({"routes": {"$replace": ["A"]}}, {"routes": {"$replace": ["A"]}}),
        ({"servers": ["a.example"]}, {"servers": ["a.example"]}),
        ({"x": 1}, {"x": 1}),
        ({"routes": {"$remove": ["R1"]}}, {"routes": {"$remove": ["R1"]}}),
    ],
)
def test_agreeing_equal_rank_writes_do_not_conflict(first, second):
    one, two = outcomes(first, second)
    assert not one.conflicts and not two.conflicts


# ------------------------------------------------------------- delete vs set

DELETE = {"$delete": True}

DELETE_VS_SET = {
    "scalar": ({"x": DELETE}, {"x": 5}, "/x"),
    "replace_list": ({"servers": DELETE}, {"servers": ["b.example"]}, "/servers"),
    "union_add": ({"routes": DELETE}, {"routes": ["Q"]}, "/routes"),
    "union_replace": ({"routes": DELETE}, {"routes": {"$replace": ["Q"]}}, "/routes"),
    "union_remove": ({"routes": DELETE}, {"routes": {"$remove": ["R1"]}}, "/routes"),
    "restrictive_allow": (
        {"sources": DELETE},
        {"sources": ["203.0.113.0/24"]},
        "/sources",
    ),
    "restrictive_deny": ({"denied": DELETE}, {"denied": {"$add": ["http"]}}, "/denied"),
    "new_child_of_deleted_object": (
        {"btest": DELETE},
        {"btest": {"authenticate": True}},
        "/btest",
    ),
    "existing_child_of_deleted_object": (
        {"btest": DELETE},
        {"btest": {"enabled": True}},
        "/btest",
    ),
    "keyed_element_field": (
        {"peers": [{"remote": "192.0.2.1", "$delete": True}]},
        {"peers": [{"remote": "192.0.2.1", "hold": 90}]},
        "/peers/192.0.2.1",
    ),
    "keyed_whole_list": (
        {"peers": DELETE},
        {"peers": [{"remote": "192.0.2.7"}]},
        "/peers",
    ),
}


@pytest.mark.parametrize("case", sorted(DELETE_VS_SET))
def test_delete_vs_set_at_equal_rank_is_a_conflict_in_both_orders(case):
    first, second, path = DELETE_VS_SET[case]
    one, two = outcomes(first, second)
    assert paths(one) == paths(two) == [path]
    assert one.blocked and two.blocked
    assert {c for c in one.conflicts[0].layers} == {"group:a", "group:b"}


def test_delete_vs_delete_and_delete_of_an_absent_key_agree():
    one, two = outcomes({"x": DELETE}, {"x": DELETE})
    assert not one.conflicts and not two.conflicts
    # a no-op delete of a field nobody set still clashes with a set at equal rank
    base = block("m", note="n")
    one, two = outcomes({"x": DELETE}, {"x": 5}, base=base)
    assert paths(one) == paths(two) == ["/x"]


def test_delete_at_a_higher_rank_overrides_without_conflict():
    g = rev("global", "g", BASE)
    low = rev("group", "a", block("m", x=5), weight=100)
    high = rev("router", "r1", block("m", x=DELETE))
    eff = run([g, low, high])
    assert not eff.modules["m"].conflicts and "x" not in eff.modules["m"].params


# ------------------------------------------------------------ three-way ties


def test_three_way_tie_lists_every_conflicting_layer():
    layers = [
        rev("group", "a", block("m", x=1), weight=100),
        rev("group", "b", block("m", x=1), weight=100),
        rev("group", "c", block("m", x=2), weight=100),
    ]
    import itertools

    for perm in itertools.permutations(layers):
        conflict = run(list(perm)).modules["m"].conflicts[0]
        assert conflict.path == "/x"
        assert sorted(conflict.layers) == ["group:a", "group:b", "group:c"]
        assert set(conflict.values) == {1, 2}


def test_three_way_delete_set_set_lists_all_three():
    eff = run(
        [
            rev("group", "a", block("m", x=1), weight=100),
            rev("group", "b", block("m", x={"$delete": True}), weight=100),
            rev("group", "c", block("m", x=1), weight=100),
        ]
    )
    (conflict,) = eff.modules["m"].conflicts
    assert sorted(conflict.layers) == ["group:a", "group:b", "group:c"]
