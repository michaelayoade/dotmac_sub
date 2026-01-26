from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload

from app.models.billing import Invoice
from app.models.catalog import CatalogOffer, NasDevice, Subscription
from app.models.network_monitoring import NetworkDevice, PopSite
from app.models.subscriber import AccountRole, Organization, Reseller, Subscriber, SubscriberAccount
from app.models.vendor import Vendor
from app.services.response import list_response


def _account_label(account: SubscriberAccount) -> str:
    subscriber = account.subscriber
    if subscriber and subscriber.subscriber:
        if subscriber.subscriber.organization:
            base = subscriber.subscriber.organization.name
        else:
            base = f"{subscriber.subscriber.first_name} {subscriber.subscriber.last_name}"
    else:
        base = "Account"
    if account.account_number:
        return f"{base} ({account.account_number})"
    return base


def _subscriber_label(subscriber: Subscriber) -> str:
    if subscriber.subscriber:
        if subscriber.subscriber.organization:
            return subscriber.subscriber.organization.name
        return f"{subscriber.subscriber.first_name} {subscriber.subscriber.last_name}"
    return "Subscriber"


def _subscription_label(subscription: Subscription) -> str:
    offer_name = subscription.offer.name if subscription.offer else "Subscription"
    account_label = _account_label(subscription.account) if subscription.account else ""
    if account_label:
        return f"{offer_name} - {account_label}"
    return offer_name


def _contact_label(role: AccountRole) -> str:
    subscriber = role.subscriber
    label = f"{subscriber.first_name} {subscriber.last_name}" if subscriber else "Contact"
    account_label = _account_label(role.account) if role.account else None
    return f"{label} - {account_label}" if account_label else label


def _invoice_label(invoice: Invoice) -> str:
    number = invoice.invoice_number or "Invoice"
    account_label = _account_label(invoice.account) if invoice.account else ""
    balance = invoice.balance_due if invoice.balance_due is not None else invoice.total
    if balance is not None:
        amount_label = f"{invoice.currency} {balance:,.2f}"
        if account_label:
            return f"{number} - {account_label} · {amount_label}"
        return f"{number} · {amount_label}"
    if account_label:
        return f"{number} - {account_label}"
    return number


def accounts(db: Session, query: str, limit: int) -> list[dict]:
    term = (query or "").strip()
    if not term:
        return []
    like_term = f"%{term}%"
    results = (
        db.query(SubscriberAccount)
        .join(Subscriber, SubscriberAccount.subscriber_id == Subscriber.id)
        .outerjoin(Subscriber, Subscriber.subscriber_id == Subscriber.id)
        .outerjoin(Organization, Subscriber.organization_id == Organization.id)
        .options(
            joinedload(SubscriberAccount.subscriber)
            .joinedload(Subscriber.subscriber),
        )
        .filter(
            or_(
                SubscriberAccount.account_number.ilike(like_term),
                Subscriber.first_name.ilike(like_term),
                Subscriber.last_name.ilike(like_term),
                Subscriber.email.ilike(like_term),
                Organization.name.ilike(like_term),
                Organization.domain.ilike(like_term),
            )
        )
        .limit(limit)
        .all()
    )
    return [{"id": account.id, "label": _account_label(account)} for account in results]


def subscribers(db: Session, query: str, limit: int) -> list[dict]:
    term = (query or "").strip()
    if not term:
        return []
    like_term = f"%{term}%"
    results = (
        db.query(Subscriber)
        .outerjoin(Subscriber, Subscriber.subscriber_id == Subscriber.id)
        .outerjoin(Organization, Subscriber.organization_id == Organization.id)
        .options(joinedload(Subscriber.subscriber).joinedload(Subscriber.organization))
        .filter(
            or_(
                Subscriber.first_name.ilike(like_term),
                Subscriber.last_name.ilike(like_term),
                Subscriber.email.ilike(like_term),
                Organization.name.ilike(like_term),
                Organization.domain.ilike(like_term),
            )
        )
        .limit(limit)
        .all()
    )
    return [{"id": sub.id, "label": _subscriber_label(sub)} for sub in results]


