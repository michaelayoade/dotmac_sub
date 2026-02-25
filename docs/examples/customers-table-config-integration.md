# Customers Table Configuration Integration

This is the minimal integration flow for the customers table:

1. Fetch merged column config and render modal state.
2. Fetch table data (server already enforces visible/allowed columns at query layer).
3. Save column config snapshot after drag+toggle.

## API Calls

- `GET /api/v1/tables/customers/columns`
- `POST /api/v1/tables/customers/columns`
- `GET /api/v1/tables/customers/data?limit=50&offset=0`

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
