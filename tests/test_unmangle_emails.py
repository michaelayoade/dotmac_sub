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


def test_guard_detects_unique_constraint_on_base_schema(db_session):
    # On main (pre-decoupling) the test schema is built from the model whose
    # email column is still unique, so the apply-guard must report it as
    # constrained and the script will refuse to write.
    assert _email_is_unique_constrained(db_session) is True
