# Audit: create-side constraints leaking into response (read) models

**Date:** 2026-06-30 · **Scope:** `app/schemas/*.py` (52 files, 213 `*Read` models) · **Trigger:** [#560](https://github.com/michaelayoade/dotmac_sub/pull/560)

## The bug class

FastAPI validates **responses** against their declared `response_model`. When a
`XxxRead` model inherits a *create-side* numeric constraint (e.g. `Field(ge=0)`)
from a shared `XxxBase`, and the stored row can legitimately violate it (a
negative/signed amount, a zero, a value at the bound), Pydantic raises
`ResponseValidationError` → **HTTP 500 for the entire page** of a list endpoint.

This is exactly what took down `/api/v1/invoices` and `/api/v1/credit-notes`
(#560): PR #272 removed `ge=0` from `InvoiceRead`/`InvoiceLineRead` but the same
constraint still reached the response via nested models it didn't touch.

## Method

1. **Static (AST):** parse every class in `app/schemas`, resolve each `*Read`
   model's *effective* field definitions across its MRO (most-derived wins), and
   flag fields whose effective definition still carries a `ge`/`gt` (lower-bound)
   constraint inherited from a `*Base` — i.e. **not** re-pinned in the Read model.
2. **Classify:** separate signed **money** fields (`Decimal`, names like
   amount/total/rate/price/charge) from **config integers** (port/vlan/slot/mtu…).
   Only the former are violatable by real data.
3. **Cross-reference:** keep only models actually served via `response_model` /
   `ListResponse[...]` in `app/api`.
4. **Confirm live:** probe each suspect endpoint read-only against production
   (`selfcare.dotmac.io`).

The AST scanner is checked in at `scripts/audit_read_constraints.py` and is
suitable as a CI guard (see *Prevent recurrence*).

## Findings

| Endpoint | Read model · field(s) | Constraint | Status |
|---|---|---|---|
| `GET /api/v1/invoices` | `PaymentAllocationRead.amount` (nested) | `ge=0` | ✅ **Fixed** (#560) |
| `GET /api/v1/credit-notes` | `CreditNoteRead.{subtotal,tax_total,total}`, `CreditNoteLineRead.{unit_price,amount}`, `CreditNoteApplicationRead.amount` | `ge=0` | ✅ **Fixed** (#560) |
| `GET /api/v1/usage-charges` | `UsageChargeRead.{total_gb,unit_price,amount,included_gb,billable_gb}` | `ge=0` | 🔴 **LIVE 500 now** (6/6 probes) |
| `GET /api/v1/ledger-entries` | `LedgerEntryRead.amount` | `ge=0` | ⚠️ **Latent** — 200 today, but ledger amounts are inherently signed (debits/credits/reversals); 500s on the first negative row |
| `GET /api/v1/payments` | `PaymentRead.amount` | `gt=0, lt=1e10` | ⚠️ **Latent** — a zero/refund/reversal payment or an amount ≥ ₦10bn → 500 |
| `GET /api/v1/tax-rates` | `TaxRateRead.rate` | `ge=0, lt=100` | ⚠️ **Latent** — a tax rate of exactly 100% (or more) → 500 |
| `GET /api/v1/invoices`, `/credit-notes` | `InvoiceLineRead.quantity`, `CreditNoteLineRead.quantity` | `gt=0` | ⚠️ **Latent** — a zero-quantity adjustment/true-up line → 500. **Note:** #560 fixed the *amount* fields but not `quantity`. |

**Excluded (37 config-int flags):** physical/protocol integers — `port_number`,
`slot_number`, `shelf_number`, `c_vlan`/`s_vlan` (1–4094), `mtu` (576–9216),
`prefix_length` (0–128), `version_number`, `retry_count`, `delay_minutes`,
`expires_month` (1–12), etc. These are application-controlled on write and the
constraint mirrors physical reality, so stored data cannot violate them. Listed
here only so the exclusion is explicit; no action needed.

## Recommended fix

Same minimal pattern as #560 — in each affected `*Read` model, redeclare the
constrained field as a plain typed field so the read model reflects stored data:

```python
class LedgerEntryRead(LedgerEntryBase):
    model_config = ConfigDict(from_attributes=True)
    # Read model reflects stored data: ledger amounts are signed (debit/credit/
    # reversal). Don't inherit the create-side ge=0 (it 500s serialization).
    amount: Decimal = Decimal("0.00")
    ...

class PaymentRead(PaymentBase):
    amount: Decimal = Decimal("0.00")        # allow zero/refund/large
    ...

class TaxRateRead(TaxRateBase):
    rate: Decimal = Decimal("0.00")          # allow 100%+
    ...

class UsageChargeRead(UsageChargeBase):       # <-- fixes the live 500
    total_gb: Decimal = Decimal("0.00")
    included_gb: Decimal = Decimal("0.00")
    billable_gb: Decimal = Decimal("0.00")
    unit_price: Decimal = Decimal("0.00")
    amount: Decimal = Decimal("0.00")
    ...

class InvoiceLineRead(InvoiceLineBase):
    quantity: Decimal = Decimal("0.000")     # allow zero-qty adjustment lines
    ...
class CreditNoteLineRead(CreditNoteLineBase):
    quantity: Decimal = Decimal("0.000")
    ...
```

Create/Update schemas keep their bounds, so **input validation is unchanged**.

**Priority:** `/usage-charges` first (actively 500ing in production). The rest
are latent — fix before the first signed/edge row lands, not after.

## Prevent recurrence (systemic)

The override approach is whack-a-mole. Two durable options:

1. **Move bounds off the shared `Base`.** Put `ge=0`/`gt=0`/`lt=…` on the
   `*Create`/`*Update` models only; leave `*Base` (which `*Read` inherits)
   unconstrained. Read models then *cannot* inherit a create-side bound.
2. **CI guard.** Run `scripts/audit_read_constraints.py` in CI and fail on any
   **money** (`Decimal`) field with a `ge`/`gt` bound reachable from a served
   `*Read` model. Pair with a parametrized regression test that round-trips each
   such read model through `model_validate` with a negative/edge value (the
   pattern already in `tests/test_invoice_read_negative_lines.py`).

## Verification

- Static scan + classification: `scripts/audit_read_constraints.py` (6 money
  true-positives, 37 config-int excluded).
- Live probes (read-only, prod): `/payments` `/ledger-entries` `/tax-rates` →
  200; `/usage-charges` → 500 (6/6); `/invoices` `/credit-notes` → 500 (pre-#560).
- Mechanism + fix proven for #560 in `tests/test_invoice_read_negative_lines.py`
  (red→green against the real schema classes).
