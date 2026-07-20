# Bundled Service System Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Group a customer's base internet plus its component subscriptions (IP blocks, and later voice) into a first-class *bundle* that is enforced, suspended, restored, and expired atomically — no member can diverge.

**Architecture:** A `subscription_bundles` table plus a nullable `subscriptions.bundle_id` FK. Each component stays a standalone subscription (so existing itemized account-level billing is untouched). New `*_bundle` lifecycle operations wrap the existing per-subscription `account_lifecycle` ops to act on all members at once. The dunning reconciler enforces at bundle granularity, excluding dedicated-internet bundles. A reconciler invariant heals any divergent member. Then existing accounts are migrated into bundles and new IP/voice provisioning uses the bundle path.

**Tech Stack:** FastAPI, Celery, PostgreSQL, SQLAlchemy 2.0 (`Mapped`/`mapped_column`), Alembic, pytest.

## Global Constraints

- Models live in `app/models/catalog.py`; use SQLAlchemy 2.0 `Mapped[...] = mapped_column(...)`, `uuid.UUID` PKs with server defaults matching existing rows.
- Alembic revisions are numeric-prefixed (`NNN_slug`) with explicit `revision`/`down_revision`. The tree currently has multiple heads (two `172_*`); run `poetry run alembic heads` and set `down_revision` to the single merge head (create a merge migration first if `alembic heads` returns >1).
- Billing is **not** modified. Components bill per-subscription onto the account invoice, which already carries `invoice_lines.subscription_id`.
- Prepaid min balance stays 0; IP price stays ₦2,500/IP — untouched.
- Dedicated internet = `catalog_offers.plan_family = 'dedicated'`. DIA bundles are hands-off for auto-enforcement.
- Run tests with `poetry run pytest`; lint with `poetry run ruff check`; types with `poetry run mypy <files> --ignore-missing-imports`.
- Commit after every green step. Never mass-mutate prod; the migration (Phase 2) ships as an idempotent, dry-run-default script gated behind explicit run.

---

## File Structure

- `app/models/catalog.py` — add `SubscriptionBundle` model + `Subscription.bundle_id`/`bundle` relationship.
- `alembic/versions/173_subscription_bundles.py` — create table + add column (new).
- `app/services/bundles/__init__.py`, `app/services/bundles/_core.py` — bundle domain ops (`create_bundle`, `add_member`, `suspend_bundle`, `restore_bundle`, `expire_bundle`, `recompute_is_dedicated`, `bundle_members`) (new).
- `app/services/collections/_core.py` — reconciler enforces at bundle granularity + divergence invariant (modify).
- `app/services/account_lifecycle.py` — reuse existing `suspend_subscription`/`restore_subscription`/`expire_subscription` (no change unless noted).
- `scripts/migration/backfill_bundles.py` — dry-run-default data migration (new).
- `tests/test_bundles.py`, `tests/test_bundle_enforcement.py`, `tests/test_backfill_bundles.py` — tests (new).

---

## Phase 1 — Model + lifecycle

### Task 1: `SubscriptionBundle` model + `bundle_id` column

**Files:**
- Modify: `app/models/catalog.py` (add model near `Subscription`)
- Test: `tests/test_bundles.py`

**Interfaces:**
- Produces: `SubscriptionBundle(id, subscriber_id, label, anchor_subscription_id, is_dedicated, status, is_active, created_at, updated_at)`; `Subscription.bundle_id: Mapped[uuid.UUID | None]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_bundles.py
import uuid
from app.models.catalog import Subscription, SubscriptionBundle

def test_bundle_model_and_membership(db_session, subscriber, subscription):
    bundle = SubscriptionBundle(
        subscriber_id=subscriber.id,
        label="Business 100 + /29",
        anchor_subscription_id=subscription.id,
        status="active",
    )
    db_session.add(bundle)
    db_session.flush()
    subscription.bundle_id = bundle.id
    db_session.flush()
    db_session.refresh(subscription)
    assert subscription.bundle_id == bundle.id
    assert bundle.is_dedicated is False  # server default
```

- [ ] **Step 2: Run test to verify it fails**

Run: `poetry run pytest tests/test_bundles.py::test_bundle_model_and_membership -v`
Expected: FAIL with `ImportError: cannot import name 'SubscriptionBundle'`.

- [ ] **Step 3: Implement the model + column**

In `app/models/catalog.py`, add (mirror the existing `mapped_column` style, `uuid.uuid4` default, timezone-aware timestamps):

