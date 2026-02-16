/**
 * import-wizard.js — Alpine.js component for the 3-step import wizard.
 *
 * Usage:
 *   <div x-data='importWizard(config)'>
 *
 * Config object (rendered from server via tojson):
 *   {
 *     previewUrl:   "/import/{entity_type}/preview",
 *     importUrl:    "/import/{entity_type}",
 *     cancelUrl:    "/import",
 *     aliasMap:     { normalized_alias: canonical_field, ... },
 *     targetFields: [ { source_field, target_field, required }, ... ],
 *     entityName:   "Chart of Accounts",
 *     columns:      { required: [...], optional: [...] },
 *   }
 *
 * Requires: csv-parser.js (provides parseCSVText, autoMatchHeaders)
 */

window.importWizard = function importWizard(config) {
    const targetFieldList = (config.targetFields || []).map(f => ({
        name: f.target_field || f.source_field,
        label: (f.target_field || f.source_field).replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase()),
        required: !!f.required,
    }));

    return {
        // ── Config ──────────────────────────────────────────────
        config,
        step: 1,
        stepLabels: ['Upload & Options', 'Column Mapping', 'Preview & Import'],

        // ── Step 1: File ────────────────────────────────────────
        fileName: '',
        fileSize: '',
        isXlsx: false,
        dragOver: false,
        skipDuplicates: true,
        dryRun: false,

        // ── Step 2: Mapping ─────────────────────────────────────
        csvHeaders: [],
        csvRows: [],
        mapping: {},           // { csvHeader: targetField }
        targetFields: targetFieldList,

        // ── Step 3: Results ─────────────────────────────────────
        previewData: null,     // Server preview response
        importResult: null,    // Server import response
        importing: false,
        previewing: false,
        parseError: '',

        // ── Computed ────────────────────────────────────────────
        get mappedCount() {
            return Object.values(this.mapping).filter(v => v).length;
        },
        get requiredFields() {
            return this.targetFields.filter(f => f.required);
        },
        get missingRequired() {
            const mapped = new Set(Object.values(this.mapping));
            return this.requiredFields.filter(f => !mapped.has(f.name));
        },
        get canProceedStep1() {
            return this.fileName && !this.parseError;
        },
        get canProceedStep2() {
            return this.missingRequired.length === 0 && this.mappedCount > 0;
        },
        get columnMappingJson() {
            // Build { csvHeader: targetField } for only mapped columns
            const m = {};
            for (const [src, tgt] of Object.entries(this.mapping)) {
                if (tgt) m[src] = tgt;
            }
            return JSON.stringify(m);
        },
        get isTargetUsed() {
            // Returns a Set of already-used target field names
            return new Set(Object.values(this.mapping).filter(v => v));
        },

        // ── File handling ───────────────────────────────────────
        handleFile(file) {
            if (!file) return;
            const ext = file.name.split('.').pop().toLowerCase();
            const allowed = ['csv', 'xls', 'xlsx', 'xlsm'];
            if (!allowed.includes(ext)) {
                this.parseError = 'Only CSV, XLS, XLSX, or XLSM files are supported.';
                return;
            }
            this.parseError = '';
            this.fileName = file.name;
            this.fileSize = this._formatSize(file.size);
            this.isXlsx = ext !== 'csv';
            this.csvHeaders = [];
            this.csvRows = [];
            this.mapping = {};
            this.previewData = null;
            this.importResult = null;

            if (!this.isXlsx) {
                // Parse CSV client-side
                const reader = new FileReader();
                reader.onload = (e) => {
                    try {
                        const { headers, rows } = parseCSVText(e.target.result);
                        if (!headers || headers.length === 0) {
                            this.parseError = 'Could not detect any columns in the file.';
                        } else {
                            this.csvHeaders = headers;
                            this.csvRows = rows.slice(0, 5);
                            this.mapping = autoMatchHeaders(headers, config.aliasMap || {});
                        }
                    } catch (err) {
                        this.parseError = 'Failed to parse CSV: ' + err.message;
                    }
                };
                reader.onerror = () => { this.parseError = 'Failed to read file.'; };
                reader.readAsText(file);
            }
            // XLSX files are parsed server-side in goToStep2()
        },

        handleDrop(e) {
            this.dragOver = false;
            const file = e.dataTransfer.files[0];
            if (file) {
                this.handleFile(file);
                // Set the file input so it's available for form submission
                const input = this.$refs.fileInput;
                const dt = new DataTransfer();
                dt.items.add(file);
                input.files = dt.files;
            }
        },

        handleFileInput(e) {
            this.handleFile(e.target.files[0]);
        },

        removeFile() {
            this.$refs.fileInput.value = '';
            this.fileName = '';
            this.fileSize = '';
            this.csvHeaders = [];
            this.csvRows = [];
            this.mapping = {};
            this.parseError = '';
            this.previewData = null;
            this.importResult = null;
        },

        // ── Navigation ──────────────────────────────────────────
        async goToStep2() {
            if (!this.canProceedStep1) return;

            if (this.isXlsx && this.csvHeaders.length === 0) {
                // XLSX: send to server for preview to get headers
                this.previewing = true;
                try {
                    const formData = new FormData();
                    formData.append('file', this.$refs.fileInput.files[0]);

                    const resp = await fetch(config.previewUrl, {
                        method: 'POST',
                        body: formData,
                    });
                    const data = await resp.json();
                    if (!resp.ok) {
                        this.parseError = data.detail || 'Preview failed';
                        return;
                    }
                    this.previewData = data;
                    this.csvHeaders = data.detected_columns || [];
                    // Build sample rows from sample_data
                    this.csvRows = (data.sample_data || []).slice(0, 5);
                    // Auto-map using server suggestions or client-side alias matching
                    if (data.column_mappings && data.column_mappings.length > 0) {
                        const m = {};
                        const used = new Set();
                        for (const cm of data.column_mappings) {
                            if (cm.target && !used.has(cm.target)) {
                                m[cm.source] = cm.target;
                                used.add(cm.target);
                            } else {
                                m[cm.source] = '';
                            }
                        }
                        this.mapping = m;
                    } else {
                        this.mapping = autoMatchHeaders(this.csvHeaders, config.aliasMap || {});
                    }
                } catch (err) {
                    this.parseError = 'Failed to preview file: ' + err.message;
                    return;
                } finally {
                    this.previewing = false;
                }
            }

            this.step = 2;
        },

        goToStep3() {
            if (!this.canProceedStep2) return;
            this.step = 3;
        },

        goBack(toStep) {
            if (toStep < this.step) {
                this.step = toStep;
                this.importResult = null;
            }
        },

        // ── Import execution ────────────────────────────────────
        async executeImport() {
            this.importing = true;
            this.importResult = null;

            try {
                const formData = new FormData();
                formData.append('file', this.$refs.fileInput.files[0]);
                formData.append('skip_duplicates', this.skipDuplicates);
                formData.append('dry_run', this.dryRun);
                formData.append('column_mapping', this.columnMappingJson);

                const resp = await fetch(config.importUrl, {
                    method: 'POST',
                    body: formData,
                });
                const data = await resp.json();
                if (!resp.ok) {
                    window.showToast && window.showToast(data.detail || 'Import failed', 'error');
                    return;
                }
                this.importResult = data;
                if (data.status === 'completed') {
                    window.showToast && window.showToast('Import completed successfully!', 'success');
                } else if (data.status === 'completed_with_errors') {
                    window.showToast && window.showToast('Import completed with some errors.', 'warning');
                } else {
                    window.showToast && window.showToast('Import failed. Check errors below.', 'error');
                }
            } catch (err) {
                window.showToast && window.showToast('Import failed: ' + err.message, 'error');
            } finally {
                this.importing = false;
            }
        },

        // ── Helpers ─────────────────────────────────────────────
        _formatSize(bytes) {
            if (bytes < 1024) return bytes + ' B';
            if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
            return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
        },

        availableTargets(currentHeader) {
            // For a given header's dropdown, show: unassigned targets + currently selected target
            const currentTarget = this.mapping[currentHeader] || '';
            return this.targetFields.filter(f =>
                !this.isTargetUsed.has(f.name) || f.name === currentTarget
            );
        },

        statusClass(status) {
            if (status === 'completed') return 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/50 dark:text-emerald-300';
            if (status === 'completed_with_errors') return 'bg-amber-100 text-amber-700 dark:bg-amber-900/50 dark:text-amber-300';
            return 'bg-rose-100 text-rose-700 dark:bg-rose-900/50 dark:text-rose-300';
        },

        statusLabel(status) {
            if (status === 'completed') return 'Completed';
            if (status === 'completed_with_errors') return 'Completed with Errors';
            return 'Failed';
        },
    };
};
