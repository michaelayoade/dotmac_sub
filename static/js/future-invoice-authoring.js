/* Prices, tax, dates and fingerprints originate from the owner, never this client. */
function futureInvoiceAuthoring(config = {}) {
    return {
        rootElement: null,
        get root() { return this.rootElement || this.$el; },
        invoiceId: config.invoiceId || '', accountId: config.accountId || '', currency: config.currency || '',
        selectedItem: null, subscriptions: [], subscriptionId: '',
        periods: [], periodStart: '', calendarStart: '', quantity: 1, lines: [],
        unresolvedSelections: [], restoring: false, submitting: false, busy: false, error: '', sequence: 0,
        init() {
            this.rootElement = this.$el;
            this.$watch('currency', () => this.reset()); this.$watch('accountId', () => this.reset());
            const sync = () => { const field = this.root.querySelector('[name="account_id"]'); if (field) this.accountId = field.value; };
            this.root.addEventListener('change', event => { if (event.target.name === 'account_id') sync(); });
            this.root.addEventListener('htmx:afterSwap', sync); sync();
            this.$nextTick(() => this.restoreSelections(config.initialSelections || []));
        },
        async restoreSelections(selections) {
            this.restoring = true;
            for (const stored of selections) {
                try {
                    const query = stored.selection.query;
                    const line = await this.read('preview', {...query, as_of: null, invoice_id: this.invoiceId});
                    this.lines.push({...line, lineId: stored.line_id, key: JSON.stringify([query.item_kind, query.item_id, query.subscription_id, line.starts_at, line.period_kind]), clientId: crypto.randomUUID()});
                } catch (error) { this.unresolvedSelections.push({...stored, reason: error.message}); this.error = 'An existing line could not be loaded. Retry it or explicitly remove it before saving.'; }
            }
            this.restoring = false;
        },
        reset() { this.sequence++; this.selectedItem = null; this.subscriptions = []; this.subscriptionId = ''; this.periods = []; this.periodStart = ''; this.lines = []; this.error = ''; },
        async read(path, values) {
            const url = new URL(`/admin/billing/future-invoice/${path}`, window.location.origin);
            Object.entries(values).forEach(([key, value]) => { if (value !== null && value !== undefined && value !== '') url.searchParams.set(key, value); });
            const response = await fetch(url, {credentials: 'same-origin', headers: {'Accept': 'application/json'}});
            const data = await response.json();
            if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Unable to load invoice choices.');
            return data;
        },
        async chooseItem(item) {
            if (!item) return;
            const sequence = ++this.sequence; this.selectedItem = item;
            this.subscriptionId = ''; this.periods = []; this.periodStart = ''; this.quantity = 1; this.error = '';
            if (item.price_type === 'one_time') return;
            try { const result = await this.read('subscriptions', {account_id: this.accountId, offer_id: item.item_id}); if (sequence !== this.sequence) return; this.subscriptions = result; if (result.length === 1) { this.subscriptionId = result[0].subscription_id; await this.loadPeriods(); } }
            catch (error) { if (sequence === this.sequence) this.error = error.message; }
        },
        async loadPeriods() {
            const sequence = ++this.sequence; this.periods = []; this.periodStart = ''; this.calendarStart = ''; this.error = '';
            if (!this.subscriptionId) return; this.busy = true;
            try { const result = await this.read('periods', {account_id: this.accountId, subscription_id: this.subscriptionId, invoice_id: this.invoiceId}); if (sequence === this.sequence) this.periods = result; }
            catch (error) { if (sequence === this.sequence) this.error = error.message; }
            finally { if (sequence === this.sequence) this.busy = false; }
        },
        pickCalendarDate() {
            const period = this.periods.find(item => item.available && item.starts_at && item.starts_on === this.calendarStart);
            this.periodStart = period ? period.starts_at : ''; this.error = period ? '' : 'Choose an available service start date shown below.';
        },
        get customerLabel() {
            const field = this.root.querySelector('select[name="account_id"]');
            return field && field.selectedIndex >= 0 ? field.options[field.selectedIndex].text : this.root.dataset.customerLabel || 'Selected customer';
        },
        get capped() { return this.periods.some(period => period.period_kind === 'capped_pending_renewal'); },
        async addLine() {
            this.error = ''; if (!this.selectedItem || !this.accountId) return;
            if (this.selectedItem.price_type !== 'one_time' && !this.subscriptionId) { this.error = 'Select the customer’s subscription.'; return; }
            if (this.selectedItem.price_type !== 'one_time' && !this.capped && !this.periodStart) { this.error = 'Select an available future service period.'; return; }
            this.busy = true; const sequence = this.sequence;
            try {
                const line = await this.read('preview', {account_id: this.accountId, currency: this.currency, item_kind: this.selectedItem.item_kind, item_id: this.selectedItem.item_id, price_id: this.selectedItem.price_id, quantity: this.selectedItem.price_type === 'one_time' ? this.quantity : 1, subscription_id: this.selectedItem.price_type === 'one_time' ? null : this.subscriptionId, period_start: this.capped ? null : this.periodStart, invoice_id: this.invoiceId});
                if (sequence !== this.sequence) return;
                const key = JSON.stringify([line.selection.query.item_kind, line.selection.query.item_id, line.subscription_id, line.starts_at, line.period_kind]);
                if (line.period_kind !== 'one_time' && this.lines.some(item => item.key === key)) throw new Error('This period or renewal is already in this draft.');
                this.lines.push({...line, key, clientId: crypto.randomUUID()}); this.periodStart = ''; this.calendarStart = '';
            } catch (error) { this.error = error.message; } finally { this.busy = false; }
        },
        getLineItemsJSON() { return JSON.stringify(this.lines.map(line => ({id: line.lineId || null, description: line.description, quantity: line.quantity, unitPrice: line.unit_price, taxRateId: line.tax_rate_id, subscription_id: line.subscription_id, catalogue_selection: line.selection}))); },
        async retrySelection(index) {
            const stored = this.unresolvedSelections[index];
            this.busy = true;
            try {
                const line = await this.read('preview', {...stored.selection.query, as_of: null, invoice_id: this.invoiceId});
                this.lines.push({...line, lineId: stored.line_id, clientId: crypto.randomUUID()});
                this.unresolvedSelections.splice(index, 1);
            } catch (error) { stored.reason = error.message; }
            finally { this.busy = false; }
        },
        validateReview() {
            if (this.busy || this.restoring) { this.error = 'Wait for all existing lines to finish loading.'; return false; }
            if (this.unresolvedSelections.length) { this.error = 'Retry or explicitly remove every unresolved existing line before saving.'; return false; }
            this.error = '';
            if (!this.accountId || !this.lines.length) { this.error = 'Select a customer and add a catalogue item.'; return false; }
            return true;
        },
    };
}
window.futureInvoiceAuthoring = futureInvoiceAuthoring;
