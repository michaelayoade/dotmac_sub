/**
 * Dotmac SM Chart Utilities — Apache ECharts implementation.
 *
 * Drop-in replacement for charts.js (Chart.js): exposes the same
 * `window.DotmacCharts` API (same function names + data/option contracts) so a
 * template migrates by swapping only its two <script src> lines
 * (chart.min.js -> echarts.min.js, charts.js -> echarts-charts.js).
 *
 * Key difference handled here: Chart.js draws on a <canvas>; ECharts renders
 * into a sized block element. Callers pass a <canvas> (or its 2d context) as
 * `ctx`; `_container()` swaps in a sibling <div> and hides the canvas, so
 * calling code is unchanged. A minimal `window.Chart.getChart` shim keeps the
 * registry + usage-records panel working.
 */

const ChartColors = {
    primary: {
        50: '#edf3eb', 100: '#dbe7d7', 200: '#b8cfb0', 300: '#90b483', 400: '#5a9147',
        500: '#367920', 600: '#206a07', 700: '#1a5706', 800: '#154605', 900: '#103504',
    },
    accent: {
        50: '#ecfeff', 100: '#cffafe', 200: '#a5f3fc', 300: '#67e8f9', 400: '#22d3ee',
        500: '#06b6d4', 600: '#0891b2', 700: '#0e7490', 800: '#155e75', 900: '#164e63',
    },
    success: '#22c55e', warning: '#f59e0b', danger: '#ef4444', info: '#3b82f6',
    slate: {
        50: '#f8fafc', 100: '#f1f5f9', 200: '#e2e8f0', 300: '#cbd5e1', 400: '#94a3b8',
        500: '#64748b', 600: '#475569', 700: '#334155', 800: '#1e293b', 900: '#0f172a',
    },
};

const CATEGORICAL = [
    ChartColors.primary[500], ChartColors.accent[500], ChartColors.success,
    ChartColors.warning, ChartColors.info, ChartColors.danger,
    ChartColors.primary[300], ChartColors.accent[700],
];

function isDarkMode() {
    return document.documentElement.classList.contains('dark');
}

function getThemeColors() {
    const dark = isDarkMode();
    return {
        text: dark ? ChartColors.slate[300] : ChartColors.slate[700],
        textMuted: dark ? ChartColors.slate[500] : ChartColors.slate[400],
        grid: dark ? ChartColors.slate[700] : ChartColors.slate[200],
        border: dark ? ChartColors.slate[700] : ChartColors.slate[200],
        background: dark ? ChartColors.slate[800] : '#ffffff',
    };
}

// Kept for API/registry compatibility (createUsageRecordsPanel + the data-attr
// registry read it). ECharts factories below build their own options, so the
// Chart.js-shaped keys here are informational only.
function getDefaultOptions(type = 'line') {
    return { responsive: true, maintainAspectRatio: false, _type: type };
}

const FONT = "'Plus Jakarta Sans', sans-serif";

function _tooltip(theme) {
    return {
        backgroundColor: theme.background,
        borderColor: theme.border,
        borderWidth: 1,
        textStyle: { color: theme.text, fontFamily: FONT, fontSize: 13 },
        padding: 12,
        extraCssText: 'border-radius:8px;',
    };
}

function _legend(theme, position) {
    const base = { textStyle: { color: theme.text, fontFamily: FONT, fontSize: 12 }, icon: 'circle', itemGap: 16 };
    if (position === 'bottom') return { ...base, bottom: 0, left: 'center' };
    if (position === 'right') return { ...base, orient: 'vertical', right: 0, top: 'middle' };
    if (position === 'left') return { ...base, orient: 'vertical', left: 0, top: 'middle' };
    return { ...base, top: 0, right: 0 };
}

function _catAxis(theme, labels) {
    return {
        type: 'category', data: labels || [], boundaryGap: true,
        axisLine: { lineStyle: { color: theme.grid } },
        axisTick: { show: false },
        axisLabel: { color: theme.textMuted, fontFamily: FONT, fontSize: 11 },
        splitLine: { show: false },
    };
}

function _valAxis(theme) {
    return {
        type: 'value',
        axisLine: { show: false },
        axisLabel: { color: theme.textMuted, fontFamily: FONT, fontSize: 11 },
        splitLine: { lineStyle: { color: theme.grid } },
    };
}

