"""Contract tests for the ledger F1 read-only offer inventory.

These pin the three properties that make the report safe to point at a real
database: it refuses to run without an explicitly named target, every statement
it can build compiles to a bare ``SELECT``, and its name signals stay signals.

No database is required. The statements are compiled, not executed, which is the
whole point: a statement that stopped being a read fails here rather than in
production.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from scripts.negotiated_price_offer_inventory import (
    ALL_QUERY_BUILDERS,
    MIN_NAME_TOKEN_LENGTH,
    TARGET_ENV_VAR,
    InventoryTargetError,
    assert_select_only,
    build_options,
    customer_like_signal,
    group_key,
    invoice_line_price_query,
    non_technical_tokens,
    party_name_tokens,
    resolve_target,
    tokenize,
)

_URL = "postgresql+psycopg://reader@example.invalid/sub_inventory_snapshot"
_OTHER_URL = "postgresql+psycopg://reader@example.invalid/other_snapshot"


def test_refuses_to_run_without_a_named_target() -> None:
    with pytest.raises(InventoryTargetError) as excinfo:
        resolve_target(argument=None, environ={})
    assert "no default target" in str(excinfo.value)


def test_blank_target_is_still_no_target() -> None:
    with pytest.raises(InventoryTargetError):
        resolve_target(argument="   ", environ={TARGET_ENV_VAR: "  "})


def test_argument_names_the_target() -> None:
    assert resolve_target(argument=_URL, environ={}) == _URL


def test_environment_variable_names_the_target() -> None:
    assert resolve_target(argument=None, environ={TARGET_ENV_VAR: _URL}) == _URL


def test_disagreeing_argument_and_environment_is_refused() -> None:
    with pytest.raises(InventoryTargetError):
        resolve_target(argument=_URL, environ={TARGET_ENV_VAR: _OTHER_URL})


def test_agreeing_argument_and_environment_is_accepted() -> None:
    assert resolve_target(argument=_URL, environ={TARGET_ENV_VAR: _URL}) == _URL


def test_application_database_url_is_never_inherited() -> None:
    """The ambient app target must not be able to become this report's target."""

    with pytest.raises(InventoryTargetError):
        resolve_target(argument=None, environ={"DATABASE_URL": _URL})


def test_build_options_requires_a_target() -> None:
    with pytest.raises(InventoryTargetError):
        build_options([], environ={})


def test_build_options_rejects_a_non_positive_window() -> None:
    with pytest.raises(InventoryTargetError):
        build_options(
            ["--database-url", _URL, "--invoice-window-days", "0"],
            environ={},
        )


def test_build_options_carries_the_named_target_and_window() -> None:
    options = build_options(
        ["--database-url", _URL, "--invoice-window-days", "90"],
        environ={},
    )
    assert options.database_url == _URL
    assert options.invoice_window_days == 90


@pytest.mark.parametrize("builder", ALL_QUERY_BUILDERS, ids=lambda fn: fn.__name__)
def test_every_statement_is_a_select(builder) -> None:
    compiled = str(builder().compile())
    assert compiled.lstrip().upper().startswith("SELECT")


def test_no_statement_contains_a_mutating_keyword() -> None:
    builders = (
        *ALL_QUERY_BUILDERS,
        lambda: invoice_line_price_query(window_days=30),
    )
    forbidden = (
        "INSERT ",
        "UPDATE ",
        "DELETE ",
        "CREATE ",
        "ALTER ",
        "DROP ",
        "TRUNCATE ",
        "GRANT ",
        "FOR UPDATE",
    )
    for builder in builders:
        compiled = str(builder().compile()).upper()
        for keyword in forbidden:
            assert keyword not in compiled, (builder, keyword)


def test_the_select_only_guard_actually_bites() -> None:
    """A guard proven only against passing input proves nothing."""

    from app.models.catalog import CatalogOffer

    with pytest.raises(AssertionError):
        assert_select_only(CatalogOffer.__table__.delete())  # type: ignore[arg-type]


def test_invoice_window_is_a_bound_cutoff_not_a_dialect_interval() -> None:
    statement = invoice_line_price_query(
        window_days=30,
        now=datetime(2026, 8, 25, tzinfo=UTC),
    )
    compiled = str(statement.compile())
    assert "make_interval" not in compiled
    assert "issued_at >=" in compiled


def test_tokenize_splits_on_punctuation_and_lowercases() -> None:
    assert tokenize("200 Mbps Fiber mr richard") == (
        "200",
        "mbps",
        "fiber",
        "mr",
        "richard",
    )
    assert tokenize(None) == ()


def test_technical_names_leave_no_unmatched_tokens() -> None:
    assert non_technical_tokens("100 Mbps Fiber Business") == ()
    assert non_technical_tokens("25Mbps Dedicated Unlimited") == ()


def test_a_customer_named_offer_surfaces_its_tokens() -> None:
    assert "richard" in non_technical_tokens("200 Mbps Fiber mr richard")
    assert "norrenberger" in non_technical_tokens("STM-1 Fiber (Norrenberger)")


def test_party_name_tokens_ignores_short_fragments() -> None:
    tokens = party_name_tokens(
        [
            {"company_name": "AScomnet Ltd", "legal_name": None, "last_name": "Ade"},
        ]
    )
    assert "ascomnet" in tokens
    assert "ade" not in tokens
    assert all(len(token) >= MIN_NAME_TOKEN_LENGTH for token in tokens)


def test_customer_like_signal_separates_heuristic_from_evidence() -> None:
    signal = customer_like_signal(
        offer_name="700 Mbps Dedicated AScomnet",
        known_party_tokens=frozenset({"ascomnet"}),
    )
    assert signal["party_name_matches"] == ["ascomnet"]
    assert "ascomnet" in signal["unmatched_tokens"]
    assert signal["has_signal"] is True


def test_a_purely_technical_name_raises_no_signal() -> None:
    signal = customer_like_signal(
        offer_name="100 Mbps Fiber Residential",
        known_party_tokens=frozenset({"ascomnet"}),
    )
    assert signal["unmatched_tokens"] == []
    assert signal["party_name_matches"] == []
    assert signal["has_signal"] is False


def test_group_key_folds_case_and_whitespace_but_not_speed_or_family() -> None:
    left = {
        "name": " 25 Mbps Fiber ",
        "speed_download_mbps": 25,
        "speed_upload_mbps": 25,
        "plan_family": "dedicated",
    }
    right = {
        "name": "25 mbps fiber",
        "speed_download_mbps": 25,
        "speed_upload_mbps": 25,
        "plan_family": "dedicated",
    }
    other = dict(right, plan_family="unlimited")
    assert group_key(left) == group_key(right)
    assert group_key(left) != group_key(other)
