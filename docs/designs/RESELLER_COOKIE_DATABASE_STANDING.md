# Ordinary reseller cookie database standing

`app_sessions.reseller_standing` owns only ordinary reseller cookie database
standing and refresh deadlines. The mixed `reseller_portal` module remains a
compatibility/delivery consumer; this declaration does not migrate its other
writers or impersonation policy.

Redis delivers the exact AuthSession UUID, principal kind/UUID, exclusive
subscriber/reseller-user identifier and reseller UUID. Missing, malformed or
unbound ordinary cookies require re-login. No username, person alias, guessed
principal kind, epoch exemption or cached identity can replace the database
binding. Only `is_impersonation is True` selects the pre-existing explicit
legacy impersonation branch; truthy strings and integers take the strict path.

Each ordinary context/refresh reads AuthSession and current reseller/principal
ownership using fresh scalar-column selects inside `no_autoflush`; query
execution requests `populate_existing` without refreshing caller-held ORM
entities. Standing requires an
active unrevoked unexpired session, exact exclusive principal FKs and no staff
Party context; active Reseller; and current active ResellerUser reseller
membership. A first-class `reseller_user` principal is authoritative from that
ResellerUser membership. Its optional deprecated `subscriber_id`/`person_id`
link is historical provenance only and may point to the deliberately retired,
inactive/canceled Subscriber row without invalidating standing. An ordinary
legacy `subscriber` principal still requires both an active Subscriber and a
dedicated active ResellerUser membership; the legacy missing-table fallback is
not an ordinary authentication decision path.
Subscriber.reseller_id scopes customer accounts, not portal operator membership;
only the dedicated ResellerUser link supplies the operator reseller binding.
Reads neither flush nor complete caller transactions. Already-flushed caller
writes remain in their transaction; pending changes to existing authority rows
are preserved and are not authentication authority until flushed. Freshness is subject to database
transaction isolation: these checks do not promise visibility beyond a caller's
existing repeatable-read snapshot or close a revoke-after-read race.

Ordinary refresh needs a database Session and a valid creation timestamp. Its
new projection deadline is the minimum of database expiry, sliding TTL and
absolute lifetime. Refusal performs no Redis refresh write. Storage failures
propagate, with the context compatibility facade and public login-page/refresh callers returning 503; they never
authorize from cache and never roll back arbitrary caller work. Refresh cookie
max-age is also capped at the returned ordinary projection deadline.

Repair is fresh authorized login through existing issuance, never reconstructing
an AuthSession from cache. The existing credential transition owner revokes DB
standing and its durable projection reconciler handles cache cleanup; cleanup
success is unnecessary for the next ordinary request to refuse a revoked row.

`tests/test_reseller_session_standing.py` supplies SQLite fast-lane evidence for
both principal kinds, binding failures, stale ORM reads, revocation, refresh
deadlines, storage failure, preservation of caller transactions, and continued
first-class standing when the retained historical Subscriber has been retired.
`tests/integration/test_reseller_session_standing_pg.py` proves the historical
link behavior and fresh revocation visibility on the migrated PostgreSQL schema.
`tests/architecture/test_reseller_session_standing_boundary.py` guards the narrow
typed read owner, strict ordinary consumers and explicit legacy exclusion.
These tests do not establish deployment or browser acceptance.

Residual: explicit administrator reseller impersonation still lacks the
separate authoritative actor/session binding. This change intentionally neither
retires it nor fabricates that actor, and makes no impersonation completion claim.
