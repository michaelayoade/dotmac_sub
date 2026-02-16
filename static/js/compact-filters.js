/**
 * Compact Filters — companion JS for the compact_filters Jinja2 macro.
 *
 * Handles chip removal: clicking the ✕ on an active filter chip clears
 * the corresponding form field and triggers an HTMX request to refresh results.
 *
 * Spinner handling is delegated to live-search.js via the shared
 * [data-live-search] + [data-live-search-spinner] convention.
 */
(function () {
    "use strict";

    function handleChipRemove(evt) {
        var btn = evt.target.closest("[data-chip-remove]");
        if (!btn) return;

        var fieldName = btn.getAttribute("data-chip-remove");
        if (!fieldName) return;

        var container = btn.closest("[data-compact-filters]");
        if (!container) return;

        var form = container.querySelector("form");
        if (!form) return;

        // Clear the matching form field
        var field = form.querySelector("[name='" + fieldName + "']");
        if (field) {
            if (field.type === "checkbox" || field.type === "radio") {
                field.checked = false;
            } else {
                field.value = "";
            }
            // Trigger HTMX change event to refresh results
            if (window.htmx) {
                htmx.trigger(field, "change");
            }
        } else {
            // Field might not be visible (date_range inputs handled by macro).
            // Build URL without this param and navigate.
            var url = new URL(form.action, window.location.origin);
            var params = new URLSearchParams(new FormData(form));
            params.delete(fieldName);
            url.search = params.toString();
            window.location.href = url.toString();
        }
    }

    document.body.addEventListener("click", handleChipRemove);
})();
