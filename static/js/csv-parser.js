/**
 * csv-parser.js — Shared CSV parser + header auto-matching.
 *
 * Provides:
 *   window.parseCSVText(text)             — parse CSV string → { headers, rows }
 *   window.autoMatchHeaders(headers, map) — auto-map headers via alias map
 */

/* ================================================================
   CSV Parser — handles quoted fields, auto-detects delimiters.
   ================================================================ */
window.parseCSVText = function parseCSVText(text) {
    // Detect delimiter from first line
    const firstLine = text.split('\n').find(l => l.trim()) || '';
    const delimiters = [',', '\t', ';', '|'];
    let delim = ',';
    let maxCount = 0;
    for (const d of delimiters) {
        const c = (firstLine.match(new RegExp(d === '|' ? '\\|' : (d === '\t' ? '\t' : d), 'g')) || []).length;
        if (c > maxCount) { maxCount = c; delim = d; }
    }

    const rows = [];
    let headers = null;
    let i = 0;
    const len = text.length;

    while (i < len) {
        const row = [];
        // Parse one row
        while (i < len) {
            let value = '';
            if (text[i] === '"') {
                // Quoted field
                i++;
                while (i < len) {
                    if (text[i] === '"') {
                        if (i + 1 < len && text[i + 1] === '"') {
                            value += '"';
                            i += 2;
                        } else {
                            i++; // closing quote
                            break;
                        }
                    } else {
                        value += text[i];
                        i++;
                    }
                }
                // Skip to delimiter or newline
                while (i < len && text[i] !== delim && text[i] !== '\n' && text[i] !== '\r') i++;
            } else {
                // Unquoted field
                while (i < len && text[i] !== delim && text[i] !== '\n' && text[i] !== '\r') {
                    value += text[i];
                    i++;
                }
            }
            row.push(value.trim());
            if (i < len && text[i] === delim) {
                i++; // skip delimiter
                continue;
            }
            break;
        }
        // Skip line endings
        if (i < len && text[i] === '\r') i++;
        if (i < len && text[i] === '\n') i++;

        // Skip empty rows
        if (row.length === 1 && row[0] === '') continue;

        if (!headers) {
            headers = row;
        } else {
            const obj = {};
            for (let c = 0; c < headers.length; c++) {
                obj[headers[c]] = row[c] || '';
            }
            rows.push(obj);
        }
    }
    return { headers: headers || [], rows };
};

/* ================================================================
   Auto-match headers against an alias map.

   @param {string[]} headers    — detected CSV column headers
   @param {Object}   aliasMap   — { normalized_alias: canonical_field }
   @returns {Object}            — { csvHeader: targetField | '' }
   ================================================================ */
window.autoMatchHeaders = function autoMatchHeaders(headers, aliasMap) {
    const mapping = {};
    const used = new Set();
    for (const h of headers) {
        const normalized = h.trim().toLowerCase().replace(/\s+/g, '_');
        const target = aliasMap[normalized];
        if (target && !used.has(target)) {
            mapping[h] = target;
            used.add(target);
        } else {
            mapping[h] = '';
        }
    }
    return mapping;
};
