/**
 * Session refresh utility for portal layouts.
 * Keeps user sessions alive by periodically pinging a refresh endpoint.
 * Redirects to login when session expires.
 *
 * @param {Object} config
 * @param {string} config.refreshUrl - Endpoint for session refresh
 * @param {string} config.loginUrl - Redirect URL on session expiry
 * @param {number} [config.intervalMs=600000] - Refresh interval (default 10 min)
 */
function initSessionRefresh(config) {
    const { refreshUrl, loginUrl, intervalMs = 10 * 60 * 1000 } = config;

    async function refreshSession() {
        try {
            const response = await fetch(refreshUrl, { credentials: "same-origin" });
            if (response.status === 401) {
                const next = window.location.pathname + window.location.search;
                window.location.href = `${loginUrl}?next=${encodeURIComponent(next)}`;
            }
        } catch (err) {
            // Ignore transient network errors.
        }
    }

    function startRefresh() {
        refreshSession();
        setInterval(refreshSession, intervalMs);
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", startRefresh);
    } else {
        startRefresh();
    }

    document.addEventListener("visibilitychange", () => {
        if (!document.hidden) {
            refreshSession();
        }
    });
}
