"""Widening detection across objects, maps and keyed lists (§1.9, future ACLs)."""

from __future__ import annotations

from app.services.composition.fields import (
    AccessKind,
    FieldSpec,
    Kind,
    SpecIndex,
    Strategy,
    Tie,
)
from app.services.composition.widening import params_widening

SPECS = SpecIndex(
    {
        # keyed list of ACL entries
        "/acl": FieldSpec(Kind.list, Strategy.keyed, key="name"),
        "/acl/*/name": FieldSpec(Kind.scalar),
        "/acl/*/sources": FieldSpec(
            Kind.list,
            Strategy.union,
            access=AccessKind.allow_list,
            tie=Tie.restrictive,
            canon="cidr",
        ),
        "/acl/*/denied": FieldSpec(
            Kind.list,
            Strategy.union,
            access=AccessKind.deny_list,
            tie=Tie.restrictive,
            canon="lower",
        ),
        "/acl/*/write": FieldSpec(
            Kind.scalar, access=AccessKind.secure_bool, secure=False
        ),
        # a map keyed by service name
        "/services": FieldSpec(Kind.map),
        "/services/*": FieldSpec(Kind.object),
        "/services/*/sources": FieldSpec(
            Kind.list,
            Strategy.union,
            access=AccessKind.allow_list,
            tie=Tie.restrictive,
            canon="cidr",
        ),
        "/services/*/enabled": FieldSpec(
            Kind.scalar, access=AccessKind.secure_bool, secure=False
        ),
        # a plain nested object
        "/btest": FieldSpec(Kind.object),
        "/btest/enabled": FieldSpec(
            Kind.scalar, access=AccessKind.secure_bool, secure=False
        ),
    }
)


def paths(before, after):
    return [item.path for item in params_widening(SPECS, before, after)]


def test_keyed_list_element_allow_list_growth_is_a_widening():
    before = {"acl": [{"name": "mgmt", "sources": ["192.0.2.0/24"]}]}
    wider = {"acl": [{"name": "mgmt", "sources": ["192.0.2.0/24", "198.51.100.0/24"]}]}
    assert paths(before, wider) == ["/acl/mgmt/sources"]
    assert paths(wider, before) == []  # narrowing is not widening


def test_keyed_list_covering_prefix_is_a_widening():
    before = {"acl": [{"name": "mgmt", "sources": ["192.0.2.1/32"]}]}
    after = {"acl": [{"name": "mgmt", "sources": ["192.0.2.0/24"]}]}
    assert paths(before, after) == ["/acl/mgmt/sources"]


def test_keyed_list_secure_bool_flip_is_a_widening():
    before = {"acl": [{"name": "mgmt", "write": False}]}
    after = {"acl": [{"name": "mgmt", "write": True}]}
    assert paths(before, after) == ["/acl/mgmt/write"]
    assert paths(after, before) == []


def test_new_keyed_element_is_compared_against_nothing_granted():
    before = {"acl": []}
    new_open = {
        "acl": [{"name": "extra", "sources": ["203.0.113.0/24"], "write": True}]
    }
    assert sorted(paths(before, new_open)) == [
        "/acl/extra/sources",
        "/acl/extra/write",
    ]
    new_closed = {"acl": [{"name": "extra", "sources": [], "write": False}]}
    assert paths(before, new_closed) == []


def test_removing_a_keyed_element_drops_its_deny_list_protection():
    before = {"acl": [{"name": "mgmt", "denied": ["telnet"], "write": False}]}
    assert paths(before, {"acl": []}) == ["/acl/mgmt/denied"]


def test_each_element_is_judged_on_its_own_key():
    before = {
        "acl": [
            {"name": "a", "sources": ["192.0.2.0/24"]},
            {"name": "b", "sources": ["198.51.100.0/24"]},
        ]
    }
    after = {
        "acl": [
            {"name": "a", "sources": ["198.51.100.0/24"]},  # a now allows b's network
            {"name": "b", "sources": ["198.51.100.0/24"]},
        ]
    }
    assert paths(before, after) == ["/acl/a/sources"]


def test_map_members_are_walked_by_key():
    before = {"services": {"api": {"sources": ["192.0.2.0/24"], "enabled": False}}}
    after = {
        "services": {
            "api": {"sources": ["192.0.2.0/24", "203.0.113.0/24"], "enabled": True}
        }
    }
    assert sorted(paths(before, after)) == [
        "/services/api/enabled",
        "/services/api/sources",
    ]
    assert paths(after, before) == []


def test_new_map_member_that_opens_access_is_a_widening():
    before = {"services": {}}
    after = {"services": {"ssh": {"sources": ["203.0.113.0/24"], "enabled": True}}}
    assert sorted(paths(before, after)) == [
        "/services/ssh/enabled",
        "/services/ssh/sources",
    ]


def test_plain_object_children_and_unchanged_documents():
    assert paths({"btest": {"enabled": False}}, {"btest": {"enabled": True}}) == [
        "/btest/enabled"
    ]
    doc = {
        "acl": [{"name": "mgmt", "sources": ["192.0.2.0/24"], "write": False}],
        "services": {"api": {"sources": ["192.0.2.0/24"], "enabled": False}},
        "btest": {"enabled": False},
    }
    assert paths(doc, doc) == []
    assert paths({}, {}) == []
