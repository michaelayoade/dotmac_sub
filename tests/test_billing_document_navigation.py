"""Billing inventory opens the exact document without duplicate action columns."""

import ast
from pathlib import Path

from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader

ROOT = Path(__file__).resolve().parents[1]


def test_every_document_facet_has_its_own_detail_destination() -> None:
    module = ast.parse(
        (ROOT / "app/services/web_billing_documents.py").read_text(encoding="utf-8")
    )
    declaration = next(
        node
        for node in module.body
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "_DETAIL_BASE"
    )
    assert ast.literal_eval(declaration.value) == {
        "invoices": "/admin/billing/invoices/",
        "payments": "/admin/billing/payments/",
        "credit_notes": "/admin/billing/credits/",
    }


def test_document_identity_link_requires_destination_permission() -> None:
    env = Environment(
        loader=ChoiceLoader(
            [
                DictLoader({"layouts/admin.html": "{% block content %}{% endblock %}"}),
                FileSystemLoader(ROOT / "templates"),
            ]
        ),
        autoescape=True,
    )
    template = env.get_template("admin/billing/documents.html")
    for facet, base, permission in (
        ("invoices", "/admin/billing/invoices/", "billing:invoice:read"),
        ("payments", "/admin/billing/payments/", "billing:payment:read"),
        ("credit_notes", "/admin/billing/credits/", "billing:credit_note:read"),
    ):
        context = {
            "facet": facet,
            "facet_label": facet,
            "facets": [],
            "row_count": 1,
            "rows": [{"id": "document-42", "number": "DOC-42"}],
            "columns": [("Number", "number")],
            "detail_base": base,
            "request": None,
        }
        env.globals["can"] = lambda request, key: False
        denied = template.render(**context)
        assert f'href="{base}document-42"' not in denied
        assert "DOC-42" in denied
        env.globals["can"] = lambda request, key, expected=permission: key == expected
        allowed = template.render(**context)
        assert f'href="{base}document-42"' in allowed
        assert 'data-label="Number"' in allowed
