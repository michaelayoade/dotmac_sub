"""Browser acceptance for report-chart rendering and failure fallback."""

from __future__ import annotations

from pathlib import Path

from playwright.sync_api import Page, expect

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _load_chart_runtime(page: Page) -> None:
    page.add_script_tag(path=str(PROJECT_ROOT / "static/js/vendor/echarts.min.js"))
    page.add_script_tag(path=str(PROJECT_ROOT / "static/js/echarts-charts.js"))


def test_report_chart_runtime_creates_a_visible_plot(anon_page: Page) -> None:
    page = anon_page
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.set_content(
        """
        <section data-report-chart-state="present">
            <div class="chart-container" style="width: 640px; min-height: 280px">
                <canvas id="report-chart"></canvas>
            </div>
            <div id="report-chart-error" role="alert" hidden>Chart unavailable.</div>
        </section>
        """
    )
    _load_chart_runtime(page)

    page.evaluate(
        """
        const chart = DotmacCharts.createDoughnutChart(
            document.getElementById('report-chart'),
            { labels: ['Internet', 'Voice'], values: [80, 20] },
            { legendPosition: 'bottom' }
        );
        DotmacCharts.registerChart('report-chart', chart);
        """
    )

    holder = page.locator(".echarts-holder")
    expect(holder).to_be_visible()
    box = holder.bounding_box()
    assert box is not None and box["width"] > 0 and box["height"] >= 260
    expect(page.get_by_role("alert")).to_be_hidden()
    assert errors == []


def test_report_chart_runtime_reveals_fallback_for_failed_chart(
    anon_page: Page,
) -> None:
    page = anon_page
    page.set_content(
        """
        <section data-report-chart-state="present">
            <div class="chart-container" style="width: 640px; min-height: 280px">
                <canvas id="failed-chart"></canvas>
            </div>
            <div id="failed-chart-error" role="alert" hidden>Chart unavailable.</div>
        </section>
        """
    )
    _load_chart_runtime(page)

    page.evaluate("DotmacCharts.registerChart('failed-chart', null)")

    expect(page.get_by_role("alert")).to_be_visible()
    expect(page.locator('[data-report-chart-state="unavailable"]')).to_be_visible()
