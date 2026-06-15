"""Seed rich, edge-case fixtures into a TEST database (e.g. dotmac_test).

Idempotent: safe to re-run. Reuses the RBAC seed data from seed_rbac.py.

Run inside the app image against the test DB, e.g.:

    docker run --rm --network dotmac_sub_default --env-file .env \
      -e DATABASE_URL=postgresql+psycopg://dotmac_app:...@postgres-local:5432/dotmac_test \
      -e REDIS_URL=redis://:PW@redis-local:6379/5 \
      -e SESSION_REDIS_URL=redis://:PW@redis-local:6379/5 \
      -e APP_ENV=development \
      -v /root/dotmac_sub/app:/app/app -v /root/dotmac_sub/scripts:/app/scripts \
      --entrypoint python dotmac_sub-app -m scripts.seed.seed_test_fixtures

This script NEVER targets production: it requires the DB name to contain "test".
"""

from __future__ import annotations

import importlib.util
import os
import sys
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Safety guard: refuse to run against anything that isn't an obvious test DB.
# ---------------------------------------------------------------------------
load_dotenv(override=False)
_DB_URL = os.getenv("DATABASE_URL", "")
_DB_NAME = _DB_URL.rsplit("/", 1)[-1].split("?")[0]
if "test" not in _DB_NAME.lower() and os.getenv("ALLOW_NON_TEST_DB") != "1":
    raise SystemExit(
        f"Refusing to seed: DATABASE_URL db name {_DB_NAME!r} does not contain "
        "'test'. Point at dotmac_test (or set ALLOW_NON_TEST_DB=1 to override)."
    )

from app.db import SessionLocal  # noqa: E402
from app.models.auth import AuthProvider, UserCredential  # noqa: E402
from app.models.billing import (  # noqa: E402
    Invoice,
    InvoiceStatus,
    Payment,
    PaymentStatus,
)
from app.models.catalog import (  # noqa: E402
    AccessType,
    BillingCycle,
    BillingMode,
    CatalogOffer,
    ContractTerm,
    OfferStatus,
    PriceBasis,
    ServiceType,
    Subscription,
    SubscriptionStatus,
)
from app.models.rbac import Permission, Role, SystemUserRole  # noqa: E402
from app.models.subscriber import (  # noqa: E402
    Reseller,
    ResellerUser,
    Subscriber,
    SubscriberStatus,
    UserType,
)
from app.models.system_user import SystemUser  # noqa: E402
from app.services.auth_flow import hash_password  # noqa: E402

# ---------------------------------------------------------------------------
# Reuse the RBAC role/permission catalogue from seed_rbac.py (no package init
# in scripts/seed, so load it by file path).
# ---------------------------------------------------------------------------
_RBAC_PATH = Path(__file__).with_name("seed_rbac.py")
_spec = importlib.util.spec_from_file_location("_seed_rbac", _RBAC_PATH)
assert _spec and _spec.loader
seed_rbac = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(seed_rbac)


# Shared known password for every seeded principal (test data only).
DEFAULT_PASSWORD = "TestPass123!"


def banner(msg: str) -> None:
    print(f"\n=== {msg} ===", flush=True)


# ---------------------------------------------------------------------------
# RBAC
# ---------------------------------------------------------------------------
def seed_rbac_catalogue(db) -> dict[str, Role]:
    banner("RBAC roles & permissions")
    for name, description in seed_rbac.DEFAULT_ROLES:
        seed_rbac._ensure_role(db, name, description)
    for key, description in seed_rbac.DEFAULT_PERMISSIONS:
        seed_rbac._ensure_permission(db, key, description)
    db.commit()

    roles = {r.name: r for r in db.query(Role).all()}
    perms = {p.key: p for p in db.query(Permission).all()}
    for role_name, keys in seed_rbac.ROLE_PERMISSIONS.items():
        role = roles.get(role_name)
        if not role:
            continue
        for key in keys:
            perm = perms.get(key)
            if perm:
                seed_rbac._ensure_role_permission(db, role.id, perm.id)
    db.commit()
    print(f"  roles: {', '.join(sorted(roles))}")
    return roles


# ---------------------------------------------------------------------------
# Resellers
# ---------------------------------------------------------------------------
def get_or_create_house(db) -> Reseller:
    house = db.query(Reseller).filter(Reseller.is_house.is_(True)).first()
    if house:
        return house
    house = db.query(Reseller).filter(Reseller.code == "HOUSE").first()
    if house:
        return house
    house = Reseller(name="House", code="HOUSE", is_active=True)
    if hasattr(house, "is_house"):
        house.is_house = True
    db.add(house)
    db.flush()
    return house