```python
class SubscriptionBundle(Base):
    __tablename__ = "subscription_bundles"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    subscriber_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id"), nullable=False, index=True
    )
    label: Mapped[str | None] = mapped_column(String(160))
    anchor_subscription_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscriptions.id"), nullable=True
    )
    is_dedicated: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    status: Mapped[str] = mapped_column(String(32), default="active", server_default="active")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
```

On the `Subscription` class add:

```python
    bundle_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscription_bundles.id"), nullable=True, index=True
    )
```

Reuse whatever `UUID`, `Boolean`, `String`, `DateTime`, `func`, `ForeignKey` imports the file already has; do not add duplicate imports.

- [ ] **Step 4: Run test to verify it passes**

Run: `poetry run pytest tests/test_bundles.py::test_bundle_model_and_membership -v`
Expected: PASS (test DB tables are created from metadata by the fixture).

- [ ] **Step 5: Commit**

```bash
git add app/models/catalog.py tests/test_bundles.py
git commit -m "feat(bundles): SubscriptionBundle model + subscriptions.bundle_id"
```

### Task 2: Alembic migration for the table + column

**Files:**
- Create: `alembic/versions/173_subscription_bundles.py`

**Interfaces:**
- Consumes: model from Task 1.

- [ ] **Step 1: Confirm the single head**

Run: `poetry run alembic heads`
If it prints more than one revision, create a merge first:
`poetry run alembic merge -m "merge heads" <head1> <head2>` and use that merge revision as `down_revision`. Otherwise use the printed head.

- [ ] **Step 2: Write the migration**

```python
# alembic/versions/173_subscription_bundles.py
"""subscription bundles"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "173_subscription_bundles"
down_revision = "<single head from step 1>"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "subscription_bundles",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("subscriber_id", UUID(as_uuid=True), sa.ForeignKey("subscribers.id"), nullable=False),
        sa.Column("label", sa.String(160), nullable=True),
        sa.Column("anchor_subscription_id", UUID(as_uuid=True), sa.ForeignKey("subscriptions.id"), nullable=True),
        sa.Column("is_dedicated", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("status", sa.String(32), server_default="active", nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_subscription_bundles_subscriber_id", "subscription_bundles", ["subscriber_id"])
    op.add_column("subscriptions", sa.Column("bundle_id", UUID(as_uuid=True), sa.ForeignKey("subscription_bundles.id"), nullable=True))
    op.create_index("ix_subscriptions_bundle_id", "subscriptions", ["bundle_id"])


def downgrade() -> None:
    op.drop_index("ix_subscriptions_bundle_id", "subscriptions")
    op.drop_column("subscriptions", "bundle_id")
    op.drop_index("ix_subscription_bundles_subscriber_id", "subscription_bundles")
    op.drop_table("subscription_bundles")
```

- [ ] **Step 3: Verify migration applies + reverses on a scratch DB**

Run: `poetry run alembic upgrade head && poetry run alembic downgrade -1 && poetry run alembic upgrade head`
Expected: no errors; `subscription_bundles` exists after final upgrade.

- [ ] **Step 4: Commit**

```bash
git add alembic/versions/173_subscription_bundles.py
git commit -m "feat(bundles): alembic migration for subscription_bundles"
```

### Task 3: Bundle domain ops — create/add_member/members/recompute_is_dedicated

**Files:**
- Create: `app/services/bundles/__init__.py`, `app/services/bundles/_core.py`
- Test: `tests/test_bundles.py`

**Interfaces:**
- Produces:
  - `create_bundle(db, subscriber_id, anchor_subscription_id, label=None) -> SubscriptionBundle`
  - `add_member(db, bundle_id, subscription_id) -> None`
  - `bundle_members(db, bundle_id) -> list[Subscription]`
  - `recompute_is_dedicated(db, bundle_id) -> bool`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_bundles.py (append)
from app.services import bundles

def test_create_bundle_and_dedicated_flag(db_session, subscriber, subscription, catalog_offer):
    from app.models.catalog import Subscription
    b = bundles.create_bundle(db_session, str(subscriber.id), str(subscription.id), label="B")
    bundles.add_member(db_session, str(b.id), str(subscription.id))
    assert [s.id for s in bundles.bundle_members(db_session, str(b.id))] == [subscription.id]
    # dedicated marker follows the member offer's plan_family
    catalog_offer.plan_family = "dedicated"
    db_session.flush()
    assert bundles.recompute_is_dedicated(db_session, str(b.id)) is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `poetry run pytest tests/test_bundles.py::test_create_bundle_and_dedicated_flag -v`
