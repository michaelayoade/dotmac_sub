"""Failure evidence capture for the Playwright suite.

A browser spec that fails with "the page never navigated" is undiagnosable
from the server log alone. The server log can only answer *whether a request
arrived*; when the answer is "no request arrived", every remaining question —
which control was clicked, whether the form was blocked by constraint
validation, what the console said, whether a script threw — is a question
about the BROWSER, and nothing in a uvicorn log can answer it.

The E2E Gate used to upload nothing at all. That made a deterministic browser
failure indistinguishable from a flake in practice, so it was re-run instead
of diagnosed. This module records, for every FAILING test:

- a Playwright trace (screenshots, DOM snapshots, network, console, sources),
- a video of the browser context,
- a final screenshot of every open page,
- the browser console log (including uncaught page errors),
- the network log (request / response / failed request).

Passing tests keep nothing, so the artifact stays small.

Everything is a documented knob: ``E2E_ARTIFACT_DIR`` (default
``test-results``), ``E2E_CAPTURE_EVIDENCE`` (default on) and
``E2E_CAPTURE_VIDEO`` (default on).
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from playwright.sync_api import Error as PlaywrightError

__all__ = [
    "artifact_dir",
    "capture_enabled",
    "instrument_browser",
    "note_outcome",
    "start_test",
]


def _flag(name: str, default: str = "1") -> bool:
    return os.getenv(name, default).strip().lower() not in {"0", "false", "no", "off"}


def capture_enabled() -> bool:
    return _flag("E2E_CAPTURE_EVIDENCE")


def _video_enabled() -> bool:
    return _flag("E2E_CAPTURE_VIDEO")


def artifact_dir() -> Path:
    return Path(os.getenv("E2E_ARTIFACT_DIR", "test-results")).resolve()


_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _slug(node_id: str) -> str:
    return _UNSAFE.sub("-", node_id).strip("-")[:150] or "unknown"


# The currently executing test. A context is created deep inside a fixture
# that has no access to the pytest item, so the item identity and its outcome
# are published here by the conftest hooks instead.
_CURRENT: dict[str, Any] = {"slug": "unknown", "failed": False, "seq": 0}


def start_test(node_id: str) -> None:
    _CURRENT["slug"] = _slug(node_id)
    _CURRENT["failed"] = False
    _CURRENT["seq"] = 0


def note_outcome(failed: bool) -> None:
    if failed:
        _CURRENT["failed"] = True


def _peek_prefix() -> str:
    """Artifact name prefix for the context currently being captured.

    A single test may build more than one context (an admin page plus an
    impersonated one, say). The sequence number keeps their artifacts from
    overwriting each other, and only advances when a context finishes.
    """

    suffix = "" if _CURRENT["seq"] == 0 else f"-{_CURRENT['seq']}"
    return f"{_CURRENT['slug']}{suffix}"


def _advance_prefix() -> None:
    _CURRENT["seq"] += 1


def _write(path: Path, lines: list[str]) -> None:
    if not lines:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _attach_page_listeners(page: Any, console: list[str], network: list[str]) -> None:
    page.on(
        "console",
        lambda message: console.append(f"[{message.type}] {message.text}"),
    )
    page.on("pageerror", lambda error: console.append(f"[pageerror] {error}"))
    page.on(
        "request",
        lambda request: network.append(f"--> {request.method} {request.url}"),
    )
    page.on(
        "response",
        lambda response: network.append(f"<-- {response.status} {response.url}"),
    )
    page.on(
        "requestfailed",
        lambda request: network.append(
            f"!!! {request.method} {request.url} {request.failure}"
        ),
    )


def _instrument_context(context: Any) -> None:
    """Trace + console + network for one context, saved only if the test failed.

    Screenshots are taken when the PAGE closes, not when the context does.
    That ordering is not incidental: the ``*_page`` fixtures depend on the
    ``*_context`` fixtures, so pytest closes every page first and the context
    sees an empty ``context.pages`` by the time it is torn down. Capturing at
    context close would have produced a zero-screenshot artifact that looked
    like working instrumentation.
    """

    console: list[str] = []
    network: list[str] = []
    seen: set[int] = set()
    videos: list[Any] = []

    def observe(page: Any) -> None:
        if id(page) in seen:
            return
        seen.add(id(page))
        _attach_page_listeners(page, console, network)

        video = getattr(page, "video", None)
        if video is not None:
            videos.append(video)

        index = len(seen) - 1
        original_page_close = page.close

        def page_close(*args: Any, **kwargs: Any) -> Any:
            if _CURRENT["failed"]:
                target = artifact_dir()
                target.mkdir(parents=True, exist_ok=True)
                try:
                    if not page.is_closed():
                        page.screenshot(
                            path=str(target / f"{_peek_prefix()}-page{index}.png"),
                            full_page=True,
                        )
                except PlaywrightError:
                    pass
            return original_page_close(*args, **kwargs)

        page.close = page_close

    context.on("page", observe)

    original_new_page = context.new_page

    def new_page(*args: Any, **kwargs: Any) -> Any:
        page = original_new_page(*args, **kwargs)
        observe(page)
        return page

    context.new_page = new_page

    try:
        context.tracing.start(screenshots=True, snapshots=True, sources=True)
        tracing = True
    except PlaywrightError:
        tracing = False

    original_close = context.close

    def close(*args: Any, **kwargs: Any) -> Any:
        failed = bool(_CURRENT["failed"])
        target = artifact_dir()
        prefix = _peek_prefix()

        if failed:
            target.mkdir(parents=True, exist_ok=True)
            # A page still open at context teardown never went through the
            # page_close wrapper above, so screenshot it here.
            for index, page in enumerate(list(context.pages)):
                try:
                    if not page.is_closed():
                        page.screenshot(
                            path=str(target / f"{prefix}-open{index}.png"),
                            full_page=True,
                        )
                except PlaywrightError:
                    pass
            _write(target / f"{prefix}-console.log", console)
            _write(target / f"{prefix}-network.log", network)

        if tracing:
            try:
                if failed:
                    context.tracing.stop(path=str(target / f"{prefix}-trace.zip"))
                else:
                    context.tracing.stop()
            except PlaywrightError:
                pass

        result = original_close(*args, **kwargs)

        # A video file only exists once the context has closed.
        for index, video in enumerate(videos):
            try:
                if failed:
                    video.save_as(str(target / f"{prefix}-page{index}.webm"))
                video.delete()
            except PlaywrightError:
                pass

        _advance_prefix()
        return result

    context.close = close


def instrument_browser(browser: Any) -> None:
    """Make every context this browser creates record failure evidence.

    Wrapping ``new_context`` once is deliberate: the suite builds contexts in
    nine different fixtures, and instrumenting them individually is exactly
    the kind of enumeration that goes stale the next time someone adds a
    tenth. A capture that silently misses the context a new spec uses is an
    unmonitored region, not coverage.
    """

    if not capture_enabled():
        return

    original_new_context = browser.new_context

    def new_context(*args: Any, **kwargs: Any) -> Any:
        if _video_enabled():
            kwargs.setdefault("record_video_dir", str(artifact_dir() / "video"))
        context = original_new_context(*args, **kwargs)
        _instrument_context(context)
        return context

    browser.new_context = new_context
