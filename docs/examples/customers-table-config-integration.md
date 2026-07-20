# Customers Table Configuration Integration

This is the minimal integration flow for the customers table:

1. Fetch merged column config and render modal state.
2. Fetch table data. The compatibility endpoint delegates row selection to the
   canonical customer-list owner, then applies visible/allowed columns.
3. Save column config snapshot after drag+toggle.

## API Calls

- `GET /api/v1/tables/customers/columns`
- `POST /api/v1/tables/customers/columns`
- `GET /api/v1/tables/customers/data?limit=50&offset=0`

The data endpoint accepts canonical customer capabilities only: `q`, `status`
or the `activation_state` alias, `customer_type`, `nas_id`, `pop_site_id`,
`sort_by` (`created_at`, `customer_name`, `name`, or `status`), `sort_dir`, and
page sizes `10`, `25`, `50`, or `100`. `offset` must align to `limit`.
Unsupported generic column filters return HTTP 400. New UI code should use the
URL-backed `/admin/customers` list; this endpoint exists for compatibility and
column-configured projections.

## Save Payload

```json
[
  { "column_key": "customer_name", "display_order": 0, "is_visible": true },
  { "column_key": "email", "display_order": 1, "is_visible": false },
  { "column_key": "account_status", "display_order": 2, "is_visible": true }
]
```

## Role Scope (Optional)

To save role-based defaults (admin or role-member):

- `POST /api/v1/tables/customers/columns?role_id=<role-uuid>`

If user-level config exists, it always takes precedence. Otherwise the API falls back to role config.
