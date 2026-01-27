from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload

from app.models.billing import Invoice
from app.models.catalog import CatalogOffer, NasDevice, Subscription
from app.models.network_monitoring import NetworkDevice, PopSite
from app.models.subscriber import Organization, Reseller, Subscriber
from app.services.response import list_response


def _subscriber_label(subscriber: Subscriber) -> str:
    """Generate label for a subscriber."""
    if subscriber.organization:
        return subscriber.organization.name
    name = f"{subscriber.first_name} {subscriber.last_name}".strip()
    if subscriber.account_number:
        return f"{name} ({subscriber.account_number})"
    return name


def _subscription_label(subscription: Subscription) -> str:
    offer_name = subscription.offer.name if subscription.offer else "Subscription"
    if subscription.subscriber:
        sub_label = _subscriber_label(subscription.subscriber)
        return f"{offer_name} - {sub_label}"
    return offer_name


def _invoice_label(invoice: Invoice) -> str:
    number = invoice.invoice_number or "Invoice"
    balance = invoice.balance_due if invoice.balance_due is not None else invoice.total
    if invoice.subscriber:
        sub_label = _subscriber_label(invoice.subscriber)
        if balance is not None:
            amount_label = f"{invoice.currency} {balance:,.2f}"
            return f"{number} - {sub_label} · {amount_label}"
        return f"{number} - {sub_label}"
    if balance is not None:
        return f"{number} · {invoice.currency} {balance:,.2f}"
    return number


def subscribers(db: Session, query: str, limit: int) -> list[dict]:
    """Search subscribers by name, email, account number, or organization."""
    term = (query or "").strip()
    if not term:
        return []
    like_term = f"%{term}%"
    results = (
        db.query(Subscriber)
        .outerjoin(Organization, Subscriber.organization_id == Organization.id)
        .options(joinedload(Subscriber.organization))
        .filter(
            or_(
                Subscriber.first_name.ilike(like_term),
                Subscriber.last_name.ilike(like_term),
                Subscriber.email.ilike(like_term),
                Subscriber.account_number.ilike(like_term),
                Subscriber.subscriber_number.ilike(like_term),
                Organization.name.ilike(like_term),
                Organization.domain.ilike(like_term),
            )
        )
        .limit(limit)
        .all()
    )
    return [{"id": sub.id, "label": _subscriber_label(sub)} for sub in results]


# Legacy alias for backwards compatibility
def accounts(db: Session, query: str, limit: int) -> list[dict]:
    """Search subscribers (accounts) - backwards compatibility alias."""
    return subscribers(db, query, limit)


def subscriptions(db: Session, query: str, limit: int) -> list[dict]:
    """Search subscriptions by offer name or subscriber details."""
    term = (query or "").strip()
    if not term:
        return []
    like_term = f"%{term}%"
    results = (
        db.query(Subscription)
        .join(CatalogOffer, Subscription.offer_id == CatalogOffer.id)
        .outerjoin(Subscriber, Subscription.subscriber_id == Subscriber.id)
        .outerjoin(Organization, Subscriber.organization_id == Organization.id)
        .options(
            joinedload(Subscription.subscriber).joinedload(Subscriber.organization),
            joinedload(Subscription.offer),
        )
        .filter(
            or_(
                CatalogOffer.name.ilike(like_term),
                Subscriber.account_number.ilike(like_term),
                Subscriber.first_name.ilike(like_term),
                Subscriber.last_name.ilike(like_term),
                Subscriber.email.ilike(like_term),
                Organization.name.ilike(like_term),
            )
        )
        .limit(limit)
        .all()
    )
    return [{"id": sub.id, "label": _subscription_label(sub)} for sub in results]


def contacts(db: Session, query: str, limit: int) -> list[dict]:
    """Search subscriber contacts - now same as subscribers search."""
    return subscribers(db, query, limit)


def people(db: Session, query: str, limit: int) -> list[dict]:
    """Search subscribers (people) by name, email, or phone."""
    term = (query or "").strip()
    if not term:
        return []
    like_term = f"%{term}%"
    results = (
        db.query(Subscriber)
        .filter(
            or_(
                Subscriber.first_name.ilike(like_term),
                Subscriber.last_name.ilike(like_term),
                Subscriber.email.ilike(like_term),
                Subscriber.phone.ilike(like_term),
            )
        )
        .limit(limit)
        .all()
    )
    items = []
    for subscriber in results:
        label = " ".join(part for part in [subscriber.first_name, subscriber.last_name] if part)
        if subscriber.email:
            label = f"{label} ({subscriber.email})"
        elif subscriber.phone:
            label = f"{label} ({subscriber.phone})"
        items.append({"id": subscriber.id, "label": label})
    return items


