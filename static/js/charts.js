/**
 * Dotmac SM Chart Utilities
 * Chart.js helper functions with dark mode support
 */

// Theme colors matching Tailwind config
const ChartColors = {
    primary: {
        50: '#ecfeff',
        100: '#cffafe',
        200: '#a5f3fc',
        300: '#67e8f9',
        400: '#22d3ee',
        500: '#06b6d4',
        600: '#0891b2',
        700: '#0e7490',
        800: '#155e75',
        900: '#164e63',
    },
    accent: {
        50: '#fff7ed',
        100: '#ffedd5',
        200: '#fed7aa',
        300: '#fdba74',
        400: '#fb923c',
        500: '#f97316',
        600: '#ea580c',
        700: '#c2410c',
        800: '#9a3412',
        900: '#7c2d12',
    },
    success: '#22c55e',
    warning: '#f59e0b',
    danger: '#ef4444',
    info: '#3b82f6',
    slate: {
        50: '#f8fafc',
        100: '#f1f5f9',
        200: '#e2e8f0',
        300: '#cbd5e1',
        400: '#94a3b8',
        500: '#64748b',
        600: '#475569',
        700: '#334155',
        800: '#1e293b',
        900: '#0f172a',
    }
};

// Detect dark mode
function isDarkMode() {
    return document.documentElement.classList.contains('dark');
}

// Get appropriate colors based on theme
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

// Default chart options
function getDefaultOptions(type = 'line') {
    const theme = getThemeColors();

    const baseOptions = {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
            legend: {
                display: true,
                position: 'top',
                align: 'end',
                labels: {
                    color: theme.text,
                    font: {
                        family: "'Plus Jakarta Sans', sans-serif",
                        size: 12,
                    },
                    usePointStyle: true,
                    pointStyle: 'circle',
                    padding: 16,
                }
            },
            tooltip: {
                backgroundColor: theme.background,
                titleColor: theme.text,
                bodyColor: theme.text,
                borderColor: theme.border,
                borderWidth: 1,
                cornerRadius: 8,
                padding: 12,
                titleFont: {
                    family: "'Outfit', sans-serif",
                    size: 14,
                    weight: 600,
                },
                bodyFont: {
                    family: "'Plus Jakarta Sans', sans-serif",
                    size: 13,
                },
                displayColors: true,
                boxPadding: 4,
            }
        },
    };

    // Add scales for charts that need them
    if (['line', 'bar', 'area'].includes(type)) {
        baseOptions.scales = {
            x: {
                grid: {
                    color: theme.grid,
                    drawBorder: false,
                },
                ticks: {
                    color: theme.textMuted,
                    font: {
                        family: "'Plus Jakarta Sans', sans-serif",
                        size: 11,
                    },
                },
            },
            y: {
                grid: {
                    color: theme.grid,
                    drawBorder: false,
                },
                ticks: {
                    color: theme.textMuted,
                    font: {
                        family: "'Plus Jakarta Sans', sans-serif",
                        size: 11,
                    },
                },
                beginAtZero: true,
            },
        };
    }

    return baseOptions;
}

// Create a line chart
function createLineChart(ctx, data, options = {}) {
    const theme = getThemeColors();
    const defaultData = {
        labels: data.labels || [],
        datasets: (data.datasets || []).map((dataset, index) => ({
            label: dataset.label || `Dataset ${index + 1}`,
            data: dataset.data || [],
            borderColor: dataset.color || ChartColors.primary[500],
            backgroundColor: dataset.fillColor || `${dataset.color || ChartColors.primary[500]}20`,
            borderWidth: 2,
            fill: dataset.fill !== false,
            tension: 0.4,
            pointRadius: 0,
            pointHoverRadius: 6,
            pointHoverBackgroundColor: dataset.color || ChartColors.primary[500],
            pointHoverBorderColor: theme.background,
            pointHoverBorderWidth: 2,
            ...dataset,
        })),
    };

    return new Chart(ctx, {
        type: 'line',
        data: defaultData,
        options: {
            ...getDefaultOptions('line'),
            ...options,
        },
    });
}

// Create a bar chart
function createBarChart(ctx, data, options = {}) {
    const defaultData = {
        labels: data.labels || [],
        datasets: (data.datasets || []).map((dataset, index) => ({
            label: dataset.label || `Dataset ${index + 1}`,
            data: dataset.data || [],
            backgroundColor: dataset.colors || [
                ChartColors.primary[500],
                ChartColors.accent[500],
                ChartColors.success,
                ChartColors.warning,
                ChartColors.info,
            ],
            borderRadius: 6,
            borderSkipped: false,
            ...dataset,
        })),
    };

    return new Chart(ctx, {
        type: 'bar',
        data: defaultData,
        options: {
            ...getDefaultOptions('bar'),
            ...options,
        },
    });
}