// Resolve a caller-supplied canvas / 2d-context / element into a sized <div>
// ECharts can render into. Idempotent: reuses the div on repeat calls.
function _container(ctx) {
    let el = ctx && ctx.canvas ? ctx.canvas : ctx;
    if (typeof el === 'string') el = document.getElementById(el);
    if (!el) return null;
    if (el.tagName === 'CANVAS') {
        if (el._echartsDiv) return el._echartsDiv;
        const div = document.createElement('div');
        div.className = 'echarts-holder';
        const parent = el.parentElement;
        const h = el.clientHeight || (parent && parent.clientHeight) || 260;
        div.style.width = '100%';
        div.style.height = (h > 40 ? h : 260) + 'px';
        el.style.display = 'none';
        if (parent) parent.insertBefore(div, el);
        el._echartsDiv = div;
        div._sourceCanvas = el;
        return div;
    }
    return el;
}

let _resizeBound = false;
function _bindResize() {
    if (_resizeBound) return;
    _resizeBound = true;
    window.addEventListener('resize', () => {
        chartRegistry.forEach((chart) => {
            if (chart && !chart._destroyed && chart._echarts) {
                try { chart._echarts.resize(); } catch (_e) { /* detached */ }
            }
        });
    });
}

// Wrap an ECharts instance with a Chart.js-ish surface: .destroy(), ._destroyed,
// .canvas/.ctx (isChartUsable checks these), .update(), .applyTheme(), .setData().
function _makeChart(container, buildOption) {
    if (!container || typeof echarts === 'undefined') return null;
    const existing = echarts.getInstanceByDom(container);
    if (existing) existing.dispose();
    const instance = echarts.init(container);
    const wrapper = {
        _echarts: instance,
        _destroyed: false,
        canvas: container,
        ctx: container,
        _build: buildOption,
        applyTheme() {
            if (this._destroyed) return;
            instance.setOption(this._build(getThemeColors()), { notMerge: true });
        },
        update() { this.applyTheme(); },
        setData(build) { if (build) this._build = build; this.applyTheme(); },
        resize() { if (!this._destroyed) instance.resize(); },
        destroy() {
            if (this._destroyed) return;
            this._destroyed = true;
            try { instance.dispose(); } catch (_e) { /* already disposed */ }
            if (container._sourceCanvas) {
                container._sourceCanvas.style.display = '';
                container._sourceCanvas._echartsDiv = null;
            }
            if (container.parentElement && container._sourceCanvas) {
                container.parentElement.removeChild(container);
            }
        },
    };
    instance.setOption(buildOption(getThemeColors()));
    container._dotmacChart = wrapper;
    _bindResize();
    return wrapper;
}

function _lineSeries(data, theme, area) {
    return (data.datasets || []).map((ds, i) => {
        const color = ds.color || CATEGORICAL[i % CATEGORICAL.length];
        const fill = area || ds.fill !== false;
        return {
            name: ds.label || `Dataset ${i + 1}`,
            type: 'line', smooth: true, showSymbol: false,
            lineStyle: { width: 2, color },
            itemStyle: { color },
            areaStyle: fill ? { color: (ds.fillColor || color), opacity: 0.13 } : undefined,
            data: ds.data || [],
        };
    });
}

function createLineChart(ctx, data, options = {}) {
    const container = _container(ctx);
    return _makeChart(container, (theme) => ({
        color: CATEGORICAL,
        grid: { left: 8, right: 16, top: 32, bottom: 8, containLabel: true },
        tooltip: { trigger: 'axis', ..._tooltip(theme) },
        legend: (options.legend && options.legend.display === false) ? { show: false } : _legend(theme, 'top'),
        xAxis: _catAxis(theme, data.labels),
        yAxis: _valAxis(theme),
        series: _lineSeries(data, theme, false),
    }));
}

function createAreaChart(ctx, data, options = {}) {
    const container = _container(ctx);
    return _makeChart(container, (theme) => ({
        color: CATEGORICAL,
        grid: { left: 8, right: 16, top: 32, bottom: 8, containLabel: true },
        tooltip: { trigger: 'axis', ..._tooltip(theme) },
        legend: (options.legend && options.legend.display === false) ? { show: false } : _legend(theme, 'top'),
        xAxis: _catAxis(theme, data.labels),
        yAxis: _valAxis(theme),
        series: _lineSeries(data, theme, true),
    }));
}

function _barSeries(data, opts) {
    const stacked = !!opts.stacked;
    return (data.datasets || []).map((ds, i) => {
        const palette = ds.colors || CATEGORICAL;
        return {
            name: ds.label || `Dataset ${i + 1}`,
            type: 'bar',
            stack: stacked ? 'total' : undefined,
            itemStyle: {
                borderRadius: opts.horizontal ? [0, 6, 6, 0] : [6, 6, 0, 0],
                color: Array.isArray(ds.colors)
                    ? (p) => palette[p.dataIndex % palette.length]
                    : (ds.backgroundColor || CATEGORICAL[i % CATEGORICAL.length]),
            },
            barMaxWidth: ds.maxBarThickness || 40,
            data: ds.data || [],
        };
    });
}

