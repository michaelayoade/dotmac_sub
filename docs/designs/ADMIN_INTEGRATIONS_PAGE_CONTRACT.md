# Admin integrations page contract

Status: implementation contract for the transitional Sub admin screens.

This contract follows `docs/UI_INFORMATION_AND_ACTION_STANDARD.md`,
`docs/PRODUCTION_UI_BRIEF.md`, and `docs/designs/INTEGRATION_PLATFORM_SOT.md`.
The starter architecture assigns the external connector control plane to the
independently deployed Integrator. These Sub screens do not define provider
schemas, provider credentials, or a new connector runtime.

## Installed integrations (`/admin/integrations/installed`)

- **Type, audience, decision:** operator work list; identify an installed
  integration needing attention and open its owner-specific management page.
- **Entity and owner:** versioned `IntegrationInstallation` and its capability
  bindings. `app.services.integrations.installations` owns state transitions;
  `app.services.web_integrations` projects installation state and runtime
  evidence. Run and delivery records own their respective observations.
- **First viewport:** title and browse action, then the installed list with
  state and latest observation. Global Paystack and CRM checks
  do not describe a selected installation and are excluded.
- **List:** installation name, plugin title when different, type, state, latest
  observed result and time, manage/enable/disable/retire actions. No duplicate
  name/title/root columns. The shared `ListQuery`/`PageMeta` contract provides
  name search, state filter, stable name sort, total, and pagination.
- **State presentation:** draft, validating, disabled, enabled, and quarantined
  retain their distinct labels. The row toggle is shown only for enabled and
  disabled installations; owner services still validate every transition.
- **Actions:** browse goes to the marketplace. Manage is the visible row action;
  state changes and retire are under More actions. Bulk controls appear after
  selection. These commands call installation services, which validate
  eligibility. Retire is destructive and requires confirmation. The UI does
  not infer connectivity from enabled state.
- **Evidence:** selecting a row's observation shows at most 50 recent job runs
  and delivery attempts for that installation only. Administrative changes
  remain in the audit log; this panel does not claim to be a general activity
  feed. Missing installations return 404.
- **States and permissions:** the empty list offers a browse path; an
  installation with no activity says so. Read requires
  `system:settings:read`; mutations require `system:settings:write`.
  Observation timestamps preserve the application timezone formatting.
- **Responsive depth:** the table scrolls on narrow screens; labels and row
  actions remain visible. Run and delivery evidence is opened on demand.

## Legacy connections (`/admin/integrations/connectors`)

- **Type, audience, decision:** legacy configuration list and editor for
  administrators maintaining existing target references. It is explicitly
  separate from versioned installations and the Integrator control plane.
- **Entity and owner:** `ConnectorConfig` via `app.services.connector`;
  `app.services.web_integrations` adapts labeled form inputs to the existing
  typed create/update schema. This does not make `ConnectorConfig` the owner of
  new provider capabilities or secret bindings.
- **First viewport and list:** name, type, auth method, enabled state, and
  a view action. Use the shared `ListQuery`/`PageMeta` contract to search by
  name, filter by state, sort by name, show total, and paginate 25 rows by
  default. Notes and endpoints appear only in detail, where needed.
- **Editor:** labeled auth fields for the selected method, HTTP(S) base URL,
  timeout, repeatable headers, retry controls, notes, and enabled state. The
  form sends ordinary fields; the adapter builds the existing dict contracts.
  Sensitive saved values are never prefilled. All saved header values are
  concealed, regardless of their names. Blank credential fields preserve
  existing values when the auth method is unchanged.
- **Sensitivity:** do not render stored endpoints in the list or editor; older
  rows can contain credential-bearing database DSNs. API serialization masks
  non-HTTP endpoints, credential-bearing URLs, and sensitive headers. Legacy
  stored DSNs still need a separately governed migration into owner-approved
  secret bindings; this UI change does not rewrite stored records.
- **Actions and states:** create/edit require `system:settings:write`; read
  requires `system:settings:read`. Empty and filtered-empty lists have plain
  text. Validation errors retain nonsecret inputs and explain the problem.
  Enabled is configuration state, not a successful connection check.
- **Responsive depth and observability:** list columns reduce on mobile;
  detail keeps notes and configuration context. Existing audit behavior for
  writes remains authoritative. Old embed URLs redirect to the connection
  detail; no generic iframe health probe or fabricated marketplace update
  confirmation is offered.

## Connector catalogue (`/admin/integrations/marketplace`)

- **Type, audience, decision:** administrator catalogue for choosing an
  available integration and opening its owner-specific setup.
- **Entity and owner:** discovered connector definitions from
  `app.services.integrations.registry`; installation counts from
  `IntegrationInstallation`. The catalogue performs no provider configuration
  or installation transition itself.
- **First viewport and cards:** name, type, description, installed count,
  and an available manage/configure destination. Multiple installations link
  to the installed list instead of choosing one arbitrarily. There are no
  module paths, file sizes, or update promises without an upgrade flow.
- **States, permissions, and responsive depth:** read requires
  `system:settings:read`; an empty catalogue states that no connectors are
  available. Cards stack on narrow screens. Setup actions delegate to the
  existing provider pages and their permission and audit rules.