def subscriptions(db: Session, query: str, limit: int) -> list[dict]:
    term = (query or "").strip()
    if not term:
        return []
    like_term = f"%{term}%"
    results = (
        db.query(Subscription)
        .join(SubscriberAccount, Subscription.account_id == SubscriberAccount.id)
        .join(CatalogOffer, Subscription.offer_id == CatalogOffer.id)
        .outerjoin(Subscriber, SubscriberAccount.subscriber_id == Subscriber.id)
        .outerjoin(Subscriber, Subscriber.subscriber_id == Subscriber.id)
        .outerjoin(Organization, Subscriber.organization_id == Organization.id)
        .options(
            joinedload(Subscription.account)
            .joinedload(SubscriberAccount.subscriber)
            .joinedload(Subscriber.subscriber),
            joinedload(Subscription.account)
            .joinedload(SubscriberAccount.subscriber)
            .joinedload(Subscriber.subscriber)
            .joinedload(Subscriber.organization),
            joinedload(Subscription.offer),
        )
        .filter(
            or_(
                CatalogOffer.name.ilike(like_term),
                SubscriberAccount.account_number.ilike(like_term),
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
    term = (query or "").strip()
    if not term:
        return []
    like_term = f"%{term}%"
    results = (
        db.query(AccountRole)
        .join(SubscriberAccount, AccountRole.account_id == SubscriberAccount.id)
        .outerjoin(Subscriber, SubscriberAccount.subscriber_id == Subscriber.id)
        .outerjoin(Subscriber, Subscriber.subscriber_id == Subscriber.id)
        .outerjoin(Organization, Subscriber.organization_id == Organization.id)
        .options(joinedload(AccountRole.account).joinedload(SubscriberAccount.subscriber))
        .filter(
            or_(
                Subscriber.first_name.ilike(like_term),
                Subscriber.last_name.ilike(like_term),
                SubscriberAccount.account_number.ilike(like_term),
                Organization.name.ilike(like_term),
            )
        )
        .limit(limit)
        .all()
    )
    return [{"id": role.id, "label": _contact_label(role)} for role in results]


def people(db: Session, query: str, limit: int) -> list[dict]:
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


def invoices(db: Session, query: str, limit: int, account_id: str | None = None) -> list[dict]:
    term = (query or "").strip()
    if not term:
        return []
    like_term = f"%{term}%"
    query_base = (
        db.query(Invoice)
        .join(SubscriberAccount, Invoice.account_id == SubscriberAccount.id)
        .outerjoin(Subscriber, SubscriberAccount.subscriber_id == Subscriber.id)
        .outerjoin(Subscriber, Subscriber.subscriber_id == Subscriber.id)
        .outerjoin(Organization, Subscriber.organization_id == Organization.id)
        .options(joinedload(Invoice.account).joinedload(SubscriberAccount.subscriber))
        .filter(
            or_(
                Invoice.invoice_number.ilike(like_term),
                SubscriberAccount.account_number.ilike(like_term),
                Subscriber.first_name.ilike(like_term),
                Subscriber.last_name.ilike(like_term),
                Organization.name.ilike(like_term),
            )
        )
    )
    if account_id:
        query_base = query_base.filter(Invoice.account_id == account_id)
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


def invoices_response(db: Session, query: str, limit: int, account_id: str | None = None) -> dict:
    return list_response(invoices(db, query, limit, account_id), limit, 0)


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
    """Search vendors by name."""
    term = (query or "").strip()
    if not term:
        return []
    like_term = f"%{term}%"
    results = (
        db.query(Vendor)
        .filter(
            or_(
                Vendor.name.ilike(like_term),
                Vendor.code.ilike(like_term),
                Vendor.contact_name.ilike(like_term),
            )
        )
        .filter(Vendor.is_active == True)
        .limit(limit)
        .all()
    )
    return [{"id": v.id, "label": v.name} for v in results]


def vendors_response(db: Session, query: str, limit: int) -> dict:
    return list_response(vendors(db, query, limit), limit, 0)


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
    from app.models.tickets import Ticket
    from app.models.workforce import WorkOrder

    term = (query or "").strip()
    if not term or len(term) < 2:
        return {"categories": []}

    like_term = f"%{term}%"
    categories = []

    # Search customers/subscribers
    customer_results = (
        db.query(Subscriber)
        .outerjoin(Subscriber, Subscriber.subscriber_id == Subscriber.id)
        .outerjoin(Organization, Subscriber.organization_id == Organization.id)
        .options(joinedload(Subscriber.subscriber).joinedload(Subscriber.organization))
        .filter(
            or_(
                Subscriber.first_name.ilike(like_term),
                Subscriber.last_name.ilike(like_term),
                Subscriber.email.ilike(like_term),
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

    # Search accounts
    account_results = (
        db.query(SubscriberAccount)
        .join(Subscriber, SubscriberAccount.subscriber_id == Subscriber.id)
        .outerjoin(Subscriber, Subscriber.subscriber_id == Subscriber.id)
        .outerjoin(Organization, Subscriber.organization_id == Organization.id)
        .options(
            joinedload(SubscriberAccount.subscriber).joinedload(Subscriber.subscriber),
            joinedload(SubscriberAccount.subscriber).joinedload(Subscriber.subscriber).joinedload(Subscriber.organization),
        )
        .filter(
            or_(
                SubscriberAccount.account_number.ilike(like_term),
                Subscriber.first_name.ilike(like_term),
                Subscriber.last_name.ilike(like_term),
                Organization.name.ilike(like_term),
            )
        )
        .limit(limit_per_type)
        .all()
    )
    if account_results:
        categories.append({
            "name": "Accounts",
            "icon": "credit-card",
            "items": [
                {
                    "id": str(acc.id),
                    "label": _account_label(acc),
                    "url": f"/admin/billing/accounts/{acc.id}",
                    "type": "account",
                }
                for acc in account_results
            ],
        })

    # Search invoices
    invoice_results = (
        db.query(Invoice)
        .join(SubscriberAccount, Invoice.account_id == SubscriberAccount.id)
        .outerjoin(Subscriber, SubscriberAccount.subscriber_id == Subscriber.id)
        .outerjoin(Subscriber, Subscriber.subscriber_id == Subscriber.id)
        .outerjoin(Organization, Subscriber.organization_id == Organization.id)
        .options(joinedload(Invoice.account).joinedload(SubscriberAccount.subscriber))
        .filter(
            or_(
                Invoice.invoice_number.ilike(like_term),
                SubscriberAccount.account_number.ilike(like_term),
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

    # Search tickets
    ticket_results = (
        db.query(Ticket)
        .filter(
            or_(
                Ticket.title.ilike(like_term),
                Ticket.ticket_number.ilike(like_term),
            )
        )
        .limit(limit_per_type)
        .all()
    )
    if ticket_results:
        categories.append({
            "name": "Tickets",
            "icon": "ticket",
            "items": [
                {
                    "id": str(t.id),
                    "label": f"#{t.ticket_number} - {t.title}" if t.ticket_number else t.title,
                    "url": f"/admin/support/tickets/{t.id}",
                    "type": "ticket",
                }
                for t in ticket_results
            ],
        })

    # Search work orders
    work_order_results = (
        db.query(WorkOrder)
        .filter(
            or_(
                WorkOrder.title.ilike(like_term),
                WorkOrder.work_order_number.ilike(like_term),
            )
        )
        .limit(limit_per_type)
        .all()
    )
    if work_order_results:
        categories.append({
            "name": "Work Orders",
            "icon": "wrench",
            "items": [
                {
                    "id": str(wo.id),
                    "label": f"#{wo.work_order_number} - {wo.title}" if wo.work_order_number else wo.title,
                    "url": f"/admin/operations/work-orders/{wo.id}",
                    "type": "work_order",
                }
                for wo in work_order_results
            ],
        })

    return {"categories": categories, "query": term}
