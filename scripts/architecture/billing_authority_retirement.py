"""Measure every displaced Sub operational-receivables authority path.

The report is intentionally two-dimensional: an exact count catches growth and
unreviewed retirement, while a digest of stable path/function/symbol identities
catches one-for-one substitution. Line numbers are excluded so formatting does
not create fake drift.
"""

from __future__ import annotations

import ast
import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class RetirementCategory(StrEnum):
    INVOICE_CREDIT_AUTHORITY = "invoice_credit_authority"
    PAYMENT_SETTLEMENT_AUTHORITY = "payment_settlement_authority"
    ALLOCATION_AUTHORITY = "allocation_authority"
    DIRECT_BALANCE_ASSIGNMENT = "direct_balance_assignment"
    TAX_FX_DECISION = "tax_fx_decision"
    PROVIDER_OR_JOB_MONEY_MUTATION = "provider_or_job_money_mutation"


@dataclass(frozen=True, slots=True, order=True)
class RetirementSite:
    category: RetirementCategory
    path: str
    scope: str
    node_kind: str
    symbol: str

    def stable_identity(self) -> str:
        return ":".join((self.path, self.scope, self.node_kind, self.symbol))


@dataclass(frozen=True, slots=True)
class CategoryMeasurement:
    count: int
    sites_sha256: str


@dataclass(frozen=True, slots=True)
class RetirementReport:
    categories: dict[RetirementCategory, CategoryMeasurement]

    def to_json(self) -> str:
        payload = {
            "version": 1,
            "categories": {
                category.value: {
                    "count": measurement.count,
                    "sites_sha256": measurement.sites_sha256,
                }
                for category, measurement in sorted(
                    self.categories.items(), key=lambda item: item[0].value
                )
            },
        }
        return json.dumps(payload, indent=2, sort_keys=True) + "\n"


_MODULE_CATEGORIES: tuple[tuple[str, RetirementCategory], ...] = (
    ("app.services.billing.invoices", RetirementCategory.INVOICE_CREDIT_AUTHORITY),
    ("app.services.billing.credit_notes", RetirementCategory.INVOICE_CREDIT_AUTHORITY),
    ("app.services.invoice_", RetirementCategory.INVOICE_CREDIT_AUTHORITY),
    ("app.services.billing.payments", RetirementCategory.PAYMENT_SETTLEMENT_AUTHORITY),
    ("app.services.payment_", RetirementCategory.PAYMENT_SETTLEMENT_AUTHORITY),
    (
        "app.services.billing.consolidated_payments",
        RetirementCategory.ALLOCATION_AUTHORITY,
    ),
    ("app.services.billing.account_credit", RetirementCategory.ALLOCATION_AUTHORITY),
    (
        "app.services.billing.reconcile_unposted",
        RetirementCategory.ALLOCATION_AUTHORITY,
    ),
    ("app.services.billing.tax", RetirementCategory.TAX_FX_DECISION),
    ("app.services.tax_accounting", RetirementCategory.TAX_FX_DECISION),
)

_MODEL_NAMES: dict[str, RetirementCategory] = {
    "Invoice": RetirementCategory.INVOICE_CREDIT_AUTHORITY,
    "InvoiceLine": RetirementCategory.INVOICE_CREDIT_AUTHORITY,
    "InvoiceStatus": RetirementCategory.INVOICE_CREDIT_AUTHORITY,
    "InvoiceClosure": RetirementCategory.INVOICE_CREDIT_AUTHORITY,
    "CreditNote": RetirementCategory.INVOICE_CREDIT_AUTHORITY,
    "CreditNoteLine": RetirementCategory.INVOICE_CREDIT_AUTHORITY,
    "CreditNoteApplication": RetirementCategory.INVOICE_CREDIT_AUTHORITY,
    "Payment": RetirementCategory.PAYMENT_SETTLEMENT_AUTHORITY,
    "PaymentStatus": RetirementCategory.PAYMENT_SETTLEMENT_AUTHORITY,
    "PaymentSettlement": RetirementCategory.PAYMENT_SETTLEMENT_AUTHORITY,
    "PaymentRefund": RetirementCategory.PAYMENT_SETTLEMENT_AUTHORITY,
    "PaymentReversal": RetirementCategory.PAYMENT_SETTLEMENT_AUTHORITY,
    "PaymentAllocation": RetirementCategory.ALLOCATION_AUTHORITY,
    "BillingAccountCreditAllocation": RetirementCategory.ALLOCATION_AUTHORITY,
    "TaxRate": RetirementCategory.TAX_FX_DECISION,
    "TaxApplication": RetirementCategory.TAX_FX_DECISION,
}

