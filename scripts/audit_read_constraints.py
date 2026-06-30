"""Audit: create-side field constraints leaking into response (*Read) models.

Bug class (see #560 / #272): FastAPI validates responses against their
`response_model`. When a `XxxRead` inherits a create-side numeric lower-bound
(`Field(ge=0)` / `gt=0`) from a shared `XxxBase`, and a stored row can violate
it (a signed amount, a zero), serialization raises ResponseValidationError ->
HTTP 500 for the whole page of a list endpoint.

This scans every class in `app/schemas`, resolves each `*Read` model's effective
field definitions across its MRO (most-derived wins), and flags served money
(`Decimal`) fields that still carry a `ge`/`gt` bound inherited from a `*Base`.

Exit status is non-zero when any MONEY/Decimal finding exists — use as a CI guard.
"""

from __future__ import annotations

import ast
import glob
import os
import re
import subprocess
import sys

NUMERIC_LOWER = {"ge", "gt"}
ALL_BOUNDS = {
    "ge", "gt", "le", "lt", "multiple_of", "max_digits",
    "max_length", "min_length", "pattern",
}
SCHEMA_DIR = "app/schemas"
MONEY_HINT = re.compile(
    r"amount|total|subtotal|tax|balance|rate|price|charge|fee|cost|"
    r"credit|debit|discount|gb",
    re.I,
)


def field_constraints(value: ast.expr) -> dict[str, str]:
    """Return the bound kwargs of a `Field(...)` assignment (empty if none)."""
    if not isinstance(value, ast.Call):
        return {}
    fn = value.func
    name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", "")
    if name != "Field":
        return {}
    return {
        kw.arg: ast.unparse(kw.value)
        for kw in value.keywords
        if kw.arg in ALL_BOUNDS
    }


def load_classes() -> dict[str, dict]:
    classes: dict[str, dict] = {}
    for path in sorted(glob.glob(os.path.join(SCHEMA_DIR, "*.py"))):
        tree = ast.parse(open(path).read(), filename=path)
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            fields: dict[str, dict] = {}
            for stmt in node.body:
                is_field = isinstance(stmt, ast.AnnAssign) and isinstance(
                    stmt.target, ast.Name
                )
                if is_field:
                    fields[stmt.target.id] = {
                        "constraints": field_constraints(stmt.value),
                        "annot": ast.unparse(stmt.annotation)
                        if stmt.annotation
                        else "",
                    }
            classes[node.name] = {
                "bases": [b.id for b in node.bases if isinstance(b, ast.Name)],
                "fields": fields,
                "file": os.path.basename(path),
            }
    return classes


def mro(classes: dict, cls: str, seen: set[str] | None = None) -> list[str]:
    seen = seen if seen is not None else set()
    if cls in seen or cls not in classes:
        return []
    seen.add(cls)
    order = [cls]
    for base in classes[cls]["bases"]:
        order += mro(classes, base, seen)
    return order


def effective_fields(classes: dict, cls: str) -> dict[str, dict]:
    eff: dict[str, dict] = {}
    for ancestor in mro(classes, cls):
        for name, meta in classes[ancestor]["fields"].items():
            if name not in eff:
                eff[name] = {**meta, "defined_in": ancestor}
    return eff


def served_models() -> set[str]:
    plain = subprocess.run(
        ["grep", "-rho", "response_model=[A-Za-z0-9_]*", "app/api"],
        capture_output=True,
        text=True,
    ).stdout
    listed = subprocess.run(
        ["grep", "-rho", r"response_model=ListResponse\[[A-Za-z0-9_]*", "app/api"],
        capture_output=True,
        text=True,
    ).stdout
    names = set(re.findall(r"response_model=([A-Za-z0-9_]+)", plain))
    names |= set(re.findall(r"ListResponse\[([A-Za-z0-9_]+)", listed))
    return names


def main() -> int:
    classes = load_classes()
    served = served_models()

    money: list[dict] = []
    other: list[dict] = []
    config: list[dict] = []
    for cls, meta in classes.items():
        if not cls.endswith("Read"):
            continue
        for fname, fmeta in effective_fields(classes, cls).items():
            cons = fmeta["constraints"]
            if not (set(cons) & NUMERIC_LOWER):
                continue
            if fmeta["defined_in"] == cls:
                continue  # re-pinned in the Read model => intentional
            annot = fmeta["annot"]
            row = {
                "read": cls,
                "file": meta["file"],
                "field": fname,
                "annot": annot,
                "cons": cons,
                "from": fmeta["defined_in"],
                "served": cls in served,
            }
            if "Decimal" in annot and MONEY_HINT.search(fname):
                money.append(row)
            elif "Decimal" in annot:
                other.append(row)
            else:
                config.append(row)

    def show(title: str, items: list[dict]) -> None:
        print(f"\n===== {title} ({len(items)}) =====")
        for r in sorted(items, key=lambda x: (not x["served"], x["read"])):
            tag = "served" if r["served"] else "—"
            print(
                f"  [{tag:6}] {r['read']}.{r['field']}: {r['annot']}  "
                f"{r['cons']}  (from {r['from']}, {r['file']})"
            )

    print(
        f"Money fields with ge/gt on Read models: {len(money)} | "
        f"other Decimal: {len(other)} | config-int: {len(config)}"
    )
    show("MONEY / signed-amount fields on response models — #560-class risk", money)
    show("Other Decimal ge/gt on response models", other)
    print(
        f"\n(Config-int ge/gt flags suppressed: {len(config)} — app-controlled "
        "physical/enum values, not data-violatable.)"
    )
    return 1 if (money or other) else 0


if __name__ == "__main__":
    sys.exit(main())
