/* Durable customer send receipts. A lost response is an unknown outcome. */
(() => {
    'use strict';
    /** @typedef {{request_id:string, accepted:boolean, materialization_status:'accepted'|'preparing'|'queued'|'failed', matched_count:number, planned_queued_count:number, planned_suppressed_count:number, skipped_count:number, delivered_count:number, submitted_count:number, pending_count:number, failed_count:number, canceled_count:number, error:string|null, status_url:string}} BulkSendStatus */
    /** @typedef {{requestId:string, fingerprint:string, state:string}} SavedSend */
    const key = 'dotmac.customer-bulk-send';
    /** @type {SavedSend|null} */
    let saved = null;
    let inFlight = false;
    let newInteraction = false;
    let timer = null;
    try { saved = JSON.parse(sessionStorage.getItem(key) || 'null'); } catch (_) { /* unavailable storage */ }
    const persist = () => {
        try {
            if (saved) sessionStorage.setItem(key, JSON.stringify(saved));
            else sessionStorage.removeItem(key);
        } catch (_) { /* Memory still protects this page from repeated submission. */ }
    };
    const show = (message, requestId) => {
        const panel = document.getElementById('customer-bulk-send-status');
        if (!panel) return;
        panel.hidden = false;
        panel.querySelector('[data-send-message]').textContent = message;
        panel.querySelector('[data-send-reference]').textContent = requestId;
    };
    /** @param {BulkSendStatus} status */
    const render = (status) => {
        const labels = {accepted: 'Accepted; waiting to prepare recipients', preparing: 'Preparing recipients', queued: 'Recipient preparation completed', failed: 'Recipient preparation failed'};
        let text = `Matched ${status.matched_count} customer(s). ${labels[status.materialization_status]}. ${status.planned_queued_count} delivery requests; ${status.planned_suppressed_count} suppressed; ${status.skipped_count} skipped.`;
        if (status.materialization_status === 'queued') {
            text += ` ${status.delivered_count} delivered; ${status.submitted_count} awaiting provider confirmation; ${status.pending_count} pending; ${status.failed_count} failed; ${status.canceled_count} canceled.`;
        }
        if (status.error) text += ` ${status.error}`;
        show(text, status.request_id);
    };
    const requestJson = async (url, options = {}) => {
        const controller = new AbortController();
        const timeout = setTimeout(() => controller.abort(), 45000);
        try {
            const response = await fetch(url, {
                ...options,
                headers: {...(options.headers || {}), Accept: 'application/json'},
                signal: controller.signal,
                redirect: 'error',
                cache: 'no-store'
            });
            const body = await response.json();
            return {response, body};
        } finally { clearTimeout(timeout); }
    };
    const responseMessage = body => {
        const message = body?.message || body?.detail;
        return typeof message === 'string' && message.trim() ? message.trim() : null;
    };
    const statusError = (response, body) => {
        if (response.status === 401) return 'Sign in again, then check this send reference.';
        if (response.status === 403) return 'You do not have permission to check this send.';
        if (response.status === 404) return 'No saved send record is available yet. Keep this reference and check again before sending another message.';
        return responseMessage(body) || 'Could not confirm the send status.';
    };
    /** @returns {Promise<BulkSendStatus>} */
    const check = async () => {
        if (!saved) throw new Error('No send reference is available.');
        const id = saved.requestId;
        const {response, body} = await requestJson(`/admin/customers/bulk/send-message/${encodeURIComponent(id)}`);
        if (!response.ok) {
            const error = new Error(statusError(response, body));
            error.status = response.status;
            throw error;
        }
        if (body.request_id !== id || body.accepted !== true) {
            throw new Error('The server returned a status that does not match this send reference. Keep the reference and check again before sending another message.');
        }
        if (saved?.requestId === id) {
            saved.state = body.materialization_status;
            persist();
            render(body);
        }
        return body;
    };
    const watch = () => {
        clearTimeout(timer);
        if (!saved) return;
        timer = setTimeout(async () => {
            try {
                const status = await check();
                if (status.materialization_status !== 'failed' && (status.materialization_status !== 'queued' || status.pending_count || status.submitted_count)) watch();
            } catch (_) {
                show('Could not confirm the send status. Use Check status before submitting another send.', saved?.requestId || '');
                // Leave manual recovery available; never resubmit a send automatically.
            }
        }, 5000);
    };
    const canonical = (value) => {
        if (Array.isArray(value)) return value.map(canonical);
        if (value && typeof value === 'object') return Object.fromEntries(Object.keys(value).sort().map(name => [name, canonical(value[name])]));
        return value;
    };
    const fingerprint = async (payload) => {
        const bytes = new TextEncoder().encode(JSON.stringify(canonical(payload)));
        return Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', bytes)), byte => byte.toString(16).padStart(2, '0')).join('');
    };
    const send = async (payload, headers) => {
        if (inFlight) throw new Error('This send is already being submitted.');
        inFlight = true;
        try {
            const hash = await fingerprint(payload);
            if (saved && saved.fingerprint !== hash && saved.state === 'unknown') {
                await check(); // An uncertain earlier send must be resolved first.
            }
            if (!saved || saved.fingerprint !== hash || (newInteraction && saved.state !== 'unknown')) {
                saved = {requestId: crypto.randomUUID(), fingerprint: hash, state: 'unknown'};
                persist(); // Keep the reference before any network request.
            }
            newInteraction = false;
            const id = saved.requestId;
            show('Submitting message; confirming its send status…', id);
            try {
                const {response, body} = await requestJson('/admin/customers/bulk/send-message', {
                    method: 'POST', headers, body: JSON.stringify({...payload, request_id: id}),
                });
                if (response.ok && body.accepted === true && body.request_id === id) {
                    saved.state = body.materialization_status;
                    persist(); render(body); watch();
                    return body;
                }
                const message = responseMessage(body);
                if ([400, 403, 404, 422].includes(response.status) && message) {
                    // Only a definite server rejection allows a new request identity.
                    saved = null; persist();
                    const error = new Error(message);
                    error.rejected = true;
                    throw error;
                }
                throw new Error(message || 'Send confirmation unavailable.');
            } catch (error) {
                if (error.rejected) throw error;
                try {
                    const status = await check();
                    watch();
                    return status;
                } catch (statusFailure) {
                    const message = statusFailure.message || error.message || 'Could not confirm the send status.';
                    const guidance = `${message} Keep this reference and check again before sending another message.`;
                    show(guidance, id);
                    if (![401, 403, 404].includes(statusFailure.status)) watch();
                    throw new Error(guidance);
                }
            }
        } finally { inFlight = false; }
    };
    const startPreview = () => {
        // A newly confirmed interaction can have a new identity only after the
        // previous acceptance is known. Unknown requests retain their identity.
        newInteraction = true;
    };
    window.DotmacCustomerBulkSend = {send, check, startPreview};
    document.addEventListener('DOMContentLoaded', () => {
        document.querySelector('[data-send-check]')?.addEventListener('click', async () => {
            try { await check(); watch(); }
            catch (error) { show(error.message, saved?.requestId || ''); }
        });
        if (saved) {
            show('Checking the previous send status…', saved.requestId);
            check().then(watch).catch(() => show('Could not confirm the previous send. Use Check status before sending again.', saved?.requestId || ''));
        }
    });
})();