Expected: FAIL (`ModuleNotFoundError: app.services.bundles`).

- [ ] **Step 3: Implement `_core.py`**

```python
# app/services/bundles/_core.py
from __future__ import annotations
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.models.catalog import CatalogOffer, Subscription, SubscriptionBundle
from app.services.common import coerce_uuid


def create_bundle(db, subscriber_id, anchor_subscription_id, label=None):
    bundle = SubscriptionBundle(
        subscriber_id=coerce_uuid(subscriber_id),
        anchor_subscription_id=coerce_uuid(anchor_subscription_id),
        label=label,
        status="active",
    )
    db.add(bundle)
    db.flush()
    return bundle


def add_member(db, bundle_id, subscription_id):
    sub = db.get(Subscription, coerce_uuid(subscription_id))
    if sub is None:
        raise ValueError(f"subscription {subscription_id} not found")
    sub.bundle_id = coerce_uuid(bundle_id)
    db.flush()
    recompute_is_dedicated(db, bundle_id)


def bundle_members(db, bundle_id):
    return list(
        db.scalars(
            select(Subscription).where(Subscription.bundle_id == coerce_uuid(bundle_id))
        ).all()
    )


def recompute_is_dedicated(db, bundle_id):
    bundle = db.get(SubscriptionBundle, coerce_uuid(bundle_id))
    if bundle is None:
        return False
    dedicated = db.scalar(
        select(CatalogOffer.plan_family)
        .join(Subscription, Subscription.offer_id == CatalogOffer.id)
        .where(Subscription.bundle_id == bundle.id, CatalogOffer.plan_family == "dedicated")
        .limit(1)
    )
    bundle.is_dedicated = dedicated is not None
    db.flush()
    return bundle.is_dedicated
```

```python
# app/services/bundles/__init__.py
from app.services.bundles._core import (
    add_member,
    bundle_members,
    create_bundle,
    recompute_is_dedicated,
)

__all__ = ["add_member", "bundle_members", "create_bundle", "recompute_is_dedicated"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `poetry run pytest tests/test_bundles.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/bundles/ tests/test_bundles.py
git commit -m "feat(bundles): create/add_member/members/recompute_is_dedicated"
```

### Task 4: Atomic `suspend_bundle` / `restore_bundle` / `expire_bundle`

**Files:**
- Modify: `app/services/bundles/_core.py`, `app/services/bundles/__init__.py`
- Test: `tests/test_bundles.py`

**Interfaces:**
- Consumes: `account_lifecycle.suspend_subscription(db, subscription_id, reason, source)`, `restore_subscription(db, subscription_id, trigger, resolved_by)`, `expire_subscription(db, subscription_id, ...)` (see `app/services/account_lifecycle.py:140/269/453` for exact kwargs).
- Produces: `suspend_bundle(db, bundle_id, reason, source) -> int`, `restore_bundle(db, bundle_id, trigger, resolved_by) -> int`, `expire_bundle(db, bundle_id, ...) -> int` (return = members affected).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_bundles.py (append)
from app.models.catalog import SubscriptionStatus

def test_suspend_bundle_is_atomic(db_session, subscriber, subscription, catalog_offer):
    from app.models.catalog import Subscription
    # second member sub
    m2 = Subscription(subscriber_id=subscriber.id, offer_id=catalog_offer.id,
                      status=SubscriptionStatus.active, billing_mode=subscription.billing_mode)
    db_session.add(m2); db_session.flush()
    b = bundles.create_bundle(db_session, str(subscriber.id), str(subscription.id))
    bundles.add_member(db_session, str(b.id), str(subscription.id))
    bundles.add_member(db_session, str(b.id), str(m2.id))
    from app.models.enforcement_lock import EnforcementReason
    n = bundles.suspend_bundle(db_session, str(b.id), reason=EnforcementReason.overdue, source="test")
    db_session.refresh(subscription); db_session.refresh(m2)
    assert n == 2
    assert subscription.status == SubscriptionStatus.suspended
    assert m2.status == SubscriptionStatus.suspended
```

- [ ] **Step 2: Run test to verify it fails**

