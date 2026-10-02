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

    function proposalKey(batchId, featureId, assetType, reason) {
        const storageKey = `network-map-import-proposal:${batchId}:${featureId}:${assetType}:${encodeURIComponent(reason)}`;
        try {
            const existing = root.sessionStorage.getItem(storageKey);
            if (existing) return existing;
            const created = commandKey();
            root.sessionStorage.setItem(storageKey, created);
            return created;
        } catch (_error) {
            return commandKey();
        }
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
            ['Matched / candidates', result.matched_count ?? result.candidate_count],
            ['Unclassified', result.unclassified_count ?? 0],
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

    const classificationEdits = new Map();
    function renderFeatureReviews(element, features) {
        const blockerHelp = {
            missing_asset_type: 'Choose one supported plant type before this feature can enter an asset review.',
            unsupported_asset_type: 'This feature type is not supported by fiber plant staging. Keep it as evidence or use the owning customer/device workflow.',
            invalid_coordinate: 'Correct the coordinates to valid longitude and latitude values.',
            coordinate_outside_nigeria: 'Move or correct the geometry to the supported Nigeria coordinate area.',
            invalid_point_geometry: 'A Point must contain exactly one valid coordinate.',
            invalid_linestring_geometry: 'A LineString must contain at least two valid coordinates.',
            invalid_polygon_geometry: 'A Polygon ring must have at least four coordinates and close on its first point.',
            missing_polygon_coordinates: 'Add polygon boundary coordinates.',
            unexpected_geometry_type: 'Choose a compatible asset type or correct the geometry in the source file.',
            multiple_geometry_components: 'Split this placemark into one point, line, or polygon per feature before applying it.',
            missing_external_id: 'Add a stable source ID to match this feature reliably.',
            duplicate_external_id: 'This source ID appears more than once in the file. Correct the duplicate or leave the features blocked for review.',
            missing_supported_geometry: 'Add a Point, LineString, or Polygon geometry.'
        };
        const matchReasonHelp = {
            canonical_external_id_match: 'A canonical asset has the same stable source ID.',
            canonical_normalized_name_match: 'A canonical asset has the same normalized name; verify the match before any change.',
            duplicate_source_external_id: 'The same source ID appears on multiple imported features.',
            duplicate_source_name: 'The same name appears on multiple imported features.',
            duplicate_source_geometry: 'Another imported feature has identical geometry.',
            changed_source_identity: 'This source identity differs from an earlier staged version.',
            unchanged_source_identity: 'This source feature matches an earlier staged version.'
        };
        const proposalEligibilityHelp = {
            eligible: 'Eligible to submit as a new point asset proposal for independent review.',
            matched: 'A match or candidate exists. This import does not propose an automatic update.',
            blocked: 'Resolve this feature’s blockers before submitting a proposal.',
            unsupported_asset_type: 'The existing proposal workflow does not support this asset type; keep it staged for its owning workflow.',
            non_point_geometry: 'The existing asset proposal workflow accepts point assets only; keep this geometry staged.',
            source_id_required: 'A support structure proposal requires a stable source ID to use as its asset code.',
            source_id_too_long: 'The source ID exceeds the proposal owner’s 80-character asset code limit.'
        };
        const assetLabels = {
            fiber_segment: 'Fiber segment',
            fiber_access_point: 'Access point',
            fdh_cabinet: 'FDH cabinet',
            splice_closure: 'Splice closure',
            service_building: 'Service building',
            support_structure: 'Support structure',
            unclassified: 'Unclassified',
            unsupported: 'Unsupported'
        };
        element.replaceChildren();
        (features || []).forEach(function (feature) {
            const properties = feature.properties || {};
            const card = root.document.createElement('article');
            card.className = 'rounded-lg border border-slate-200 p-3 text-xs dark:border-slate-700';
            const heading = root.document.createElement('div');
            heading.className = 'font-semibold';
            heading.textContent = `${properties.name || `Placemark ${properties.row_number}`} · ${assetLabels[properties.asset_type] || properties.asset_type} · ${properties.geometry_type || feature.geometry?.type || 'Unknown geometry'}`;
            card.appendChild(heading);
            const typeLabel = root.document.createElement('label');
            typeLabel.className = 'mt-2 flex items-center gap-2';
            typeLabel.textContent = 'Asset type';
            const typeSelect = root.document.createElement('select');
            typeSelect.className = 'rounded border-slate-300 text-xs dark:bg-slate-900';
            typeSelect.dataset.featureId = properties.staged_feature_id;
            [
                ['unclassified', 'Unclassified'],
                ['fiber_segment', 'Fiber segment'],
                ['fiber_access_point', 'Access point'],
                ['fdh_cabinet', 'FDH cabinet'],
                ['splice_closure', 'Splice closure'],
                ['service_building', 'Service building'],
                ['support_structure', 'Support structure']
            ].forEach(function (option) {
                const item = root.document.createElement('option');
                item.value = option[0];
                item.textContent = option[1];
                typeSelect.appendChild(item);
            });
            typeSelect.value = classificationEdits.get(properties.staged_feature_id) || properties.asset_type;
            typeSelect.addEventListener('change', function () {
                classificationEdits.set(properties.staged_feature_id, typeSelect.value);
                let pending = card.querySelector('[data-classification-pending]');
                if (!pending) {
                    pending = root.document.createElement('p');
                    pending.dataset.classificationPending = 'true';
                    pending.className = 'mt-1 text-amber-700 dark:text-amber-300';
                    pending.textContent = 'Type change is not saved yet. Save type review to revalidate blockers.';
                    card.appendChild(pending);
                }
            });
            typeLabel.appendChild(typeSelect);
            card.appendChild(typeLabel);
            if (classificationEdits.has(properties.staged_feature_id)) {
                const pending = root.document.createElement('p');
                pending.dataset.classificationPending = 'true';
                pending.className = 'mt-1 text-amber-700 dark:text-amber-300';
                pending.textContent = 'Type change is not saved yet. Save type review to revalidate blockers.';
                card.appendChild(pending);
            }
            const state = root.document.createElement('p');
            state.className = 'mt-1 text-slate-500 dark:text-slate-400';
            state.textContent = `Row ${properties.row_number} · ${properties.match_status}` + (properties.suggested_asset_type ? ` · suggested ${assetLabels[properties.suggested_asset_type] || properties.suggested_asset_type}` : '');
            card.appendChild(state);
            if (properties.proposal_eligibility) {
                const eligibility = root.document.createElement('p');
                eligibility.className = 'mt-1 text-slate-600 dark:text-slate-300';
                eligibility.textContent = proposalEligibilityHelp[properties.proposal_eligibility]
                    || 'Proposal eligibility requires review.';
                card.appendChild(eligibility);
            }
            if (properties.description) {
                const description = root.document.createElement('p');
                description.className = 'mt-1 whitespace-pre-wrap';
                description.textContent = properties.description;
                card.appendChild(description);
            }
            (properties.blocker_codes || []).forEach(function (code) {
                const blocker = root.document.createElement('p');
                blocker.className = 'mt-1 text-rose-700 dark:text-rose-300';
                blocker.textContent = blockerHelp[code] || `Review required: ${code.replaceAll('_', ' ')}.`;
                card.appendChild(blocker);
            });
            (properties.match_reasons || []).forEach(function (reason) {
                const detail = root.document.createElement('p');
                detail.className = 'mt-1 text-slate-600 dark:text-slate-300';
                detail.textContent = matchReasonHelp[reason] || `Match note: ${reason.replaceAll('_', ' ')}.`;
                card.appendChild(detail);
            });
            if ((properties.candidate_asset_ids || []).length) {
                const candidates = root.document.createElement('p');
                candidates.className = 'mt-1 font-mono';
                candidates.textContent = `Candidate asset IDs: ${properties.candidate_asset_ids.join(', ')}`;
                card.appendChild(candidates);
            }
            (properties.resource_warnings || []).forEach(function (warning) {
                const notice = root.document.createElement('p');
                notice.className = 'mt-1 text-amber-700 dark:text-amber-300';
                if (warning.startsWith('network_link_not_expanded:')) {
                    notice.textContent = `Linked document “${warning.split(':').slice(1).join(':')}” was not loaded. This feature's local geometry and metadata are preserved.`;
                } else {
                    notice.textContent = `${warning.replaceAll('_', ' ')}. The feature geometry is preserved; preview uses its default symbol.`;
                }
                card.appendChild(notice);
            });
            element.appendChild(card);
        });
    }

    function updateReviewActions(features, bulkType, bulkApply, acceptSuggestions) {
        const items = features || [];
        const hasUnclassified = items.some(function (feature) {
            return ['unclassified', 'unsupported'].includes(feature.properties?.asset_type);
        });
        const hasSuggestions = items.some(function (feature) {
            return Boolean(feature.properties?.suggested_asset_type);
        });
        if (bulkType?.closest('label')) bulkType.closest('label').hidden = !hasUnclassified;
        if (bulkApply) bulkApply.hidden = !hasUnclassified;
        if (acceptSuggestions) acceptSuggestions.hidden = !hasSuggestions;
    }

    if (settings.can_import) {
        const openButton = root.document.getElementById('btn-import-kmz');
        const dialog = root.document.getElementById('network-map-import-dialog');
        const form = root.document.getElementById('network-map-import-form');
        const status = root.document.getElementById('network-map-import-status');
        const resultHost = root.document.getElementById('network-map-import-result');
        const counts = root.document.getElementById('network-map-import-counts');
        const featureReviews = root.document.getElementById('network-map-import-features');
        const submit = root.document.getElementById('network-map-import-submit');
        const key = root.document.getElementById('network-map-import-key');
        const bulkType = root.document.getElementById('network-map-import-bulk-type');
        const bulkApply = root.document.getElementById('network-map-import-bulk-apply');
        const acceptSuggestions = root.document.getElementById('network-map-import-accept-suggestions');
        const saveClassifications = root.document.getElementById('network-map-import-save-classifications');
        const applyImport = root.document.getElementById('network-map-import-apply');
        let currentBatchId = null;
        let currentFeatures = [];
        let classificationFingerprint = null;
        let classificationCommandKey = null;

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
        ['network-map-kmz-file', 'network-map-import-reason'].forEach(function (id) {
            root.document.getElementById(id)?.addEventListener('change', function () {
                key.value = commandKey();
            });
        });
        form.addEventListener('submit', async function (event) {
            event.preventDefault();
            submit.disabled = true;
            setStatus(status, 'Validating and staging the map file…', 'progress');
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
                currentBatchId = payload.batch_id;
                currentFeatures = payload.features || [];
                classificationEdits.clear();
                updateReviewActions(currentFeatures, bulkType, bulkApply, acceptSuggestions);
                renderFeatureReviews(featureReviews, payload.features);
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
                setStatus(status, error.message || 'The map import failed.', 'error');
            } finally {
                submit.disabled = false;
            }
        });
        bulkApply.addEventListener('click', function () {
            if (!bulkType.value) return;
            currentFeatures.forEach(function (feature) {
                const properties = feature.properties || {};
                if (properties.asset_type === 'unclassified' || properties.asset_type === 'unsupported') {
                    classificationEdits.set(properties.staged_feature_id, bulkType.value);
                }
            });
            renderFeatureReviews(featureReviews, currentFeatures);
        });
        acceptSuggestions.addEventListener('click', function () {
            currentFeatures.forEach(function (feature) {
                const properties = feature.properties || {};
                if (properties.suggested_asset_type) {
                    classificationEdits.set(properties.staged_feature_id, properties.suggested_asset_type);
                }
            });
            renderFeatureReviews(featureReviews, currentFeatures);
        });
        saveClassifications.addEventListener('click', async function () {
            if (!currentBatchId || classificationEdits.size === 0) return;
            const reason = root.document.getElementById('network-map-import-reason')?.value?.trim();
            if (!reason) {
                setStatus(status, 'Enter a review reason before saving feature types.', 'error');
                return;
            }
            saveClassifications.disabled = true;
            setStatus(status, 'Saving classification review; live map remains unchanged…', 'progress');
            try {
                const featureEdits = Array.from(classificationEdits.entries()).map(function (entry) {
                    return { staged_feature_id: entry[0], asset_type: entry[1] };
                });
                const fingerprint = JSON.stringify({ batch: currentBatchId, reason: reason, features: featureEdits });
                if (fingerprint !== classificationFingerprint) {
                    classificationFingerprint = fingerprint;
                    classificationCommandKey = commandKey();
                }
                const response = await root.fetch(`/admin/network/map/imports/${currentBatchId}/classifications`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken() },
                    body: JSON.stringify({
                        command_key: classificationCommandKey,
                        reason: reason,
                        features: featureEdits
                    })
                });
                const payload = await response.json().catch(function () { return {}; });
                if (!response.ok) throw new Error(payload.message || `Classification review failed (${response.status}).`);
                currentFeatures = payload.features || [];
                classificationEdits.clear();
                classificationFingerprint = null;
                classificationCommandKey = null;
                updateReviewActions(currentFeatures, bulkType, bulkApply, acceptSuggestions);
                renderCounts(counts, payload);
                renderFeatureReviews(featureReviews, currentFeatures);
                renderPreview(currentFeatures);
                const remaining = payload.blocker_count || 0;
                setStatus(status, `Classification review recorded. ${remaining} blocked feature(s) remain; live map unchanged.`, remaining ? 'error' : 'success');
            } catch (error) {
                setStatus(status, error.message || 'Could not save classification review.', 'error');
            } finally {
                saveClassifications.disabled = false;
            }
        });
        applyImport?.addEventListener('click', async function () {
            if (classificationEdits.size > 0) {
                setStatus(status, 'Save the pending type review first. The server revalidates blockers after each saved review.', 'error');
                return;
            }
            const eligible = currentFeatures.filter(function (feature) {
                return feature.properties?.proposal_eligibility === 'eligible';
            });
            const matched = currentFeatures.filter(function (feature) {
                return feature.properties?.proposal_eligibility === 'matched';
            }).length;
            const reason = root.document.getElementById('network-map-import-reason')?.value?.trim() || '';
            if (reason.length < 3) {
                setStatus(status, 'Enter a reason of at least three characters before submitting proposals.', 'error');
                return;
            }
            if (!eligible.length) {
                setStatus(status, `No eligible new point assets are ready. ${matched} matched feature(s) remain unchanged; routes and unsupported types stay staged.`, 'error');
                return;
            }
            const skipped = currentFeatures.length - eligible.length - matched;
            const summary = `Proposed additions: ${eligible.length}\nMatched or candidate features: ${matched} (no update proposed)\nUpdates: 0\nSkipped and kept staged: ${skipped}`;
            if (!root.confirm(`${summary}\n\nSubmit eligible point proposals to independent review? Route geometry and unsupported types stay staged. No map asset changes until the proposals are separately reviewed.`)) return;
            applyImport.disabled = true;
            let submitted = 0;
            const failures = [];
            try {
                for (const feature of eligible) {
                    const properties = feature.properties || {};
                    const point = feature.geometry.coordinates;
                    const assetType = {
                        fiber_access_point: 'access_point',
                        fdh_cabinet: 'fdh_cabinet',
                        splice_closure: 'splice_closure',
                        support_structure: 'support_structure'
                    }[properties.asset_type];
                    const code = properties.external_id && properties.external_id.length <= 80
                        ? properties.external_id
                        : null;
                    if (properties.asset_type === 'support_structure' && !code) {
                        failures.push(`${properties.name}: ${properties.external_id ? 'source ID exceeds the asset code limit.' : 'support structures need a source ID/code.'}`);
                        continue;
                    }
                    const response = await root.fetch('/admin/network/map-v2/proposals', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken() },
                        body: JSON.stringify({
                            asset_type: assetType,
                            operation: 'create',
                            name: properties.name,
                            code: code,
                            latitude: point[1],
                            longitude: point[0],
                            notes: properties.description || null,
                            reason: reason,
                            idempotency_key: proposalKey(currentBatchId, properties.staged_feature_id, properties.asset_type, reason)
                        })
                    });
                    const result = await response.json().catch(function () { return {}; });
                    if (!response.ok) failures.push(`${properties.name}: ${result.message || `proposal failed (${response.status})`}`);
                    else submitted += 1;
                }
                const summary = `${submitted} proposal(s) submitted for independent review; ${matched} matched/candidate feature(s) received no proposal; ${currentFeatures.length - submitted - matched} feature(s) remain staged or skipped.`;
                setStatus(status, summary + (failures.length ? ` Issues: ${failures.join(' ')}` : ' No canonical map data changed.'), failures.length ? 'error' : 'success');
            } catch (error) {
                setStatus(status, error.message || 'Could not submit the import proposals.', 'error');
            } finally {
                applyImport.disabled = false;
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
