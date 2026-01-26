/* Chart registry/initializer for service pages (opt-in via data attributes). */
(() => {
    function parseJson(value) {
        if (!value) return null;
        try {
            return JSON.parse(value);
        } catch (error) {
            console.warn("charts-registry: invalid JSON", error);
            return null;
        }
    }

    function mergeDeep(target, source) {
        if (!source || typeof source !== "object") return target;
        const output = Array.isArray(target) ? [...target] : { ...target };
        Object.keys(source).forEach((key) => {
            const sourceValue = source[key];
            if (sourceValue && typeof sourceValue === "object" && !Array.isArray(sourceValue)) {
                output[key] = mergeDeep(output[key] || {}, sourceValue);
            } else {
                output[key] = sourceValue;
            }
        });
        return output;
    }

    function resolveChartFactory(type) {
        const map = {
            line: "createLineChart",
            bar: "createBarChart",
            area: "createAreaChart",
            doughnut: "createDoughnutChart",
            stackedBar: "createStackedBarChart",
            horizontalBar: "createHorizontalBarChart",
        };
        return map[type] || "createLineChart";
    }

    function buildDatasets(series, xKey, yKey, fallbackLabel) {
        const labels = [];
        const datasets = (series || []).map((item, index) => {
            const points = item.data || [];
            if (labels.length === 0) {
                points.forEach((point) => labels.push(point[xKey]));
            }
            return {
                label: item.label || fallbackLabel || `Series ${index + 1}`,
                data: points.map((point) => point[yKey]),
            };
        });
        return { labels, datasets };
    }

    function getAccessToken() {
        const sessionToken = document.cookie.split("; ").find((row) => row.startsWith("session_token="));
        return sessionStorage.getItem("access_token") || (sessionToken ? sessionToken.split("=")[1] : null);
    }

    async function initializeChart(canvas) {
        const endpoint = canvas.dataset.chartEndpoint;
        const type = canvas.dataset.chart || "line";
        const title = canvas.dataset.chartTitle;
        const xKey = canvas.dataset.chartX || "x";
        const yKey = canvas.dataset.chartY || "y";
        const label = canvas.dataset.chartLabel;
        const customOptions = parseJson(canvas.dataset.chartOptions) || {};

        if (!endpoint) {
            console.warn("charts-registry: missing data-chart-endpoint");
            return;
        }
        if (!window.Chart || !window.DotmacCharts) {
            console.warn("charts-registry: Chart.js not loaded");
            return;
        }

        let payload;
        try {
            const token = getAccessToken();
            const headers = token ? { Authorization: `Bearer ${token}` } : undefined;
            const response = await fetch(endpoint, {
                credentials: "same-origin",
                headers,
            });
            if (!response.ok) {
                throw new Error(`Chart data fetch failed: ${response.status}`);
            }
            payload = await response.json();
        } catch (error) {
            console.error("charts-registry: fetch error", error);
            return;
        }

        const chartData = buildDatasets(payload.series || [], xKey, yKey, label);
        const defaultOptions = window.DotmacCharts.getDefaultOptions(type);
        let options = mergeDeep(defaultOptions, customOptions);
        if (title) {
            options = mergeDeep(options, {
                plugins: {
                    title: { display: true, text: title },
                },
            });
        }

        const factoryName = resolveChartFactory(type);
        const factory = window.DotmacCharts[factoryName];
        if (typeof factory !== "function") {
            console.warn(`charts-registry: missing factory ${factoryName}`);
            return;
        }
        const existing = window.Chart.getChart(canvas);
        if (existing) {
            existing.destroy();
        }
        factory(canvas, chartData, options);
    }

    function initAll() {
        document.querySelectorAll("[data-chart]").forEach((canvas) => {
            initializeChart(canvas);
        });
    }

    document.addEventListener("DOMContentLoaded", initAll);

    window.DotmacChartRegistry = { initAll };
})();