function createBarChart(ctx, data, options = {}) {
    const container = _container(ctx);
    const horizontal = options.indexAxis === 'y' || options.horizontal;
    const stacked = !!options.stacked;
    return _makeChart(container, (theme) => {
        const cat = _catAxis(theme, data.labels);
        const val = _valAxis(theme);
        return {
            color: CATEGORICAL,
            grid: { left: 8, right: 16, top: 32, bottom: 8, containLabel: true },
            tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' }, ..._tooltip(theme) },
            legend: (options.legend && options.legend.display === false) ? { show: false } : _legend(theme, 'top'),
            xAxis: horizontal ? val : cat,
            yAxis: horizontal ? cat : val,
            series: _barSeries(data, { stacked, horizontal }),
        };
    });
}

function createHorizontalBarChart(ctx, data, options = {}) {
    return createBarChart(ctx, data, { horizontal: true, ...options });
}

function createStackedBarChart(ctx, data, options = {}) {
    return createBarChart(ctx, data, { stacked: true, ...options });
}

function createDoughnutChart(ctx, data, options = {}) {
    const container = _container(ctx);
    const pie = !!options.pie;
    const inner = pie ? '0%' : (typeof options.cutout === 'string' ? options.cutout : '62%');
    const colors = data.colors || CATEGORICAL;
    const items = (data.labels || []).map((name, i) => ({
        name, value: (data.values || [])[i] || 0,
        itemStyle: { color: colors[i % colors.length] },
    }));
    return _makeChart(container, (theme) => ({
        tooltip: { trigger: 'item', ..._tooltip(theme) },
        legend: _legend(theme, options.legendPosition || 'right'),
        series: [{
            type: 'pie',
            radius: [inner, '78%'],
            center: options.legendPosition === 'bottom' ? ['50%', '44%'] : ['40%', '50%'],
            avoidLabelOverlap: true,
            itemStyle: { borderColor: theme.background, borderWidth: 2 },
            label: { show: false },
            data: items,
        }],
    }));
}

function createSparkline(ctx, data, color = ChartColors.primary[500]) {
    const container = _container(ctx);
    return _makeChart(container, () => ({
        grid: { left: 0, right: 0, top: 2, bottom: 2 },
        xAxis: { type: 'category', show: false, data: (data || []).map((_, i) => i), boundaryGap: false },
        yAxis: { type: 'value', show: false },
        tooltip: { show: false },
        series: [{
            type: 'line', smooth: true, showSymbol: false,
            lineStyle: { width: 1.5, color, cap: 'round' },
            areaStyle: { color, opacity: 0.13 },
            data: data || [],
        }],
    }));
}

// --- registry (theme refresh + lifecycle), matching charts.js semantics ------
const chartRegistry = new Map();

function isChartUsable(chart) {
    return Boolean(chart && !chart._destroyed && chart.canvas && chart.ctx);
}

function registerChart(id, chart) {
    const existing = chartRegistry.get(id);
    if (existing && existing !== chart) {
        try { if (!existing._destroyed) existing.destroy(); } catch (_e) { /* stale */ }
    }
    chartRegistry.set(id, chart);
}

function unregisterChart(id) {
    const chart = chartRegistry.get(id);
    if (chart) {
        try { if (!chart._destroyed) chart.destroy(); } catch (_e) { /* stale */ }
        chartRegistry.delete(id);
    }
}

function updateChartTheme(chart) {
    if (chart && typeof chart.applyTheme === 'function') chart.applyTheme();
}

const observer = new MutationObserver((mutations) => {
    mutations.forEach((mutation) => {
        if (mutation.attributeName === 'class') {
            chartRegistry.forEach((chart, id) => {
                if (!isChartUsable(chart)) { chartRegistry.delete(id); return; }
                try { updateChartTheme(chart); } catch (_e) { chartRegistry.delete(id); }
            });
        }
    });
});

document.addEventListener('DOMContentLoaded', () => {
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ['class'] });
});

