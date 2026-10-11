# Runbook: user lost their MFA device

Use this when a user cannot sign in because their authenticator (and recovery
codes) are gone. Removal of a second factor is a privileged, audited action:
it must go through the owner flow for the user's principal type, never through
a database edit.

## 1. Verify the requester first

- Verify through a channel **on file**, never the channel the request arrived
  on. Call the phone number recorded on the account, or message the address
  recorded on the account, or have the user's manager confirm via a known
  channel (staff).
- A request that arrives by email, chat or an inbound call is not itself proof.
  If the contact details on file changed recently, treat it as suspicious and
  escalate to security instead of proceeding.
- Open a ticket before acting. The ticket is the evidence trail.

## 2. Record verification in the reason

Every removal requires a reason (3 to 500 characters) stored in the audit
evidence. Write the verification channel and the ticket, for example:
`Call-back to phone on file, identity confirmed, ticket SUP-1234`.
Do not put secrets, codes or personal data in the reason.

## 3. Choose the path by principal type

| Principal | Where | Who may do it |
|---|---|---|
| Staff (system user) | Admin, System, Users, user detail, **Disable MFA** (form with Reason). Owner: `staff_provisioning.remove_staff_mfa` | Admin role and `rbac:assign` |
| Reseller user | Admin, Resellers, reseller detail, Portal access, **More**, **Remove MFA** (Reason). Owner: `reseller_onboarding.remove_reseller_user_mfa` | Admin role and `reseller:write`; only for users of that reseller |
| Subscriber (customer) | API `DELETE /api/v1/mfa-methods/{id}?reason=...` (one factor at a time) | Administrators with `auth:credential:write` |

Rules enforced by the system (a refusal is deliberate, do not work around it):

- The actor must be a human holding the admin role, plus `rbac:assign` (staff)
  or `reseller:write` (reseller). API keys are refused.
- No self-removal.
- Staff: the target must not hold the admin role. If an administrator lost their
  device, use the break-glass procedure; do not edit the database.
- Reseller: the user must belong to the reseller whose page you are on.
- The generic API refuses staff and reseller factors (HTTP 422).

## 4. What happens on removal

Atomically: all factors of the principal are disabled, the authenticator seed
is erased, unused recovery codes are deactivated, lockout counters are reset,
and all active sessions are revoked. An audit record (actor, subject, reason)
is written in the same transaction. Repeating the action is a recorded no-op.

The user then receives an email at the address on file: "Your multi-factor
authentication was removed by <administrator> at <time>. If you didn't request
this, contact support or security." The message contains no secrets. If the
user did not ask for this, treat it as an account-takeover signal: contact
security, reset the password and review the audit record.

## 5. Re-enrolment

- Admin and staff sign-in: when `admin_mfa_required` is on (setting key
  `admin_mfa_required`, env `ADMIN_MFA_REQUIRED`, default off; legacy
  `force_2fa` is also honoured), the next sign-in goes straight to forced
  enrolment: the user scans a new QR code, confirms a code and receives new
  recovery codes. When the setting is off, enrolment is optional and the user
  can add MFA from their profile.
- Reseller and customer portals do not force enrolment; the user re-adds MFA
  from their security settings.
- Tell the user to store the new recovery codes offline.