_BALANCE_FIELDS = frozenset(
    {
        "balance",
        "balance_after",
        "balance_due",
        "amount_paid",
        "applied_total",
        "refunded_amount",
        "prepaid_funding_before",
        "prepaid_funding_after",
    }
)
_TAX_FX_FIELDS = frozenset(
    {
        "tax_total",
        "tax_rate",
        "tax_rate_id",
        "tax_application",
        "fx_rate",
        "exchange_rate",
    }
)
_MONEY_MUTATION_METHODS = frozenset(
    {
        "allocate",
        "apply",
        "create",
        "issue",
        "mark_settled",
        "reallocate",
        "refund",
        "reverse",
        "settle",
        "update",
        "void",
        "write_off",
    }
)


def _category_for_module(module: str) -> RetirementCategory | None:
    for prefix, category in _MODULE_CATEGORIES:
        if module.startswith(prefix):
            return category
    return None


def _attribute_name(node: ast.expr) -> str | None:
    return node.attr if isinstance(node, ast.Attribute) else None


def _root_name(node: ast.expr) -> str | None:
    current = node
    while isinstance(current, ast.Attribute):
        current = current.value
    return current.id if isinstance(current, ast.Name) else None


class _Visitor(ast.NodeVisitor):
    def __init__(self, *, path: str) -> None:
        self.path = path
        self.scope: list[str] = ["<module>"]
        self.sites: list[RetirementSite] = []

    def _record(
        self, category: RetirementCategory, node_kind: str, symbol: str
    ) -> None:
        self.sites.append(
            RetirementSite(
                category=category,
                path=self.path,
                scope=self.scope[-1],
                node_kind=node_kind,
                symbol=symbol,
            )
        )

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        category = _category_for_module(module)
        if category is not None:
            for alias in node.names:
                self._record(category, "import", f"{module}.{alias.name}")
        if module == "app.models.billing":
            for alias in node.names:
                model_category = _MODEL_NAMES.get(alias.name)
                if model_category is not None:
                    self._record(
                        model_category, "model_import", f"{module}.{alias.name}"
                    )
        self.generic_visit(node)

    def _record_assignment_target(self, target: ast.expr) -> None:
        field = _attribute_name(target)
        if field in _BALANCE_FIELDS:
            self._record(
                RetirementCategory.DIRECT_BALANCE_ASSIGNMENT,
                "assignment",
                field,
            )
        if field in _TAX_FX_FIELDS:
            self._record(
                RetirementCategory.TAX_FX_DECISION,
                "assignment",
                field,
            )

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            self._record_assignment_target(target)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._record_assignment_target(node.target)
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self._record_assignment_target(node.target)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Attribute):
            method = node.func.attr
            root = _root_name(node.func.value)
            financial_root = root in {
                "CreditNotes",
                "Invoices",
                "Payments",
                "payment",
                "invoice",
                "credit_note",
            }
            transport_or_job = any(
                token in self.path
                for token in ("provider", "webhook", "tasks/", "scheduled", "scripts/")
            )
            if financial_root and method in _MONEY_MUTATION_METHODS:
                category = (
                    RetirementCategory.PROVIDER_OR_JOB_MONEY_MUTATION
                    if transport_or_job
                    else RetirementCategory.PAYMENT_SETTLEMENT_AUTHORITY
                    if root in {"Payments", "payment"}
                    else RetirementCategory.INVOICE_CREDIT_AUTHORITY
                )
                self._record(category, "money_call", f"{root}.{method}")
        self.generic_visit(node)


def authority_sites(project_root: Path) -> tuple[RetirementSite, ...]:
    roots = (project_root / "app", project_root / "scripts")
    sites: list[RetirementSite] = []
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.py")):
            relative = path.relative_to(project_root)
            if relative == Path("scripts/architecture/billing_authority_retirement.py"):
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(relative))
            visitor = _Visitor(path=str(relative))
            visitor.visit(tree)
            sites.extend(visitor.sites)
    return tuple(sorted(sites))


def measure_authority(project_root: Path) -> RetirementReport:
    sites = authority_sites(project_root)
    categories: dict[RetirementCategory, CategoryMeasurement] = {}
    for category in RetirementCategory:
        identities = [
            site.stable_identity() for site in sites if site.category is category
        ]
        digest = hashlib.sha256("\n".join(identities).encode()).hexdigest()
        categories[category] = CategoryMeasurement(
            count=len(identities), sites_sha256=digest
        )
    return RetirementReport(categories=categories)


__all__ = [
    "CategoryMeasurement",
    "RetirementCategory",
    "RetirementReport",
    "RetirementSite",
    "authority_sites",
    "measure_authority",
]
