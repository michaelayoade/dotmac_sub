"""ETA notification service for field service work orders.

Sends SMS and email notifications to customers when technicians are dispatched
or when ETA is updated.
"""

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models.workforce import WorkOrder
from app.services.common import coerce_uuid

logger = logging.getLogger(__name__)


def _resolve_customer_contact(db: Session, work_order: WorkOrder) -> dict | None:
    """Resolve customer contact information from work order.

    Returns dict with name, email, phone if found, otherwise None.
    """
    # Try to get contact from account
    if work_order.account_id:
        from app.models.subscriber import SubscriberAccount, AccountRole

        account = db.get(SubscriberAccount, work_order.account_id)
        if account and account.subscriber and account.subscriber.person:
            person = account.subscriber.person
            return {
                "name": person.display_name or f"{person.first_name} {person.last_name}".strip(),
                "email": person.email,
                "phone": person.phone,
            }

        # Check account roles for a primary contact
        if account and account.account_roles:
            primary = next(
                (r for r in account.account_roles if r.is_primary and r.person),
                None
            )
            if primary and primary.person:
                person = primary.person
                return {
                    "name": person.display_name or f"{person.first_name} {person.last_name}".strip(),
                    "email": person.email,
                    "phone": person.phone,
                }

    # Try service order if linked
    if work_order.service_order_id:
        from app.models.provisioning import ServiceOrder

        service_order = db.get(ServiceOrder, work_order.service_order_id)
        if service_order and service_order.account_id:
            from app.models.subscriber import SubscriberAccount

            account = db.get(SubscriberAccount, service_order.account_id)
            if account and account.subscriber and account.subscriber.person:
                person = account.subscriber.person
                return {
                    "name": person.display_name or f"{person.first_name} {person.last_name}".strip(),
                    "email": person.email,
                    "phone": person.phone,
                }

    return None


def send_eta_notification(db: Session, work_order_id: str) -> bool:
    """Send ETA notification to customer for a work order.

    Args:
        db: Database session
        work_order_id: The work order UUID

    Returns:
        True if notification was sent successfully
    """
    from app.services import sms as sms_service
    from app.services import email as email_service

    work_order = db.get(WorkOrder, coerce_uuid(work_order_id))
    if not work_order:
        logger.error(f"Work order not found: {work_order_id}")
        return False

    # Get estimated arrival time
    eta = work_order.estimated_arrival_at
    if not eta:
        # Try to calculate from scheduled start
        if work_order.scheduled_start:
            eta = work_order.scheduled_start
        else:
            logger.warning(f"No ETA available for work order {work_order_id}")
            return False

    # Get customer contact
    contact = _resolve_customer_contact(db, work_order)
    if not contact:
        logger.warning(f"No customer contact found for work order {work_order_id}")
        return False

    # Get technician name
    technician_name = "Our technician"
    if work_order.assigned_to_person_id:
        from app.models.person import Person

        technician = db.get(Person, work_order.assigned_to_person_id)
        if technician:
            technician_name = technician.display_name or technician.first_name or "Our technician"

    # Format ETA time
    eta_time = eta.strftime("%H:%M") if eta else "soon"

    context = {
        "customer_name": contact.get("name", "Valued Customer"),
        "technician_name": technician_name,
        "eta_time": eta_time,
        "work_order_title": work_order.title or "Service Visit",
    }

    sent = False

    # Send SMS if phone available
    if contact.get("phone"):
        try:
            result = sms_service.send_with_template(
                db=db,
                template_code="technician_eta",
                to_phone=contact["phone"],
                context=context,
            )
            if result:
                logger.info(f"ETA SMS sent to {contact['phone']} for work order {work_order_id}")
                sent = True
        except Exception as exc:
            logger.error(f"Failed to send ETA SMS: {exc}")

    # Send email if available
    if contact.get("email"):
        try:
            from app.models.notification import NotificationTemplate, NotificationChannel

            template = (
                db.query(NotificationTemplate)
                .filter(NotificationTemplate.code == "technician_assigned")
                .filter(NotificationTemplate.channel == NotificationChannel.email)
                .filter(NotificationTemplate.is_active.is_(True))
                .first()
            )

            subject = f"Your technician {technician_name} is on the way!"
            body = f"""Dear {context['customer_name']},

Your technician {technician_name} is on the way and will arrive at approximately {eta_time}.

Service: {context['work_order_title']}

Please ensure someone is available at the service location.

Thank you for your patience!"""

            if template:
                subject = template.subject or subject
                body = template.body
                for key, value in context.items():
                    body = body.replace(f"{{{{{key}}}}}", str(value))
                    body = body.replace(f"{{{{ {key} }}}}", str(value))

            email_service.send_email(
                db=db,
                to_email=contact["email"],
                subject=subject,
                body=body,
            )
            logger.info(f"ETA email sent to {contact['email']} for work order {work_order_id}")
            sent = True
        except Exception as exc:
            logger.error(f"Failed to send ETA email: {exc}")

    return sent


