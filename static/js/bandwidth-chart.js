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

// Bandwidth chart Alpine.js component
function bandwidthChart(config = {}) {
    return {
        // Configuration
        subscriptionId: config.subscriptionId || null,
        apiBasePath: config.apiBasePath || '/api/bandwidth',
        useMyEndpoints: config.useMyEndpoints || false, // Use /my/ endpoints for customer portal

        // State
        chart: null,
        eventSource: null,
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

        // Initialize
        async init() {
            await this.loadData();
            this.initChart();
            this.connectSSE();
        },

        // Cleanup
        destroy() {
            if (this.eventSource) {
                this.eventSource.close();
                this.eventSource = null;
            }
            if (this.chart) {
                DotmacCharts.unregisterChart(this.getChartId());
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

            // Prepare data
            const labels = this.seriesData.map(d => new Date(d.timestamp));
            const rxData = this.seriesData.map(d => d.rx_bps / 1000000); // Convert to Mbps
            const txData = this.seriesData.map(d => d.tx_bps / 1000000);

            const data = {
                labels: labels,
                datasets: [
                    {
                        label: 'Download',
                        data: rxData,
                        color: DotmacCharts.colors.accent[500],
                        fillColor: DotmacCharts.colors.accent[500] + '40',
                        fill: true,
                    },
                    {
                        label: 'Upload',
                        data: txData,
                        color: DotmacCharts.colors.primary[500],
                        fillColor: DotmacCharts.colors.primary[500] + '40',
                        fill: true,
                    },
                ],
            };

            const options = {
                scales: {
                    x: {
                        type: 'time',
                        time: {
                            displayFormats: {
                                minute: 'HH:mm',
                                hour: 'HH:mm',
                                day: 'MMM d',
                            },
                        },
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
            }

            this.chart = DotmacCharts.createAreaChart(ctx, data, options);
            DotmacCharts.registerChart(this.getChartId(), this.chart);
        },

        // Connect to SSE for real-time updates
        connectSSE() {
            if (this.eventSource) {
                this.eventSource.close();
            }

            // Only connect SSE for short time ranges
            if (this.timeRange !== '1h' && this.timeRange !== '24h') {
                return;
            }

            try {
                this.eventSource = new EventSource(this.getLiveEndpoint());

                this.eventSource.addEventListener('bandwidth', (event) => {
                    const data = JSON.parse(event.data);
                    this.currentRx = data.rx_bps || 0;
                    this.currentTx = data.tx_bps || 0;

                    // Update peak if necessary
                    if (this.currentRx > this.peakRx) this.peakRx = this.currentRx;
                    if (this.currentTx > this.peakTx) this.peakTx = this.currentTx;

                    // Add new point to chart
                    if (this.chart && this.chart.data.labels) {
                        const now = new Date();
                        this.chart.data.labels.push(now);
                        this.chart.data.datasets[0].data.push(this.currentRx / 1000000);
                        this.chart.data.datasets[1].data.push(this.currentTx / 1000000);

                        // Remove old points (keep last 3600 for 1h view)
                        const maxPoints = this.timeRange === '1h' ? 3600 : 86400;
                        while (this.chart.data.labels.length > maxPoints) {
                            this.chart.data.labels.shift();
                            this.chart.data.datasets[0].data.shift();
                            this.chart.data.datasets[1].data.shift();
                        }

                        this.chart.update('none'); // Update without animation
                    }
                });

                this.eventSource.addEventListener('error', (event) => {
                    console.error('SSE error:', event);
                    // Reconnect after 5 seconds
                    setTimeout(() => this.connectSSE(), 5000);
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
        apiBasePath: config.apiBasePath || '/api/bandwidth',
        useMyEndpoints: config.useMyEndpoints || false,

        currentRx: 0,
        currentTx: 0,
        loading: true,
        eventSource: null,

        get currentRxFormatted() { return formatBps(this.currentRx); },
        get currentTxFormatted() { return formatBps(this.currentTx); },

        async init() {
            await this.loadCurrent();
            this.connectSSE();
        },

        destroy() {
            if (this.eventSource) {
                this.eventSource.close();
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
            try {
                this.eventSource = new EventSource(this.getLiveEndpoint());

                this.eventSource.addEventListener('bandwidth', (event) => {
                    const data = JSON.parse(event.data);
                    this.currentRx = data.rx_bps || 0;
                    this.currentTx = data.tx_bps || 0;
                });

                this.eventSource.addEventListener('error', () => {
                    setTimeout(() => this.connectSSE(), 5000);
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