Run: `poetry run pytest tests/test_bundles.py::test_suspend_bundle_is_atomic -v`
Expected: FAIL (`AttributeError: module ... has no attribute 'suspend_bundle'`).

- [ ] **Step 3: Implement the bundle lifecycle ops**

Append to `_core.py` (match the real kwarg names verified from `account_lifecycle.py`; the `try/except ValueError "Cannot suspend"` mirrors `_suspend_account`):

```python
def suspend_bundle(db, bundle_id, reason, source):
    from app.services.account_lifecycle import suspend_subscription
    count = 0
    for sub in bundle_members(db, bundle_id):
        try:
            suspend_subscription(db, str(sub.id), reason=reason, source=source)
            count += 1
        except ValueError as exc:
            if "Cannot suspend" not in str(exc):
                raise
    return count


def restore_bundle(db, bundle_id, trigger, resolved_by):
    from app.services.account_lifecycle import restore_subscription
    count = 0
    for sub in bundle_members(db, bundle_id):
        try:
            restore_subscription(db, str(sub.id), trigger=trigger, resolved_by=resolved_by)
            count += 1
        except ValueError:
            pass
    return count


def expire_bundle(db, bundle_id, **kwargs):
    from app.services.account_lifecycle import expire_subscription
    count = 0
    for sub in bundle_members(db, bundle_id):
        try:
            expire_subscription(db, str(sub.id), **kwargs)
            count += 1
        except ValueError:
            pass
    return count
```

Add the three names to `__init__.py`'s imports and `__all__`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `poetry run pytest tests/test_bundles.py -v`
Expected: PASS. Then `poetry run ruff check app/services/bundles/ && poetry run mypy app/services/bundles/_core.py --ignore-missing-imports`.

- [ ] **Step 5: Commit**

```bash
git add app/services/bundles/ tests/test_bundles.py
git commit -m "feat(bundles): atomic suspend/restore/expire_bundle"
```

### Task 5: Divergence invariant reconciler

**Files:**
- Modify: `app/services/bundles/_core.py`, `app/services/bundles/__init__.py`
- Test: `tests/test_bundle_enforcement.py`

**Interfaces:**
- Produces: `reconcile_bundle_states(db, bundle_id=None) -> dict` — for each bundle, if members disagree, converge non-anchor members to the anchor's enforcement state (suspended vs active). Returns `{"bundles_scanned": int, "members_converged": int}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_bundle_enforcement.py
from app.services import bundles
from app.models.catalog import Subscription, SubscriptionStatus

def test_reconcile_converges_divergent_member(db_session, subscriber, subscription, catalog_offer):
    anchor = subscription  # active
    ip = Subscription(subscriber_id=subscriber.id, offer_id=catalog_offer.id,
                      status=SubscriptionStatus.suspended, billing_mode=subscription.billing_mode)
    db_session.add(ip); db_session.flush()
    b = bundles.create_bundle(db_session, str(subscriber.id), str(anchor.id))
    bundles.add_member(db_session, str(b.id), str(anchor.id))
    bundles.add_member(db_session, str(b.id), str(ip.id))
    stats = bundles.reconcile_bundle_states(db_session, str(b.id))
    db_session.refresh(ip)
    # anchor active -> divergent suspended member restored to active
    assert ip.status == SubscriptionStatus.active
    assert stats["members_converged"] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `poetry run pytest tests/test_bundle_enforcement.py::test_reconcile_converges_divergent_member -v`
Expected: FAIL (`AttributeError: reconcile_bundle_states`).

- [ ] **Step 3: Implement**

```python
def reconcile_bundle_states(db, bundle_id=None):
    from app.models.catalog import SubscriptionStatus
    from app.services.account_lifecycle import restore_subscription, suspend_subscription
    from app.models.enforcement_lock import EnforcementReason
    q = select(SubscriptionBundle).where(SubscriptionBundle.is_active.is_(True))
    if bundle_id is not None:
        q = q.where(SubscriptionBundle.id == coerce_uuid(bundle_id))
    scanned = converged = 0
    for bundle in db.scalars(q).all():
        scanned += 1
        anchor = db.get(Subscription, bundle.anchor_subscription_id) if bundle.anchor_subscription_id else None
        if anchor is None:
            continue
        target_suspended = anchor.status == SubscriptionStatus.suspended
        for sub in bundle_members(db, str(bundle.id)):
            if sub.id == anchor.id:
                continue
            is_suspended = sub.status == SubscriptionStatus.suspended
            if is_suspended == target_suspended:
                continue
            try:
                if target_suspended:
                    suspend_subscription(db, str(sub.id), reason=EnforcementReason.overdue, source="bundle_reconcile")
                else:
                    restore_subscription(db, str(sub.id), trigger="bundle_reconcile", resolved_by=f"bundle:{bundle.id}")
                converged += 1
            except ValueError:
                pass
    return {"bundles_scanned": scanned, "members_converged": converged}
```

