#!/usr/bin/env python3
"""Targeted interaction / negative edge cases the GET-only crawl can't cover:
negative login, anonymous redirects, cross-customer IDOR, RBAC 403s, and
API-level payment guards. Results -> docs/testing/results/targeted.json.

Run AFTER drive_edge_cases.py (don't run concurrently — the test app is 1 worker).
    python scripts/testing/drive_targeted.py
"""

from __future__ import annotations

import json
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8010"
PW = "TestPass123!"
OUT = Path("docs/testing/results")
LAUNCH = ["--headless=new", "--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]

# fixture ids (from DB)
ACTIVE_SUB = "78231107-a1f4-447a-892a-cc67603ca409"
ACTIVE_INV = "c73c20a3-6bc9-4449-b65c-a8a401af1aea"
OVERDUE_SUB = "d1fe7da6-d6d2-4bfc-a873-7626a231f536"
OVERDUE_INV = "a8db5086-d967-4b82-9448-f4568685222c"
OVERDUE_SUBSCRIPTION = "224c10ca-a76b-4a33-86c1-0d71116a9975"

results = []


def rec(cid, desc, expected, actual, ok, notes=""):
    results.append(
        {
            "id": cid,
            "desc": desc,
            "expected": expected,
            "actual": actual,
            "pass": ok,
            "notes": notes,
        }
    )
    flag = "PASS" if ok else "FAIL"
    print(
        f"[{flag}] {cid}: {desc} | expected={expected} actual={actual} {notes}",
        flush=True,
    )


def portal_login(context, username, login_path="/portal/auth/login"):
    page = context.new_page()
    page.goto(BASE + login_path, wait_until="domcontentloaded", timeout=30000)
    page.fill('input[name="username"]', username)
    page.fill('input[name="password"]', PW)
    page.press('input[name="password"]', "Enter")
    page.wait_for_timeout(1200)
    return page


def status_of(page, path):
    r = page.goto(BASE + path, wait_until="domcontentloaded", timeout=20000)
    page.wait_for_timeout(200)
    return r.status if r else None, page.url


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True, args=LAUNCH)

        # ---- B. Anonymous redirects --------------------------------------
        ctx = b.new_context()
        page = ctx.new_page()
        st, url = status_of(page, "/admin/dashboard")
        ok = "/auth/login" in url or st in (302, 303, 401, 403)
        rec("4.11", "anon /admin/dashboard", "redirect to login", f"{st} {url}", ok)
        st, url = status_of(page, "/portal/dashboard")
        ok = "/portal/auth/login" in url or st in (302, 303, 401, 403)
        rec("1.0", "anon /portal/dashboard", "redirect to login", f"{st} {url}", ok)
        ctx.close()

        # ---- C. Customer IDOR --------------------------------------------
        # Actor = overdue.customer (NOT used in the lockout test below, so its
        # login won't have been locked). Targets = active.customer's resources.
        ctx = b.new_context()
        page = portal_login(ctx, "overdue.customer@test.local")
        login_ok = "/portal/auth/login" not in page.url
        # own resources should be 200
        st_own_inv, _ = status_of(page, f"/portal/billing/invoices/{OVERDUE_INV}")
        st_own_sub, _ = status_of(page, f"/portal/services/{OVERDUE_SUBSCRIPTION}")
        # other customer's resources should be 403/404
        st_inv, _ = status_of(page, f"/portal/billing/invoices/{ACTIVE_INV}")
        rec(
            "1.14a",
            "IDOR other customer's invoice",
            "403/404 (not 200)",
            f"login_ok={login_ok} own={st_own_inv} other={st_inv}",
            login_ok and st_own_inv == 200 and st_inv in (403, 404),
        )
        st_sub, _ = status_of(page, f"/portal/services/{ACTIVE_SUB}")
        rec(
            "1.14b",
            "IDOR other customer's subscription",
            "403/404",
            f"own={st_own_sub} other={st_sub}",
            st_own_sub == 200 and st_sub in (403, 404),
        )
        ctx.close()

        # ---- E. RBAC 403 (staff least-privilege) -------------------------
        for role_user, cid in [
            ("support@test.local", "4.5"),
            ("finance@test.local", "2.14"),
        ]:
            ctx = b.new_context()
            page = portal_login(ctx, role_user, "/auth/login")
            checks = {}
            for p in [
                "/admin/system/roles",
                "/admin/system/settings",
                "/admin/network/core-devices",
                "/admin/billing/invoices",
                "/admin/customers",
            ]:
                s, _ = status_of(page, p)
                checks[p] = s
            # system + network should be forbidden for both support & finance
            blocked = all(
                checks[p] in (403, 302, 303)
                for p in ["/admin/system/roles", "/admin/network/core-devices"]
            )
            rec(
                cid,
                f"least-privilege 403 ({role_user.split('@')[0]})",
                "403 on system/network",
                json.dumps(checks),
                blocked,
            )
            ctx.close()

        # ---- F. Payment guards via API (admin bearer) --------------------
        api = b.new_context(base_url=BASE)
        login = api.request.post(
            "/api/v1/auth/login", data={"username": "admin@test.local", "password": PW}
        )
        token = login.json().get("access_token") if login.ok else None
        hdr = {"Authorization": f"Bearer {token}"}
        if token:
            PAY = "/api/v1/payments"
            # F1 valid payment
            p1 = api.request.post(
                PAY,
                headers=hdr,
                data={
                    "account_id": ACTIVE_SUB,
                    "amount": 1234.0,
                    "status": "succeeded",
                    "currency": "NGN",
                },
            )
            # F2 duplicate within 1 min -> guard should reject
            p2 = api.request.post(
                PAY,
                headers=hdr,
                data={
                    "account_id": ACTIVE_SUB,
                    "amount": 1234.0,
                    "status": "succeeded",
                    "currency": "NGN",
                },
            )
            rec(
                "2.5",
                "duplicate-payment guard (<1min)",
                "2nd rejected (4xx)",
                f"first={p1.status} dup={p2.status}",
                p2.status >= 400 or not p2.ok,
                "" if p1.ok else f"first-failed-body={p1.text()[:140]}",
            )
            # F3 neither scope -> exactly-one-scope validator
            p3 = api.request.post(
                PAY,
                headers=hdr,
                data={"amount": 10.0, "currency": "NGN", "status": "succeeded"},
            )
            rec(
                "2.6",
                "payment w/ no account scope",
                "422/400",
                str(p3.status),
                p3.status in (400, 422),
            )
            # F4 negative amount -> amount gt=0
            p4 = api.request.post(
                PAY,
                headers=hdr,
                data={
                    "account_id": ACTIVE_SUB,
                    "amount": -5.0,
                    "currency": "NGN",
                    "status": "succeeded",
                },
            )
            rec(
                "2.12",
                "negative payment amount",
                "422/400",
                str(p4.status),
                p4.status in (400, 422),
            )
        else:
            rec("2.5", "duplicate-payment guard", "n/a", "admin-token-failed", False)
        api.close()

        # ---- A. Negative login (LAST: locks active.customer; re-run
        #         scripts/testing/test_stack.sh seed OR the unlock SQL after) --
        ctx = b.new_context()
        page = ctx.new_page()
        page.goto(BASE + "/portal/auth/login", wait_until="domcontentloaded")
        last = ""
        for i in range(6):
            page.fill('input[name="username"]', "active.customer@test.local")
            page.fill('input[name="password"]', "WrongPass!" + str(i))
            page.press('input[name="password"]', "Enter")
            page.wait_for_timeout(700)
            last = page.inner_text("body")[:4000].lower()
        locked = any(w in last for w in ("lock", "too many", "try again", "attempt"))
        rec(
            "1.3",
            "wrong password x6 (customer)",
            "lockout/rate-limit msg",
            "locked-msg" if locked else "no-lock-msg",
            locked,
        )
        page.goto(BASE + "/portal/auth/login", wait_until="domcontentloaded")
        page.fill('input[name="username"]', "nobody@nowhere.test")
        page.fill('input[name="password"]', "whatever123")
        page.press('input[name="password"]', "Enter")
        page.wait_for_timeout(700)
        txt = page.inner_text("body").lower()
        leaks = "no such user" in txt or "no account" in txt or "doesn't exist" in txt
        rec(
            "1.4",
            "unknown-user login enumeration",
            "generic error",
            "leak" if leaks else "generic",
            not leaks,
        )
        ctx.close()

        b.close()

    (OUT / "targeted.json").write_text(json.dumps(results, indent=2))
    npass = sum(1 for r in results if r["pass"])
    print(f"\n===== TARGETED: {npass}/{len(results)} pass =====", flush=True)


if __name__ == "__main__":
    main()