def invoices(db: Session, query: str, limit: int, subscriber_id: str | None = None) -> list[dict]:
    """Search invoices by number or subscriber details."""
    term = (query or "").strip()
    if not term:
        return []
    like_term = f"%{term}%"
    query_base = (
        db.query(Invoice)
        .outerjoin(Subscriber, Invoice.subscriber_id == Subscriber.id)
        .outerjoin(Organization, Subscriber.organization_id == Organization.id)
        .options(joinedload(Invoice.subscriber))
        .filter(
            or_(
                Invoice.invoice_number.ilike(like_term),
                Subscriber.account_number.ilike(like_term),
                Subscriber.first_name.ilike(like_term),
                Subscriber.last_name.ilike(like_term),
                Organization.name.ilike(like_term),
            )
        )
    )
    if subscriber_id:
        query_base = query_base.filter(Invoice.subscriber_id == subscriber_id)
    results = query_base.limit(limit).all()
    return [{"id": invoice.id, "label": _invoice_label(invoice)} for invoice in results]


def accounts_response(db: Session, query: str, limit: int) -> dict:
    return list_response(accounts(db, query, limit), limit, 0)


def subscribers_response(db: Session, query: str, limit: int) -> dict:
    return list_response(subscribers(db, query, limit), limit, 0)


def subscriptions_response(db: Session, query: str, limit: int) -> dict:
    return list_response(subscriptions(db, query, limit), limit, 0)


def contacts_response(db: Session, query: str, limit: int) -> dict:
    return list_response(contacts(db, query, limit), limit, 0)


def invoices_response(db: Session, query: str, limit: int, subscriber_id: str | None = None) -> dict:
    return list_response(invoices(db, query, limit, subscriber_id), limit, 0)


def people_response(db: Session, query: str, limit: int) -> dict:
    return list_response(people(db, query, limit), limit, 0)


def nas_devices(db: Session, query: str, limit: int) -> list[dict]:
    """Search NAS devices by name or IP."""
    term = (query or "").strip()
    if not term:
        return []
    like_term = f"%{term}%"
    results = (
        db.query(NasDevice)
        .filter(
            or_(
                NasDevice.name.ilike(like_term),
                NasDevice.code.ilike(like_term),
                NasDevice.ip_address.ilike(like_term),
                NasDevice.management_ip.ilike(like_term),
                NasDevice.nas_ip.ilike(like_term),
            )
        )
        .filter(NasDevice.is_active == True)
        .limit(limit)
        .all()
    )
    items = []
    for device in results:
        label = device.name
        if device.management_ip:
            label = f"{label} ({device.management_ip})"
        elif device.ip_address:
            label = f"{label} ({device.ip_address})"
        items.append({"id": device.id, "label": label})
    return items


def nas_devices_response(db: Session, query: str, limit: int) -> dict:
    return list_response(nas_devices(db, query, limit), limit, 0)


def network_devices(db: Session, query: str, limit: int) -> list[dict]:
    """Search network devices by name or management IP."""
    term = (query or "").strip()
    if not term:
        return []
    like_term = f"%{term}%"
    results = (
        db.query(NetworkDevice)
        .filter(
            or_(
                NetworkDevice.name.ilike(like_term),
                NetworkDevice.hostname.ilike(like_term),
                NetworkDevice.mgmt_ip.ilike(like_term),
            )
        )
        .filter(NetworkDevice.is_active == True)
        .limit(limit)
        .all()
    )
    items = []
    for device in results:
        label = device.name
        if device.mgmt_ip:
            label = f"{label} ({device.mgmt_ip})"
        items.append({"id": device.id, "label": label})
    return items


def network_devices_response(db: Session, query: str, limit: int) -> dict:
    return list_response(network_devices(db, query, limit), limit, 0)


def pop_sites(db: Session, query: str, limit: int) -> list[dict]:
    """Search POP sites by name or location."""
    term = (query or "").strip()
    if not term:
        return []
    like_term = f"%{term}%"
    results = (
        db.query(PopSite)
        .filter(
            or_(
                PopSite.name.ilike(like_term),
                PopSite.code.ilike(like_term),
                PopSite.city.ilike(like_term),
                PopSite.region.ilike(like_term),
            )
        )
        .filter(PopSite.is_active == True)
        .limit(limit)
        .all()
    )
    items = []
    for site in results:
        label = site.name
        if site.city:
            label = f"{label} ({site.city})"
        items.append({"id": site.id, "label": label})
    return items


