# Portal authentication source usage

`portal_authentication_successes_total{src=...}` counts delivered customer
portal logins. Its only label is the verified source: `user_credential`,
`access_credential`, or `radius_user`. It carries no identifier or credential
material. Unknown source values are ignored rather than creating labels.

Password-only login increments after session delivery and response cookie
construction. MFA increments only after a valid challenge completes, its
database session commits, delivery succeeds and the response cookie is built.
The source comes from the verified password result or the validated challenge,
never a request parameter. Challenges, rejected factors, revoked source
markers, audit failures and failed session delivery do not count. Impersonation,
refresh and existing-session reads do not count. Telemetry failure preserves
the delivered login result.

Compare source counts over a representative deployment interval, aggregating
across workers with `sum by (src) (increase(portal_authentication_successes_total[7d]))`.
Counters reset with processes; this is observational telemetry, not a durable
audit or a count of unique users. Missing telemetry is not proof of zero usage.
No cutoff threshold is selected here. Retiring service-secret portal login
still requires inventory, distinct credential enrollment/recovery and an
approved cutoff. PPPoE/RADIUS network authentication is unchanged.
