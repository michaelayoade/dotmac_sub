# Settings Hub consolidation

Source baseline: `origin/main` at `7c52f50e3c74be098df11a442a8c79e7a1d1ea1a`.
Branch: `codex/settings-hub-migration`. Local implementation only; no push,
release, data migration, or deployment is authorised by this change.

## Page contract

The audience is authorised tenant administrators configuring operational policy.
The entry point is `/admin/system/settings-hub`. Its nine tabs are System,
Security & Access, Billing Setup, Network, Notifications, Catalog & Subscribers,
Operations, Integrations & AI, and Logs & Audit. Every active registered setting
has one presentation assignment in `app/services/settings_hub_catalog.py`.
Unknown domains and section identifiers fail coverage or return 404; they do
not silently fall back to an unrelated editor.

The first viewport names the section and its save scope. Effective values show
whether they came from the tenant, platform, or registered default. Unchanged
values do not create tenant overrides. Explicit checkbox false inputs distinguish
an unchecked field from an absent field; absent inputs are never changed.
Validation errors preserve submitted non-secret values, including invalid JSON,
while secret inputs remain blank. The MFA editor preserves the legacy `force_2fa`
fallback until an explicit canonical value is saved, and labels that provenance.

The shared registered-form adapter is used by both the hub and specialised
configuration pages. Monitoring submits its three domains as one registered batch.
Legacy unowned controls retain their pre-existing persistence path and remain
flagged as debt; this slice neither certifies their runtime effect nor grants them
new ownership. They cannot write a registered setting through the legacy path.

Labels, types, bounds,
choices, required flags and secrecy come from `SettingSpec`; presentation code
does not duplicate defaults. Lists and objects use JSON textareas. Secret
values never enter the generic editor view, and blank secret submissions keep
the stored value. The responsive editor has one column on small screens and
two on larger screens, with associated input labels and explicit errors.

Reads use `control.settings_spec` and its existing kernel resolver/cache.
`ui.settings_hub` owns navigation and section projection only. Writes still use
`control.settings_form_updates`, its typed command context, validation, atomic
transaction, audit attribution and existing scope rules. New section forms
submit only their section's identities; out-of-section fields refuse the batch.
The ledger-live switch remains a separate protected action. Branding, company
information, SMTP profiles, collection accounts, custom fields, ticket settings,
and other specialised resources keep their existing owners and workflows.

## Register and coverage

`docs/settings-hub-migration-register.json` is the reproducible source census.
Regenerate with `python -m scripts.architecture.settings_hub_inventory`.
It inventories active specs after factory expansion and retired-alias filtering,
retired aliases, specialised/seeded keys, canonical module/feature controls,
settings-management routes, static form field names, old-to-new route mappings,
and source fingerprints. Text fingerprints normalise checkout line endings, so
Windows and Linux produce the same register for identical source text. Actions include their source handler, route-local
permissions and adapter service calls. Empty route-local permission lists are
not a statement that a route is public: router, middleware and owner checks
still apply. Form-field rows are evidence, not additional setting declarations.

Existing unowned controls are explicitly flagged as debt. Retaining their
specialised screens does not certify that they affect runtime behaviour.
Bootstrap-only job-history keys remain inventoried as operational data; this
migration does not invent editable settings for them. A read-only reconciliation
against an explicitly named deployment is required to identify stored-only keys
that cannot appear in a source census. No deployment database was accessed.

## Starter alignment

The reference inspected was `dotmac_starter_mt` at
`152b885be4a368fda6fe21f42eae5e43254f79e3`. Its settings feature uses registered
specs, a shared resolver/writer, explicit tenant context, secret masking, and
effective-value provenance. Sub retains its existing kernel bridge, tenant
scope, inheritance and pinned packages. Product navigation belongs to Sub;
the reference application's `/admin/settings` URL is not a shared-kernel route
requirement. Branding storage and module authority are not migrated here.

## Route cutover

The old `/admin/system/settings` GET/POST and its branding, SMTP sender,
bank-account and ledger-switch action paths are removed. The old editor template
is removed. Generic forms now live under `/admin/system/settings-hub/edit/…`.
Branding posts to `/admin/system/branding`; SMTP sender actions live below
`/admin/system/email/smtp-senders`. Bank-account actions and the protected
ledger switch live under the hub. The source register records each mapping.
Internal links and contextual anchors are updated. Existing specialised config
URLs, JSON settings APIs, customer Test Connection activation, and the
`/admin/settings` entry redirect remain owned by their existing surfaces.

## Acceptance and release gate

1. Regenerate the census and run its source coverage, route-retirement, complex
   field rendering and runtime-registry parity tests.
2. Verify subsection isolation, invalid-batch atomicity, secret preservation,
   protected-switch permission checks and unknown-section refusal.
3. In an environment with the pinned dependencies, run the focused settings,
   branding, navigation, payment-configuration and architecture suites. Run the
   repository's complete required checks before publication.
4. Browser-check desktop/mobile, light/dark, read-only administrators, editor
   administrators, SMTP sender operations, branding upload/removal, customer
   bank-account actions, ledger-switch refusal and Test Connection settings.
5. Reconcile deployment-only keys read-only before a release. A source census
   alone is not database or browser acceptance.

There is no schema or stored-value transformation in this change. Before
release, rollback is the previous application revision. Keep settings rows,
secrets, audit evidence and retired aliases intact. Do not introduce a second
compatibility writer. Publication/deployment requires separate authorisation.