def get_or_create_reseller(db, *, name: str, code: str, email: str) -> Reseller:
    r = db.query(Reseller).filter(Reseller.code == code).first()
    if r:
        return r
    r = Reseller(name=name, code=code, contact_email=email, is_active=True)
    db.add(r)
    db.flush()
    return r


# ---------------------------------------------------------------------------
# System users (admin/staff)
# ---------------------------------------------------------------------------
def get_or_create_system_user(
    db, *, email: str, first: str, last: str, role: Role, password: str
) -> SystemUser:
    su = db.query(SystemUser).filter(SystemUser.email == email).first()
    if not su:
        su = SystemUser(
            first_name=first,
            last_name=last,
            display_name=f"{first} {last}",
            email=email,
            user_type=UserType.system_user,
            is_active=True,
        )
        db.add(su)
        db.flush()
    # role link
    link = (
        db.query(SystemUserRole)
        .filter(
            SystemUserRole.system_user_id == su.id,
            SystemUserRole.role_id == role.id,
        )
        .first()
    )
    if not link:
        db.add(SystemUserRole(system_user_id=su.id, role_id=role.id))
    # credential
    cred = (
        db.query(UserCredential)
        .filter(UserCredential.system_user_id == su.id)
        .filter(UserCredential.provider == AuthProvider.local)
        .first()
    )
    if not cred:
        db.add(
            UserCredential(
                system_user_id=su.id,
                provider=AuthProvider.local,
                username=email,
                password_hash=hash_password(password),
                must_change_password=False,
                is_active=True,
            )
        )
    else:
        cred.username = email
        cred.password_hash = hash_password(password)
        cred.must_change_password = False
        cred.is_active = True
    db.flush()
    return su


# ---------------------------------------------------------------------------
# Catalog offers
# ---------------------------------------------------------------------------
def get_or_create_offer(db, *, name: str, **kwargs) -> CatalogOffer:
    offer = db.query(CatalogOffer).filter(CatalogOffer.name == name).first()
    if offer:
        return offer
    offer = CatalogOffer(
        name=name,
        service_type=kwargs.get("service_type", ServiceType.residential),
        access_type=kwargs.get("access_type", AccessType.fiber),
        price_basis=kwargs.get("price_basis", PriceBasis.flat),
        billing_cycle=kwargs.get("billing_cycle", BillingCycle.monthly),
        billing_mode=kwargs.get("billing_mode", BillingMode.prepaid),
        contract_term=kwargs.get("contract_term", ContractTerm.month_to_month),
        status=kwargs.get("status", OfferStatus.active),
        is_active=kwargs.get("is_active", True),
        speed_download_mbps=kwargs.get("speed_download_mbps"),
        speed_upload_mbps=kwargs.get("speed_upload_mbps"),
        plan_family=kwargs.get("plan_family"),
        code=kwargs.get("code"),
    )
    db.add(offer)
    db.flush()
    return offer


# ---------------------------------------------------------------------------
# Subscribers / customers
# ---------------------------------------------------------------------------
def get_or_create_subscriber(
    db,
    *,
    email: str,
    first: str,
    last: str,
    reseller: Reseller,
    status: SubscriberStatus,
    user_type: UserType = UserType.customer,
    password: str | None = None,
) -> Subscriber:
    sub = db.query(Subscriber).filter(Subscriber.email == email).first()
    if not sub:
        sub = Subscriber(
            first_name=first,
            last_name=last,
            email=email,
            reseller_id=reseller.id,
            status=status,
            user_type=user_type,
            is_active=status
            not in (SubscriberStatus.disabled, SubscriberStatus.canceled),
        )
        db.add(sub)
        db.flush()
    else:
        sub.status = status
        sub.reseller_id = reseller.id
        sub.user_type = user_type
    if password:
        cred = (
            db.query(UserCredential)
            .filter(UserCredential.subscriber_id == sub.id)
            .filter(UserCredential.provider == AuthProvider.local)
            .first()
        )
        if not cred:
            db.add(
                UserCredential(
                    subscriber_id=sub.id,
                    provider=AuthProvider.local,
                    username=email,
                    password_hash=hash_password(password),
                    must_change_password=False,
                    is_active=True,
                )
            )
        else:
            cred.username = email
            cred.password_hash = hash_password(password)
            cred.is_active = True
    db.flush()
    return sub