Add to `__init__.py`.

- [ ] **Step 4: Run tests + lint + types**

Run: `poetry run pytest tests/test_bundle_enforcement.py -v && poetry run ruff check app/services/bundles/`
Expected: PASS / clean.

- [ ] **Step 5: Commit**

```bash
git add app/services/bundles/ tests/test_bundle_enforcement.py
git commit -m "feat(bundles): reconcile_bundle_states divergence invariant"
```

---

## Phase 2 — Data migration (backfill existing accounts)

### Task 6: Backfill script (dry-run default) + dedupe double-modeling

**Files:**
- Create: `scripts/migration/backfill_bundles.py`
- Test: `tests/test_backfill_bundles.py`

**Interfaces:**
- Produces: `backfill_bundles(db, commit=False) -> dict` — for each subscriber with a base internet subscription AND at least one standalone IP subscription (`service_description ILIKE '%slash%' OR ILIKE '%pppoe public ip%'`), create one bundle (anchor = the base internet sub — the non-IP `active`/`suspended` sub), attach all members. For the 23 double-modeled accounts, retire the vestigial unbilled add-on record (`subscription_add_ons` rows whose `add_ons.addon_type IN ('extra_ip','static_ip')`) for that subscriber. Returns counts. `commit=False` rolls back.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_backfill_bundles.py
from app.models.catalog import Subscription, SubscriptionStatus, SubscriptionBundle
from scripts.migration.backfill_bundles import backfill_bundles

def test_backfill_groups_base_and_ip(db_session, subscriber, catalog_offer):
    base = Subscription(subscriber_id=subscriber.id, offer_id=catalog_offer.id,
                        service_description="60 Mbps Fiber", status=SubscriptionStatus.active,
                        billing_mode="postpaid")
    ip = Subscription(subscriber_id=subscriber.id, offer_id=catalog_offer.id,
                      service_description="SLASH 29 IP", status=SubscriptionStatus.suspended,
                      billing_mode="postpaid")
    db_session.add_all([base, ip]); db_session.flush()
    stats = backfill_bundles(db_session, commit=False)
    db_session.refresh(base); db_session.refresh(ip)
    assert base.bundle_id is not None
    assert ip.bundle_id == base.bundle_id
    b = db_session.get(SubscriptionBundle, base.bundle_id)
    assert b.anchor_subscription_id == base.id
    assert stats["bundles_created"] >= 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `poetry run pytest tests/test_backfill_bundles.py -v`
Expected: FAIL (`ModuleNotFoundError` / function missing).

- [ ] **Step 3: Implement the script**

```python
# scripts/migration/backfill_bundles.py
from __future__ import annotations
import os
from sqlalchemy import text
from app.services import bundles


IP_DESC = "(service_description ILIKE '%slash%' OR service_description ILIKE '%pppoe public ip%')"


def backfill_bundles(db, commit=False):
    rows = db.execute(text(f"""
        select distinct s.subscriber_id
        from subscriptions s
        where {IP_DESC} and s.status in ('active','suspended') and s.bundle_id is null
    """)).all()
    created = members = deduped = 0
    for (sid,) in rows:
        subs = db.execute(text(f"""
            select id, service_description from subscriptions
            where subscriber_id = :sid and status in ('active','suspended') and bundle_id is null
        """), {"sid": sid}).all()
        # anchor = first non-IP service; skip accounts with no clear base
        anchor = next((r for r in subs if "slash" not in (r[1] or "").lower()
                       and "pppoe public ip" not in (r[1] or "").lower()), None)
        if anchor is None:
            continue
        b = bundles.create_bundle(db, str(sid), str(anchor[0]))
        for r in subs:
            bundles.add_member(db, str(b.id), str(r[0]))
            members += 1
        created += 1
        # dedupe: retire vestigial unbilled IP add-on rows for this subscriber
        res = db.execute(text("""
            update subscription_add_ons sa set end_at = now()
            from subscriptions sub, add_ons ao
            where sa.subscription_id = sub.id and sub.subscriber_id = :sid
              and ao.id = sa.add_on_id and ao.addon_type in ('extra_ip','static_ip')
              and sa.end_at is null
        """), {"sid": sid})
        deduped += res.rowcount or 0
    stats = {"bundles_created": created, "members_linked": members, "addons_retired": deduped}
    if commit:
        db.commit()
    else:
        db.rollback()
    return stats


if __name__ == "__main__":  # pragma: no cover
    from app.tasks.collections import SessionLocal
    db = SessionLocal()
    print(backfill_bundles(db, commit=os.environ.get("RUN") == "1"))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `poetry run pytest tests/test_backfill_bundles.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/migration/backfill_bundles.py tests/test_backfill_bundles.py