// Alpine usage-records panel — ported from charts.js; uses the factories above
// and the window.Chart.getChart shim. Chart.js-shaped options passed through are
// tolerated (the factories read only their own keys).
function createUsageRecordsPanel(config = {}) {
    return {
        recordsView: config.defaultView === 'table' ? 'table' : 'chart',
        chartRecords: Array.isArray(config.chartRecords) ? config.chartRecords : [],
        chartId: config.chartId || 'usage-records-chart',
        chartLabel: config.chartLabel || 'Usage (GB)',
        init() { if (this.recordsView === 'chart') this.$nextTick(() => this.renderChart()); },
        setRecordsView(view) {
            if (view !== 'chart' && view !== 'table') return;
            this.recordsView = view;
            if (view === 'chart') this.$nextTick(() => this.renderChart());
        },
        renderChart() {
            if (!this.$refs.recordsChartCanvas || !window.DotmacCharts) return;
            const existing = window.Chart && window.Chart.getChart(this.$refs.recordsChartCanvas);
            if (existing) existing.destroy();
            if (!this.chartRecords.length) return;
            const split = this.chartRecords.some((r) => r.download_value !== undefined || r.upload_value !== undefined);
            const chart = split
                ? createStackedBarChart(this.$refs.recordsChartCanvas, {
                    labels: this.chartRecords.map((r) => r.label),
                    datasets: [
                        { label: 'Download', data: this.chartRecords.map((r) => Number(r.download_value || 0)), backgroundColor: ChartColors.primary[500], maxBarThickness: 28 },
                        { label: 'Upload', data: this.chartRecords.map((r) => Number(r.upload_value || 0)), backgroundColor: ChartColors.accent[500], maxBarThickness: 28 },
                    ],
                })
                : createBarChart(this.$refs.recordsChartCanvas, {
                    labels: this.chartRecords.map((r) => r.label),
                    datasets: [{ label: this.chartLabel, data: this.chartRecords.map((r) => Number(r.value || 0)), backgroundColor: ChartColors.accent[500], maxBarThickness: 28 }],
                }, { legend: { display: false } });
            registerChart(this.chartId, chart);
        },
    };
}

// Minimal Chart.js compat shim so registry/panel code that calls
// `window.Chart.getChart(el)` keeps working against ECharts wrappers.
if (typeof window.Chart === 'undefined') {
    window.Chart = {
        getChart(el) {
            const c = _container(el);
            return c ? c._dotmacChart : undefined;
        },
    };
}

window.DotmacCharts = {
    colors: ChartColors,
    isDarkMode, getThemeColors, getDefaultOptions,
    createLineChart, createBarChart, createHorizontalBarChart, createDoughnutChart,
    createStackedBarChart, createSparkline, createAreaChart, createUsageRecordsPanel,
    registerChart, unregisterChart, updateChartTheme,
};

// Data-attribute auto-init registry (ported from charts.js), ECharts-backed.
if (!window.DotmacChartRegistry) {
    (() => {
        function parseJson(value) {
            if (!value) return null;
            try { return JSON.parse(value); } catch (error) { console.warn('charts-registry: invalid JSON', error); return null; }
        }
        function resolveChartFactory(type) {
            const map = {
                line: 'createLineChart', bar: 'createBarChart', area: 'createAreaChart',
                doughnut: 'createDoughnutChart', stackedBar: 'createStackedBarChart', horizontalBar: 'createHorizontalBarChart',
            };
            return map[type] || 'createLineChart';
        }
        function buildDatasets(series, xKey, yKey, fallbackLabel) {
            const labels = [];
            const datasets = (series || []).map((item, index) => {
                const points = item.data || [];
                if (labels.length === 0) points.forEach((point) => labels.push(point[xKey]));
                return { label: item.label || fallbackLabel || `Series ${index + 1}`, data: points.map((point) => point[yKey]) };
            });
            return { labels, datasets };
        }
        function getAccessToken() {
            const sessionToken = document.cookie.split('; ').find((row) => row.startsWith('session_token='));
            return sessionStorage.getItem('access_token') || (sessionToken ? sessionToken.split('=')[1] : null);
        }
        async function initializeChart(canvas) {
            const endpoint = canvas.dataset.chartEndpoint;
            const type = canvas.dataset.chart || 'line';
            const xKey = canvas.dataset.chartX || 'x';
            const yKey = canvas.dataset.chartY || 'y';
            const label = canvas.dataset.chartLabel;
            const customOptions = parseJson(canvas.dataset.chartOptions) || {};
            if (!endpoint || !window.DotmacCharts) return;
            let payload;
            try {
                const token = getAccessToken();
                const headers = token ? { Authorization: `Bearer ${token}` } : undefined;
                const response = await fetch(endpoint, { credentials: 'same-origin', headers });
                if (!response.ok) throw new Error(`Chart data fetch failed: ${response.status}`);
                payload = await response.json();
            } catch (error) { console.error('charts-registry: fetch error', error); return; }
            const chartData = buildDatasets(payload.series || [], xKey, yKey, label);
            const factory = window.DotmacCharts[resolveChartFactory(type)];
            if (typeof factory !== 'function') return;
            const existing = window.Chart && window.Chart.getChart(canvas);
            if (existing) existing.destroy();
            factory(canvas, chartData, customOptions);
        }
        function initAll() {
            document.querySelectorAll('[data-chart]').forEach((canvas) => initializeChart(canvas));
        }
        document.addEventListener('DOMContentLoaded', initAll);
        window.DotmacChartRegistry = { initAll };
    })();
}
