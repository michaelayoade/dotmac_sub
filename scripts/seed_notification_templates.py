#!/usr/bin/env python3
"""Seed default notification templates for all customer journeys.

Usage:
    python scripts/seed_notification_templates.py
"""

import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.db import SessionLocal
from app.models.notification import NotificationChannel, NotificationTemplate


TEMPLATES = [
    # Billing Journey
    {
        "code": "invoice_issued",
        "name": "Invoice Issued",
        "channel": NotificationChannel.email,
        "subject": "Your Invoice #{{invoice_number}} is Ready",
        "body": """Dear {{customer_name}},

Your invoice #{{invoice_number}} for {{amount}} is now available.

Due Date: {{due_date}}

You can view and pay your invoice by logging into your account or clicking the link below:
{{invoice_link}}

Thank you for your business.

Best regards,
{{company_name}}""",
    },
    {
        "code": "invoice_due_7d",
        "name": "Invoice Due in 7 Days",
        "channel": NotificationChannel.email,
        "subject": "Payment Reminder: Invoice #{{invoice_number}} Due in 7 Days",
        "body": """Dear {{customer_name}},

This is a friendly reminder that your invoice #{{invoice_number}} for {{amount}} is due in 7 days.

Due Date: {{due_date}}

Please make your payment before the due date to avoid any service interruption.

{{invoice_link}}

Thank you,
{{company_name}}""",
    },
    {
        "code": "invoice_due_1d",
        "name": "Invoice Due Tomorrow",
        "channel": NotificationChannel.email,
        "subject": "URGENT: Invoice #{{invoice_number}} Due Tomorrow",
        "body": """Dear {{customer_name}},

Your invoice #{{invoice_number}} for {{amount}} is due tomorrow.

Please make your payment today to avoid late fees and service interruption.

{{invoice_link}}

Thank you,
{{company_name}}""",
    },
    {
        "code": "invoice_due_1d_sms",
        "name": "Invoice Due Tomorrow (SMS)",
        "channel": NotificationChannel.sms,
        "subject": None,
        "body": "REMINDER: Your invoice #{{invoice_number}} for {{amount}} is due tomorrow. Pay now to avoid service interruption. {{short_link}}",
    },
    {
        "code": "invoice_overdue",
        "name": "Invoice Overdue",
        "channel": NotificationChannel.email,
        "subject": "OVERDUE: Invoice #{{invoice_number}} - Immediate Payment Required",
        "body": """Dear {{customer_name}},

Your invoice #{{invoice_number}} for {{amount}} is now overdue.

Original Due Date: {{due_date}}
Days Overdue: {{days_overdue}}

Please make your payment immediately to avoid service suspension.

{{invoice_link}}

If you have already made this payment, please disregard this notice.

Thank you,
{{company_name}}""",
    },
    {
        "code": "invoice_overdue_sms",
        "name": "Invoice Overdue (SMS)",
        "channel": NotificationChannel.sms,
        "subject": None,
        "body": "OVERDUE: Invoice #{{invoice_number}} for {{amount}} is {{days_overdue}} days past due. Pay now to avoid suspension. {{short_link}}",
    },
    {
        "code": "payment_received",
        "name": "Payment Received",
        "channel": NotificationChannel.email,
        "subject": "Payment Received - Thank You!",
        "body": """Dear {{customer_name}},

We have received your payment of {{amount}}.

Payment Reference: {{payment_reference}}
Invoice: #{{invoice_number}}

Thank you for your prompt payment!

Best regards,
{{company_name}}""",
    },

    # Collections Journey
    {
        "code": "dunning_notice",
        "name": "Dunning Notice",
        "channel": NotificationChannel.email,
        "subject": "Important: Action Required on Your Account",
        "body": """Dear {{customer_name}},

Your account has an outstanding balance of {{amount}} that requires immediate attention.

Days Past Due: {{days_overdue}}

Please make a payment as soon as possible to avoid service interruption. If you're experiencing difficulties, please contact us to discuss payment options.

{{payment_link}}

Thank you,
{{company_name}}""",
    },
    {
        "code": "dunning_notice_sms",
        "name": "Dunning Notice (SMS)",
        "channel": NotificationChannel.sms,
        "subject": None,
        "body": "ACTION REQUIRED: Your account has an outstanding balance of {{amount}}. Pay now to avoid service suspension: {{short_link}}",
    },
    {
        "code": "suspension_warning",
        "name": "Suspension Warning",
        "channel": NotificationChannel.email,
        "subject": "WARNING: Your Service Will Be Suspended",
        "body": """Dear {{customer_name}},

Your account is {{days_overdue}} days past due with an outstanding balance of {{amount}}.

YOUR SERVICE WILL BE SUSPENDED IN {{days_until_suspension}} DAYS if payment is not received.

Please make your payment immediately:
{{payment_link}}

If you have questions or need to set up a payment plan, please contact us immediately.

{{company_name}}""",
    },
    {
        "code": "suspension_warning_sms",
        "name": "Suspension Warning (SMS)",
        "channel": NotificationChannel.sms,
        "subject": None,
        "body": "WARNING: Service suspension in {{days_until_suspension}} days. Balance: {{amount}}. Pay now: {{short_link}}",
    },
    {
        "code": "service_suspended",
        "name": "Service Suspended",
        "channel": NotificationChannel.email,
        "subject": "Your Service Has Been Suspended",
        "body": """Dear {{customer_name}},

Your service has been suspended due to non-payment.

Outstanding Balance: {{amount}}

To restore your service, please make your payment in full:
{{payment_link}}

Once payment is received, your service will be restored within 24 hours.

{{company_name}}""",
    },
    {
        "code": "service_suspended_sms",
        "name": "Service Suspended (SMS)",
        "channel": NotificationChannel.sms,
        "subject": None,
        "body": "Your service has been SUSPENDED due to non-payment of {{amount}}. Pay now to restore: {{short_link}}",
    },

    # Sales Journey
    {
        "code": "quote_sent",
        "name": "Quote Sent",
        "channel": NotificationChannel.email,
        "subject": "Your Quote from {{company_name}}",
        "body": """Dear {{customer_name}},

Thank you for your interest in our services. Please find your quote attached.

Quote #: {{quote_number}}
Total: {{amount}}
Valid Until: {{expiry_date}}

To accept this quote, please click the link below:
{{quote_link}}

If you have any questions, please don't hesitate to contact us.

Best regards,
{{sales_rep_name}}
{{company_name}}""",
    },
    {
        "code": "quote_accepted",
        "name": "Quote Accepted",
        "channel": NotificationChannel.email,
        "subject": "Quote #{{quote_number}} Accepted - Next Steps",
        "body": """Dear {{customer_name}},

Thank you for accepting quote #{{quote_number}}!

We're excited to get started. Here's what happens next:

1. Our team will process your order
2. You'll receive a service order confirmation
3. We'll schedule installation (if applicable)

If you have any questions, please contact us.

Best regards,
{{company_name}}""",
    },

    # Self-Service Journey
    {
        "code": "plan_change_requested",
        "name": "Plan Change Requested",
        "channel": NotificationChannel.email,
        "subject": "Plan Change Request Received",
        "body": """Dear {{customer_name}},

We've received your request to change your plan.

Current Plan: {{current_plan}}
Requested Plan: {{new_plan}}
Effective Date: {{effective_date}}

Your request is being reviewed. You'll receive a confirmation once approved.

Thank you,
{{company_name}}""",
    },
    {
        "code": "plan_change_approved",
        "name": "Plan Change Approved",
        "channel": NotificationChannel.email,
        "subject": "Plan Change Approved - Effective {{effective_date}}",
        "body": """Dear {{customer_name}},

Great news! Your plan change has been approved.

Previous Plan: {{current_plan}}
New Plan: {{new_plan}}
Effective Date: {{effective_date}}

Your new plan will be active on the effective date. Any billing adjustments will appear on your next invoice.

Thank you for choosing us!
{{company_name}}""",
    },

    # Field Service Journey
    {
        "code": "work_order_scheduled",
        "name": "Work Order Scheduled",
        "channel": NotificationChannel.email,
        "subject": "Your Appointment is Scheduled - {{scheduled_date}}",
        "body": """Dear {{customer_name}},

Your service appointment has been scheduled.

Date: {{scheduled_date}}
Time: {{scheduled_time}}
Service: {{work_order_title}}

Our technician will arrive during the scheduled time window. Please ensure someone is available at the service location.

Service Address:
{{service_address}}

If you need to reschedule, please contact us at least 24 hours in advance.

Thank you,
{{company_name}}""",
    },
    {
        "code": "technician_assigned",
        "name": "Technician Assigned",
        "channel": NotificationChannel.email,
        "subject": "Your Technician Has Been Assigned",
        "body": """Dear {{customer_name}},

A technician has been assigned to your service request.

Technician: {{technician_name}}
Date: {{scheduled_date}}
Time: {{scheduled_time}}

You'll receive an update when the technician is on their way.

Thank you,
{{company_name}}""",
    },
    {
        "code": "technician_assigned_sms",
        "name": "Technician Assigned (SMS)",
        "channel": NotificationChannel.sms,
        "subject": None,
        "body": "{{technician_name}} has been assigned to your service on {{scheduled_date}} at {{scheduled_time}}. We'll notify you when they're on the way.",
    },
    {
        "code": "technician_eta",
        "name": "Technician ETA",
        "channel": NotificationChannel.sms,
        "subject": None,
        "body": "Your technician {{technician_name}} is on the way! Estimated arrival: {{eta_time}}. Please ensure someone is available.",
    },
    {
        "code": "work_order_completed",
        "name": "Work Order Completed",
        "channel": NotificationChannel.email,
        "subject": "Service Completed - Work Order #{{work_order_number}}",
        "body": """Dear {{customer_name}},

Your service has been completed.

Work Order: #{{work_order_number}}
Service: {{work_order_title}}
Completed By: {{technician_name}}
Completed At: {{completed_at}}

Summary:
{{completion_notes}}

If you have any questions or concerns about the work performed, please contact us.

Thank you for choosing {{company_name}}!""",
    },

    # Support Journey
    {
        "code": "ticket_created",
        "name": "Support Ticket Created",
        "channel": NotificationChannel.email,
        "subject": "Support Ticket #{{ticket_number}} Created",
        "body": """Dear {{customer_name}},

We've received your support request.

Ticket #: {{ticket_number}}
Subject: {{ticket_subject}}
Priority: {{priority}}

Our support team will review your request and respond as soon as possible.

You can track your ticket status at:
{{ticket_link}}

Thank you,
{{company_name}} Support""",
    },
    {
        "code": "ticket_updated",
        "name": "Support Ticket Updated",
        "channel": NotificationChannel.email,
        "subject": "Update on Ticket #{{ticket_number}}",
        "body": """Dear {{customer_name}},

There's an update on your support ticket.

Ticket #: {{ticket_number}}
Subject: {{ticket_subject}}
Status: {{status}}

Update:
{{update_message}}

View full ticket:
{{ticket_link}}

Thank you,
{{company_name}} Support""",
    },
    {
        "code": "ticket_resolved",
        "name": "Support Ticket Resolved",
        "channel": NotificationChannel.email,
        "subject": "Ticket #{{ticket_number}} Resolved",
        "body": """Dear {{customer_name}},

Your support ticket has been resolved.

Ticket #: {{ticket_number}}
Subject: {{ticket_subject}}
Resolution: {{resolution}}

If this doesn't fully address your issue, please reply to this email or reopen the ticket:
{{ticket_link}}

Thank you for your patience!
{{company_name}} Support""",
    },
]


def seed_templates():
    """Seed notification templates to database."""
    db = SessionLocal()
    try:
        created = 0
        skipped = 0

        for template_data in TEMPLATES:
            # Check if template with this code already exists
            existing = (
                db.query(NotificationTemplate)
                .filter(NotificationTemplate.code == template_data["code"])
                .first()
            )

            if existing:
                print(f"  Skipped: {template_data['code']} (already exists)")
                skipped += 1
                continue

            template = NotificationTemplate(
                code=template_data["code"],
                name=template_data["name"],
                channel=template_data["channel"],
                subject=template_data.get("subject"),
                body=template_data["body"],
                is_active=True,
            )
            db.add(template)
            print(f"  Created: {template_data['code']}")
            created += 1

        db.commit()
        print(f"\nDone! Created: {created}, Skipped: {skipped}")

    except Exception as e:
        db.rollback()
        print(f"Error: {e}")
        raise
    finally:
        db.close()


if __name__ == "__main__":
    print("Seeding notification templates...\n")
    seed_templates()
