-- Audit: postpaid services suspended by PREPAID balance enforcement (leaky guard).
--
-- Root cause: `_suspend_account` (called by prepaid enforcement with
-- reason=prepaid) suspended ALL of a subscriber's active subscriptions, not just
-- the prepaid ones. Combined with billing-mode reclassification (prepaid→postpaid
-- mode-inheritance fixes), this left postpaid services suspended on a prepaid
-- basis. The going-forward fix scopes prepaid enforcement to prepaid subs only
-- (collections/_core.py `_suspend_account(only_billing_mode=...)`); this audit
-- finds the DATA already in that state so ops can review/restore.
--
-- Read-only. Run inside the db container:
--   docker exec dotmac_pg_local psql -U postgres -d dotmac_sub \
--     -f /path/to/audit_prepaid_suspended_postpaid.sql

\echo '== A. Postpaid subs currently suspended that carry a prepaid suspend event =='
-- Split by overdue debt: NO overdue debt => suspension is NOT justified by
-- dunning either => safe-to-restore candidate. WITH overdue debt => leave to
-- the normal dunning/restore flow.
WITH flagged AS (
  SELECT DISTINCT s.id AS subscription_id, s.subscriber_id, sub.account_number
  FROM subscriptions s
  JOIN subscribers sub ON sub.id = s.subscriber_id
  JOIN subscription_lifecycle_events e ON e.subscription_id = s.id
  WHERE s.billing_mode = 'postpaid'
    AND s.status = 'suspended'
    AND e.event_type = 'suspend'
    AND lower(coalesce(e.reason, '')) LIKE '%prepaid%'
),
overdue AS (
  SELECT i.account_id, count(*) AS overdue_invoices,
         sum(i.balance_due) AS overdue_amount
  FROM invoices i
  WHERE i.is_active IS TRUE
    AND i.balance_due > 0
    AND (
      i.status = 'overdue'
      OR (i.status IN ('issued', 'partially_paid')
          AND i.due_at IS NOT NULL AND i.due_at <= now())
    )
  GROUP BY i.account_id
)
SELECT
  CASE WHEN o.account_id IS NULL THEN 'safe_to_restore (no overdue debt)'
       ELSE 'has_overdue_debt (leave to dunning)' END AS recommendation,
  count(*) AS subs,
  coalesce(sum(o.overdue_amount), 0) AS total_overdue
FROM flagged f
LEFT JOIN overdue o ON o.account_id = f.subscriber_id
GROUP BY 1 ORDER BY 1;

\echo '== B. The accounts, itemised (for the review list) =='
WITH flagged AS (
  SELECT DISTINCT s.id AS subscription_id, s.subscriber_id, sub.account_number
  FROM subscriptions s
  JOIN subscribers sub ON sub.id = s.subscriber_id
  JOIN subscription_lifecycle_events e ON e.subscription_id = s.id
  WHERE s.billing_mode = 'postpaid'
    AND s.status = 'suspended'
    AND e.event_type = 'suspend'
    AND lower(coalesce(e.reason, '')) LIKE '%prepaid%'
)
SELECT f.account_number, f.subscription_id,
       (SELECT max(e2.created_at)
          FROM subscription_lifecycle_events e2
         WHERE e2.subscription_id = f.subscription_id
           AND e2.event_type = 'suspend'
           AND lower(coalesce(e2.reason, '')) LIKE '%prepaid%') AS last_prepaid_suspend,
       coalesce((SELECT sum(i.balance_due) FROM invoices i
                  WHERE i.account_id = f.subscriber_id AND i.is_active IS TRUE
                    AND i.balance_due > 0
                    AND (i.status = 'overdue'
                         OR (i.status IN ('issued','partially_paid')
                             AND i.due_at IS NOT NULL AND i.due_at <= now()))
                ), 0) AS overdue_amount
FROM flagged f
ORDER BY overdue_amount ASC, f.account_number;

\echo '== C. Volume of prepaid suspend events touching postpaid subs, by date =='
-- Confirms recurrence (each prepaid enforcement run is a fresh batch).
SELECT date_trunc('day', e.created_at)::date AS day,
       count(DISTINCT s.id) AS postpaid_subs_suspended
FROM subscription_lifecycle_events e
JOIN subscriptions s ON s.id = e.subscription_id
WHERE s.billing_mode = 'postpaid'
  AND e.event_type = 'suspend'
  AND lower(coalesce(e.reason, '')) LIKE '%prepaid%'
GROUP BY 1 ORDER BY 1 DESC LIMIT 14;