def pop_sites_response(db: Session, query: str, limit: int) -> dict:
    return list_response(pop_sites(db, query, limit), limit, 0)


def vendors(db: Session, query: str, limit: int) -> list[dict]:
    """Search vendors - module removed."""
    return []


def vendors_response(db: Session, query: str, limit: int) -> dict:
    return list_response([], limit, 0)


def resellers(db: Session, query: str, limit: int) -> list[dict]:
    """Search reseller accounts by name."""
    term = (query or "").strip()
    if not term:
        return []
    like_term = f"%{term}%"
    results = (
        db.query(Reseller)
        .filter(
            or_(
                Reseller.name.ilike(like_term),
                Reseller.code.ilike(like_term),
            )
        )
        .filter(Reseller.is_active == True)
        .limit(limit)
        .all()
    )
    return [{"id": r.id, "label": r.name} for r in results]


def resellers_response(db: Session, query: str, limit: int) -> dict:
    return list_response(resellers(db, query, limit), limit, 0)


def organizations(db: Session, query: str, limit: int) -> list[dict]:
    """Search organizations by name."""
    term = (query or "").strip()
    if not term:
        return []
    like_term = f"%{term}%"
    results = (
        db.query(Organization)
        .filter(
            or_(
                Organization.name.ilike(like_term),
                Organization.domain.ilike(like_term),
            )
        )
        .limit(limit)
        .all()
    )
    return [{"id": org.id, "label": org.name} for org in results]


def organizations_response(db: Session, query: str, limit: int) -> dict:
    return list_response(organizations(db, query, limit), limit, 0)


def catalog_offers(db: Session, query: str, limit: int) -> list[dict]:
    """Search catalog offers by name."""
    term = (query or "").strip()
    if not term:
        return []
    like_term = f"%{term}%"
    results = (
        db.query(CatalogOffer)
        .filter(
            or_(
                CatalogOffer.name.ilike(like_term),
                CatalogOffer.code.ilike(like_term),
            )
        )
        .filter(CatalogOffer.is_active == True)
        .limit(limit)
        .all()
    )
    items = []
    for offer in results:
        label = offer.name
        if offer.code:
            label = f"{label} ({offer.code})"
        items.append({"id": offer.id, "label": label})
    return items


def catalog_offers_response(db: Session, query: str, limit: int) -> dict:
    return list_response(catalog_offers(db, query, limit), limit, 0)


def global_search(db: Session, query: str, limit_per_type: int = 3) -> dict:
    """
    Search across multiple entity types for global search suggestions.
    Returns categorized results with navigation URLs.
    """
    term = (query or "").strip()
    if not term or len(term) < 2:
        return {"categories": []}

    like_term = f"%{term}%"
    categories = []

    # Search subscribers (customers)
    customer_results = (
        db.query(Subscriber)
        .outerjoin(Organization, Subscriber.organization_id == Organization.id)
        .options(joinedload(Subscriber.organization))
        .filter(
            or_(
                Subscriber.first_name.ilike(like_term),
                Subscriber.last_name.ilike(like_term),
                Subscriber.email.ilike(like_term),
                Subscriber.account_number.ilike(like_term),
                Organization.name.ilike(like_term),
            )
        )
        .limit(limit_per_type)
        .all()
    )
    if customer_results:
        categories.append({
            "name": "Customers",
            "icon": "users",
            "items": [
                {
                    "id": str(sub.id),
                    "label": _subscriber_label(sub),
                    "url": f"/admin/subscribers/{sub.id}",
                    "type": "customer",
                }
                for sub in customer_results
            ],
        })

    # Search invoices
    invoice_results = (
        db.query(Invoice)
        .outerjoin(Subscriber, Invoice.subscriber_id == Subscriber.id)
        .outerjoin(Organization, Subscriber.organization_id == Organization.id)
        .options(joinedload(Invoice.subscriber))
        .filter(
            or_(
                Invoice.invoice_number.ilike(like_term),
                Subscriber.account_number.ilike(like_term),
            )
        )
        .limit(limit_per_type)
        .all()
    )
    if invoice_results:
        categories.append({
            "name": "Invoices",
            "icon": "document-text",
            "items": [
                {
                    "id": str(inv.id),
                    "label": _invoice_label(inv),
                    "url": f"/admin/billing/invoices/{inv.id}",
                    "type": "invoice",
                }
                for inv in invoice_results
            ],
        })

    return {"categories": categories, "query": term}
