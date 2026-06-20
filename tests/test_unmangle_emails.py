"""Tests for the +NNNN email un-mangle one-off helpers."""

from __future__ import annotations

import pytest

from scripts.one_off.unmangle_plus_suffixed_emails import (
    _email_is_unique_constrained,
    _real_email,
)


@pytest.mark.parametrize(
    "mangled,expected",
    [
        ("wanserverng+8265@gmail.com", "wanserverng@gmail.com"),
        ("wanserverng+8266@gmail.com", "wanserverng@gmail.com"),
        ("Owner+12@ABC.COM", "owner@abc.com"),  # normalised to lower-case
    ],
)
def test_real_email_strips_numeric_tag(mangled, expected):
    assert _real_email(mangled) == expected


@pytest.mark.parametrize(
    "value",
    [
        "plain@gmail.com",  # no tag
        "jane+newsletter@gmail.com",  # non-numeric (legitimate) tag
        "jane+2024+1@gmail.com",  # extra '+' in tag is not pure digits
        "not-an-email",
        "",
    ],
)
def test_real_email_ignores_non_generated_addresses(value):
    assert _real_email(value) is None


def test_guard_reports_no_unique_constraint_after_decoupling(db_session):
    # Post-decoupling (#316) the model's email column is non-unique, so the test
    # schema built from it has no UNIQUE on subscribers.email. The apply-guard
    # therefore reports False — i.e. --apply is permitted (un-mangling, which
    # creates intentional duplicates, is now safe). The guard exists to refuse
    # only against an OLD prod DB where the constraint hasn't been dropped yet.
    assert _email_is_unique_constrained(db_session) is False