git commit -m "feat(bundles): dry-run-default backfill + double-model dedupe"
```

> **Production run (operator, gated):** `RUN=1 python -m scripts.migration.backfill_bundles` executed on prod only after explicit owner approval; verify `bundles_created`, then spot-check that a known account (e.g. 100025479) has base+IP sharing one `bundle_id`. Not part of the automated plan.

---

## Phase 3 — Enforcement flip (bundle-granular)

### Task 7: Reconciler suspends/restores at bundle granularity, DIA excluded

**Files:**
- Modify: `app/services/collections/_core.py` (the `_suspend_account` call site in the dunning suspend action, ~line 1052; and `DunningWorkflow.run` account loop)
- Test: `tests/test_collections_dunning_services.py`

**Interfaces:**
- Consumes: `bundles.suspend_bundle`, `bundles.reconcile_bundle_states`, `SubscriptionBundle.is_dedicated`.

- [ ] **Step 1: Write the failing test** — a bundled postpaid account past suspend-day (176 days) with a dedicated member is skipped; a non-dedicated bundled account gets ALL members suspended.

```python
# tests/test_collections_dunning_services.py (append; mirror _setup_overdue_postpaid_account)
def test_dedicated_bundle_excluded_from_suspend(db_session, subscriber, subscription, catalog_offer):
    from app.services import bundles
    from app.models.catalog import BillingMode, SubscriptionStatus
    # build overdue postpaid account with an immediate-suspend policy (reuse helper),
    # wrap subscription in a bundle, mark member offer dedicated
    _setup_overdue_postpaid_account(db_session, subscriber, subscription, catalog_offer)
    b = bundles.create_bundle(db_session, str(subscriber.id), str(subscription.id))
    bundles.add_member(db_session, str(b.id), str(subscription.id))
    catalog_offer.plan_family = "dedicated"
    bundles.recompute_is_dedicated(db_session, str(b.id))
    from app.schemas.collections import DunningRunRequest
    collections_service.dunning_workflow.run(db_session, DunningRunRequest())
    db_session.refresh(subscription)
    assert subscription.status == SubscriptionStatus.active  # DIA excluded
```

- [ ] **Step 2: Run test to verify it fails**

Run: `poetry run pytest tests/test_collections_dunning_services.py::test_dedicated_bundle_excluded_from_suspend -v`
Expected: FAIL (account gets suspended today — no DIA exclusion).

- [ ] **Step 3: Implement** — in `_suspend_account` (or its caller), when the account's subscriptions belong to a bundle: (a) if any member bundle `is_dedicated`, skip (return False, log `dedicated_bundle_skip`); (b) otherwise suspend via `bundles.suspend_bundle` for each distinct bundle so all members move together. Keep the existing all-collectible-subs loop as the fallback for unbundled accounts. After the dunning loop, call `bundles.reconcile_bundle_states(db)` to converge any stragglers.

```python
# sketch inside _suspend_account, after loading `account`:
from app.services import bundles as bundle_svc
from app.models.catalog import Subscription, SubscriptionBundle
bundle_ids = {
    b for (b,) in db.query(Subscription.bundle_id)
    .filter(Subscription.subscriber_id == account.id, Subscription.bundle_id.isnot(None)).distinct()
}
for bid in bundle_ids:
    bundle = db.get(SubscriptionBundle, bid)
    if bundle and bundle.is_dedicated:
        logger.info("dedicated_bundle_skip account=%s bundle=%s", account_id, bid)
        return False