def ensure_subscription(
    db,
    *,
    subscriber: Subscriber,
    offer: CatalogOffer,
    status: SubscriptionStatus,
) -> Subscription:
    existing = (
        db.query(Subscription)
        .filter(Subscription.subscriber_id == subscriber.id)
        .first()
    )
    if existing:
        return existing
    sub = Subscription(
        subscriber_id=subscriber.id,
        offer_id=offer.id,
        status=status,
        billing_mode=offer.billing_mode,
        contract_term=offer.contract_term,
        start_at=datetime.now(UTC) - timedelta(days=30),
    )
    db.add(sub)
    db.flush()
    return sub


def make_invoice(
    db,
    *,
    subscriber: Subscriber,
    status: InvoiceStatus,
    total: Decimal,
    balance_due: Decimal,
    days_ago_issued: int = 20,
    days_to_due: int = -5,
) -> Invoice:
    issued = datetime.now(UTC) - timedelta(days=days_ago_issued)
    inv = Invoice(
        account_id=subscriber.id,
        invoice_number=f"INV-{uuid.uuid4().hex[:10].upper()}",
        status=status,
        currency="NGN",
        subtotal=total,
        tax_total=Decimal("0.00"),
        total=total,
        balance_due=balance_due,
        issued_at=issued,
        due_at=issued + timedelta(days=days_to_due + days_ago_issued),
        paid_at=datetime.now(UTC) if status == InvoiceStatus.paid else None,
        is_active=True,
    )
    db.add(inv)
    db.flush()
    return inv


