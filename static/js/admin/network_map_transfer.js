(function (root) {
    'use strict';

    const dataElement = root.document.getElementById('network-map-transfer-data');
    if (!dataElement) return;
    const settings = JSON.parse(dataElement.textContent || '{}');
    const context = root.networkMapTransferContext;

    function csrfToken() {
        const meta = root.document.querySelector('meta[name="csrf-token"]')?.content;
        if (meta) return meta;
        const match = root.document.cookie.match(/(?:^|;\s*)csrf_token=([^;]+)/);
        return match ? decodeURIComponent(match[1]) : '';
    }

    function commandKey() {
        if (root.crypto && typeof root.crypto.randomUUID === 'function') {
            return root.crypto.randomUUID();
        }
        return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function (value) {
            const random = Math.random() * 16 | 0;
            return (value === 'x' ? random : (random & 3 | 8)).toString(16);
        });
    }

    function setStatus(element, message, kind) {
        element.classList.remove(
            'hidden', 'border-emerald-200', 'text-emerald-800',
            'border-rose-200', 'text-rose-800', 'border-slate-200',
            'dark:border-emerald-800', 'dark:text-emerald-300',
            'dark:border-rose-800', 'dark:text-rose-300'
        );
        element.textContent = message;
        if (kind === 'success') {
            element.classList.add('border-emerald-200', 'text-emerald-800', 'dark:border-emerald-800', 'dark:text-emerald-300');
        } else if (kind === 'error') {
            element.classList.add('border-rose-200', 'text-rose-800', 'dark:border-rose-800', 'dark:text-rose-300');
        } else {
            element.classList.add('border-slate-200');
        }
    }

    let previewLayer = null;
    function renderPreview(features) {
        if (!context || !context.map || !root.L) return;
        if (previewLayer) context.map.removeLayer(previewLayer);
        previewLayer = root.L.geoJSON(
            { type: 'FeatureCollection', features: features || [] },
            {
                style: {
                    color: 'var(--color-semantic-warning-600)',
                    weight: 3,
                    dashArray: '8,6',
                    fillOpacity: 0.12
                },
                pointToLayer: function (_feature, latlng) {
                    return root.L.circleMarker(latlng, {
                        radius: 7,
                        color: 'var(--color-semantic-warning-600)',
                        weight: 2,
                        fillOpacity: 0.35
                    });
                },
                onEachFeature: function (feature, layer) {
                    const label = root.document.createElement('span');
                    label.textContent = `${feature.properties.name} — staged preview`;
                    layer.bindTooltip(label);
                }
            }
        ).addTo(context.map);
        const bounds = previewLayer.getBounds();
        if (bounds.isValid()) context.map.fitBounds(bounds, { padding: [40, 40] });
    }

    function renderCounts(element, result) {
        const values = [
            ['Features', result.feature_count],
            ['New', result.new_count],
            ['Candidates', result.candidate_count],
            ['Blocked', result.blocker_count]
        ];
        element.replaceChildren();
        values.forEach(function (entry) {
            const wrapper = root.document.createElement('div');
            wrapper.className = 'rounded-lg bg-slate-50 p-3 dark:bg-slate-900/30';
            const term = root.document.createElement('dt');
            term.className = 'text-xs text-slate-500 dark:text-slate-400';
            term.textContent = entry[0];
            const detail = root.document.createElement('dd');
            detail.className = 'mt-1 text-lg font-semibold tabular-nums';
            detail.textContent = String(entry[1]);
            wrapper.append(term, detail);
            element.appendChild(wrapper);
        });
    }

    if (settings.can_import) {
        const openButton = root.document.getElementById('btn-import-kmz');
        const dialog = root.document.getElementById('network-map-import-dialog');
        const form = root.document.getElementById('network-map-import-form');
        const status = root.document.getElementById('network-map-import-status');
        const resultHost = root.document.getElementById('network-map-import-result');
        const counts = root.document.getElementById('network-map-import-counts');
        const submit = root.document.getElementById('network-map-import-submit');
        const key = root.document.getElementById('network-map-import-key');

        openButton?.addEventListener('click', function () {
            form.reset();
            status.classList.add('hidden');
            resultHost.classList.add('hidden');
            key.value = commandKey();
            dialog.showModal();
        });
        dialog.querySelectorAll('[data-close-import]').forEach(function (button) {
            button.addEventListener('click', function () { dialog.close(); });
        });
        ['network-map-kmz-file', 'network-map-import-profile', 'network-map-import-reason'].forEach(function (id) {
            root.document.getElementById(id)?.addEventListener('change', function () {
                key.value = commandKey();
            });
        });
        form.addEventListener('submit', async function (event) {
            event.preventDefault();
            submit.disabled = true;
            setStatus(status, 'Validating and staging the KMZ file…', 'progress');
            resultHost.classList.add('hidden');
            try {
                const response = await root.fetch('/admin/network/map/imports', {
                    method: 'POST',
                    headers: { 'X-CSRF-Token': csrfToken() },
                    body: new FormData(form)
                });
                const raw = await response.text();
                let payload;
                try {
                    payload = raw ? JSON.parse(raw) : {};
                } catch (_error) {
                    payload = { message: raw || `Import failed (${response.status}).` };
                }
                if (!response.ok) throw new Error(payload.message || `Import failed (${response.status}).`);
                renderCounts(counts, payload);
                resultHost.classList.remove('hidden');
                renderPreview(payload.features);
                const replay = payload.created ? '' : ' Existing immutable batch reused.';
                const truncated = payload.preview_truncated ? ' The preview is limited to the first 5,000 features.' : '';
                setStatus(
                    status,
                    `${payload.status === 'blocked' ? 'Import staged with blockers.' : 'Import staged for review.'}${replay}${truncated}`,
                    payload.status === 'blocked' ? 'error' : 'success'
                );
                key.value = commandKey();
            } catch (error) {
                setStatus(status, error.message || 'The KMZ import failed.', 'error');
            } finally {
                submit.disabled = false;
            }
        });
    }

    function selectedExportLayers() {
        if (!context) return [];
        const mapping = {
            pop: 'infrastructure',
            fdh: 'infrastructure',
            closures: 'infrastructure',
            accessPoints: 'infrastructure',
            supportStructures: 'infrastructure',
            feeder: 'fiber',
            distribution: 'fiber',
            drop: 'fiber',
            networkDevices: 'network_devices',
            onts: 'onts',
            customersConnected: 'customers',
            customersNotConnected: 'customers'
        };
        const selected = new Set();
        Object.values(context.layerToggles).forEach(function (layerName) {
            if (context.map.hasLayer(context.layers[layerName])) selected.add(mapping[layerName]);
        });
        if (selected.size === 0) {
            const focus = new URLSearchParams(root.location.search).get('focus');
            if (focus === 'customers') selected.add('customers');
            else ['infrastructure', 'fiber', 'network_devices', 'onts'].forEach(value => selected.add(value));
        }
        if (!settings.can_export_customers) selected.delete('customers');
        return Array.from(selected).filter(Boolean);
    }

    if (settings.can_export) {
        const exportButton = root.document.getElementById('btn-export-kmz');
        exportButton?.addEventListener('click', async function () {
            const selected = selectedExportLayers();
            if (selected.length === 0) return;
            const parameters = new URLSearchParams({
                layers: selected.join(','),
                scope: 'visible',
                include_customers: String(settings.can_export_customers && selected.includes('customers'))
            });
            const filters = {
                'filter-customer-status': 'customer_status',
                'filter-device-status': 'device_status',
                'filter-device-type': 'device_type',
                'filter-ont-status': 'ont_status',
                'filter-signal': 'signal_quality',
                'filter-lifecycle': 'support_lifecycle',
                'filter-inspection': 'inspection_status',
                'filter-cable': 'segment_type'
            };
            Object.entries(filters).forEach(function (entry) {
                const value = root.document.getElementById(entry[0])?.value;
                if (value) parameters.set(entry[1], value);
            });
            if (context && context.map) {
                const bounds = context.map.getBounds();
                parameters.set('south', String(bounds.getSouth()));
                parameters.set('west', String(bounds.getWest()));
                parameters.set('north', String(bounds.getNorth()));
                parameters.set('east', String(bounds.getEast()));
            }
            const originalLabel = exportButton.textContent;
            exportButton.disabled = true;
            exportButton.textContent = 'Preparing KMZ…';
            try {
                const response = await root.fetch(`/admin/network/map/export.kmz?${parameters.toString()}`);
                if (!response.ok) {
                    const payload = await response.json().catch(function () { return {}; });
                    throw new Error(payload.message || `Export failed (${response.status}).`);
                }
                const blob = await response.blob();
                const disposition = response.headers.get('Content-Disposition') || '';
                const match = disposition.match(/filename="?([^";]+)"?/i);
                const link = root.document.createElement('a');
                link.href = root.URL.createObjectURL(blob);
                link.download = match ? match[1] : 'network-map.kmz';
                root.document.body.appendChild(link);
                link.click();
                link.remove();
                root.URL.revokeObjectURL(link.href);
            } catch (error) {
                if (root.htmx) {
                    root.htmx.trigger(root.document.body, 'showToast', {
                        message: error.message || 'The KMZ export failed.',
                        type: 'error'
                    });
                } else {
                    root.alert(error.message || 'The KMZ export failed.');
                }
            } finally {
                exportButton.disabled = false;
                exportButton.textContent = originalLabel;
            }
        });
    }
})(window);