def send_technician_assigned_notification(db: Session, work_order_id: str) -> bool:
    """Send notification when technician is assigned to work order.

    Args:
        db: Database session
        work_order_id: The work order UUID

    Returns:
        True if notification was sent successfully
    """
    from app.services import sms as sms_service
    from app.services import email as email_service

    work_order = db.get(WorkOrder, coerce_uuid(work_order_id))
    if not work_order:
        logger.error(f"Work order not found: {work_order_id}")
        return False

    # Get customer contact
    contact = _resolve_customer_contact(db, work_order)
    if not contact:
        logger.warning(f"No customer contact found for work order {work_order_id}")
        return False

    # Get technician name
    technician_name = "A technician"
    if work_order.assigned_to_person_id:
        from app.models.person import Person

        technician = db.get(Person, work_order.assigned_to_person_id)
        if technician:
            technician_name = technician.display_name or technician.first_name or "A technician"

    # Format scheduled time
    scheduled_date = "To be confirmed"
    scheduled_time = ""
    if work_order.scheduled_start:
        scheduled_date = work_order.scheduled_start.strftime("%B %d, %Y")
        scheduled_time = work_order.scheduled_start.strftime("%H:%M")

    context = {
        "customer_name": contact.get("name", "Valued Customer"),
        "technician_name": technician_name,
        "scheduled_date": scheduled_date,
        "scheduled_time": scheduled_time,
        "work_order_title": work_order.title or "Service Visit",
    }

    sent = False

    # Send SMS
    if contact.get("phone"):
        try:
            result = sms_service.send_with_template(
                db=db,
                template_code="technician_assigned_sms",
                to_phone=contact["phone"],
                context=context,
            )
            if result:
                sent = True
        except Exception as exc:
            logger.error(f"Failed to send technician assigned SMS: {exc}")

    # Send email
    if contact.get("email"):
        try:
            from app.models.notification import NotificationTemplate, NotificationChannel

            template = (
                db.query(NotificationTemplate)
                .filter(NotificationTemplate.code == "technician_assigned")
                .filter(NotificationTemplate.channel == NotificationChannel.email)
                .filter(NotificationTemplate.is_active.is_(True))
                .first()
            )

            subject = f"Your Technician Has Been Assigned"
            body = f"""Dear {context['customer_name']},

A technician has been assigned to your service request.

Technician: {technician_name}
Date: {scheduled_date}
Time: {scheduled_time}

Service: {context['work_order_title']}

You'll receive an update when the technician is on their way.

Thank you for your patience!"""

            if template:
                subject = template.subject or subject
                body = template.body
                for key, value in context.items():
                    body = body.replace(f"{{{{{key}}}}}", str(value))
                    body = body.replace(f"{{{{ {key} }}}}", str(value))

            email_service.send_email(
                db=db,
                to_email=contact["email"],
                subject=subject,
                body=body,
            )
            sent = True
        except Exception as exc:
            logger.error(f"Failed to send technician assigned email: {exc}")

    return sent


def send_work_order_completed_notification(db: Session, work_order_id: str) -> bool:
    """Send notification when work order is completed.

    Args:
        db: Database session
        work_order_id: The work order UUID

    Returns:
        True if notification was sent successfully
    """
    from app.services import email as email_service

    work_order = db.get(WorkOrder, coerce_uuid(work_order_id))
    if not work_order:
        logger.error(f"Work order not found: {work_order_id}")
        return False

    # Get customer contact
    contact = _resolve_customer_contact(db, work_order)
    if not contact or not contact.get("email"):
        logger.warning(f"No customer email found for work order {work_order_id}")
        return False

    # Get technician name
    technician_name = "Our technician"
    if work_order.assigned_to_person_id:
        from app.models.person import Person

        technician = db.get(Person, work_order.assigned_to_person_id)
        if technician:
            technician_name = technician.display_name or technician.first_name or "Our technician"

    context = {
        "customer_name": contact.get("name", "Valued Customer"),
        "work_order_number": str(work_order.id)[:8].upper(),
        "work_order_title": work_order.title or "Service Visit",
        "technician_name": technician_name,
        "completed_at": datetime.now(timezone.utc).strftime("%B %d, %Y at %H:%M"),
        "completion_notes": work_order.notes or "Work completed successfully.",
    }

    try:
        from app.models.notification import NotificationTemplate, NotificationChannel

        template = (
            db.query(NotificationTemplate)
            .filter(NotificationTemplate.code == "work_order_completed")
            .filter(NotificationTemplate.channel == NotificationChannel.email)
            .filter(NotificationTemplate.is_active.is_(True))
            .first()
        )

        subject = f"Service Completed - Work Order #{context['work_order_number']}"
        body = f"""Dear {context['customer_name']},

Your service has been completed.

Work Order: #{context['work_order_number']}
Service: {context['work_order_title']}
Completed By: {technician_name}
Completed At: {context['completed_at']}

Summary:
{context['completion_notes']}

If you have any questions or concerns about the work performed, please contact us.

Thank you for choosing us!"""

        if template:
            subject = template.subject or subject
            body = template.body
            for key, value in context.items():
                body = body.replace(f"{{{{{key}}}}}", str(value))
                body = body.replace(f"{{{{ {key} }}}}", str(value))

        email_service.send_email(
            db=db,
            to_email=contact["email"],
            subject=subject,
            body=body,
        )
        logger.info(f"Work order completed email sent for {work_order_id}")
        return True
    except Exception as exc:
        logger.error(f"Failed to send work order completed email: {exc}")
        return False
