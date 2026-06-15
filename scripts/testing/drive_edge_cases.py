#!/usr/bin/env python3
"""Drive the dotmac_test stack (:8010) through every authenticated page per role.

Logs in as a role via the real login form (CSRF handled by the browser), crawls
all same-origin nav links under that role's path prefix, visits each, and records
HTTP status + crash/error detection + a screenshot. Results -> docs/testing/results/.

Usage:
    python scripts/testing/drive_edge_cases.py <role> [more_roles...]
    python scripts/testing/drive_edge_cases.py all

Roles: admin support finance active overdue prepaid suspended new reseller
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8010"
PASSWORD = "TestPass123!"
MAX_PAGES = 110  # safety cap per role
OUT = Path("docs/testing/results")
SHOTS = OUT / "screenshots"

# role -> (login_path, username, page_prefix_to_crawl)
ROLES = {
    "admin": ("/auth/login", "admin@test.local", "/admin"),
    "support": ("/auth/login", "support@test.local", "/admin"),
    "finance": ("/auth/login", "finance@test.local", "/admin"),
    "active": ("/portal/auth/login", "active.customer@test.local", "/portal"),
    "overdue": ("/portal/auth/login", "overdue.customer@test.local", "/portal"),
    "prepaid": ("/portal/auth/login", "prepaid.customer@test.local", "/portal"),
    "suspended": ("/portal/auth/login", "suspended.customer@test.local", "/portal"),
    "new": ("/portal/auth/login", "new.customer@test.local", "/portal"),
    "reseller": ("/reseller/auth/login", "reseller@test.local", "/reseller"),
}

ERROR_MARKERS = re.compile(
    r"Internal Server Error|Traceback \(most recent|Something went wrong|"
    r"Application error|Jinja2|UndefinedError|500 - ",
    re.I,
)

LAUNCH_ARGS = [
    "--headless=new",
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-crash-reporter",
]


def login(context, role):
    path, user, _ = ROLES[role]
    page = context.new_page()
    resp = page.goto(BASE + path, wait_until="domcontentloaded", timeout=30000)
    page.fill('input[name="username"]', user)
    page.fill('input[name="password"]', PASSWORD)
    page.press('input[name="password"]', "Enter")
    try:
        page.wait_for_load_state("domcontentloaded", timeout=15000)
    except Exception:
        pass
    page.wait_for_timeout(800)
    landed = page.url
    ok = path not in landed  # left the login page => logged in
    return page, ok, landed


def discover_links(page, prefix):
    hrefs = page.eval_on_selector_all(
        "a[href]", "els => els.map(e => e.getAttribute('href'))"
    )
    paths = set()
    for h in hrefs:
        if not h:
            continue
        if h.startswith(BASE):
            h = h[len(BASE) :]
        if not h.startswith("/"):
            continue
        if "logout" in h or "impersonate" in h or "/auth/" in h:
            continue
        # only crawl this role's area (+ shared root pages)
        if h.startswith(prefix):
            paths.add(h.split("#")[0].split("?")[0])
    return sorted(paths)


def visit(page, path):
    rec = {"path": path, "status": None, "error": None, "title": None}
    try:
        resp = page.goto(BASE + path, wait_until="domcontentloaded", timeout=20000)
        rec["status"] = resp.status if resp else None
        page.wait_for_timeout(300)
        rec["title"] = (page.title() or "")[:80]
        body = page.content()
        if rec["status"] and rec["status"] >= 500:
            rec["error"] = f"HTTP {rec['status']}"
        elif ERROR_MARKERS.search(body):
            m = ERROR_MARKERS.search(body)
            rec["error"] = f"error-marker:{m.group(0)[:40]}"
    except Exception as e:
        rec["error"] = f"exception:{type(e).__name__}:{str(e)[:80]}"
    return rec


def run_role(browser, role):
    print(f"\n########## ROLE: {role} ##########", flush=True)
    _, _, prefix = ROLES[role]
    context = browser.new_context(
        viewport={"width": 1366, "height": 900}, ignore_https_errors=True
    )
    context.set_default_timeout(20000)
    results = {"role": role, "login_ok": False, "landed": None, "pages": []}
    shotdir = SHOTS / role
    shotdir.mkdir(parents=True, exist_ok=True)

    page, ok, landed = login(context, role)
    results["login_ok"] = ok
    results["landed"] = landed
    print(f"  login_ok={ok} landed={landed}", flush=True)
    if not ok:
        page.screenshot(path=str(shotdir / "00_login_failed.png"))
        context.close()
        return results

    page.screenshot(path=str(shotdir / "00_landing.png"))
    to_visit = discover_links(page, prefix)
    print(f"  discovered {len(to_visit)} links under {prefix}", flush=True)

    seen = set()
    second_level = set()
    for i, path in enumerate(to_visit):
        if path in seen or len(seen) >= MAX_PAGES:
            continue
        seen.add(path)
        rec = visit(page, path)
        slug = re.sub(r"[^a-z0-9]+", "_", path.strip("/").lower())[:60] or "root"
        try:
            page.screenshot(path=str(shotdir / f"{i:02d}_{slug}.png"))
        except Exception:
            pass
        flag = "OK " if not rec["error"] else "ERR"
        print(f"  [{flag}] {rec['status']} {path}  {rec['error'] or ''}", flush=True)
        results["pages"].append(rec)
        # collect one level deeper (new links surfaced on this page)
        if not rec["error"]:
            for h in discover_links(page, prefix):
                if h not in seen:
                    second_level.add(h)

    # one level deeper (capped)
    extra = sorted(second_level - seen)[:MAX_PAGES]
    print(f"  second-level: {len(extra)} more", flush=True)
    for i, path in enumerate(extra):
        if path in seen or len(seen) >= MAX_PAGES:
            continue
        seen.add(path)
        rec = visit(page, path)
        slug = re.sub(r"[^a-z0-9]+", "_", path.strip("/").lower())[:60] or "root"
        try:
            page.screenshot(path=str(shotdir / f"L2_{i:02d}_{slug}.png"))
        except Exception:
            pass
        flag = "OK " if not rec["error"] else "ERR"
        print(f"  [{flag}] {rec['status']} {path}  {rec['error'] or ''}", flush=True)
        results["pages"].append(rec)

    context.close()
    return results


def main():
    roles = sys.argv[1:]
    if not roles or roles == ["all"]:
        roles = list(ROLES)
    OUT.mkdir(parents=True, exist_ok=True)
    SHOTS.mkdir(parents=True, exist_ok=True)
    all_results = {}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=LAUNCH_ARGS)
        for role in roles:
            if role not in ROLES:
                print(f"unknown role {role}", flush=True)
                continue
            t0 = time.time()
            res = run_role(browser, role)
            res["seconds"] = round(time.time() - t0, 1)
            all_results[role] = res
            (OUT / f"{role}.json").write_text(json.dumps(res, indent=2))
            n = len(res["pages"])
            errs = sum(1 for p in res["pages"] if p["error"])
            print(
                f"  == {role}: {n} pages, {errs} errors, {res['seconds']}s", flush=True
            )
        browser.close()
    # summary
    summary = {
        r: {
            "login_ok": d["login_ok"],
            "pages": len(d["pages"]),
            "errors": [p for p in d["pages"] if p["error"]],
        }
        for r, d in all_results.items()
    }
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2))
    print("\n===== SUMMARY =====", flush=True)
    for r, d in summary.items():
        print(
            f"{r:10} login={d['login_ok']} pages={d['pages']} "
            f"errors={len(d['errors'])}",
            flush=True,
        )


if __name__ == "__main__":
    main()
