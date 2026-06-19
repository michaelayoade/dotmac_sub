"""Read-only audit: identify Playwright/QA/e2e test-artifact customers in the
subscriber list. SELECT-only — makes no writes. Safe to run against prod.

    docker compose exec -T -e PYTHONPATH=/app app \
        python scripts/one_off/find_qa_customers.py
"""
from app.db import SessionLocal
from sqlalchemy import text

# Patterns that mark a row as a likely test artifact. Tuned to the conventions
# in seed_test_fixtures.py (@test.local) and fix_qa_active_on_terminal_subscriber
# (qa/test/e2e/demo login prefixes), plus "playwright".
EMAIL_RE = r'(playwright|e2e|qa[-_.]|@test\.|test\.local|^test[-_.]|demo[-_.]|selfcare-e2e)'
NAME_RE = r'(playwright|e2e|qa\M|\mtest\M|demo)'

db = SessionLocal()
try:
    rows = db.execute(text(f"""
        SELECT s.id, s.email, s.first_name, s.last_name, s.company_name,
               s.display_name, s.status, s.created_at, s.is_active,
               r.code AS reseller_code,
               (SELECT count(*) FROM subscriptions sub WHERE sub.subscriber_id = s.id) AS sub_count
        FROM subscribers s
        LEFT JOIN resellers r ON r.id = s.reseller_id
        WHERE lower(coalesce(s.email,'')) ~ :email_re
           OR lower(coalesce(s.first_name,'')) ~ :name_re
           OR lower(coalesce(s.last_name,'')) ~ :name_re
           OR lower(coalesce(s.company_name,'')) ~ :name_re
           OR lower(coalesce(s.display_name,'')) ~ :name_re
        ORDER BY s.created_at DESC
    """), {"email_re": EMAIL_RE, "name_re": NAME_RE}).fetchall()

    print(f"=== Candidate test-artifact subscribers: {len(rows)} ===")
    for x in rows:
        print(f"{str(x.created_at)[:19]} | {x.status:10} | active={x.is_active} | "
              f"subs={x.sub_count} | resel={x.reseller_code} | {x.email} | "
              f"{x.first_name} {x.last_name} | co={x.company_name} dn={x.display_name}")

    # Subscriptions whose login uses a QA prefix (catches artifacts whose
    # subscriber row looks clean but service login is e2e/qa/test/demo).
    sub_rows = db.execute(text("""
        SELECT sub.login, sub.status, s.email, s.created_at
        FROM subscriptions sub
        JOIN subscribers s ON s.id = sub.subscriber_id
        WHERE lower(coalesce(sub.login,'')) ~ '^(qa|test|e2e|demo|playwright)[-_.]'
        ORDER BY s.created_at DESC
    """)).fetchall()
    print(f"\n=== Subscriptions with QA-prefixed logins: {len(sub_rows)} ===")
    for x in sub_rows:
        print(f"{str(x.created_at)[:19]} | {x.status:10} | login={x.login} | {x.email}")
finally:
    db.close()
