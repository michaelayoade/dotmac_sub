from __future__ import annotations

import ast
import inspect

import pytest

from app.services import web_system_import_wizard
from app.services.financial_imports import FINANCIAL_IMPORT_MODULES


def test_wizard_has_no_raw_financial_model_construction():
    tree = ast.parse(inspect.getsource(web_system_import_wizard))
    forbidden = {"Invoice", "Payment", "Subscription", "LedgerEntry"}
    constructed = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert constructed.isdisjoint(forbidden)


@pytest.mark.parametrize("module", sorted(FINANCIAL_IMPORT_MODULES))
def test_legacy_wizard_financial_apply_fails_closed(module):
    with pytest.raises(ValueError, match="durable dry run"):
        web_system_import_wizard.execute_import(
            object(),
            module=module,
            data_format="json",
            raw_text="[]",
            source_name="architecture-test.json",
            dry_run=False,
        )
