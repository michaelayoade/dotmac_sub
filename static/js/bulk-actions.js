/**
 * Bulk Actions Component for Alpine.js
 * Provides reusable multi-select functionality for list tables
 *
 * Usage:
 *   <div x-data="bulkActions({ entityName: 'customers' })">
 *     <!-- Table with bulk select checkboxes -->
 *   </div>
 */

function bulkActions(config = {}) {
    return {
        // Selection state
        selected: [],
        allIds: [],

        // Configuration
        entityName: config.entityName || 'items',
        csrfToken: '',

        // UI state
        loading: false,
        actionInProgress: null,

        /**
         * Initialize the component
         * Collects all row IDs from data-bulk-id attributes
         */
        init() {
            // Get CSRF token from meta tag
            const meta = document.querySelector('meta[name="csrf-token"]');
            this.csrfToken = meta ? meta.getAttribute('content') : '';

            // Collect all row IDs from data attributes
            this.$nextTick(() => {
                this.refreshIds();
            });

            // Re-collect IDs after HTMX swaps (for pagination)
            document.body.addEventListener('htmx:afterSwap', (event) => {
                if (this.$root.contains(event.target)) {
                    this.refreshIds();
                }
            });
        },

        /**
         * Refresh the list of all IDs from the DOM
         */
        refreshIds() {
            this.allIds = Array.from(
                this.$root.querySelectorAll('[data-bulk-id]')
            ).map(el => el.dataset.bulkId);

            // Remove any selected IDs that are no longer in the list
            this.selected = this.selected.filter(id => this.allIds.includes(id));
        },

        /**
         * Check if all rows are selected
         */
        get isAllSelected() {
            return this.allIds.length > 0 &&
                   this.allIds.every(id => this.selected.includes(id));
        },

        /**
         * Check if some but not all rows are selected (for indeterminate checkbox)
         */
        get isIndeterminate() {
            return this.selected.length > 0 && !this.isAllSelected;
        },

        /**
         * Check if any rows are selected
         */
        get hasSelection() {
            return this.selected.length > 0;
        },

        /**
         * Get count of selected items
         */
        get selectedCount() {
            return this.selected.length;
        },

        /**
         * Toggle all rows selection
         */
        toggleAll() {
            if (this.isAllSelected) {
                this.selected = [];
            } else {
                this.selected = [...this.allIds];
            }
        },

        /**
         * Toggle a single row selection
         */
        toggleRow(id) {
            const idx = this.selected.indexOf(id);
            if (idx > -1) {
                this.selected.splice(idx, 1);
            } else {
                this.selected.push(id);
            }
        },

        /**
         * Check if a specific row is selected
         */
        isSelected(id) {
            return this.selected.includes(id);
        },

        /**
         * Clear all selections
         */
        clearSelection() {
            this.selected = [];
        },

        /**
         * Perform a bulk action
         * @param {string} action - Action name (delete, export, activate, etc.)
         * @param {string} endpoint - API endpoint to POST to
         * @param {object} options - Additional options
         */
        async performAction(action, endpoint, options = {}) {
            if (this.selected.length === 0) {
                this.showToast('No items selected', 'warning');
                return;
            }

            // Show confirmation dialog if specified
            if (options.confirm) {
                const confirmed = await this.showConfirm(options.confirm);
                if (!confirmed) return;
            }

            this.loading = true;
            this.actionInProgress = action;

            try {
                const response = await fetch(endpoint, {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'X-CSRF-Token': this.csrfToken,
                    },
                    body: JSON.stringify({
                        ids: this.selected,
                        action: action,
                        ...options.data
                    }),
                });

                if (response.ok) {
                    const contentType = response.headers.get('content-type');

                    // Handle file download (export)
                    if (contentType && (contentType.includes('text/csv') || contentType.includes('application/octet-stream'))) {
                        const blob = await response.blob();
                        const filename = this.getFilenameFromResponse(response) || `${this.entityName}_export.csv`;
                        this.downloadBlob(blob, filename);
                        this.showToast(`Exported ${this.selected.length} ${this.entityName}`, 'success');
                    } else {
                        // Handle JSON response
                        const result = await response.json();

                        if (result.success_count !== undefined) {
                            const message = result.failed_count > 0
                                ? `${result.success_count} succeeded, ${result.failed_count} failed`
                                : `${result.success_count} ${this.entityName} ${action}d successfully`;
                            this.showToast(message, result.failed_count > 0 ? 'warning' : 'success');
                        } else {
                            this.showToast(result.message || `Action completed successfully`, 'success');
                        }

                        // Clear selection and refresh the page/table
                        this.clearSelection();

                        if (options.refreshSelector) {
                            // HTMX refresh of specific element
                            const target = document.querySelector(options.refreshSelector);
                            if (target && target.getAttribute('hx-get')) {
                                htmx.trigger(target, 'refresh');
                            }
                        } else {
                            // Full page reload
                            window.location.reload();
                        }
                    }
                } else {
                    const error = await response.json().catch(() => ({ detail: 'Action failed' }));
                    this.showToast(error.detail || error.message || 'Action failed', 'error');
                }
            } catch (error) {
                console.error('Bulk action error:', error);
                this.showToast('Network error. Please try again.', 'error');
            } finally {
                this.loading = false;
                this.actionInProgress = null;
            }
        },

        /**
         * Show a confirmation dialog
         */
        showConfirm(message) {
            return new Promise((resolve) => {
                // Use native confirm for now - can be replaced with custom modal
                resolve(window.confirm(message));
            });
        },

        /**
         * Show a toast notification
         */
        showToast(message, type = 'info') {
            window.dispatchEvent(new CustomEvent('show-toast', {
                detail: { message, type }
            }));
        },

        /**
         * Download a blob as a file
         */
        downloadBlob(blob, filename) {
            const url = window.URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = filename;
            document.body.appendChild(a);
            a.click();
            window.URL.revokeObjectURL(url);
            document.body.removeChild(a);
        },

        /**
         * Extract filename from Content-Disposition header
         */
        getFilenameFromResponse(response) {
            const disposition = response.headers.get('content-disposition');
            if (disposition) {
                const match = disposition.match(/filename[^;=\n]*=((['"]).*?\2|[^;\n]*)/);
                if (match && match[1]) {
                    return match[1].replace(/['"]/g, '');
                }
            }
            return null;
        },

        /**
         * Shortcut for delete action with confirmation
         */
        async bulkDelete(endpoint) {
            await this.performAction('delete', endpoint, {
                confirm: `Are you sure you want to delete ${this.selected.length} ${this.entityName}? This action cannot be undone.`
            });
        },

        /**
         * Shortcut for export action
         */
        async bulkExport(endpoint, format = 'csv') {
            await this.performAction('export', endpoint, {
                data: { format }
            });
        },

        /**
         * Shortcut for status update action
         */
        async bulkUpdateStatus(endpoint, status) {
            await this.performAction('update_status', endpoint, {
                data: { status },
                confirm: `Update ${this.selected.length} ${this.entityName} to ${status}?`
            });
        }
    };
}

// Make it available globally for Alpine
window.bulkActions = bulkActions;

/**
 * Export All - downloads CSV of all records matching current search/filters.
 * Works independently of the bulkActions component (no selection needed).
 *
 * Uses native browser download (window.location) instead of fetch+blob,
 * which is more reliable across browsers (no popup blocker issues,
 * no blob URL failures, no silent redirect-following).
 *
 * @param {string} baseUrl - GET endpoint path (e.g. "/finance/ar/invoices/export")
 */
function exportAll(baseUrl) {
    // Gather current search/filter params from the URL
    const params = new URLSearchParams(window.location.search);
    const exportParams = new URLSearchParams();

    // Forward known filter params (matches all list page filter names)
    for (const key of ['search', 'status', 'category', 'type', 'start_date', 'end_date', 'customer_id', 'supplier_id', 'date_from', 'date_to']) {
        const val = params.get(key);
        if (val) {
            exportParams.set(key, val);
        }
    }

    const url = exportParams.toString()
        ? `${baseUrl}?${exportParams.toString()}`
        : baseUrl;

    // Show immediate feedback
    window.dispatchEvent(new CustomEvent('show-toast', {
        detail: { message: 'Preparing export...', type: 'info' }
    }));

    // Use fetch for download so we can provide success/error feedback,
    // then fall back to iframe if blob download fails.
    fetch(url, { method: 'GET', credentials: 'same-origin' })
        .then(function(response) {
            if (!response.ok) {
                throw new Error('Export failed (HTTP ' + response.status + ')');
            }
            return response.blob().then(function(blob) {
                // Extract filename from Content-Disposition header
                var disposition = response.headers.get('content-disposition');
                var filename = 'export.csv';
                if (disposition) {
                    var match = disposition.match(/filename[^;=\n]*=((['"]).*?\2|[^;\n]*)/);
                    if (match && match[1]) {
                        filename = match[1].replace(/['"]/g, '');
                    }
                }

                // Trigger download via object URL
                var a = document.createElement('a');
                a.href = window.URL.createObjectURL(blob);
                a.download = filename;
                document.body.appendChild(a);
                a.click();
                window.URL.revokeObjectURL(a.href);
                document.body.removeChild(a);

                window.dispatchEvent(new CustomEvent('show-toast', {
                    detail: { message: 'Export downloaded successfully', type: 'success' }
                }));
            });
        })
        .catch(function(error) {
            console.warn('Fetch export failed, falling back to iframe:', error);
            // Fallback: use hidden iframe for browsers where fetch+blob fails
            var iframe = document.createElement('iframe');
            iframe.style.display = 'none';
            iframe.src = url;
            document.body.appendChild(iframe);
            setTimeout(function() {
                document.body.removeChild(iframe);
            }, 30000);

            window.dispatchEvent(new CustomEvent('show-toast', {
                detail: { message: 'Export started — check your downloads', type: 'success' }
            }));
        });
}

window.exportAll = exportAll;
