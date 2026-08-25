# Control Relationships and Branding SOT

## Mutual Exclusivity Registry

`app.services.control_relationships` is the source of truth for relationships
between settings, event handlers, and competing operational controls. Every
relationship uses one declared mode:

- `exclusive`: one configured choice is active.
- `precedence`: the first non-empty value in the declared order wins.
- `chain`: stages are enabled in order.
- `fanout`: multiple independent outcomes may run.
- `competing`: multiple candidates may try, but one atomic claim wins.
- `incompatible`: two writers or implementations must not run together.

The registry currently enforces payment-provider exclusivity and the CRM-to-
native quote migration sequence at settings mutation time. It also owns event
handler stages and exclusive capabilities. The dispatcher validates the full
production handler topology at initialization and runs internal state changes
before customer communication and external integrations.

Run the complete diagnostic locally or in a read-only production job:

```bash
poetry run python scripts/one_off/audit_control_relationships.py
```

The command exits `2` when an error-severity setting conflict exists. The same
manifest is available to authorized administrators at
`/admin/system/control-relationships`.

Adding a new provider, handler, migration switch, scheduler alternative, or
assignment engine requires a registry declaration and a test. Do not implement
ad hoc conflict checks in routes or tasks.

## Branding Ownership

`app.services.brand_profiles` owns customer-facing identity. Resolution order
is field-based:

1. organization profile
2. reseller profile
3. platform profile
4. legacy domain settings
5. deployment `brand.json`/environment defaults

Empty values inherit from the next scope. A team sender profile is not a brand:
Finance, Support, and Field Service can use different sender addresses while
the subscriber's reseller/organization relationship selects the customer-
facing brand.

### Readers

Every customer-facing render reads this owner rather than resolving branding
itself: the runtime web theme, the invoice PDF, the queued-notification wrapper
(`app.services.email_template.wrap_email_html`), and transactional email.

Transactional email (`app.services.email`) resolves exactly one typed
`EmailBrand` snapshot per rendered message through `resolve_email_brand`, then
passes it down. It keeps no branding settings reads, no separate
company-identity lookup, and no colour literals beyond the design-system
neutrals; the guard is `tests/architecture/test_email_brand_boundary.py`.
Optional `brand_*_id` arguments on the render entry points forward the brand
scope to the owner so a reseller- or organization-scoped profile reaches the
message.

Because a brand seed is chosen for identity rather than legibility, a reader
that renders a brand colour as *text* corrects it first:
`brand_theme.readable_text_color` walks the brand's own generated scale until
WCAG AA is met against the surface, and `brand_theme.on_color_text_color` picks
the label colour for text sitting on a brand fill. A seed with no legible stop
resolves to a structural neutral, never to a second palette.

### Display brand is not sender identity

Display brand and legal-sender identity are separately owned and must not be
folded together. The `From:` name and address come from the SMTP sender profile
(`app.services.email.get_smtp_config`); `legal_name` and `legal_address`
attribute the legal entity on documents. A white-label build may change what
the product looks like; it must never silently change who is legally
responsible for a message. `EmailBrand` therefore carries display fields only,
enforced by `test_brand_snapshot_carries_display_identity_only`.

Migration `262_brand_profiles` creates the canonical table and backfills the
platform profile from existing company and branding settings. The compatibility
backfill is idempotent and dry-run by default:

```bash
poetry run python scripts/one_off/backfill_brand_profiles.py
poetry run python scripts/one_off/backfill_brand_profiles.py --apply
```

Existing platform branding and company-information forms remain supported and
synchronize only the fields they own. They do not overwrite unrelated values
set through the canonical API. Scoped profiles are managed through:

- `GET /api/v1/branding/resolve`
- `GET /api/v1/branding/profiles`
- `PUT /api/v1/branding/profiles/{platform|reseller|organization}`
- `DELETE /api/v1/branding/profiles/{scope}`
- `GET /api/v1/me/branding` for the authenticated customer/mobile runtime

All endpoints use `system:settings` permissions. Customer and reseller portal
contexts use the canonical resolver, as do the public theme, manifest, sidebar,
and authentication branding paths.

## Deployment

1. Apply migration `262_brand_profiles`.
2. Deploy the application; legacy settings remain readable.
3. Run both one-off scripts in dry-run mode.
4. Resolve error-severity control findings.
5. Apply the branding backfill and verify platform, reseller, and customer
   portal rendering.
6. Configure scoped reseller/organization profiles only where white-labeling is
   intentional. Absence means inheritance, not a missing configuration.
