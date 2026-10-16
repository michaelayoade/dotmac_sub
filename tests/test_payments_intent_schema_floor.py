"""Pure decision and malformed catalog contract checks; no database claims."""

import ast
import hashlib
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest
from sqlalchemy.orm import Session

from app.services.domain_errors import DomainError
from app.services.payments_intent_schema_floor import (
    _GUARD_DIGEST,
    PaymentsIntentSchemaObservation,
    PaymentsIntentSchemaState,
    classify_payments_intent_schema_floor,
    legacy_image_rollback_allowed,
    read_payments_intent_schema_floor,
)

PREDECESSOR = PaymentsIntentSchemaObservation(True, 0, 0, 0, 0, False)
INSTALLED = PaymentsIntentSchemaObservation(True, 2, 2, 1, 1, True)


def test_only_complete_predecessor_admits_legacy_image() -> None:
    assert legacy_image_rollback_allowed(PREDECESSOR)
    for field, value in (
        ("legacy_shape_complete", False),
        ("correlation_columns", 1),
        ("correlation_constraints", 1),
        ("correlation_triggers", 1),
        ("correlation_functions", 1),
        ("installed_shape_complete", True),
        ("correlation_columns", -1),
        ("correlation_columns", False),
    ):
        assert not legacy_image_rollback_allowed(replace(PREDECESSOR, **{field: value}))


def test_supported_floor_classification_requires_complete_base_and_installed_shapes() -> (
    None
):
    assert (
        classify_payments_intent_schema_floor(PREDECESSOR)
        is PaymentsIntentSchemaState.PREDECESSOR
    )
    assert (
        classify_payments_intent_schema_floor(INSTALLED)
        is PaymentsIntentSchemaState.INSTALLED
    )
    assert not legacy_image_rollback_allowed(INSTALLED)
    for observation in (
        replace(INSTALLED, legacy_shape_complete=False),
        replace(INSTALLED, correlation_constraints=1),
        replace(PREDECESSOR, correlation_columns=1),
        replace(PREDECESSOR, installed_shape_complete=True),
    ):
        assert (
            classify_payments_intent_schema_floor(observation)
            is PaymentsIntentSchemaState.UNKNOWN
        )


@pytest.mark.parametrize(
    "catalog",
    [
        None,
        (),
        (True, 0, 0, 0, 0, None),
        (True, False, 0, 0, 0, False),
        (True, -1, 0, 0, 0, False),
    ],
)
def test_malformed_catalog_is_unknown(catalog: tuple[object, ...] | None) -> None:
    db = Mock(spec=Session)
    db.get_bind.return_value.dialect.name = "postgresql"
    db.execute.side_effect = [
        Mock(one_or_none=lambda: ("runtime", "runtime", True, False, False)),
        Mock(one_or_none=lambda: catalog),
    ]
    with pytest.raises(DomainError, match="schema floor could not be proved"):
        read_payments_intent_schema_floor(db)


@pytest.mark.parametrize(
    "identity",
    [
        None,
        ("a", "a"),
        ("a", "other", True, False, False),
        ("a", "a", True, True, False),
        ("a", "a", True, False, True),
        ("a", "a", 1, 0, 0),
    ],
)
def test_role_switch_or_privileged_login_is_unknown(
    identity: tuple[object, ...] | None,
) -> None:
    db = Mock(spec=Session)
    db.get_bind.return_value.dialect.name = "postgresql"
    db.execute.return_value.one_or_none.return_value = identity
    with pytest.raises(DomainError):
        read_payments_intent_schema_floor(db)
    assert db.execute.call_count == 1


def test_migration_guard_and_catalog_digest_remain_bound() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "alembic/versions/691_payments_intent_correlation.py"
    )
    parsed = ast.parse(source.read_text())
    body = next(
        ast.literal_eval(node.value)
        for node in parsed.body
        if isinstance(node, ast.Assign)
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == "GUARD_BODY"
    )
    assert (
        hashlib.md5(body.encode(), usedforsecurity=False).hexdigest() == _GUARD_DIGEST
    )