// Create a horizontal bar chart
function createHorizontalBarChart(ctx, data, options = {}) {
    return createBarChart(ctx, data, {
        indexAxis: 'y',
        ...options,
    });
}

// Create a doughnut/pie chart
function createDoughnutChart(ctx, data, options = {}) {
    const theme = getThemeColors();
    const defaultData = {
        labels: data.labels || [],
        datasets: [{
            data: data.values || [],
            backgroundColor: data.colors || [
                ChartColors.primary[500],
                ChartColors.accent[500],
                ChartColors.success,
                ChartColors.warning,
                ChartColors.danger,
                ChartColors.info,
            ],
            borderWidth: 0,
            hoverOffset: 4,
        }],
    };

    return new Chart(ctx, {
        type: options.pie ? 'pie' : 'doughnut',
        data: defaultData,
        options: {
            responsive: true,
            maintainAspectRatio: false,
            cutout: options.pie ? 0 : '70%',
            plugins: {
                legend: {
                    display: true,
                    position: options.legendPosition || 'right',
                    labels: {
                        color: theme.text,
                        font: {
                            family: "'Plus Jakarta Sans', sans-serif",
                            size: 12,
                        },
                        usePointStyle: true,
                        pointStyle: 'circle',
                        padding: 16,
                    }
                },
                tooltip: {
                    backgroundColor: theme.background,
                    titleColor: theme.text,
                    bodyColor: theme.text,
                    borderColor: theme.border,
                    borderWidth: 1,
                    cornerRadius: 8,
                    padding: 12,
                }
            },
            ...options,
        },
    });
}

// Create a stacked bar chart
function createStackedBarChart(ctx, data, options = {}) {
    return createBarChart(ctx, data, {
        scales: {
            x: {
                stacked: true,
                ...getDefaultOptions('bar').scales?.x,
            },
            y: {
                stacked: true,
                ...getDefaultOptions('bar').scales?.y,
            },
        },
        ...options,
    });
}

// Create a sparkline (mini line chart)
function createSparkline(ctx, data, color = ChartColors.primary[500]) {
    return new Chart(ctx, {
        type: 'line',
        data: {
            labels: data.map((_, i) => i),
            datasets: [{
                data: data,
                borderColor: color,
                backgroundColor: `${color}20`,
                borderWidth: 1.5,
                fill: true,
                tension: 0.4,
                pointRadius: 0,
            }],
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: { display: false },
                tooltip: { enabled: false },
            },
            scales: {
                x: { display: false },
                y: { display: false },
            },
            elements: {
                line: {
                    borderCapStyle: 'round',
                },
            },
        },
    });
}

// Create an area chart
function createAreaChart(ctx, data, options = {}) {
    return createLineChart(ctx, {
        ...data,
        datasets: (data.datasets || []).map(ds => ({
            ...ds,
            fill: true,
        })),
    }, options);
}

// Update chart colors on theme change
function updateChartTheme(chart) {
    const theme = getThemeColors();

    if (chart.options.scales) {
        if (chart.options.scales.x) {
            chart.options.scales.x.grid.color = theme.grid;
            chart.options.scales.x.ticks.color = theme.textMuted;
        }
        if (chart.options.scales.y) {
            chart.options.scales.y.grid.color = theme.grid;
            chart.options.scales.y.ticks.color = theme.textMuted;
        }
    }

    if (chart.options.plugins.legend) {
        chart.options.plugins.legend.labels.color = theme.text;
    }

    if (chart.options.plugins.tooltip) {
        chart.options.plugins.tooltip.backgroundColor = theme.background;
        chart.options.plugins.tooltip.titleColor = theme.text;
        chart.options.plugins.tooltip.bodyColor = theme.text;
        chart.options.plugins.tooltip.borderColor = theme.border;
    }

    chart.update();
}

// Global chart registry for theme updates
const chartRegistry = new Map();

function registerChart(id, chart) {
    chartRegistry.set(id, chart);
}

function unregisterChart(id) {
    const chart = chartRegistry.get(id);
    if (chart) {
        chart.destroy();
        chartRegistry.delete(id);
    }
}

// Listen for dark mode changes
const observer = new MutationObserver((mutations) => {
    mutations.forEach((mutation) => {
        if (mutation.attributeName === 'class') {
            chartRegistry.forEach((chart) => {
                updateChartTheme(chart);
            });
        }
    });
});

// Start observing when DOM is ready
document.addEventListener('DOMContentLoaded', () => {
    observer.observe(document.documentElement, {
        attributes: true,
        attributeFilter: ['class'],
    });
});

// Export for use
window.DotmacCharts = {
    colors: ChartColors,
    isDarkMode,
    getThemeColors,
    getDefaultOptions,
    createLineChart,
    createBarChart,
    createHorizontalBarChart,
    createDoughnutChart,
    createStackedBarChart,
    createSparkline,
    createAreaChart,
    registerChart,
    unregisterChart,
    updateChartTheme,
};

if (!window.DotmacChartRegistry) {
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
}
