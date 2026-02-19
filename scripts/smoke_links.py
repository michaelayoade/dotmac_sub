#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sys
from collections import deque
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse, urlunparse

import httpx


DEFAULT_EXCLUDED_PATH_SNIPPETS = {
    "/logout",
    "/stop-impersonation",
    "/auth/refresh",
    "/auth/reset-password",
}

LOGIN_REDIRECT_PATHS = (
    "/auth/login",
    "/portal/auth/login",
    "/reseller/auth/login",
    "/vendor/auth/login",
)

SKIP_SCHEMES = ("mailto:", "tel:", "javascript:")


class LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        for key, value in attrs:
            if key.lower() == "href" and value:
                self.hrefs.append(value)


def _normalize_url(
    base_url: str,
    current_url: str,
    href: str | None,
    excluded: set[str],
) -> str | None:
    if not href:
        return None
    href = href.strip()
    if not href or href.startswith("#"):
        return None
    if href.startswith(SKIP_SCHEMES):
        return None

    if href.startswith(("http://", "https://")):
        absolute = href
    else:
        absolute = urljoin(current_url, href)

    if not absolute.startswith(base_url):
        return None

    absolute = absolute.split("#", 1)[0]
    parsed = urlparse(absolute)
    path = parsed.path or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")

    if any(snippet in parsed.path for snippet in excluded):
        return None
    if parsed.path.startswith("/api"):
        return None

    return urlunparse(parsed._replace(path=path, fragment=""))


def _extract_links(html: str) -> list[str]:
    parser = LinkParser()
    parser.feed(html)
    return parser.hrefs


def _is_login_redirect(location: str | None) -> bool:
    if not location:
        return False
    parsed = urlparse(location)
    return any(parsed.path.startswith(path) for path in LOGIN_REDIRECT_PATHS)


def _login(client: httpx.Client, base_url: str, login_path: str, username: str, password: str) -> None:
    url = f"{base_url}{login_path}"
    response = client.post(
        url,
        data={"username": username, "password": password, "remember": "false"},
        follow_redirects=True,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"Login failed at {login_path}: HTTP {response.status_code}")


def _crawl_links(
    client: httpx.Client,
    base_url: str,
    start_paths: list[str],
    max_pages: int,
    excluded: set[str],
    allow_login_redirects: bool,
    show_response: bool,
) -> None:
    queue: deque[tuple[str, str | None]] = deque(
        (f"{base_url}{path}", None) for path in start_paths
    )
    visited: set[str] = set()

    while queue and len(visited) < max_pages:
        url, parent = queue.popleft()
        if url in visited:
            continue

        response = client.get(url, follow_redirects=False)
        visited.add(url)

        status = response.status_code
        if status >= 400:
            detail = f"{url} returned HTTP {status}"
            if parent:
                detail += f" (found on {parent})"
            if show_response:
                snippet = response.text[:500].replace("\n", " ")
                detail += f" :: {snippet}"
            raise RuntimeError(detail)

        if 300 <= status < 400:
            location = response.headers.get("location")
            if _is_login_redirect(location):
                if allow_login_redirects:
                    continue
                detail = f"{url} redirected to login: {location}"
                if parent:
                    detail += f" (found on {parent})"
                raise RuntimeError(detail)
            normalized = _normalize_url(base_url, url, location, excluded)
            if normalized and normalized not in visited:
                queue.append((normalized, url))
            continue

        content_type = response.headers.get("content-type", "")
        if "text/html" not in content_type:
            continue

        for href in _extract_links(response.text):
            normalized = _normalize_url(base_url, url, href, excluded)
            if normalized and normalized not in visited:
                queue.append((normalized, url))

    if queue and len(visited) >= max_pages:
        print(f"Reached max pages ({max_pages}); {len(queue)} URLs remain in queue.")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Smoke test internal links.")
    parser.add_argument(
        "--base-url",
        default=os.getenv("SMOKE_BASE_URL", os.getenv("PLAYWRIGHT_BASE_URL", "http://localhost:8000")),
        help="Base URL for the app (default: http://localhost:8000).",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=int(os.getenv("SMOKE_MAX_PAGES", "200")),
        help="Maximum number of pages to scan.",
    )
    parser.add_argument(
        "--login-path",
        default=os.getenv("SMOKE_LOGIN_PATH", "/auth/login"),
        help="Login path for form-based auth (default: /auth/login).",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        help="Exclude path substring (repeatable).",
    )
    parser.add_argument(
        "--allow-login-redirects",
        action="store_true",
        help="Treat redirects to login pages as non-failures.",
    )
    parser.add_argument(
        "--show-response",
        action="store_true",
        help="Include response body snippet in error output.",
    )
    parser.add_argument("--username", default=os.getenv("SMOKE_USERNAME"))
    parser.add_argument("--password", default=os.getenv("SMOKE_PASSWORD"))
    parser.add_argument(
        "--start",
        action="append",
        default=[],
        help="Start path (repeatable).",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    base_url = args.base_url.rstrip("/")
    if not args.start:
        args.start = ["/", "/auth/login", "/web/network", "/web/billing"]
    excluded = set(DEFAULT_EXCLUDED_PATH_SNIPPETS)
    env_excludes = os.getenv("SMOKE_EXCLUDE", "")
    if env_excludes:
        excluded.update(item.strip() for item in env_excludes.split(",") if item.strip())
    excluded.update(item.strip() for item in args.exclude if item.strip())

    with httpx.Client(base_url=base_url, timeout=15.0, headers={"User-Agent": "smoke-links/1.0"}) as client:
        if args.username and args.password:
            _login(client, base_url, args.login_path, args.username, args.password)

        _crawl_links(
            client,
            base_url,
            args.start,
            max(1, args.max_pages),
            excluded,
            args.allow_login_redirects,
            args.show_response,
        )

    print("Smoke link check complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
