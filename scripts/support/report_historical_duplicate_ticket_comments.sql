-- Read-only candidate report for comments created before idempotency keys.
-- Exact content/author matches within five minutes are candidates for operator
-- review, not proof of duplication. This statement never updates or deletes.
WITH ordered_comments AS (
    SELECT
        comment.id,
        comment.ticket_id,
        comment.created_at,
        comment.author_type,
        comment.author_person_id,
        comment.author_system_user_id,
        comment.is_internal,
        comment.body,
        comment.attachments,
        lag(comment.id) OVER duplicate_window AS previous_comment_id,
        lag(comment.created_at) OVER duplicate_window AS previous_created_at
    FROM support_ticket_comments AS comment
    WHERE comment.idempotency_key IS NULL
    WINDOW duplicate_window AS (
        PARTITION BY
            comment.ticket_id,
            comment.author_type,
            comment.author_person_id,
            comment.author_system_user_id,
            comment.is_internal,
            comment.body,
            comment.attachments::text
        ORDER BY comment.created_at, comment.id
    )
)
SELECT
    ticket_id,
    previous_comment_id,
    id AS candidate_duplicate_comment_id,
    previous_created_at,
    created_at AS candidate_created_at,
    created_at - previous_created_at AS elapsed,
    author_type,
    author_person_id,
    author_system_user_id,
    is_internal,
    body,
    attachments
FROM ordered_comments
WHERE previous_created_at IS NOT NULL
  AND created_at - previous_created_at <= interval '5 minutes'
ORDER BY created_at DESC, ticket_id;
