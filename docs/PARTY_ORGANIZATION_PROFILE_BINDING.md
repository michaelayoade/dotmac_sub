# Party Organization Profile Binding

**Status:** Additive schema and guarded binding implemented locally; no backfill or cutover  
**Decision owner:** Michael  
**System of record:** Sub  
**Binding writer:** `party.registry`  
**Audit owner:** `party.organization_profile_audit`

## Decision

`Organization`, `Reseller`, `Vendor`, and `FieldVendor` are business/domain
profiles of a canonical Organization Party. They are not separate legal
identities. One Party may therefore own one profile of each type and hold
several explicit roles at the same time:

```text
Party: ABC Networks Ltd (organization)
  Organization profile
  Reseller profile
  Vendor profile
  FieldVendor auth projection
  roles:
    reseller:default          active
    vendor:default            suspended
    partner:infrastructure    active
```

Suspending the vendor role does not suspend reseller operations or the
infrastructure agreement. A partner role remains an explicitly typed
collaboration and never becomes a reseller alias or permission shortcut.

## Schema and binding contract

Migration 352 adds the following nullable fields to all four profile tables:

- `party_id`;
- `party_bound_at`;
- `party_binding_source`; and
- `party_binding_reason`.

Each `party_id` is a restricted foreign key to `parties.id` and unique within
its profile table. Thus one Party may own an Organization, Reseller, Vendor,
and FieldVendor profile, but it cannot silently own two Reseller profiles or
two Vendor profiles.

The database requires complete nonblank provenance whenever a profile is
bound and requires all evidence fields to be null when it is unbound. Migration
342 is schema-only. It does not infer links from name, email, phone, code,
`account_type`, ERP identifiers, or the FieldVendor string UUID; assign a role;
or update any existing row.

`party.registry` exposes three guarded commands:

- `bind_organization_profile`;
- `bind_reseller_profile`; and
- `bind_vendor_profiles`.

Every target must be an active or quarantined Organization Party. Exact retries
return the current binding and preserve its original evidence. A different
target is refused and requires the future reviewed merge/repoint workflow.
Archived, merged, or Person Parties cannot receive these profiles.

Binding is identity linkage only. It creates no reseller, vendor, partner,
customer, or subscriber role; grants no portal scope or permission; and changes
no profile, billing, account, subscription, sales, support, procurement, or
authentication status. Role assignment remains a separate explicit
`party.registry` command.

## Vendor and FieldVendor invariant

`Vendor` is the native quoting/procurement identity. `FieldVendor` is the live
mobile-auth projection. Today they are joined by
`FieldVendor.crm_vendor_id == str(Vendor.id)`, which is a nullable string rather
than a foreign key.

`bind_vendor_profiles` treats them as one atomic pair:

1. the exact FieldVendor twin must exist;
2. both profiles must be unbound, or both must already bind the requested
   Party with complete evidence;
3. missing, partial, conflicting, or duplicate profile state is refused; and
4. both links are written inside one savepoint.

The string bridge is used only to locate the current projection during this
migration phase. It is not canonical identity and cannot authorize a login,
role, permission, merge, or repoint.

## Read-only audit

`scripts.migration.audit_party_organization_profiles` uses a PostgreSQL
`REPEATABLE READ, READ ONLY` transaction and reports only aggregate counts:

- installed binding columns and bound/unbound profiles;
- missing, invalid, orphan, partial, conflicting, and aligned vendor twins;
- bound Reseller/Vendor profiles missing their explicit Party role;
- Parties carrying two or more reseller/vendor/partner role types; and
- remaining reseller/vendor/partner values in legacy
  `Organization.account_type`.

It emits no names, email, phone, profile UUIDs, Party UUIDs, bridge UUIDs, or
binding reasons. It cannot bind, assign, repair, backfill, or call CRM.

## Authority migration

Current runtime owners remain temporarily unchanged:

| Concern | Current compatibility state | Target owner |
| --- | --- | --- |
| Organization classification | `Organization.account_type` | explicit `PartyRole` rows |
| Reseller operational state | `Reseller.is_active` and reseller services | reseller domain profile + Party role contract |
| Vendor operational state | `Vendor.is_active` and vendor services | vendor domain profile + Party role contract |
| Vendor portal projection | `FieldVendor.is_active` and string UUID bridge | Party-linked vendor membership/auth context |

The shadow phase begins only after migration 352 is applied and reviewed
profile bindings/roles are populated. Cutover requires:

1. every in-scope profile has reviewed binding provenance;
2. Vendor/FieldVendor pairs have no missing, partial, conflicting, invalid, or
   orphan bridge state;
3. canonical role state matches the intended reseller/vendor/partner contracts;
4. reseller customer scope, billing, commission, vendor procurement, and portal
   authorization tests pass against explicit context; and
5. all runtime readers are migrated to the named owners with parity evidence.

Only then may `Organization.account_type` stop driving role decisions and the
FieldVendor string bridge be retired. No current read path changes merely
because migration 352 or a nullable link exists.

The additive people/principal slice now consumes these reviewed Organization
Party bindings; see `docs/PARTY_PRINCIPAL_CONTEXT_BINDING.md`. Backfill and
runtime cutover remain separate work and may not infer organization context
again.