def make_payment(
    db, *, subscriber: Subscriber, amount: Decimal, status: PaymentStatus
) -> Payment:
    pmt = Payment(
        account_id=subscriber.id,
        amount=amount,
        currency="NGN",
        status=status,
        paid_at=datetime.now(UTC) if status == PaymentStatus.succeeded else None,
        external_id=f"TRX-{uuid.uuid4().hex[:8].upper()}",
        is_active=True,
    )
    db.add(pmt)
    db.flush()
    return pmt


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def main() -> None:
    print(f"Seeding test fixtures into DB: {_DB_NAME}")
    db = SessionLocal()
    created: list[tuple[str, str, str]] = []  # (kind, login, url)
    try:
        roles = seed_rbac_catalogue(db)

        banner("Resellers")
        house = get_or_create_house(db)
        e2e_reseller = get_or_create_reseller(
            db,
            name="E2E Reseller",
            code="E2E-RESELLER",
            email="reseller@test.local",
        )
        db.commit()
        print(f"  house={house.code} e2e={e2e_reseller.code}")

        banner("System users (admin/staff)")
        get_or_create_system_user(
            db,
            email="admin@test.local",
            first="Admin",
            last="User",
            role=roles["admin"],
            password=DEFAULT_PASSWORD,
        )
        get_or_create_system_user(
            db,
            email="support@test.local",
            first="Support",
            last="Agent",
            role=roles["support"],
            password=DEFAULT_PASSWORD,
        )
        get_or_create_system_user(
            db,
            email="finance@test.local",
            first="Finance",
            last="Manager",
            role=roles["finance_manager"],
            password=DEFAULT_PASSWORD,
        )
        db.commit()
        created += [
            ("admin", "admin@test.local", "/auth/login"),
            ("support", "support@test.local", "/auth/login"),
            ("finance", "finance@test.local", "/auth/login"),
        ]

        banner("Catalog offers")
        offer_prepaid = get_or_create_offer(
            db,
            name="Prepaid Fibre 10/2",
            billing_mode=BillingMode.prepaid,
            contract_term=ContractTerm.month_to_month,
            plan_family="residential_prepaid",
            code="PRE-10-2",
            speed_download_mbps=10,
            speed_upload_mbps=2,
        )
        offer_postpaid = get_or_create_offer(
            db,
            name="Postpaid Fibre 20/5",
            billing_mode=BillingMode.postpaid,
            contract_term=ContractTerm.twelve_month,
            plan_family="residential_postpaid",
            code="POST-20-5",
            speed_download_mbps=20,
            speed_upload_mbps=5,
        )
        get_or_create_offer(
            db,
            name="Archived Legacy 5/1",
            status=OfferStatus.archived,
            is_active=False,
            code="ARC-5-1",
            speed_download_mbps=5,
            speed_upload_mbps=1,
        )
        get_or_create_offer(
            db,
            name="Inactive Draft 50/10",
            status=OfferStatus.inactive,
            is_active=False,
            code="INA-50-10",
            speed_download_mbps=50,
            speed_upload_mbps=10,
        )
        db.commit()
        print("  offers: prepaid, postpaid, archived, inactive")

        banner("Customers (edge states)")
        # 1. Active postpaid customer — paid invoice + succeeded payment (House)
        c_active = get_or_create_subscriber(
            db,
            email="active.customer@test.local",
            first="Ada",
            last="Active",
            reseller=house,
            status=SubscriberStatus.active,
            password=DEFAULT_PASSWORD,
        )
        ensure_subscription(
            db,
            subscriber=c_active,
            offer=offer_postpaid,
            status=SubscriptionStatus.active,
        )
        make_invoice(
            db,
            subscriber=c_active,
            status=InvoiceStatus.paid,
            total=Decimal("15000.00"),
            balance_due=Decimal("0.00"),
        )
        make_payment(
            db,
            subscriber=c_active,
            amount=Decimal("15000.00"),
            status=PaymentStatus.succeeded,
        )

        # 2. Overdue customer — unpaid overdue invoice (House) → dunning/arrangement
        c_overdue = get_or_create_subscriber(
            db,
            email="overdue.customer@test.local",
            first="Obi",
            last="Overdue",
            reseller=house,
            status=SubscriberStatus.delinquent,
            password=DEFAULT_PASSWORD,
        )
        ensure_subscription(
            db,
            subscriber=c_overdue,
            offer=offer_postpaid,
            status=SubscriptionStatus.active,
        )
        make_invoice(
            db,
            subscriber=c_overdue,
            status=InvoiceStatus.overdue,
            total=Decimal("15000.00"),
            balance_due=Decimal("15000.00"),
            days_ago_issued=45,
            days_to_due=-30,
        )

        # 3. Prepaid customer — active prepaid subscription (under E2E reseller)
        c_prepaid = get_or_create_subscriber(
            db,
            email="prepaid.customer@test.local",
            first="Pat",
            last="Prepaid",
            reseller=e2e_reseller,
            status=SubscriberStatus.active,
            password=DEFAULT_PASSWORD,
        )
        ensure_subscription(
            db,
            subscriber=c_prepaid,
            offer=offer_prepaid,
            status=SubscriptionStatus.active,
        )

        # 4. Suspended customer (under E2E reseller) — login/access edge
        c_suspended = get_or_create_subscriber(
            db,
            email="suspended.customer@test.local",
            first="Sam",
            last="Suspended",
            reseller=e2e_reseller,
            status=SubscriberStatus.suspended,
            password=DEFAULT_PASSWORD,
        )
        ensure_subscription(
            db,
            subscriber=c_suspended,
            offer=offer_postpaid,
            status=SubscriptionStatus.suspended,
        )

        # 5. Brand-new customer — no subscription (onboarding edge)
        get_or_create_subscriber(
            db,
            email="new.customer@test.local",
            first="Nia",
            last="New",
            reseller=house,
            status=SubscriberStatus.new,
            password=DEFAULT_PASSWORD,
        )
        db.commit()
        for email in (
            "active.customer@test.local",
            "overdue.customer@test.local",
            "prepaid.customer@test.local",
            "suspended.customer@test.local",
            "new.customer@test.local",
        ):
            created.append(("customer", email, "/portal/auth/login"))

        banner("Reseller portal user")
        reseller_su = get_or_create_subscriber(
            db,
            email="reseller@test.local",
            first="Rita",
            last="Reseller",
            reseller=e2e_reseller,
            status=SubscriberStatus.active,
            user_type=UserType.reseller,
            password=DEFAULT_PASSWORD,
        )
        link = (
            db.query(ResellerUser)
            .filter(ResellerUser.subscriber_id == reseller_su.id)
            .first()
        )
        if not link:
            db.add(
                ResellerUser(
                    subscriber_id=reseller_su.id,
                    reseller_id=e2e_reseller.id,
                    is_active=True,
                )
            )
        db.commit()
        created.append(("reseller", "reseller@test.local", "/reseller/auth/login"))

        banner(f"SUMMARY — seeded logins (password for all: {DEFAULT_PASSWORD})")
        for kind, login, url in created:
            print(f"  [{kind:9}] {login:34} -> {url}")
        print("\nDone.")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