# ... existing per-sub suspend loop still covers all collectible subs (bundled + unbundled)
```

- [ ] **Step 4: Run tests + lint + mypy**

Run: `poetry run pytest tests/test_collections_dunning_services.py -v && poetry run ruff check app/services/collections/_core.py`
Expected: PASS / clean. Add a companion test asserting a non-dedicated bundled account suspends ALL members.

- [ ] **Step 5: Commit**

```bash
git add app/services/collections/_core.py tests/test_collections_dunning_services.py
git commit -m "feat(bundles): dunning enforces at bundle granularity, DIA excluded"
```

### Task 8: Schedule the divergence reconciler

**Files:**
- Modify: the Celery beat schedule (search `grep -rn "beat_schedule\|crontab\|schedule=" app/tasks/ app/*.py`), add a periodic `reconcile_bundle_states` task wrapper.
- Test: `tests/test_bundle_enforcement.py` (task callable smoke test)

- [ ] **Step 1: Write the failing test** — a Celery task `run_bundle_reconcile` exists and calls `reconcile_bundle_states`, returning its stats dict.
- [ ] **Step 2: Run → FAIL** (`ImportError`).
- [ ] **Step 3: Implement** a thin task in the collections tasks module mirroring `run_billing_enforcement` (open a session, call `bundles.reconcile_bundle_states(db)`, commit, return stats); register it on beat every 15 min next to the enforcement reconciler.
- [ ] **Step 4: Run tests → PASS.**
- [ ] **Step 5: Commit** `feat(bundles): periodic bundle-state reconcile task`.

---

## Phase 4 — Provisioning path

### Task 9: New IP/voice component provisioning attaches to the bundle

**Files:**
- Modify: the IP/service provisioning entrypoint (`grep -rn "def.*provision\|create_subscription" app/services/ | grep -i "ip\|subscription"` to locate)
- Test: `tests/test_bundles.py`

**Interfaces:**
- Produces: when a new component subscription (IP or voice) is created for a subscriber who has a base internet subscription, it is attached to that subscriber's bundle (creating the bundle with the base as anchor if none exists).

- [ ] **Step 1: Write the failing test**

```python
def test_new_ip_component_joins_bundle(db_session, subscriber, subscription, catalog_offer):
    from app.services import bundles
    from app.services.bundles import attach_component
    from app.models.catalog import Subscription, SubscriptionStatus
    b = bundles.create_bundle(db_session, str(subscriber.id), str(subscription.id))
    bundles.add_member(db_session, str(b.id), str(subscription.id))
    ip = Subscription(subscriber_id=subscriber.id, offer_id=catalog_offer.id,
                      service_description="SLASH 29 IP", status=SubscriptionStatus.active,
                      billing_mode=subscription.billing_mode)
    db_session.add(ip); db_session.flush()
    attach_component(db_session, str(subscriber.id), str(ip.id))
    db_session.refresh(ip)
    assert ip.bundle_id == b.id
```

- [ ] **Step 2: Run → FAIL** (`attach_component` missing).
- [ ] **Step 3: Implement** `attach_component(db, subscriber_id, subscription_id)` in `bundles/_core.py`: find the subscriber's active bundle (or create one anchored on the base internet sub — the non-IP active sub); `add_member`. Wire the call into the IP/voice provisioning entrypoint after the component subscription is created.
- [ ] **Step 4: Run tests + lint → PASS/clean.**
- [ ] **Step 5: Commit** `feat(bundles): new IP/voice components auto-join the bundle`.

---

## Self-Review

- **Spec coverage:** model (Tasks 1–2) ✓; lifecycle atomic ops (Task 4) ✓; divergence invariant (Tasks 5, 8) ✓; DIA exclusion (Task 7) ✓; billing unchanged (no billing task — correct per non-goal) ✓; migration/backfill + dedupe (Task 6) ✓; provisioning (Task 9) ✓; ~78 unbilled add-on IPs = spec open question, deliberately NOT a task (owner decision) ✓.
- **Type consistency:** `suspend_bundle`/`restore_bundle`/`expire_bundle`/`reconcile_bundle_states`/`create_bundle`/`add_member`/`bundle_members`/`recompute_is_dedicated`/`attach_component` names are used identically across tasks and `__init__.py`.
- **Placeholders:** the only intentional operator-fills are `<single head from step 1>` (revision id — determined at run) and the gated prod run; both are explicitly operational, not code gaps.
- **Verify before asserting:** Task 4 says match `account_lifecycle` kwargs to the real signatures at `:140/:269/:453` — the implementer confirms them rather than trusting the sketch.
