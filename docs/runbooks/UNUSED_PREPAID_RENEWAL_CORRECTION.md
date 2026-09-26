# Reviewed unused prepaid renewal correction

Owner: `financial.prepaid_service_renewals`.

Use this repair when a scheduled direct prepaid renewal created an
`AccountAdjustment` and active `ServiceEntitlement` for a period in which the
customer was not receiving service, such as a relocation hold. The selected
adjustment and entitlement must be the exact linked pair and must have no
invoice evidence.

The preview is read-only and fingerprint-bound. The apply command reverses the
ledger debit and marks the entitlement `reversed` atomically under the prepaid
renewal owner. It does not edit the subscriber deposit, delete evidence, or
change the subscription directly.

After the correction, preview and execute the reviewed renewal for the exact
confirmed service-start date. A new period beginning 25 September 2026 is
separate from the unused 13 August–13 September period; do not reuse the old
period's dates or fingerprint.

The available prepaid funding includes both the later payment and any existing
verified opening balance. For this account, reversing the ₦18,812.50 unused
renewal changes the verified balance from ₦2,187.38 to ₦21,000.00: ₦20,000.00
new payment plus ₦999.88 existing opening balance.
