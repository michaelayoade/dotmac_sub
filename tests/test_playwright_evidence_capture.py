"""Positive control for the Playwright failure-evidence capture.

An instrumentation hook that quietly captures nothing looks exactly like a
test suite that never failed. The E2E Gate spent an entire diagnosis cycle in
that position: it uploaded no artifact, so "no trace" was indistinguishable
from "no failure", and the only available response to a red gate was a
re-run.

So the capture is proven here, not assumed. These tests drive
``tests.playwright.evidence`` with browser doubles and assert on the FILES it
writes: a failing test must produce a trace, a screenshot, a console log and
a network log; a passing test must produce none of them. The negative case is
half the control — without it, an implementation that wrote everything
unconditionally would also pass.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.playwright import evidence


class FakeVideo:
    def __init__(self) -> None:
        self.saved_to: str | None = None
        self.deleted = False

    def save_as(self, path: str) -> None:
        self.saved_to = path
        Path(path).write_bytes(b"webm")

    def delete(self) -> None:
        self.deleted = True


class FakeTracing:
    def __init__(self) -> None:
        self.started = False
        self.stopped_to: str | None = None
        self.stopped = False

    def start(self, **_kwargs: object) -> None:
        self.started = True

    def stop(self, path: str | None = None) -> None:
        self.stopped = True
        self.stopped_to = path


class FakePage:
    def __init__(self) -> None:
        self.listeners: dict[str, list] = {}
        self.video = FakeVideo()
        self._closed = False
        self.screenshots: list[str] = []

    def on(self, event: str, handler) -> None:
        self.listeners.setdefault(event, []).append(handler)

    def emit(self, event: str, payload) -> None:
        for handler in self.listeners.get(event, []):
            handler(payload)

    def is_closed(self) -> bool:
        return self._closed

    def screenshot(self, path: str, full_page: bool = False) -> None:
        self.screenshots.append(path)
        Path(path).write_bytes(b"png")

    def close(self) -> None:
        self._closed = True


class FakeContext:
    def __init__(self) -> None:
        self.tracing = FakeTracing()
        self.pages: list[FakePage] = []
        self.listeners: dict[str, list] = {}
        self.closed = False

    def on(self, event: str, handler) -> None:
        self.listeners.setdefault(event, []).append(handler)

    def new_page(self) -> FakePage:
        page = FakePage()
        self.pages.append(page)
        return page

    def close(self) -> None:
        self.closed = True


class FakeBrowser:
    def __init__(self) -> None:
        self.context_kwargs: dict[str, object] = {}

    def new_context(self, **kwargs: object) -> FakeContext:
        self.context_kwargs = kwargs
        return FakeContext()


class FakeConsoleMessage:
    type = "error"
    text = "An invalid form control with name='first_name' is not focusable."


class FakeRequest:
    method = "POST"
    url = "http://127.0.0.1:8001/admin/customers/person/abc/edit"
    failure = None


@pytest.fixture()
def artifacts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("E2E_ARTIFACT_DIR", str(tmp_path))
    monkeypatch.setenv("E2E_CAPTURE_EVIDENCE", "1")
    monkeypatch.setenv("E2E_CAPTURE_VIDEO", "1")
    return tmp_path


def _run(browser: FakeBrowser, *, failed: bool) -> tuple[FakeContext, FakePage]:
    evidence.start_test("tests/playwright/e2e/test_subscribers.py::test_notes")
    context = browser.new_context()
    page = context.new_page()
    page.emit("console", FakeConsoleMessage())
    page.emit("request", FakeRequest())
    if failed:
        evidence.note_outcome(True)
    page.close()
    context.close()
    return context, page


def test_a_failing_spec_writes_trace_screenshot_console_and_network(
    artifacts: Path,
) -> None:
    browser = FakeBrowser()
    evidence.instrument_browser(browser)

    context, page = _run(browser, failed=True)

    written = sorted(p.name for p in artifacts.iterdir() if p.is_file())
    assert any(name.endswith("-trace.zip") for name in written), written
    assert any(name.endswith("-console.log") for name in written), written
    assert any(name.endswith("-network.log") for name in written), written
    assert any(name.endswith(".png") for name in written), written
    assert any(name.endswith(".webm") for name in written), written

    console = next(artifacts.glob("*-console.log")).read_text(encoding="utf-8")
    # The exact vocabulary matters: the failure this capture exists to explain
    # is a form that never POSTs, and Chromium reports that ONLY on the
    # console. A capture that recorded the network but not the console would
    # have missed it entirely.
    assert "not focusable" in console

    network = next(artifacts.glob("*-network.log")).read_text(encoding="utf-8")
    assert "POST" in network and "/edit" in network

    assert context.tracing.started
    assert context.tracing.stopped_to is not None
    assert page.video.saved_to is not None


def test_a_passing_spec_writes_nothing(artifacts: Path) -> None:
    browser = FakeBrowser()
    evidence.instrument_browser(browser)

    context, page = _run(browser, failed=False)

    assert list(artifacts.iterdir()) == []
    assert context.tracing.started
    assert context.tracing.stopped_to is None
    assert page.video.saved_to is None
    assert page.video.deleted


def test_capture_can_be_switched_off(
    artifacts: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("E2E_CAPTURE_EVIDENCE", "0")
    browser = FakeBrowser()
    evidence.instrument_browser(browser)

    context, _page = _run(browser, failed=True)

    assert list(artifacts.iterdir()) == []
    assert not context.tracing.started


def test_video_recording_is_requested_at_context_creation(artifacts: Path) -> None:
    """A video cannot be turned on after the fact, so the wrapper must inject
    ``record_video_dir`` when the context is built — the one moment where
    forgetting it silently yields no video and no error."""

    browser = FakeBrowser()
    evidence.instrument_browser(browser)
    evidence.start_test("tests/playwright/e2e/test_subscribers.py::test_notes")
    browser.new_context()

    assert "record_video_dir" in browser.context_kwargs


def test_two_contexts_in_one_spec_do_not_overwrite_each_other(
    artifacts: Path,
) -> None:
    browser = FakeBrowser()
    evidence.instrument_browser(browser)

    evidence.start_test("tests/playwright/e2e/test_x.py::test_two_contexts")
    first = browser.new_context()
    first_page = first.new_page()
    second = browser.new_context()
    second_page = second.new_page()
    evidence.note_outcome(True)
    first_page.close()
    first.close()
    second_page.close()
    second.close()

    traces = sorted(p.name for p in artifacts.glob("*-trace.zip"))
    assert len(traces) == 2, traces
