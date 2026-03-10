/**
 * Bandwidth Chart Component
 *
 * Reusable Alpine.js component for displaying real-time bandwidth charts
 * with SSE updates and time range selection.
 */

// Format bytes per second to human-readable format
function formatBps(bps) {
    if (bps === 0) return '0 bps';
    const units = ['bps', 'Kbps', 'Mbps', 'Gbps', 'Tbps'];
    const i = Math.floor(Math.log(bps) / Math.log(1000));
    const value = bps / Math.pow(1000, i);
    return value.toFixed(value < 10 ? 2 : 1) + ' ' + units[i];
}

// Format bytes to human-readable format
function formatBytes(bytes) {
    if (bytes === 0) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.floor(Math.log(bytes) / Math.log(1024));
    const value = bytes / Math.pow(1024, i);
    return value.toFixed(value < 10 ? 2 : 1) + ' ' + units[i];
}

function parseSsePayload(raw) {
    if (raw == null) return null;
    if (typeof raw === 'object') return raw;
    try {
        return JSON.parse(raw);
    } catch (_e) {
        // Fallback for legacy payloads serialized with single quotes
        try {
            return JSON.parse(String(raw).replace(/'/g, '"'));
        } catch (_e2) {
            return null;
        }
    }
}

// Bandwidth chart Alpine.js component
function bandwidthChart(config = {}) {
    return {
        // Configuration
        subscriptionId: config.subscriptionId || null,
        apiBasePath: config.apiBasePath || '/api/v1/bandwidth',
        useMyEndpoints: config.useMyEndpoints || false, // Use /my/ endpoints for customer portal
        enableLive: config.enableLive !== false,

        // State
        chart: null,
        eventSource: null,
        reconnectTimer: null,
        isDestroyed: false,
        loading: true,
        error: null,

        // Data
        seriesData: [],
        currentRx: 0,
        currentTx: 0,
        peakRx: 0,
        peakTx: 0,
        totalRx: 0,
        totalTx: 0,

        // Time range
        timeRange: '24h',
        timeRanges: [
            { value: '1h', label: '1h' },
            { value: '24h', label: '24h' },
            { value: '7d', label: '7d' },
            { value: '30d', label: '30d' },
        ],

        // Computed
        get currentRxFormatted() { return formatBps(this.currentRx); },
        get currentTxFormatted() { return formatBps(this.currentTx); },
        get peakRxFormatted() { return formatBps(this.peakRx); },
        get peakTxFormatted() { return formatBps(this.peakTx); },
        get totalRxFormatted() { return formatBytes(this.totalRx); },
        get totalTxFormatted() { return formatBytes(this.totalTx); },

        formatTimeLabel(timestamp) {
            const dt = new Date(timestamp);
            if (this.timeRange === '7d' || this.timeRange === '30d') {
                return dt.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
            }
            return dt.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
        },

        // Initialize
        async init() {
            this.isDestroyed = false;
            await this.loadData();
            this.initChart();
            this.connectSSE();
        },

        // Cleanup
        destroy() {
            this.isDestroyed = true;
            if (this.reconnectTimer) {
                clearTimeout(this.reconnectTimer);
                this.reconnectTimer = null;
            }
            if (this.eventSource) {
                this.eventSource.close();
                this.eventSource = null;
            }
            if (this.chart) {
                DotmacCharts.unregisterChart(this.getChartId());
                this.chart = null;
            }
        },

        getChartId() {
            return `bandwidth-chart-${this.subscriptionId || 'my'}`;
        },

        getSeriesEndpoint() {
            if (this.useMyEndpoints) {
                return `${this.apiBasePath}/my/series`;
            }
            return `${this.apiBasePath}/series/${this.subscriptionId}`;
        },

        getStatsEndpoint() {
            if (this.useMyEndpoints) {
                return `${this.apiBasePath}/my/stats`;
            }
            return `${this.apiBasePath}/stats/${this.subscriptionId}`;
        },

        getLiveEndpoint() {
            if (this.useMyEndpoints) {
                return `${this.apiBasePath}/my/live`;
            }
            return `${this.apiBasePath}/live/${this.subscriptionId}`;
        },

        // Load historical data
        async loadData() {
            this.loading = true;
            this.error = null;

            try {
                // Calculate time range
                const end = new Date();
                let start;
                switch (this.timeRange) {
                    case '1h': start = new Date(end - 60 * 60 * 1000); break;
                    case '24h': start = new Date(end - 24 * 60 * 60 * 1000); break;
                    case '7d': start = new Date(end - 7 * 24 * 60 * 60 * 1000); break;
                    case '30d': start = new Date(end - 30 * 24 * 60 * 60 * 1000); break;
                    default: start = new Date(end - 24 * 60 * 60 * 1000);
                }

                // Fetch series data
                const seriesUrl = new URL(this.getSeriesEndpoint(), window.location.origin);
                seriesUrl.searchParams.set('start_at', start.toISOString());
                seriesUrl.searchParams.set('end_at', end.toISOString());

                const seriesResponse = await fetch(seriesUrl);
                if (!seriesResponse.ok) throw new Error('Failed to load bandwidth data');
                const seriesResult = await seriesResponse.json();
                this.seriesData = seriesResult.data || [];

                // Fetch stats
                const statsUrl = new URL(this.getStatsEndpoint(), window.location.origin);
                statsUrl.searchParams.set('period', this.timeRange);

                const statsResponse = await fetch(statsUrl);
                if (statsResponse.ok) {
                    const stats = await statsResponse.json();
                    this.currentRx = stats.current_rx_bps || 0;
                    this.currentTx = stats.current_tx_bps || 0;
                    this.peakRx = stats.peak_rx_bps || 0;
                    this.peakTx = stats.peak_tx_bps || 0;
                    this.totalRx = stats.total_rx_bytes || 0;
                    this.totalTx = stats.total_tx_bytes || 0;
                }

                this.loading = false;
            } catch (e) {
                console.error('Error loading bandwidth data:', e);
                this.error = e.message;
                this.loading = false;
            }
        },

        // Initialize Chart.js chart
        initChart() {
            const canvas = this.$refs.canvas;
            if (!canvas) return;

            const ctx = canvas.getContext('2d');
            if (!ctx) return;
            const existing = window.Chart && window.Chart.getChart ? window.Chart.getChart(canvas) : null;
            if (existing && !existing._destroyed) {
                try {
                    existing.destroy();
                } catch (_e) {
                    // Ignore stale instance errors
                }
            }

            // Prepare data
            const labels = this.seriesData.map(d => this.formatTimeLabel(d.timestamp));
            const rxData = this.seriesData.map(d => d.rx_bps / 1000000); // Convert to Mbps
            const txData = this.seriesData.map(d => d.tx_bps / 1000000);
            const sparseSeries = labels.length <= 1;

            const data = {
                labels: labels,
                datasets: [
                    {
                        label: 'Download',
                        data: rxData,
                        color: DotmacCharts.colors.accent[500],
                        fillColor: DotmacCharts.colors.accent[500] + '40',
                        fill: true,
                        pointRadius: sparseSeries ? 3 : 0,
                    },
                    {
                        label: 'Upload',
                        data: txData,
                        color: DotmacCharts.colors.primary[500],
                        fillColor: DotmacCharts.colors.primary[500] + '40',
                        fill: true,
                        pointRadius: sparseSeries ? 3 : 0,
                    },
                ],
            };

            const options = {
                scales: {
                    x: {
                        grid: {
                            display: false,
                        },
                    },
                    y: {
                        beginAtZero: true,
                        ticks: {
                            callback: (value) => value + ' Mbps',
                        },
                    },
                },
                plugins: {
                    legend: {
                        display: true,
                        position: 'top',
                    },
                    tooltip: {
                        callbacks: {
                            label: (context) => {
                                return context.dataset.label + ': ' + formatBps(context.raw * 1000000);
                            },
                        },
                    },
                },
                interaction: {
                    intersect: false,
                    mode: 'index',
                },
            };

            // Destroy existing chart if any
            if (this.chart) {
                this.chart.destroy();
                this.chart = null;
            }

            this.chart = DotmacCharts.createAreaChart(ctx, data, options);
            DotmacCharts.registerChart(this.getChartId(), this.chart);
        },

        // Connect to SSE for real-time updates
        connectSSE() {
            if (this.isDestroyed) return;
            if (!this.enableLive) return;
            if (this.eventSource) {
                this.eventSource.close();
                this.eventSource = null;
            }

            // Keep live streaming only for 1h to avoid excessive chart updates.
            if (this.timeRange !== '1h') {
                return;
            }

            try {
                const source = new EventSource(this.getLiveEndpoint());
                this.eventSource = source;

                source.addEventListener('bandwidth', (event) => {
                    if (this.isDestroyed || this.eventSource !== source) return;
                    const data = parseSsePayload(event.data);
                    if (!data) return;
                    this.currentRx = data.rx_bps || 0;
                    this.currentTx = data.tx_bps || 0;

                    // Update peak if necessary
                    if (this.currentRx > this.peakRx) this.peakRx = this.currentRx;
                    if (this.currentTx > this.peakTx) this.peakTx = this.currentTx;

                    // Add new point to chart
                    if (this.chart && this.chart.data.labels) {
                        if (!this.chart.canvas || !this.chart.ctx) {
                            return;
                        }
                        if (!this.chart.data.datasets || this.chart.data.datasets.length < 2) {
                            return;
                        }
                        const now = new Date();
                        this.chart.data.labels.push(this.formatTimeLabel(now));
                        this.chart.data.datasets[0].data.push(this.currentRx / 1000000);
                        this.chart.data.datasets[1].data.push(this.currentTx / 1000000);

                        // Keep chart lightweight to avoid client-side rendering loops.
                        const maxPoints = 900;
                        while (this.chart.data.labels.length > maxPoints) {
                            this.chart.data.labels.shift();
                            this.chart.data.datasets[0].data.shift();
                            this.chart.data.datasets[1].data.shift();
                        }

                        try {
                            this.chart.update('none'); // Update without animation
                        } catch (e) {
                            console.warn('Bandwidth chart update skipped:', e);
                        }
                    }
                });

                source.addEventListener('error', (event) => {
                    console.error('SSE error:', event);
                    if (this.eventSource === source) {
                        source.close();
                        this.eventSource = null;
                    }
                    if (this.reconnectTimer) {
                        clearTimeout(this.reconnectTimer);
                    }
                    this.reconnectTimer = setTimeout(() => {
                        this.connectSSE();
                    }, 5000);
                });

            } catch (e) {
                console.error('Failed to connect SSE:', e);
            }
        },

        // Handle time range change
        async setTimeRange(range) {
            this.timeRange = range;
            await this.loadData();
            this.initChart();

            // Reconnect SSE if needed
            this.connectSSE();
        },
    };
}

// Mini bandwidth widget for dashboard
function bandwidthWidget(config = {}) {
    return {
        subscriptionId: config.subscriptionId || null,
        apiBasePath: config.apiBasePath || '/api/v1/bandwidth',
        useMyEndpoints: config.useMyEndpoints || false,

        currentRx: 0,
        currentTx: 0,
        loading: true,
        eventSource: null,
        reconnectTimer: null,
        isDestroyed: false,

        get currentRxFormatted() { return formatBps(this.currentRx); },
        get currentTxFormatted() { return formatBps(this.currentTx); },

        async init() {
            this.isDestroyed = false;
            await this.loadCurrent();
            this.connectSSE();
        },

        destroy() {
            this.isDestroyed = true;
            if (this.reconnectTimer) {
                clearTimeout(this.reconnectTimer);
                this.reconnectTimer = null;
            }
            if (this.eventSource) {
                this.eventSource.close();
                this.eventSource = null;
            }
        },

        getLiveEndpoint() {
            if (this.useMyEndpoints) {
                return `${this.apiBasePath}/my/live`;
            }
            return `${this.apiBasePath}/live/${this.subscriptionId}`;
        },

        async loadCurrent() {
            try {
                const endpoint = this.useMyEndpoints
                    ? `${this.apiBasePath}/my/stats?period=1h`
                    : `${this.apiBasePath}/stats/${this.subscriptionId}?period=1h`;

                const response = await fetch(endpoint);
                if (response.ok) {
                    const stats = await response.json();
                    this.currentRx = stats.current_rx_bps || 0;
                    this.currentTx = stats.current_tx_bps || 0;
                }
                this.loading = false;
            } catch (e) {
                console.error('Error loading bandwidth:', e);
                this.loading = false;
            }
        },

        connectSSE() {
            if (this.isDestroyed) return;
            try {
                if (this.eventSource) {
                    this.eventSource.close();
                    this.eventSource = null;
                }
                const source = new EventSource(this.getLiveEndpoint());
                this.eventSource = source;

                source.addEventListener('bandwidth', (event) => {
                    if (this.isDestroyed || this.eventSource !== source) return;
                    const data = parseSsePayload(event.data);
                    if (!data) return;
                    this.currentRx = data.rx_bps || 0;
                    this.currentTx = data.tx_bps || 0;
                });

                source.addEventListener('error', () => {
                    if (this.eventSource === source) {
                        source.close();
                        this.eventSource = null;
                    }
                    if (this.reconnectTimer) {
                        clearTimeout(this.reconnectTimer);
                    }
                    this.reconnectTimer = setTimeout(() => this.connectSSE(), 5000);
                });
            } catch (e) {
                console.error('SSE connection failed:', e);
            }
        },
    };
}

// Export
window.BandwidthChart = {
    formatBps,
    formatBytes,
    bandwidthChart,
    bandwidthWidget,
};
