-- Audit: "service running but mobile says expired / IP expired" (Layer 2).
--
-- Context: the mobile app derived service expiry from `next_billing_at` with no
-- regard to billing mode or status. For POSTPAID that date is the next invoice
-- date, not an expiry; for PREPAID it routinely lags a few days into the past
-- between the billing anniversary and when the runner advances it. Either way an
-- ACTIVE service gets mislabelled "expired — renew now" (and the IP mini-stat
-- next to it shows a red "Expired", read as "the IP expired").
--
-- The mobile-side guard ships in fix/mobile-postpaid-false-expiry. These queries
-- size + monitor the underlying DATA so the dates stop being load-bearing for a
-- meaning they don't reliably carry. Read-only. Run inside the db container:
--   docker exec dotmac_pg_local psql -U postgres -d dotmac_sub -f \
--     /path/to/audit_false_expiry.sql      (or paste blocks individually)

\echo '== A. Active subs whose date-math expiry contradicts status (false-expired) =='
WITH cur AS (
  SELECT s.id, s.billing_mode, s.access_state,
         COALESCE(s.end_at, s.next_billing_at) AS expires_at, s.end_at
  FROM subscriptions s
  WHERE s.status = 'active'
)
SELECT billing_mode,
       count(*)                                                        AS active_subs,
       count(*) FILTER (WHERE expires_at IS NULL)                      AS no_expiry_date,
       count(*) FILTER (WHERE expires_at < now())                      AS expires_in_past_FALSE_EXPIRED,
       count(*) FILTER (WHERE expires_at >= now()
                          AND expires_at < now() + interval '3 days')  AS expires_within_3d,
       count(*) FILTER (WHERE end_at IS NOT NULL)                      AS has_contract_end
FROM cur GROUP BY billing_mode ORDER BY active_subs DESC;

\echo '== B. How stale are the false-expired dates? (rolling cohort, not dead accounts) =='
SELECT billing_mode, access_state,
       count(*)                                          AS n,
       min(now()::date - next_billing_at::date)          AS min_days_past,
       max(now()::date - next_billing_at::date)          AS max_days_past,
       round(avg(now()::date - next_billing_at::date))   AS avg_days_past
FROM subscriptions
WHERE status = 'active' AND COALESCE(end_at, next_billing_at) < now()
GROUP BY billing_mode, access_state ORDER BY n DESC;

\echo '== C. Distinct customers currently shown "expired" in-app (blast radius) =='
SELECT count(DISTINCT subscriber_id) AS customers_seeing_expired
FROM subscriptions
WHERE status = 'active' AND COALESCE(end_at, next_billing_at) < now();

\echo '== D. billing_day vs next_billing_at day-of-month mismatch (anchor drift) =='
-- Near-universal mismatch => the configured billing_day is decorative and
-- next_billing_at follows legacy Splynx anchors. Decide the single source of
-- truth for the billing anniversary, then re-derive next_billing_at from it.
SELECT sub.billing_mode,
       count(*) AS active_subs_with_next_billing,
       count(*) FILTER (
         WHERE s.billing_day IS NOT NULL
           AND s.billing_day <> EXTRACT(DAY FROM sub.next_billing_at)::int
       ) AS day_of_month_mismatch
FROM subscriptions sub
JOIN subscribers s ON s.id = sub.subscriber_id
WHERE sub.status = 'active' AND sub.next_billing_at IS NOT NULL
GROUP BY sub.billing_mode;

\echo '== E. Postpaid subs carrying a prepaid-flavoured lifecycle event (mode drift) =='
-- A postpaid subscriber with a suspend reason='prepaid' suggests a prepaid-path
-- task touched a postpaid account (ties into the billing-mode denormalization
-- audit). Lists candidates for manual review.
SELECT e.created_at, e.event_type, e.reason, sub.id AS subscription_id,
       sub.billing_mode, s.account_number
FROM subscription_lifecycle_events e
JOIN subscriptions sub ON sub.id = e.subscription_id
JOIN subscribers s ON s.id = sub.subscriber_id
WHERE sub.billing_mode = 'postpaid'
  AND lower(coalesce(e.reason, '')) LIKE '%prepaid%'
ORDER BY e.created_at DESC
LIMIT 50;
